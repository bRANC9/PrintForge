// PrintForge 3D viewer (Three.js, ES module).
//
// Three.js is vendored under `static/vendor/three/` and resolved through an
// import map in `templates/projects/detail.html` (no CDN, works offline):
//
//     <script type="importmap">
//     { "imports": {
//         "three": "{% static 'vendor/three/three.module.min.js' %}",
//         "three/addons/": "{% static 'vendor/three/addons/' %}"
//     } }
//     </script>
//
// Vendored versions: three r170 (three.module.min.js), Alpine 3.17.4.
// The module exposes `window.PrintForgeViewer` so the classic Alpine code in
// app.js can drive it without a build step.
//
// Annotation mode (docs/visual-editing.md 3.6): the user clicks the model
// surface to place a visual prompt. Clicks are ray-cast against the mesh and
// the hit is mapped back to the original STL / OpenSCAD coordinate space
// (`modelPoint = hit.point - viewerOffset`). Shift+click grows the active
// annotation into a multi-triangle region; clicking empty space clears the
// current selection. Every change is broadcast as a bubbling CustomEvent on
// the container (`viewer-annotation-*`, `viewer-selection-changed`).
//
// Stored annotations (`setStoredAnnotations`) render the visual prompts a
// version was built from, read-only and muted in gray. They are not
// interactive and are cleared when the model changes or annotation mode is
// entered; the page (project.js) re-applies them on version selection.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";

const MODEL_COLOR = 0x4c8dff;
const GRID_MAJOR = 0x3a4652;
const GRID_MINOR = 0x232b33;

const ANNOTATION_COLOR = 0xffb020;
const ANNOTATION_ACTIVE_COLOR = 0x6fe3ff;
// Stored (previous-version) visual prompts are read-only and visually muted so
// they never look like part of the current editable selection.
const STORED_ANNOTATION_COLOR = 0x9aa4b2;
const DRAG_THRESHOLD_PX = 5;

const instances = new WeakMap();

function emit(container, state, message, extra) {
    container.dataset.viewerState = state;
    container.dispatchEvent(
        new CustomEvent("viewer-state", {
            bubbles: true,
            detail: Object.assign({ state, message: message || "" }, extra || {}),
        })
    );
}

function emitAnnotation(container, name, detail) {
    container.dispatchEvent(
        new CustomEvent(name, {
            bubbles: true,
            detail: detail || {},
        })
    );
}

function disposeMaterial(material) {
    if (!material) return;
    if (Array.isArray(material)) {
        material.forEach(disposeMaterial);
    } else {
        material.dispose();
    }
}

/** Dispose every geometry/material under a temporary object graph. */
function disposeObject(root) {
    if (!root) return;
    root.traverse((node) => {
        if (node.geometry) node.geometry.dispose();
        if (node.material) disposeMaterial(node.material);
    });
}

function round(value) {
    return Math.round(value * 10) / 10;
}

function round3(value) {
    return Math.round(value * 1000) / 1000;
}

/** Millimetre coordinate triple, rounded to display precision. */
function pointToArray(vector) {
    return [round(vector.x), round(vector.y), round(vector.z)];
}

/** Unit direction triple (normals are translation-invariant). */
function directionToArray(vector) {
    return [round3(vector.x), round3(vector.y), round3(vector.z)];
}

/** Finite number triple with per-component fallbacks (stored annotations). */
function normalizeTriple(raw, fallback) {
    const source = Array.isArray(raw) ? raw : [];
    return fallback.map((fallbackValue, index) => {
        const value = Number(source[index]);
        return Number.isFinite(value) ? value : fallbackValue;
    });
}

/**
 * Coerce one stored (`ModelVersion.annotations_json`) annotation onto the
 * viewer's internal shape. Stored data is read-only provenance written by the
 * backend, so every field is guarded; missing coordinates degrade to the
 * origin / up vector instead of breaking the render loop.
 */
function normalizeStoredAnnotation(raw, index) {
    const annotation = raw && typeof raw === "object" ? raw : {};
    const faces = Array.isArray(annotation.faces)
        ? annotation.faces
              .map((face) => Number(face))
              .filter((face) => Number.isInteger(face) && face >= 0)
        : [];
    return {
        id: annotation.id ? String(annotation.id) : `stored-${index}`,
        kind: annotation.kind === "region" ? "region" : "point",
        point: normalizeTriple(annotation.point, [0, 0, 0]),
        normal: normalizeTriple(annotation.normal, [0, 1, 0]),
        faces,
        region: annotation.region && typeof annotation.region === "object"
            ? Object.assign({}, annotation.region)
            : null,
        instruction: typeof annotation.instruction === "string" ? annotation.instruction : "",
    };
}

function niceGridSize(maxDim) {
    const dimensions = Math.max(maxDim || 1, 0.001);
    const magnitude = Math.pow(10, Math.floor(Math.log10(dimensions)));
    for (const step of [1, 2, 5, 10]) {
        const candidate = magnitude * step;
        if (candidate >= dimensions * 2) return candidate;
    }
    return magnitude * 10;
}

class StlViewer {
    constructor(container) {
        this.container = container;
        this.currentUrl = null;
        this.token = 0;
        this.requestToken = 0;
        this.mesh = null;
        this.dimensions = null;

        // Translation applied in `setGeometry` to center/rest the model; the
        // inverse maps a raycast hit back to original STL/OpenSCAD coordinates.
        this.viewerOffset = new THREE.Vector3();
        this.annotationScale = 1;

        // Annotation state (docs/visual-editing.md 3.6).
        this.annotationMode = false;
        this.annotations = [];
        this.activeAnnotationId = null;
        this.annotationSeq = 0;
        this.annotationVisuals = new Map();

        // Read-only stored annotations of the selected version (the visual
        // prompts that produced it). Kept separate from the editable set.
        this.storedAnnotations = [];
        this.storedAnnotationVisuals = [];
        this.raycaster = new THREE.Raycaster();
        this.pointer = new THREE.Vector2();
        this.pointerDown = null;

        this.scene = new THREE.Scene();
        this.scene.background = null;

        this.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 10000);
        this.camera.position.set(80, 80, 80);

        this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
        this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
        this.renderer.setSize(container.clientWidth || 320, container.clientHeight || 320, false);
        this.renderer.domElement.classList.add("viewer-webgl");
        container.appendChild(this.renderer.domElement);

        this.controls = new OrbitControls(this.camera, this.renderer.domElement);
        this.controls.enableDamping = true;
        this.controls.dampingFactor = 0.08;
        this.controls.screenSpacePanning = true;
        this.controls.minDistance = 0.01;
        this.controls.maxDistance = 100000;

        this.scene.add(new THREE.HemisphereLight(0xffffff, 0x334455, 1.7));

        const keyLight = new THREE.DirectionalLight(0xffffff, 1.5);
        keyLight.position.set(1, 1.6, 1);
        this.scene.add(keyLight);

        const fillLight = new THREE.DirectionalLight(0xffffff, 0.5);
        fillLight.position.set(-1, -0.5, -1);
        this.scene.add(fillLight);

        this.grid = new THREE.GridHelper(200, 20, GRID_MAJOR, GRID_MINOR);
        this.scene.add(this.grid);

        this.axes = new THREE.AxesHelper(40);
        this.scene.add(this.axes);

        this.modelGroup = new THREE.Group();
        this.scene.add(this.modelGroup);

        // Markers / selection overlays live outside the model mesh so a model
        // reload can dispose them independently (and they never get ray-cast).
        this.annotationGroup = new THREE.Group();
        this.scene.add(this.annotationGroup);

        this.loader = new STLLoader();
        this.resizeObserver = new ResizeObserver(() => this.resize());
        this.resizeObserver.observe(container);

        this.onPointerDown = this.handlePointerDown.bind(this);
        this.onPointerUp = this.handlePointerUp.bind(this);
        this.onPointerLeave = this.handlePointerLeave.bind(this);
        this.renderer.domElement.addEventListener("pointerdown", this.onPointerDown);
        this.renderer.domElement.addEventListener("pointerup", this.onPointerUp);
        this.renderer.domElement.addEventListener("pointerleave", this.onPointerLeave);

        this.animate = this.animate.bind(this);
        this.renderer.setAnimationLoop(this.animate);
        this.resize();
    }

    animate() {
        this.controls.update();
        this.renderer.render(this.scene, this.camera);
    }

    resize() {
        const width = this.container.clientWidth;
        const height = this.container.clientHeight;
        if (!width || !height) return;
        this.camera.aspect = width / height;
        this.camera.updateProjectionMatrix();
        this.renderer.setSize(width, height, false);
    }

    apply(config) {
        const settings = config || {};
        const stlUrl = settings.stlUrl || "";
        const token = settings.token === undefined || settings.token === null ? 0 : settings.token;

        if (settings.annotationMode !== undefined) {
            this.setAnnotationMode(settings.annotationMode);
        }

        if (!stlUrl) {
            this.requestToken += 1;
            this.currentUrl = null;
            this.token = token;
            this.clearStoredAnnotations();
            this.clearModel();
            emit(this.container, "empty", "Nincs STL ehhez a verzióhoz.");
            return;
        }
        if (stlUrl === this.currentUrl && token === this.token) return;
        // A new version invalidates the previous version's stored markers; the
        // page re-applies them through `setStoredAnnotations` on selection.
        this.clearStoredAnnotations();
        this.token = token;
        this.load(stlUrl);
    }

    load(url) {
        const token = ++this.requestToken;
        this.currentUrl = url;
        emit(this.container, "loading", "STL betöltése…");
        this.loader.load(
            url,
            (geometry) => {
                if (token !== this.requestToken) {
                    geometry.dispose();
                    return;
                }
                try {
                    this.setGeometry(geometry);
                    emit(this.container, "ready", "", { dimensions: this.dimensions });
                } catch (error) {
                    geometry.dispose();
                    this.currentUrl = null;
                    emit(this.container, "error", "A modell feldolgozása sikertelen.");
                    console.error("PrintForge viewer: geometry setup failed", error);
                }
            },
            undefined,
            (error) => {
                if (token !== this.requestToken) return;
                this.currentUrl = null;
                this.clearModel();
                emit(this.container, "error", "Az STL nem tölthető be (hiányzó fájl vagy jogosultság).");
                console.error("PrintForge viewer: STL load failed", error);
            }
        );
    }

    setGeometry(geometry) {
        this.clearModel();

        geometry.computeBoundingBox();
        const rawBox = geometry.boundingBox.clone();
        const rawSize = rawBox.getSize(new THREE.Vector3());
        const center = rawBox.getCenter(new THREE.Vector3());

        // Center on X/Z and rest the model on the grid plane (Y = 0).
        geometry.translate(-center.x, -center.y, -center.z);
        geometry.translate(0, rawSize.y / 2, 0);
        geometry.computeBoundingBox();

        // Net translation applied to the geometry, kept so annotation clicks
        // can be mapped back to the original STL/OpenSCAD coordinate space
        // (docs/visual-editing.md 6.).
        this.viewerOffset.set(-center.x, -center.y + rawSize.y / 2, -center.z);

        const size = geometry.boundingBox.getSize(new THREE.Vector3());
        const material = new THREE.MeshStandardMaterial({
            color: MODEL_COLOR,
            metalness: 0.15,
            roughness: 0.55,
        });
        const mesh = new THREE.Mesh(geometry, material);
        this.mesh = mesh;
        this.modelGroup.add(mesh);

        const maxDim = Math.max(size.x, size.y, size.z) || 1;
        this.dimensions = { x: round(size.x), y: round(size.y), z: round(size.z) };
        // Marker/arrow sizing relative to the model, so it reads on any scale.
        this.annotationScale = Math.max(maxDim * 0.08, 0.8);

        // Stored annotations live in original STL coordinates, so rebuild their
        // visuals against the new offset/scale (this also covers the case where
        // the page set them before the STL finished loading).
        this.refreshStoredAnnotationVisuals();

        this.grid.geometry.dispose();
        disposeMaterial(this.grid.material);
        this.scene.remove(this.grid);
        const gridSize = niceGridSize(maxDim);
        this.grid = new THREE.GridHelper(gridSize, 20, GRID_MAJOR, GRID_MINOR);
        this.scene.add(this.grid);

        this.scene.remove(this.axes);
        this.axes.geometry.dispose();
        disposeMaterial(this.axes.material);
        this.axes = new THREE.AxesHelper(maxDim * 0.6);
        this.scene.add(this.axes);

        this.frameObject(size);
    }

    frameObject(size) {
        const maxDim = Math.max(size.x, size.y, size.z) || 1;
        const radius = maxDim / 2;
        const fov = THREE.MathUtils.degToRad(this.camera.fov);
        const distance = Math.max((radius / Math.sin(fov / 2)) * 1.4, 1);

        const target = new THREE.Vector3(0, size.y / 2, 0);
        this.controls.target.copy(target);
        this.camera.position.set(distance * 0.75, target.y + distance * 0.6, distance * 0.75);
        this.camera.near = Math.max(distance / 1000, 0.01);
        this.camera.far = distance * 100;
        this.camera.updateProjectionMatrix();
        this.camera.lookAt(target);
        this.controls.update();
    }

    clearModel() {
        this.clearAnnotations();
        if (!this.mesh) return;
        this.modelGroup.remove(this.mesh);
        this.mesh.geometry.dispose();
        disposeMaterial(this.mesh.material);
        this.mesh = null;
        this.dimensions = null;
    }

    // ------------------------------------------------------------------
    // Annotation mode
    // ------------------------------------------------------------------

    setAnnotationMode(enabled) {
        const next = Boolean(enabled);
        if (this.annotationMode === next) return;
        this.annotationMode = next;
        if (next) {
            // The editable selection takes over while annotating; stored markers
            // are not restored automatically when the mode is switched off.
            this.clearStoredAnnotations();
        }
        if (this.renderer && this.renderer.domElement) {
            this.renderer.domElement.style.cursor = next ? "crosshair" : "";
        }
        if (!next) this.pointerDown = null;
    }

    getAnnotations() {
        return this.annotations.map((annotation) => this.cloneAnnotation(annotation));
    }

    clearAnnotations() {
        if (!this.annotations.length && !this.annotationVisuals.size) {
            this.activeAnnotationId = null;
            return [];
        }
        const removed = this.annotations.slice();
        this.annotations = [];
        this.activeAnnotationId = null;
        removed.forEach((annotation) => this.disposeAnnotationVisual(annotation.id));
        removed.forEach((annotation) => {
            emitAnnotation(this.container, "viewer-annotation-removed", {
                annotation: this.cloneAnnotation(annotation),
                annotations: this.getAnnotations(),
            });
        });
        this.emitSelection();
        return removed.map((annotation) => this.cloneAnnotation(annotation));
    }

    removeAnnotation(id) {
        const index = this.annotations.findIndex((annotation) => annotation.id === id);
        if (index === -1) return null;
        const removed = this.annotations.splice(index, 1)[0];
        if (this.activeAnnotationId === id) this.activeAnnotationId = null;
        this.disposeAnnotationVisual(id);
        emitAnnotation(this.container, "viewer-annotation-removed", {
            annotation: this.cloneAnnotation(removed),
            annotations: this.getAnnotations(),
        });
        this.emitSelection();
        return this.cloneAnnotation(removed);
    }

    cloneAnnotation(annotation) {
        if (!annotation) return null;
        return {
            id: annotation.id,
            kind: annotation.kind,
            point: annotation.point.slice(),
            normal: annotation.normal.slice(),
            faces: annotation.faces.slice(),
            region: annotation.region ? Object.assign({}, annotation.region) : null,
            instruction: annotation.instruction || "",
        };
    }

    /**
     * Map a raycast hit on the translated display geometry back to the original
     * STL/OpenSCAD coordinate space. The translation does not change normals.
     */
    modelPointFromHit(hit) {
        return hit.point.clone().sub(this.viewerOffset);
    }

    handlePointerDown(event) {
        if (!this.annotationMode || event.button !== 0) return;
        this.pointerDown = {
            x: event.clientX,
            y: event.clientY,
            shift: event.shiftKey,
        };
    }

    handlePointerLeave() {
        this.pointerDown = null;
    }

    handlePointerUp(event) {
        if (!this.annotationMode || event.button !== 0) return;
        const down = this.pointerDown;
        this.pointerDown = null;
        if (!down) return;
        const moved = Math.hypot(event.clientX - down.x, event.clientY - down.y);
        // A drag is an orbit/pan gesture, not a placement click.
        if (moved > DRAG_THRESHOLD_PX) return;
        this.handleAnnotationClick(event, event.shiftKey || down.shift);
    }

    raycast(event) {
        if (!this.mesh) return null;
        const rect = this.renderer.domElement.getBoundingClientRect();
        if (!rect.width || !rect.height) return null;
        this.pointer.set(
            ((event.clientX - rect.left) / rect.width) * 2 - 1,
            -((event.clientY - rect.top) / rect.height) * 2 + 1
        );
        this.raycaster.setFromCamera(this.pointer, this.camera);
        const hits = this.raycaster.intersectObject(this.mesh, false);
        return hits.length ? hits[0] : null;
    }

    handleAnnotationClick(event, shiftKey) {
        const hit = this.raycast(event);
        if (!hit) {
            this.clearSelection();
            return;
        }
        const faceIndex = typeof hit.faceIndex === "number" ? hit.faceIndex : null;
        const point = this.modelPointFromHit(hit);
        const normal = hit.face ? hit.face.normal.clone() : new THREE.Vector3(0, 1, 0);

        const active = this.activeAnnotationId
            ? this.annotations.find((annotation) => annotation.id === this.activeAnnotationId)
            : null;

        if (shiftKey && active && faceIndex !== null) {
            if (!active.faces.includes(faceIndex)) {
                active.faces.push(faceIndex);
                this.recomputeAnnotation(active);
                this.refreshAnnotationVisual(active);
                emitAnnotation(this.container, "viewer-annotation-updated", {
                    annotation: this.cloneAnnotation(active),
                    annotations: this.getAnnotations(),
                });
            }
            this.emitSelection();
            return;
        }

        const annotation = this.createAnnotation(point, normal, faceIndex);
        const previousActiveId = this.activeAnnotationId;
        this.annotations.push(annotation);
        this.activeAnnotationId = annotation.id;
        if (previousActiveId && previousActiveId !== annotation.id) {
            const previous = this.annotations.find((item) => item.id === previousActiveId);
            if (previous) this.refreshAnnotationVisual(previous);
        }
        this.addAnnotationVisual(annotation);
        emitAnnotation(this.container, "viewer-annotation-added", {
            annotation: this.cloneAnnotation(annotation),
            annotations: this.getAnnotations(),
        });
        this.emitSelection();
    }

    /** Clear the current selection: the active (last) annotation, if any. */
    clearSelection() {
        if (this.activeAnnotationId) {
            this.removeAnnotation(this.activeAnnotationId);
            return;
        }
        this.emitSelection();
    }

    emitSelection() {
        const active = this.activeAnnotationId
            ? this.annotations.find((annotation) => annotation.id === this.activeAnnotationId)
            : null;
        emitAnnotation(this.container, "viewer-selection-changed", {
            selection: active ? this.cloneAnnotation(active) : null,
            annotations: this.getAnnotations(),
        });
    }

    createAnnotation(point, normal, faceIndex) {
        this.annotationSeq += 1;
        const annotation = {
            id: `a${this.annotationSeq}`,
            kind: "point",
            point: pointToArray(point),
            normal: directionToArray(normal),
            faces: faceIndex === null ? [] : [faceIndex],
            region: null,
            instruction: "",
        };
        this.recomputeAnnotation(annotation);
        return annotation;
    }

    /**
     * A single triangle is a `point` (the hit point/normal); two or more form a
     * `region` summarised by centroid / averaged normal / axis-aligned size.
     */
    recomputeAnnotation(annotation) {
        const faces = annotation.faces || [];
        if (faces.length <= 1) {
            annotation.kind = "point";
            annotation.region = null;
            return;
        }
        annotation.kind = "region";
        annotation.region = this.regionSummary(faces);
        annotation.point = annotation.region.centroid.slice();
        annotation.normal = annotation.region.normal.slice();
    }

    regionSummary(faces) {
        const fallback = {
            centroid: [0, 0, 0],
            normal: [0, 0, 1],
            size: [0, 0, 0],
            count: 0,
        };
        if (!this.mesh) return fallback;
        const position = this.mesh.geometry.getAttribute("position");
        if (!position) return fallback;

        const centroid = new THREE.Vector3();
        const normal = new THREE.Vector3();
        const box = new THREE.Box3();
        const a = new THREE.Vector3();
        const b = new THREE.Vector3();
        const c = new THREE.Vector3();
        const cb = new THREE.Vector3();
        const ab = new THREE.Vector3();
        const faceNormal = new THREE.Vector3();
        let count = 0;

        faces.forEach((faceIndex) => {
            const base = faceIndex * 3;
            if (base + 2 >= position.count) return;
            a.fromBufferAttribute(position, base);
            b.fromBufferAttribute(position, base + 1);
            c.fromBufferAttribute(position, base + 2);
            cb.subVectors(c, b);
            ab.subVectors(a, b);
            faceNormal.crossVectors(cb, ab).normalize();
            normal.add(faceNormal);
            centroid.add(a).add(b).add(c).multiplyScalar(1 / 3);
            box.expandByPoint(a);
            box.expandByPoint(b);
            box.expandByPoint(c);
            count += 1;
        });

        if (!count) return fallback;
        centroid.multiplyScalar(1 / count).sub(this.viewerOffset);
        if (normal.lengthSq() > 0) {
            normal.normalize();
        } else {
            normal.set(0, 1, 0);
        }
        const size = box.getSize(new THREE.Vector3());
        return {
            centroid: pointToArray(centroid),
            normal: directionToArray(normal),
            size: pointToArray(size),
            count,
        };
    }

    addAnnotationVisual(annotation) {
        const visual = this.buildAnnotationVisual(annotation);
        if (!visual) return;
        this.annotationGroup.add(visual);
        this.annotationVisuals.set(annotation.id, visual);
    }

    refreshAnnotationVisual(annotation) {
        this.disposeAnnotationVisual(annotation.id);
        this.addAnnotationVisual(annotation);
    }

    disposeAnnotationVisual(id) {
        const visual = this.annotationVisuals.get(id);
        if (!visual) return;
        this.annotationGroup.remove(visual);
        disposeObject(visual);
        this.annotationVisuals.delete(id);
    }

    // ------------------------------------------------------------------
    // Stored (read-only) annotations
    // ------------------------------------------------------------------

    /**
     * Render the stored annotations of a version read-only: the same marker +
     * normal-arrow visuals as live annotations, but in the muted
     * `STORED_ANNOTATION_COLOR`, with no raycast/editing and no events. The
     * markers persist while orbiting because they are static scene objects.
     * Repeated calls replace the previous stored set.
     */
    setStoredAnnotations(annotations) {
        this.clearStoredAnnotations();
        const list = Array.isArray(annotations) ? annotations : [];
        this.storedAnnotations = list.map((raw, index) => normalizeStoredAnnotation(raw, index));
        this.storedAnnotations.forEach((annotation) => this.addStoredAnnotationVisual(annotation));
        return this.storedAnnotations.length;
    }

    /** Drop the stored markers and their data (temporary resources disposed). */
    clearStoredAnnotations() {
        this.disposeStoredVisuals();
        this.storedAnnotations = [];
    }

    disposeStoredVisuals() {
        this.storedAnnotationVisuals.forEach((visual) => {
            this.annotationGroup.remove(visual);
            disposeObject(visual);
        });
        this.storedAnnotationVisuals = [];
    }

    /**
     * Rebuild the stored visuals against the current `viewerOffset`/scale.
     * Called after a model loads; the data survives so the page does not have to
     * re-set it when geometry arrives.
     */
    refreshStoredAnnotationVisuals() {
        if (!this.storedAnnotations.length) return;
        this.disposeStoredVisuals();
        this.storedAnnotations.forEach((annotation) => this.addStoredAnnotationVisual(annotation));
    }

    addStoredAnnotationVisual(annotation) {
        const visual = this.buildAnnotationVisual(annotation, true);
        if (!visual) return;
        this.annotationGroup.add(visual);
        this.storedAnnotationVisuals.push(visual);
    }

    buildAnnotationVisual(annotation, stored) {
        const group = new THREE.Group();
        const displayPoint = new THREE.Vector3().fromArray(annotation.point).add(this.viewerOffset);
        const normal = new THREE.Vector3().fromArray(annotation.normal);
        if (normal.lengthSq() === 0) normal.set(0, 1, 0);
        normal.normalize();

        const active = !stored && annotation.id === this.activeAnnotationId;
        const color = stored
            ? STORED_ANNOTATION_COLOR
            : active
              ? ANNOTATION_ACTIVE_COLOR
              : ANNOTATION_COLOR;
        const scale = this.annotationScale || 1;

        const markerGeometry = new THREE.SphereGeometry(Math.max(scale * 0.35, 0.2), 16, 12);
        const markerMaterial = new THREE.MeshBasicMaterial({ color });
        const marker = new THREE.Mesh(markerGeometry, markerMaterial);
        marker.position.copy(displayPoint);
        group.add(marker);

        group.add(this.buildArrow(displayPoint, normal, Math.max(scale * 1.2, 0.5), color, Math.max(scale * 0.09, 0.03)));

        const selection = this.buildSelectionVisual(annotation, active, stored);
        if (selection) group.add(selection);

        return group;
    }

    /**
     * Hand-built shaft + head arrow. Deliberately not `THREE.ArrowHelper`,
     * which shares module-level geometries that must not be disposed.
     */
    buildArrow(origin, direction, length, color, radius) {
        const shaftLength = Math.max(length * 0.7, 0.1);
        const headLength = Math.max(length * 0.3, 0.1);
        const material = new THREE.MeshBasicMaterial({ color });
        const group = new THREE.Group();

        const shaft = new THREE.Mesh(
            new THREE.CylinderGeometry(radius, radius, shaftLength, 12, 1, false),
            material
        );
        shaft.position.y = shaftLength / 2;
        group.add(shaft);

        const head = new THREE.Mesh(
            new THREE.ConeGeometry(radius * 2.4, headLength, 16),
            material
        );
        head.position.y = shaftLength + headLength / 2;
        group.add(head);

        const axis = new THREE.Vector3(0, 1, 0);
        group.quaternion.setFromUnitVectors(axis, direction.clone().normalize());
        group.position.copy(origin);
        return group;
    }

    /** Translucent overlay + wireframe edges over the selected triangles. */
    buildSelectionVisual(annotation, active, stored) {
        const faces = annotation.faces || [];
        if (!faces.length || !this.mesh) return null;
        const position = this.mesh.geometry.getAttribute("position");
        if (!position) return null;

        const coords = [];
        faces.forEach((faceIndex) => {
            const base = faceIndex * 3;
            if (base + 2 >= position.count) return;
            for (let corner = 0; corner < 3; corner += 1) {
                coords.push(
                    position.getX(base + corner),
                    position.getY(base + corner),
                    position.getZ(base + corner)
                );
            }
        });
        if (!coords.length) return null;

        const color = stored
            ? STORED_ANNOTATION_COLOR
            : active
              ? ANNOTATION_ACTIVE_COLOR
              : ANNOTATION_COLOR;
        const overlayGeometry = new THREE.BufferGeometry();
        overlayGeometry.setAttribute("position", new THREE.Float32BufferAttribute(coords, 3));

        const overlay = new THREE.Mesh(
            overlayGeometry,
            new THREE.MeshBasicMaterial({
                color,
                transparent: true,
                opacity: stored ? 0.2 : active ? 0.5 : 0.32,
                side: THREE.DoubleSide,
                depthWrite: false,
                polygonOffset: true,
                polygonOffsetFactor: -2,
                polygonOffsetUnits: -2,
            })
        );

        const wire = new THREE.LineSegments(
            new THREE.WireframeGeometry(overlayGeometry),
            new THREE.LineBasicMaterial({
                color,
                transparent: true,
                opacity: stored ? 0.45 : 0.9,
            })
        );

        const group = new THREE.Group();
        group.add(overlay);
        group.add(wire);
        return group;
    }

    dispose() {
        this.requestToken += 1;
        const canvas = this.renderer.domElement;
        canvas.removeEventListener("pointerdown", this.onPointerDown);
        canvas.removeEventListener("pointerup", this.onPointerUp);
        canvas.removeEventListener("pointerleave", this.onPointerLeave);
        this.resizeObserver.disconnect();
        this.renderer.setAnimationLoop(null);
        this.clearModel();
        this.clearStoredAnnotations();
        this.controls.dispose();
        this.renderer.dispose();
        if (canvas.parentNode === this.container) {
            this.container.removeChild(canvas);
        }
    }
}

const viewerApi = {
    supported() {
        return typeof window.WebGLRenderingContext !== "undefined";
    },

    mount(container, config) {
        if (!viewerApi.supported()) {
            emit(container, "error", "A böngésző nem támogatja a WebGL-t.");
            return null;
        }

        let viewer = instances.get(container);
        if (!viewer) {
            try {
                viewer = new StlViewer(container);
            } catch (error) {
                console.error("PrintForge viewer: init failed", error);
                emit(container, "error", "A 3D nézet nem indítható el ebben a böngészőben.");
                return null;
            }
            instances.set(container, viewer);
            container.dataset.viewerMounted = "true";
        }
        viewer.apply(config);
        return viewer;
    },

    dispose(container) {
        const viewer = instances.get(container);
        if (!viewer) return;
        viewer.dispose();
        instances.delete(container);
        delete container.dataset.viewerMounted;
    },

    get(container) {
        return instances.get(container) || null;
    },
};

window.PrintForgeViewer = viewerApi;
window.dispatchEvent(new CustomEvent("printforge:viewer-ready", { detail: viewerApi }));
