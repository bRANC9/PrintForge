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
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";

const MODEL_COLOR = 0x4c8dff;
const GRID_MAJOR = 0x3a4652;
const GRID_MINOR = 0x232b33;

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

function disposeMaterial(material) {
    if (!material) return;
    if (Array.isArray(material)) {
        material.forEach(disposeMaterial);
    } else {
        material.dispose();
    }
}

function round(value) {
    return Math.round(value * 10) / 10;
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

        this.loader = new STLLoader();
        this.resizeObserver = new ResizeObserver(() => this.resize());
        this.resizeObserver.observe(container);

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

        if (!stlUrl) {
            this.requestToken += 1;
            this.currentUrl = null;
            this.token = token;
            this.clearModel();
            emit(this.container, "empty", "Nincs STL ehhez a verzióhoz.");
            return;
        }
        if (stlUrl === this.currentUrl && token === this.token) return;
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
        if (!this.mesh) return;
        this.modelGroup.remove(this.mesh);
        this.mesh.geometry.dispose();
        disposeMaterial(this.mesh.material);
        this.mesh = null;
        this.dimensions = null;
    }

    dispose() {
        this.requestToken += 1;
        this.resizeObserver.disconnect();
        this.renderer.setAnimationLoop(null);
        this.clearModel();
        this.controls.dispose();
        this.renderer.dispose();
        if (this.renderer.domElement.parentNode === this.container) {
            this.container.removeChild(this.renderer.domElement);
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
