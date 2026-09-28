// PrintForge project detail enhancements (Phase 8 / #27 / #29).
//
//   GET/PATCH /api/v1/projects/{id}/                       (metadata + license + tags)
//   GET/POST  /api/v1/projects/{id}/versions/              (POST is multipart, #27)
//   GET       /api/v1/versions/{id}/status/
//   POST      /api/v1/projects/{id}/versions/from-mesh/      (multipart mesh upload)
//   POST      /api/v1/projects/{id}/publish/ | /unpublish/
//   POST      /api/v1/projects/{id}/download/
//   POST      /api/v1/projects/{id}/rate/  (DELETE to clear)
//   GET/POST  /api/v1/projects/{id}/share/ , DELETE .../shares/{share_id}/
//   POST      /api/v1/projects/{id}/description-ai/
//   GET       /api/v1/tags/                                  (paginated)
//   POST      /api/v1/versions/{id}/annotations/             (visual editing)
//   GET       /api/v1/projects/{id}/versions/                (annotation poll)
//   GET       /api/v1/agent-runs/                            (clarification/assumption provenance)
//   POST      /api/v1/agent-runs/{id}/clarifications/        (answer blocking questions)
//
// The render findings shown on the version page are NOT read from
// `/versions/{id}/status/`: that endpoint answers `{status, stage, errors}` and
// nothing else, so it cannot carry a warning. They are read from the version
// list instead -- `ModelVersionSerializer` exposes the whole `validation_json`
// read-only, and `validation_json["warnings"]` is where the backend records
// every advisory finding (meshcheck at import, the post-export dimension check
// for a prompt). `pollVersion` re-reads the list once the status turns terminal
// so a finished render shows the findings it just produced.
//
// Reuses the vendored Three.js viewer through `x-stl-viewer` (app.js) with the
// same `stlUrl` / `viewerToken` contract as the original component, plus the
// `annotationMode` flag (docs/visual-editing.md 3.6), the optional explicit
// `modelFormat` (the source-mesh download URL has no extension) and the
// `readOnly` flag of the source-mesh preview.
(function () {
    "use strict";

    const ext = window.PrintForgeExt;
    const PF = window.PrintForge;
    const POLL_INTERVAL_MS = 2000;
    const POLL_TIMEOUT_MS = 10 * 60 * 1000;

    function sleep(ms) {
        return new Promise((resolve) => window.setTimeout(resolve, ms));
    }

    /**
     * Project one viewer annotation onto the frozen API shape
     * (docs/visual-editing.md 2.). `region` must be an object, never null.
     */
    function annotationPayload(annotation) {
        const point = Array.isArray(annotation.point) ? annotation.point.slice(0, 3) : [];
        const normal = Array.isArray(annotation.normal) ? annotation.normal.slice(0, 3) : [];
        while (point.length < 3) point.push(0);
        while (normal.length < 3) normal.push(0);
        return {
            id: String(annotation.id || ""),
            kind: annotation.kind === "region" ? "region" : "point",
            point: point.map(Number),
            normal: normal.map(Number),
            faces: (annotation.faces || [])
                .map((face) => Number(face))
                .filter((face) => Number.isFinite(face)),
            region: annotation.region || {},
            instruction: annotation.instruction || "",
        };
    }

    function idFromLocation() {
        const segments = window.location.pathname.split("/").filter(Boolean);
        const last = segments.length ? segments[segments.length - 1] : "";
        return /^\d+$/.test(last) ? last : "";
    }

    function projectWorkspaceComponent() {
        const component = {
            projectId: "",
            project: null,
            workspace: null,
            versions: [],
            selectedVersionId: null,
            prompt: "",
            referenceNote: "",
            referenceImage: null,
            referencePreview: "",
            loading: true,
            error: "",
            notice: "",
            forbidden: false,

            licenses: ext.LICENSES,
            descriptionDraft: "",
            licenseDraft: "",
            tagNames: [],
            tagInput: "",
            tagCatalogue: [],
            tagsDirty: false,
            savingMeta: false,
            sourceLabels: { manual: "kézi", ai: "AI", empty: "üres" },

            rating: { average: null, count: 0 },
            myRating: null,
            ratingBusy: false,

            shares: [],
            shareUserId: "",
            shareBusy: false,
            shareError: "",
            copiedShareId: null,

            publishing: false,
            aiBusy: false,
            downloadBusy: false,
            printBusy: false,
            printed: false,
            creatingVersion: false,
            polling: false,
            statusText: "",
            errors: [],

            // Planner clarification / assumptions (docs/planner-clarification.md 6.).
            agentRuns: [],
            clarificationRun: null,
            clarificationQuestions: [],
            clarificationError: "",
            submittingClarifications: false,
            clarifyAsk: false,

            viewerState: "empty",
            viewerMessage: "",
            dimensions: "",
            viewerToken: 0,

            // Read-only preview of the uploaded source mesh of the selected
            // version. Annotation is NOT available here: the raw mesh is in its
            // own units, annotations are in millimetres (docs/visual-editing.md
            // 2./6.). The viewer refuses it too (viewer.js `setReadOnly`).
            sourceMeshOpen: false,
            sourceMeshState: "empty",
            sourceMeshMessage: "",

            // Mesh upload (POST /projects/{id}/versions/from-mesh/).
            meshFile: null,
            meshForm: {
                scaleMm: "",
                rotateX: "",
                rotateY: "",
                rotateZ: "",
                repair: false,
                note: "",
            },
            uploadingMesh: false,
            meshError: "",

            // Visual-prompt annotations (docs/visual-editing.md 3.6).
            annotations: [],
            annotationMode: false,
            sendingAnnotations: false,
            annotationPrompt: "",
            activeAnnotationId: null,

            // Version history controls (docs/version-history-controls.md 4.).
            editingVersionId: null,
            editPrompt: "",
            editError: "",
            regenerateBusyId: null,
            confirmDeleteVersionId: null,
            deletingVersionId: null,
            // Version id -> the user confirmed "yes, two bodies is intended".
            // In memory only and keyed per version: it answers a question about
            // one render, it is not a property of the model.
            acknowledgedBodySplit: {},
            originLabels: {
                generate: "generate",
                annotation: "annotation",
                regenerate: "regenerate",
                manual: "manual",
            },

            formatDate: PF.formatDate,
            formatError(value) {
                if (value === null || value === undefined) return "";
                return typeof value === "string" ? value : JSON.stringify(value);
            },

            get busy() {
                // A mesh upload is a render like any other: including it here
                // keeps the prompt form, the version list and the annotation
                // panel from starting a second one while it runs.
                return this.creatingVersion || this.uploadingMesh || this.polling;
            },

            get hasManualMeta() {
                return Boolean(this.project)
                    && (this.project.description_source === "manual"
                        || this.project.tags_source === "manual");
            },

            get selectedVersion() {
                return this.versions.find((version) => version.id === this.selectedVersionId) || null;
            },

            get viewerLabel() {
                const version = this.selectedVersion;
                const projectName = this.project ? this.project.name : "Projekt";
                return version ? `v${version.version} · ${projectName}` : "";
            },

            /**
             * Read-only visual prompts the selected version was built from
             * (`ModelVersion.annotations_json`, api-dev). Empty for base versions.
             */
            get storedAnnotations() {
                const version = this.selectedVersion;
                const stored = version ? version.annotations_json : null;
                return Array.isArray(stored) ? stored : [];
            },

            /** Skills the selected version was built from (docs/skills.md 7.). */
            get usedSkills() {
                const version = this.selectedVersion;
                const validation =
                    version && version.validation_json ? version.validation_json : null;
                const skills =
                    validation && Array.isArray(validation.skills) ? validation.skills : [];
                return skills;
            },

            /**
             * Planner assumptions recorded on the selected version
             * (docs/planner-clarification.md 4./6.). Prefers the read-only
             * `assumptions` field and falls back to `validation_json` so the
             * panel also works before api-dev lands the flat serializer field.
             */
            get versionAssumptions() {
                const version = this.selectedVersion;
                if (!version) return [];
                if (Array.isArray(version.assumptions)) return version.assumptions;
                const validation = version.validation_json;
                if (validation && Array.isArray(validation.assumptions)) {
                    return validation.assumptions;
                }
                return [];
            },

            /** Whether the version was flagged for human review. */
            get versionReviewRequired() {
                const version = this.selectedVersion;
                if (!version) return false;
                if (typeof version.review_required === "boolean") {
                    return version.review_required;
                }
                const validation = version.validation_json;
                return Boolean(validation && validation.review_required);
            },

            /** True while the latest run still has unanswered questions. */
            get showClarifications() {
                return this.clarificationQuestions.length > 0;
            },

            get workspaceName() {
                return this.workspace ? this.workspace.name : "";
            },

            get workspaceDetailUrlTemplate() {
                const root = this.$root;
                return root && root.dataset ? root.dataset.workspaceDetailUrl || "" : "";
            },

            get workspaceHref() {
                if (!this.workspace) return "/workspaces/";
                return PF.fillPkTemplate(this.workspaceDetailUrlTemplate, this.workspace.id);
            },

            get stlUrl() {
                return this.selectedVersionId
                    ? PF.endpoints.artifact(this.selectedVersionId, "stl")
                    : "";
            },

            get scadUrl() {
                return this.selectedVersionId
                    ? PF.endpoints.artifact(this.selectedVersionId, "scad")
                    : "";
            },

            /** True when the selected version has a rendered preview artifact. */
            get hasPreviewImage() {
                const version = this.selectedVersion;
                return Boolean(version && this.selectedVersionId && version.preview_image);
            },

            /**
             * The rendered preview (what the vision model saw), served through the
             * artifact endpoint so it works for every storage backend.
             */
            get previewImageUrl() {
                return this.hasPreviewImage
                    ? PF.endpoints.artifact(this.selectedVersionId, "preview")
                    : "";
            },

            // -------------------------------------------------------------
            // Source mesh (read-only preview)
            // -------------------------------------------------------------

            /**
             * True when the selected version was built from an uploaded mesh and
             * that mesh can be downloaded. A generated version has neither.
             */
            get sourceMeshAvailable() {
                const version = this.selectedVersion;
                return Boolean(
                    version && version.source_mesh_available && version.source_mesh_url
                );
            },

            get sourceMeshUrl() {
                return this.sourceMeshAvailable ? this.selectedVersion.source_mesh_url : "";
            },

            /**
             * Container format of the stored source mesh, exactly as the
             * serializer reports it (`"stl" | "obj" | "glb" | ""`). Required:
             * `source_mesh_url` is `/api/v1/versions/{id}/source-mesh/`, which
             * carries no extension, so the viewer cannot guess the parser.
             */
            get sourceMeshFormat() {
                const version = this.selectedVersion;
                const format = version ? String(version.source_mesh_format || "") : "";
                return format.toLowerCase();
            },

            sourceMeshFormatLabel(format) {
                const value = String(format || "").toLowerCase();
                if (value === "stl") return "STL";
                if (value === "obj") return "OBJ";
                if (value === "glb" || value === "gltf") return "GLB";
                return "formátum nélkül";
            },

            // -------------------------------------------------------------
            // Render findings (`validation_json["warnings"]`)
            // -------------------------------------------------------------

            /**
             * Every advisory finding the render recorded on the selected
             * version, verbatim.
             *
             * This is deliberately ONE list, and it is the very list the
             * source-mesh panel used to read: `get_source_mesh_warnings` is
             * literally `list(validation_json["warnings"])` (backend
             * `designs/services.py:792-800` -> `api/serializers.py:384-386`),
             * so `source_mesh_warnings` and `validation_json.warnings` are the
             * *same strings* for every version -- a prompt-generated one
             * included. Keeping two would either print each line twice (a mesh
             * import with a dimension mismatch) or invent a provenance the row
             * does not carry: nothing in `validation_json` says which producer
             * wrote which line. So the canonical key wins and the flat field is
             * only a fallback for the day `validation_json` leaves the list
             * serializer.
             */
            get versionWarnings() {
                const version = this.selectedVersion;
                if (!version) return [];
                const validation = version.validation_json;
                const canonical =
                    validation && Array.isArray(validation.warnings)
                        ? validation.warnings
                        : [];
                const flat = Array.isArray(version.source_mesh_warnings)
                    ? version.source_mesh_warnings
                    : [];
                const collected = [];
                // Union, not concat: for an imported mesh the two sources are the
                // same array, and a repeated line would read as a second finding.
                canonical.concat(flat).forEach((warning) => {
                    if (typeof warning !== "string") return;
                    const text = warning.trim();
                    if (text && !collected.includes(text)) collected.push(text);
                });
                return collected;
            },

            /**
             * Which of the three display boxes a finding belongs to.
             *
             * The sentences are written by the backend, so this only recognises
             * the openings those producers use -- it never re-reads a number out
             * of the text, and the numbers stay where the backend put them:
             *
             *   "critical"  `designs/cad/dimensions.py::dimension_issues` and
             *                `designs/cad/meshcheck.py::check_mesh`: the rendered
             *                mesh is not the part that was asked for, or cannot
             *                be printed as it stands.
             *   "question"  `mesh has N disconnected bodies`: genuinely ambiguous
             *                (two separate prints is often correct), so the user
             *                decides, we do not decide for them.
             *   "note"      anything else (skill warnings, future producers).
             *
             * An unrecognised line degrades to a note: a wording change upstream
             * costs emphasis, it never invents a defect.
             */
            warningKind(warning) {
                const text = String(warning || "");
                // `mesh has 2 disconnected bodies (expect separate prints)`
                if (/disconnected bod(y|ies)/i.test(text)) return "question";
                // `rendered Y extent 195.0mm differs from the requested height
                //  90.0mm by +117% (tolerance +-25%)` and
                // `rendered part starts at Z=-1.500mm; ...` -- both
                // `dimension_issues`; the rendered part is wrong either way.
                if (/^rendered .+ differs from the requested /i.test(text)) return "critical";
                if (/^rendered part starts at Z=/i.test(text)) return "critical";
                // `Y extent 0.050mm is below the 0.1mm print minimum ...`,
                // `Y extent 1400.000mm exceeds the 1000.0mm print envelope ...` --
                // `meshcheck._extent_warnings`, i.e. a unit mix-up.
                if (/extent .+ (is below|exceeds) the .+ (print minimum|print envelope)/i.test(text)) {
                    return "critical";
                }
                // `shortest edge is 0.109mm, below the 0.4mm printable feature
                // size` -- a nozzle cannot lay that wall.
                if (/^shortest edge is /i.test(text)) return "critical";
                return "note";
            },

            /** The part is not what was asked for / will not print as it stands. */
            get criticalWarnings() {
                return this.versionWarnings.filter(
                    (warning) => this.warningKind(warning) === "critical"
                );
            },

            /** Ambiguous findings the user is asked to confirm. */
            get bodySplitWarnings() {
                return this.versionWarnings.filter(
                    (warning) => this.warningKind(warning) === "question"
                );
            },

            /** Informational findings: shown, but not worth a raised voice. */
            get noteWarnings() {
                return this.versionWarnings.filter(
                    (warning) => this.warningKind(warning) === "note"
                );
            },

            /**
             * Whether the user has already confirmed the multi-body finding for
             * *this* version. Keyed by version id on purpose: a fresh version
             * with two bodies is a new question, not an old answer.
             */
            get bodySplitAcknowledged() {
                const version = this.selectedVersion;
                if (!version) return false;
                return Boolean(this.acknowledgedBodySplit[version.id]);
            },

            /** Collapse the multi-body question into a one-line note. */
            acknowledgeBodySplit() {
                const version = this.selectedVersion;
                if (!version) return;
                this.acknowledgedBodySplit[version.id] = true;
            },

            /**
             * "Nem, ez hiba": the finding means the part is not what was wanted,
             * so the existing "Szerkesztés" affordance is the honest next step --
             * edit the prompt (with the measured numbers) and regenerate. Reused,
             * not invented: `startEditVersion` already backs the per-version edit
             * form, this only opens it and focuses the textarea so the user does
             * not have to hunt for the right row in the version history.
             *
             * Both the dimension mismatch and the "these two bodies are not
             * intended" answer go here; the difference is only the copy.
             */
            requestCorrection() {
                const version = this.selectedVersion;
                if (!version) return;
                this.startEditVersion(version, { focus: true });
            },

            /**
             * True when the version was produced from a prompt and the
             * specification asked for no geometry at all.
             *
             * `ModelSpecification._omit_empty_collections` drops `primitives` and
             * `operations` from the serialised spec when they are empty
             * (backend `agents/spec.py:443-461`), so "no geometry" reaches the UI
             * as *both keys missing*, not as `primitives: []`. The CAD backend
             * then falls back to its own template, which is exactly why this
             * needed saying out loud: the result otherwise looks finished.
             */
            get versionGeometryEmpty() {
                const version = this.selectedVersion;
                if (!version) return false;
                // An imported mesh has no primitives by design -- the uploaded
                // mesh *is* the geometry, and its specification is the
                // `{"generator": "mesh", ...}` descriptor, not a parametric one.
                if (this.sourceMeshAvailable) return false;
                const specification = version.specification_json;
                if (!specification || typeof specification !== "object"
                    || Array.isArray(specification)) {
                    return false;
                }
                if (specification.generator === "mesh") return false;
                const primitives = Array.isArray(specification.primitives)
                    ? specification.primitives
                    : [];
                const operations = Array.isArray(specification.operations)
                    ? specification.operations
                    : [];
                return primitives.length === 0 && operations.length === 0;
            },

            /** Any of the four blocks has something to say. */
            get renderFindingsVisible() {
                return Boolean(
                    this.versionGeometryEmpty
                    || this.criticalWarnings.length
                    || this.bodySplitWarnings.length
                    || this.noteWarnings.length
                );
            },

            toggleSourceMesh() {
                if (!this.sourceMeshAvailable) return;
                this.sourceMeshOpen = !this.sourceMeshOpen;
                // The canvas is created fresh each time it is opened, so the
                // previous instance's state must not survive it.
                if (this.sourceMeshOpen) {
                    this.sourceMeshState = "empty";
                    this.sourceMeshMessage = "";
                }
            },

            /**
             * `viewer-state` of the source-mesh canvas. The page's root listens
             * on `.document`, so the same events also reach `handleViewerState`;
             * that one ignores the read-only canvases, this one keeps them.
             */
            handleSourceMeshState(event) {
                const detail = event.detail || {};
                this.sourceMeshState = detail.state || "empty";
                this.sourceMeshMessage = detail.message || "";
            },

            /** Parse a stored JSON payload (spec/response) for display. */
            prettyJson(value) {
                if (value === null || value === undefined) return "";
                if (typeof value === "string") return value;
                try {
                    return JSON.stringify(value, null, 2);
                } catch (error) {
                    return "";
                }
            },

            /** Recorded planner/reviser exchanges of a version (LLM trace). */
            llmTraceFor(version) {
                const validation = version && version.validation_json ? version.validation_json : null;
                const trace = validation && validation.llm_trace ? validation.llm_trace : null;
                return Array.isArray(trace) ? trace : [];
            },

            llmTraceLabel(entry) {
                if (!entry) return "";
                if (entry.agent === "planner") return "Planner (fő generátor)";
                if (entry.agent === "reviser") return `Reviser (javítás, ${entry.attempt}. kísérlet)`;
                return entry.agent || "ismeretlen";
            },

            /**
             * Stored vision self-check verdict of the selected version
             * (`validation_json.vision_review`, docs/vision-self-check.md 5.):
             * either `{matches, issues, summary}` or a `{skipped, reason}` marker.
             */
            get visionReview() {
                return this.visionReviewFor(this.selectedVersion);
            },

            visionReviewFor(version) {
                const validation = version && version.validation_json ? version.validation_json : null;
                const review =
                    validation && validation.vision_review ? validation.vision_review : null;
                return review && typeof review === "object" ? review : null;
            },

            /** Show the panel when a verdict (or at least a preview) exists. */
            get visionReviewVisible() {
                const review = this.visionReview;
                if (review && (typeof review.matches === "boolean" || review.skipped)) return true;
                return this.hasPreviewImage;
            },

            /** Non-matching issues reported by the reviewer (empty when it matched). */
            get visionIssues() {
                const review = this.visionReview;
                if (!review || review.matches) return [];
                return Array.isArray(review.issues) ? review.issues : [];
            },

            /** The specification the vision model was shown beside the image. */
            get visionSpecificationText() {
                const version = this.selectedVersion;
                const specification =
                    version && version.specification_json ? version.specification_json : null;
                if (!specification || !Object.keys(specification).length) return "";
                return this.prettyJson(specification);
            },

            visionReviewLabel(review) {
                if (!review) return "";
                if (review.skipped) return "Kihagyva";
                return review.matches ? "Egyezik" : "Eltérés";
            },

            /**
             * Raw vision input/output (`validation_json.vision_trace`):
             * the literal system/user prompt the reviewer received and the
             * model's raw JSON answer.
             */
            get visionTrace() {
                return this.visionTraceFor(this.selectedVersion);
            },

            visionTraceFor(version) {
                const validation = version && version.validation_json ? version.validation_json : null;
                const trace = validation && validation.vision_trace ? validation.vision_trace : null;
                return trace && typeof trace === "object" ? trace : null;
            },

            get visionTracePrompt() {
                const trace = this.visionTrace;
                return trace ? String(trace.prompt || "") : "";
            },

            get visionTraceResponse() {
                const trace = this.visionTrace;
                return trace ? this.prettyJson(trace.response) : "";
            },

            get visionTraceTextResponse() {
                const trace = this.visionTrace;
                return trace ? this.prettyJson(trace.text_response) : "";
            },

            get visionTraceGuardOverride() {
                const trace = this.visionTrace;
                return Boolean(trace && trace.guard_override);
            },

            get ratingLabel() {
                if (!this.rating || !this.rating.count) return "Még nincs értékelés";
                return `${Number(this.rating.average).toFixed(1)} ★ · ${this.rating.count} értékelés`;
            },

            get downloadCount() {
                return this.project ? this.project.download_count || 0 : 0;
            },

            get printCount() {
                return this.project ? this.project.print_count || 0 : 0;
            },

            get printLabel() {
                return this.printCount ? `${this.printCount} nyomtatás` : "Még senki sem nyomtatta";
            },

            sourceLabel(source) {
                return this.sourceLabels[source] || source || "üres";
            },

            init() {
                this.projectId = (this.$el && this.$el.dataset
                    ? this.$el.dataset.projectId
                    : "") || idFromLocation();
                this.bindViewerEvents();
                if (!this.projectId) {
                    this.error = "Hiányzó projekt azonosító az URL-ben.";
                    this.loading = false;
                    return;
                }
                this.reload();
                if (ext.skillPickerState) this.loadSkillCatalogue();
            },

            destroy() {
                const root = this.$el;
                if (!root || !root.removeEventListener) return;
                root.removeEventListener("viewer-annotation-added", this.onAnnotationAddedEvent);
                root.removeEventListener("viewer-annotation-updated", this.onAnnotationUpdatedEvent);
                root.removeEventListener("viewer-annotation-removed", this.onAnnotationRemovedEvent);
                root.removeEventListener("viewer-selection-changed", this.onSelectionChangedEvent);
            },

            /** Bridge the viewer's bubbling CustomEvents into Alpine state. */
            bindViewerEvents() {
                const root = this.$el;
                if (!root || !root.addEventListener) return;
                this.onAnnotationAddedEvent = (event) => this.handleAnnotationAdded(event);
                this.onAnnotationUpdatedEvent = (event) => this.handleAnnotationUpdated(event);
                this.onAnnotationRemovedEvent = (event) => this.handleAnnotationRemoved(event);
                this.onSelectionChangedEvent = (event) => this.handleSelectionChanged(event);
                root.addEventListener("viewer-annotation-added", this.onAnnotationAddedEvent);
                root.addEventListener("viewer-annotation-updated", this.onAnnotationUpdatedEvent);
                root.addEventListener("viewer-annotation-removed", this.onAnnotationRemovedEvent);
                root.addEventListener("viewer-selection-changed", this.onSelectionChangedEvent);
            },

            async reload() {
                this.loading = true;
                this.error = "";
                this.forbidden = false;
                await this.loadTagCatalogue();
                try {
                    this.applyProject(await PF.api.getProject(this.projectId));
                    await this.loadWorkspace();
                    await this.loadPublicRating();
                    await this.loadVersions();
                    await this.loadAgentRuns();
                    if (!this.selectedVersionId && this.versions.length) {
                        this.selectVersion(this.versions[0]);
                    }
                    await this.loadShares();
                } catch (error) {
                    if (ext.isPermissionError(error)) {
                        this.forbidden = true;
                        this.error = "Nincs hozzáférésed ehhez a projekthez.";
                    } else {
                        this.error = error.message || String(error);
                    }
                } finally {
                    this.loading = false;
                }
            },

            /** Project `.tags` are slugs; map them back to names for editing. */
            projectTagNames(slugs) {
                return (slugs || []).map((slug) => {
                    const found = this.tagCatalogue.find((tag) => tag.slug === slug);
                    return found ? found.name : slug;
                });
            },

            applyProject(project) {
                this.project = project || null;
                if (!this.project) return;
                this.descriptionDraft = this.project.description || "";
                this.licenseDraft = this.project.license || "";
                this.tagNames = this.projectTagNames(this.project.tags);
                this.tagsDirty = false;
            },

            async loadTagCatalogue() {
                try {
                    let url = ext.endpoints.tags();
                    const collected = [];
                    while (url) {
                        const payload = await PF.request(url);
                        collected.push(...PF.unwrapList(payload));
                        url = ext.sameOrigin(payload && payload.next);
                    }
                    this.tagCatalogue = collected;
                } catch (error) {
                    this.tagCatalogue = [];
                }
                // Re-map slugs to names now that the catalogue is known, unless
                // the user already edited the draft.
                if (this.project && !this.tagsDirty) {
                    this.tagNames = this.projectTagNames(this.project.tags);
                }
            },

            async loadVersions() {
                this.versions = await PF.api.listVersions(this.projectId);
            },

            /** Workspace name for the breadcrumb (`project.workspace` is an id). */
            async loadWorkspace() {
                const workspaceId = this.project ? this.project.workspace : null;
                if (!workspaceId || !ext.endpoints.workspace) {
                    this.workspace = null;
                    return;
                }
                try {
                    this.workspace = await PF.request(ext.endpoints.workspace(workspaceId));
                } catch (error) {
                    // The breadcrumb simply omits the workspace on failure.
                    this.workspace = null;
                }
            },

            /**
             * Seed the aggregate rating from the public community projection
             * (the project serializer itself carries no rating). The caller's
             * own score still only becomes known after a rate/unrate call.
             */
            async loadPublicRating() {
                if (!this.project || !this.project.is_public) return;
                try {
                    const payload = await PF.request(
                        ext.endpoints.communityProject(this.projectId)
                    );
                    if (payload && payload.rating) this.rating = payload.rating;
                } catch (error) {
                    // A not-yet-indexed public project simply has no rating.
                }
            },

            async loadShares() {
                try {
                    const payload = await PF.request(ext.endpoints.shares(this.projectId));
                    this.shares = Array.isArray(payload) ? payload : PF.unwrapList(payload);
                } catch (error) {
                    // VIEWERs (and non-members) may not read shares; hide the list.
                    this.shares = [];
                }
            },

            selectVersion(version) {
                if (!version) return;
                if (String(version.id) !== String(this.selectedVersionId)) {
                    // Annotations belong to a single base version.
                    this.clearAnnotations();
                    this.closeSourceMeshIfAbsent(version);
                }
                this.selectedVersionId = version.id;
                this.showStoredAnnotations(version);
            },

            /**
             * The source-mesh preview is per version: switching to a version
             * without an upload closes the panel instead of leaving the previous
             * version's mesh on screen under the new version's name.
             */
            closeSourceMeshIfAbsent(version) {
                if (version && version.source_mesh_available && version.source_mesh_url) return;
                this.sourceMeshOpen = false;
                this.sourceMeshState = "empty";
                this.sourceMeshMessage = "";
            },

            /**
             * Delete one version (2025-09): the API removes its artifacts and
             * refuses when print jobs / build plates still reference it. The
             * selection follows the new list so the viewer never shows a model
             * that no longer exists.
             */
            async deleteVersion(version) {
                if (!version || this.deletingVersionId) return;
                this.deletingVersionId = version.id;
                this.errors = [];
                this.error = "";
                try {
                    await PF.api.deleteVersion(version.id);
                    this.confirmDeleteVersionId = null;
                    if (String(this.selectedVersionId) === String(version.id)) {
                        this.selectedVersionId = null;
                        this.clearAnnotations();
                    }
                    await this.loadVersions();
                    if (!this.selectedVersionId && this.versions.length) {
                        this.selectVersion(this.versions[0]);
                    }
                    this.notice = `A v${version.version} verzió törölve.`;
                } catch (error) {
                    if (ext.isPermissionError(error)) {
                        this.errors = ["Nincs jogosultságod a verzió törléséhez."];
                    } else {
                        this.errors = [error.message || String(error)];
                    }
                } finally {
                    this.deletingVersionId = null;
                }
            },

            /**
             * Render the selected version's stored (previous) visual prompts
             * read-only in the viewer.
             *
             * The viewer's `apply()` clears stored markers whenever the STL
             * URL/token changes, so this runs on a macrotask — after Alpine has
             * flushed the `x-stl-viewer` directive effect — and waits for the
             * lazily mounted viewer instance.
             */
            showStoredAnnotations(version) {
                if (!version) return;
                const versionId = String(version.id);
                const stored = Array.isArray(version.annotations_json)
                    ? version.annotations_json
                    : [];
                let attempts = 40;
                const render = () => {
                    // A newer selection supersedes this (async) render.
                    if (String(this.selectedVersionId) !== versionId) return;
                    const viewer = this.viewerInstance();
                    if (!viewer) {
                        if (attempts <= 0) return;
                        attempts -= 1;
                        window.setTimeout(render, 50);
                        return;
                    }
                    viewer?.setStoredAnnotations?.(stored);
                };
                window.setTimeout(render, 0);
            },

            // -------------------------------------------------------------
            // Metadata (description / license / tags)
            // -------------------------------------------------------------
            addTag() {
                const raw = this.tagInput || "";
                raw.split(",")
                    .map((name) => name.trim())
                    .filter(Boolean)
                    .forEach((name) => {
                        const exists = this.tagNames.some(
                            (item) => item.toLowerCase() === name.toLowerCase()
                        );
                        if (!exists) this.tagNames.push(name);
                    });
                this.tagInput = "";
                this.tagsDirty = true;
            },

            removeTag(index) {
                this.tagNames.splice(index, 1);
                this.tagsDirty = true;
            },

            async saveMeta() {
                if (!this.project || this.savingMeta) return;
                const patch = {};
                if (this.descriptionDraft !== (this.project.description || "")) {
                    patch.description = this.descriptionDraft;
                }
                if (this.licenseDraft !== (this.project.license || "")) {
                    patch.license = this.licenseDraft;
                }
                if (this.tagsDirty) patch.tags = this.tagNames;

                this.error = "";
                this.notice = "";
                if (!Object.keys(patch).length) {
                    this.notice = "Nincs mentendő változás.";
                    return;
                }
                this.savingMeta = true;
                try {
                    this.applyProject(
                        await PF.request(PF.endpoints.project(this.projectId), {
                            method: "PATCH",
                            body: patch,
                        })
                    );
                    this.notice = "Projekt adatai elmentve.";
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.savingMeta = false;
                }
            },

            async generateAiDescription() {
                if (!this.project || this.aiBusy) return;
                if (this.hasManualMeta) {
                    const confirmed = window.confirm(
                        "A leírás vagy a címkék kézzel szerkesztettek. Az AI csak az üres " +
                            "mezőket tölti ki, a manuálisakat nem írja felül. Folytatod?"
                    );
                    if (!confirmed) return;
                }
                this.aiBusy = true;
                this.error = "";
                this.notice = "";
                try {
                    const result = await PF.request(ext.endpoints.descriptionAi(this.projectId), {
                        method: "POST",
                        body: {},
                    });
                    if (result && result.project) this.applyProject(result.project);
                    if (result && result.applied) {
                        this.notice = "AI leírás és címkék generálva.";
                    } else if (result && result.reason === "manual") {
                        this.notice = "A manuális mezőket az AI nem írja felül.";
                    } else if (result && result.error) {
                        this.error = `AI hiba: ${result.error}`;
                    } else {
                        this.notice = "Nem történt változás.";
                    }
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.aiBusy = false;
                }
            },

            async togglePublish() {
                if (!this.project || this.publishing) return;
                this.publishing = true;
                this.error = "";
                this.notice = "";
                const url = this.project.is_public
                    ? ext.endpoints.unpublish(this.projectId)
                    : ext.endpoints.publish(this.projectId);
                try {
                    const updated = await PF.request(url, { method: "POST", body: {} });
                    this.applyProject(updated);
                    this.notice = updated.is_public
                        ? "A projekt publikálva a közösségben."
                        : "A projekt eltávolítva a közösségből.";
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.publishing = false;
                }
            },

            // -------------------------------------------------------------
            // Download / rating
            // -------------------------------------------------------------
            async downloadLatest() {
                if (this.downloadBusy) return;
                this.downloadBusy = true;
                this.error = "";
                this.notice = "";
                const body = this.selectedVersionId ? { model_version: this.selectedVersionId } : {};
                try {
                    const data = await PF.request(ext.endpoints.download(this.projectId), {
                        method: "POST",
                        body,
                    });
                    if (data && data.url) {
                        if (this.project) this.project.download_count = data.download_count;
                        this.notice = data.counted === false
                            ? `Letöltés (már számoltuk, összesen ${data.download_count}).`
                            : `Letöltés naplózva (összesen ${data.download_count}).`;
                        window.location.assign(data.url);
                    } else {
                        this.error = "Nincs letölthető STL ehhez a projekthez.";
                    }
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.downloadBusy = false;
                }
            },

            async markPrinted() {
                if (!this.project || this.printBusy) return;
                this.printBusy = true;
                this.error = "";
                this.notice = "";
                try {
                    const data = await PF.request(ext.endpoints.printProject(this.projectId), {
                        method: "POST",
                    });
                    if (data && data.print_count !== undefined) {
                        this.project.print_count = data.print_count;
                    }
                    this.printed = true;
                    this.notice = data && data.counted === false
                        ? "Már jelezted, hogy kinyomtattad."
                        : "Köszönjük, hogy jelezted!";
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.printBusy = false;
                }
            },

            applyRating(data) {
                if (data && data.summary) this.rating = data.summary;
                this.myRating = data && data.mine !== undefined ? data.mine : null;
            },

            async rate(score) {
                if (this.ratingBusy) return;
                this.ratingBusy = true;
                this.error = "";
                try {
                    this.applyRating(
                        await PF.request(ext.endpoints.rate(this.projectId), {
                            method: "POST",
                            body: { score },
                        })
                    );
                    this.notice = "Köszönjük az értékelést!";
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.ratingBusy = false;
                }
            },

            async unrate() {
                if (this.ratingBusy) return;
                this.ratingBusy = true;
                this.error = "";
                try {
                    this.applyRating(
                        await PF.request(ext.endpoints.rate(this.projectId), { method: "DELETE" })
                    );
                    this.notice = "Értékelés törölve.";
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.ratingBusy = false;
                }
            },

            // -------------------------------------------------------------
            // Sharing
            // -------------------------------------------------------------
            shareLabel(share) {
                if (!share) return "";
                if (share.token) return "Nyilvános link";
                return `Felhasználó #${share.shared_with}`;
            },

            async createShareLink() {
                if (this.shareBusy) return;
                this.shareBusy = true;
                this.shareError = "";
                try {
                    await PF.request(ext.endpoints.shares(this.projectId), {
                        method: "POST",
                        body: { create_link: true },
                    });
                    await this.loadShares();
                } catch (error) {
                    this.shareError = error.message || String(error);
                } finally {
                    this.shareBusy = false;
                }
            },

            async createUserShare() {
                const userId = Number.parseInt(this.shareUserId, 10);
                if (!Number.isFinite(userId) || userId <= 0) {
                    this.shareError = "Adj meg egy felhasználó azonosítót.";
                    return;
                }
                if (this.shareBusy) return;
                this.shareBusy = true;
                this.shareError = "";
                try {
                    await PF.request(ext.endpoints.shares(this.projectId), {
                        method: "POST",
                        body: { user: userId },
                    });
                    this.shareUserId = "";
                    await this.loadShares();
                } catch (error) {
                    this.shareError = error.message || String(error);
                } finally {
                    this.shareBusy = false;
                }
            },

            async revokeShare(share) {
                if (!share || this.shareBusy) return;
                this.shareBusy = true;
                this.shareError = "";
                try {
                    await PF.request(ext.endpoints.share(this.projectId, share.id), {
                        method: "DELETE",
                    });
                    this.shares = this.shares.filter((item) => item.id !== share.id);
                } catch (error) {
                    this.shareError = error.message || String(error);
                } finally {
                    this.shareBusy = false;
                }
            },

            async copyToken(share) {
                if (!share || !share.token) return;
                const url = `${window.location.origin}/share/${share.token}/`;
                try {
                    await navigator.clipboard.writeText(url);
                    this.copiedShareId = share.id;
                    window.setTimeout(() => {
                        this.copiedShareId = null;
                    }, 1500);
                } catch (error) {
                    this.shareError = "A vágólap nem érhető el.";
                }
            },

            // -------------------------------------------------------------
            // Versions (#27 reference image)
            // -------------------------------------------------------------
            onReferenceChange(event) {
                const file = event && event.target && event.target.files
                    ? event.target.files[0]
                    : null;
                this.referenceImage = file || null;
                if (this.referencePreview) URL.revokeObjectURL(this.referencePreview);
                this.referencePreview = file ? URL.createObjectURL(file) : "";
            },

            clearReference() {
                if (this.referencePreview) URL.revokeObjectURL(this.referencePreview);
                this.referenceImage = null;
                this.referencePreview = "";
            },

            async submitVersion() {
                const prompt = this.prompt.trim();
                if (this.busy) return;
                if (!prompt && !this.referenceImage) return;

                this.creatingVersion = true;
                this.error = "";
                this.errors = [];
                this.statusText = "Verzió létrehozása…";
                try {
                    // Snapshot what we already know before submitting: the agent
                    // path may only switch to a version/run that is newer than
                    // these, otherwise a failed run would silently reuse the old
                    // model (bug: "whatever the input, the model is the same").
                    const baselineVersionId = this.newestVersionId();
                    const baselineRunId = await this.latestAgentRunId();
                    const formData = new FormData();
                    formData.append("prompt", prompt);
                    if (this.referenceNote.trim()) {
                        formData.append("reference_note", this.referenceNote.trim());
                    }
                    if (this.referenceImage) {
                        formData.append("reference_image", this.referenceImage);
                    }
                    // Skills (docs/skills.md): ignored by the current
                    // VersionCreateSerializer until api-dev adds `skill_ids`.
                    if (this.autoSkillSelection) {
                        formData.append("auto_skill_selection", "true");
                    }
                    (this.selectedSkillIds || []).forEach((skillId) => {
                        formData.append("skill_ids", skillId);
                    });
                    // Planner clarify policy (docs/planner-clarification.md 5.):
                    // default "assume"; "ask" lets the Planner stop with questions.
                    formData.append(
                        "clarify",
                        this.clarifyAsk
                            ? ext.CLARIFY_POLICIES.ask
                            : ext.CLARIFY_POLICIES.assume
                    );
                    const created = await ext.requestForm(
                        PF.endpoints.versions(this.projectId),
                        formData
                    );
                    this.prompt = "";
                    this.referenceNote = "";
                    this.clearReference();

                    if (created && created.mode === "agent") {
                        // The AI workflow builds the specification and creates the
                        // version itself; only switch to it once the run finished
                        // successfully AND a version newer than the baseline
                        // exists. A failed/timed-out run must not silently keep
                        // showing the previous model as if it were the result.
                        const status = await this.pollAgentRun(baselineRunId);
                        await this.loadVersions();
                        if (status === "clarification") {
                            // The run stopped for user input: no version was
                            // created, so the clarification panel now shows the
                            // questions (docs/planner-clarification.md 6.).
                            this.notice = "A Planner visszakérdezett – válaszolj a kérdésekre a folytatáshoz.";
                            return;
                        }
                        if (status !== "done") {
                            this.error = status === "failed"
                                ? "A generálás nem sikerült: nem készült új verzió, a korábbi modell maradt kiválasztva."
                                : "Időtúllépés: a generálás nem fejeződött be, a korábbi modell maradt kiválasztva.";
                            if (!this.errors.length) {
                                this.errors = [this.error];
                            }
                            return;
                        }
                        const newest = this.versions
                            .filter((version) => Number(version.id) > Number(baselineVersionId))
                            .reduce(
                                (max, version) => (!max || version.id > max.id ? version : max),
                                null
                            );
                        if (!newest) {
                            this.error = "A generálás befejeződött, de nem készült új verzió.";
                            this.errors = ["Nem készült új verzió."];
                            return;
                        }
                        this.selectedVersionId = newest.id;
                        this.viewerToken += 1;
                        return;
                    }

                    await this.loadVersions();
                    const versionId = (created && created.id)
                        || (this.versions.length ? this.versions[0].id : null);
                    if (versionId) {
                        this.selectedVersionId = versionId;
                        await this.pollVersion(versionId);
                        // The STL usually only exists once the worker is done.
                        this.viewerToken += 1;
                    }
                    await this.loadVersions();
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.creatingVersion = false;
                }
            },

            /** Highest version id currently loaded (0 when there is none). */
            newestVersionId() {
                return this.versions.reduce(
                    (max, version) => Math.max(max, Number(version.id) || 0),
                    0
                );
            },

            // -------------------------------------------------------------
            // Mesh import (POST /projects/{id}/versions/from-mesh/)
            // -------------------------------------------------------------

            onMeshChange(event) {
                const input = event && event.target;
                const file = input && input.files && input.files[0] ? input.files[0] : null;
                this.meshFile = file || null;
                this.meshError = "";
            },

            clearMesh() {
                this.meshFile = null;
                this.meshForm = {
                    scaleMm: "",
                    rotateX: "",
                    rotateY: "",
                    rotateZ: "",
                    repair: false,
                    note: "",
                };
                // The <input type="file"> keeps its own value, so it is reset
                // here; otherwise picking the same file again fires no change
                // event and the form looks empty while it is not.
                const input = this.$refs && this.$refs.meshFile;
                if (input) input.value = "";
            },

            /**
             * The server's own rejection reason, never a generic "failed".
             *
             * `designs.services` answers a bad upload with a readable message:
             * an unsupported suffix (naming `stl, obj, glb`), an empty file, a
             * payload over `mesh_max_source_bytes`, or - the case that matters
             * most - the blocking meshcheck problems of a mesh that is not
             * printable. That text is the only place the user learns *why* their
             * mesh was refused, so it is shown verbatim behind a Hungarian lead
             * instead of being replaced.
             */
            meshErrorText(error) {
                const detail = (error && error.message) || String(error);
                if (error && ext.isPermissionError(error)) {
                    return "Nincs jogosultságod a mesh feltöltéséhez.";
                }
                if (error && error.status === 400) {
                    return `A mesh elutasítva: ${detail}`;
                }
                return `A mesh feltöltése sikertelen: ${detail}`;
            },

            /**
             * Create the next version from the chosen mesh. Mirrors the
             * non-agent branch of `submitVersion`: the endpoint answers with the
             * new version (`status: "queued"`), the render is enqueued, so the
             * version is polled and the viewer token bumped afterwards.
             */
            async submitMesh() {
                if (this.uploadingMesh) return;
                if (!this.meshFile) {
                    // A zero-byte file is left to the server: only "nothing is
                    // chosen" is rejected here.
                    this.meshError = "Válassz ki egy mesh fájlt (STL, OBJ vagy GLB).";
                    return;
                }

                this.uploadingMesh = true;
                this.meshError = "";
                this.error = "";
                this.errors = [];
                this.notice = "";
                this.statusText = "Mesh feltöltése…";
                try {
                    const formData = new FormData();
                    formData.append("file", this.meshFile);
                    // Optional transform. Omitting a key is meaningful: the
                    // backend then applies the mesh backend's own default (and
                    // the `mesh_*` setting) instead of a value hardcoded here.
                    const scale = Number.parseFloat(this.meshForm.scaleMm);
                    if (Number.isFinite(scale)) formData.append("scale_mm", String(scale));
                    // `rotate_deg` is a three-item list; a multipart QueryDict
                    // carries a list as repeated keys, which is what DRF's
                    // ListField reads. All three or none - a partial rotation
                    // is not a request the API can express.
                    const rotation = ["rotateX", "rotateY", "rotateZ"].map((key) =>
                        Number.parseFloat(this.meshForm[key])
                    );
                    if (rotation.every((angle) => Number.isFinite(angle))) {
                        rotation.forEach((angle) => formData.append("rotate_deg", String(angle)));
                    }
                    if (this.meshForm.repair) formData.append("repair", "true");
                    const note = (this.meshForm.note || "").trim();
                    if (note) formData.append("reference_note", note);

                    const created = await ext.requestForm(
                        ext.endpoints.versionFromMesh(this.projectId),
                        formData
                    );
                    this.clearMesh();

                    await this.loadVersions();
                    const versionId = (created && created.id)
                        || (this.versions.length ? this.versions[0].id : null);
                    if (versionId) {
                        this.selectedVersionId = versionId;
                        await this.pollVersion(versionId);
                        // The STL usually only exists once the worker is done.
                        this.viewerToken += 1;
                    }
                    await this.loadVersions();
                    if (versionId) {
                        this.notice = "A meshből készült az új verzió.";
                    }
                } catch (error) {
                    this.meshError = this.meshErrorText(error);
                } finally {
                    this.uploadingMesh = false;
                }
            },

            /** Highest agent-run id already recorded for this project. */
            async latestAgentRunId() {
                try {
                    const runs = await PF.api.listAgentRuns();
                    return runs
                        .filter((run) => Number(run.project) === Number(this.projectId))
                        .reduce((max, run) => Math.max(max, run.id), 0);
                } catch (error) {
                    return 0;
                }
            },

            // -------------------------------------------------------------
            // Planner clarification (docs/planner-clarification.md 6.)
            // -------------------------------------------------------------

            /**
             * Blocking questions of a run. Prefers the documented flat
             * `clarifications` field and falls back to `state_json`, which the
             * current serializer already exposes.
             */
            runClarifications(run) {
                if (!run) return [];
                if (Array.isArray(run.clarifications)) return run.clarifications;
                const state = run.state_json;
                if (state && Array.isArray(state.clarifications)) return state.clarifications;
                return [];
            },

            /**
             * Show the clarification form only when the latest run stopped for
             * user input and produced no version. Answering starts a new run,
             * which supersedes the panel; it reappears only if that run also
             * asks. A run with `state_json.version_id` did not stop for input.
             */
            refreshClarificationState(runs) {
                const list = (runs || this.agentRuns || []).filter(
                    (run) => Number(run.project) === Number(this.projectId)
                );
                const latest = list.reduce(
                    (max, run) => (!max || run.id > max.id ? run : max),
                    null
                );
                this.clarificationRun = null;
                this.clarificationQuestions = [];
                if (!latest) return;
                const status = String(latest.status || "").toLowerCase();
                // `clarification` is a terminal run state with no version; it is
                // exactly the case the panel exists for (docs/planner-clarification.md 6.).
                const visible = ["done", "completed", "complete", "clarification"];
                if (status && !visible.includes(status)) {
                    return;
                }
                const clarifications = this.runClarifications(latest);
                if (!clarifications.length) return;
                const state = latest.state_json || {};
                if (state.version_id) return;
                this.clarificationRun = latest;
                this.clarificationQuestions = clarifications.map((item) => ({
                    field: item.field || "",
                    question: item.question || "",
                    answer: "",
                }));
            },

            async loadAgentRuns() {
                try {
                    this.agentRuns = await PF.api.listAgentRuns();
                } catch (error) {
                    this.agentRuns = [];
                }
                this.refreshClarificationState(this.agentRuns);
            },

            /** POST the answers, then poll for the follow-up run (new version). */
            async submitClarifications() {
                if (!this.clarificationRun || this.submittingClarifications) return;
                const answers = this.clarificationQuestions.map((item) => ({
                    field: item.field,
                    answer: (item.answer || "").trim(),
                }));
                if (answers.some((item) => !item.answer)) {
                    this.clarificationError = "Minden kérdésre adj választ.";
                    return;
                }
                const runId = this.clarificationRun.id;
                const baselineVersionId = this.newestVersionId();
                this.submittingClarifications = true;
                this.clarificationError = "";
                this.error = "";
                this.notice = "";
                this.errors = [];
                this.statusText = "Válaszok elküldése…";
                try {
                    await PF.request(ext.endpoints.runClarifications(runId), {
                        method: "POST",
                        body: { answers },
                    });
                    // The answers are consumed; the follow-up run supersedes this
                    // panel. It reappears only if the new run also asks.
                    this.clarificationRun = null;
                    this.clarificationQuestions = [];
                    const status = await this.pollClarificationRun(runId, baselineVersionId);
                    if (status === "clarification") {
                        this.notice = "A Planner további kérdéseket tett fel.";
                    } else if (status === "done") {
                        this.notice = "A válaszok alapján elkészült az új verzió.";
                    }
                } catch (error) {
                    this.clarificationError = error.message || String(error);
                } finally {
                    this.submittingClarifications = false;
                }
            },

            /**
             * Poll the runs started by an answered clarification. Returns
             * "done" | "clarification" | "failed" | "timeout". Only a genuinely
             * newer version (id above the baseline) is ever selected.
             */
            async pollClarificationRun(parentRunId, baselineVersionId) {
                this.polling = true;
                const deadline = Date.now() + POLL_TIMEOUT_MS;
                try {
                    while (Date.now() < deadline) {
                        await sleep(POLL_INTERVAL_MS);
                        let runs = [];
                        try {
                            runs = await PF.api.listAgentRuns();
                        } catch (error) {
                            this.statusText = `Állapot lekérdezése sikertelen: ${error.message}`;
                            continue;
                        }
                        this.agentRuns = runs;
                        const mine = runs.filter(
                            (run) =>
                                Number(run.project) === Number(this.projectId)
                                && run.id > parentRunId
                        );
                        if (!mine.length) {
                            this.statusText = "A válaszok feldolgozása…";
                            continue;
                        }
                        const run = mine.reduce((a, b) => (a.id > b.id ? a : b));
                        const state = String(run.status || "").toLowerCase();
                        this.statusText = `AI: ${run.status}`;
                        if (state === "failed") {
                            this.errors = run.error ? [run.error] : ["A generálás nem sikerült."];
                            this.error = "A válaszok feldolgozása nem sikerült.";
                            return "failed";
                        }
                        if (state !== "done" && state !== "completed" && state !== "complete") {
                            continue;
                        }
                        if (this.runClarifications(run).length) {
                            this.refreshClarificationState(runs);
                            this.statusText = "A Planner további kérdéseket tett fel.";
                            return "clarification";
                        }
                        const versionId = (run.state_json && run.state_json.version_id) || null;
                        if (!versionId) {
                            this.error = "A válaszok feldolgozása nem készített verziót.";
                            this.errors = ["Nem készült új verzió."];
                            return "failed";
                        }
                        await this.loadVersions();
                        const newest = this.versions
                            .filter((version) => Number(version.id) > Number(baselineVersionId))
                            .reduce(
                                (max, version) => (!max || version.id > max.id ? version : max),
                                null
                            );
                        if (newest) {
                            this.selectedVersionId = newest.id;
                            this.viewerToken += 1;
                            this.showStoredAnnotations(newest);
                        }
                        this.refreshClarificationState(runs);
                        this.statusText = "A modell elkészült.";
                        return "done";
                    }
                    this.statusText = "Időtúllépés a válaszok feldolgozása közben.";
                    this.error = "Időtúllépés: a válaszok feldolgozása nem fejeződött be.";
                    return "timeout";
                } finally {
                    this.polling = false;
                }
            },

            /**
             * Poll the AI run started by this submit until it is terminal.
             * Returns "done" | "failed" | "timeout" so the caller never has to
             * guess whether the run is usable (the old implicit `undefined` made
             * a failed run look like a finished one).
             */
            async pollAgentRun(baselineRunId) {
                this.polling = true;
                const deadline = Date.now() + POLL_TIMEOUT_MS;
                try {
                    while (Date.now() < deadline) {
                        await sleep(POLL_INTERVAL_MS);
                        let runs = [];
                        try {
                            runs = await PF.api.listAgentRuns();
                        } catch (error) {
                            this.statusText = `Állapot lekérdezése sikertelen: ${error.message}`;
                            continue;
                        }
                        const mine = runs.filter(
                            (run) =>
                                Number(run.project) === Number(this.projectId)
                                && run.id > baselineRunId
                        );
                        if (!mine.length) {
                            this.statusText = "AI feldolgozás…";
                            continue;
                        }
                        const run = mine.reduce((a, b) => (a.id > b.id ? a : b));
                        const state = String(run.status || "").toLowerCase();
                        this.statusText = `AI: ${run.status}`;
                        if (state === "done") {
                            this.agentRuns = runs;
                            // A run can finish "done" without a version when the
                            // Planner stopped for user input (docs/planner-clarification.md 4.).
                            if (this.runClarifications(run).length) {
                                this.refreshClarificationState(runs);
                                this.statusText = "A Planner visszakérdezett.";
                                return "clarification";
                            }
                            this.statusText = "A modell elkészült.";
                            return "done";
                        }
                        if (state === "failed") {
                            this.errors = run.error ? [run.error] : ["A generálás nem sikerült."];
                            this.statusText = "A generálás nem sikerült.";
                            return "failed";
                        }
                    }
                    this.statusText = "Időtúllépés a generálás közben.";
                    return "timeout";
                } finally {
                    this.polling = false;
                }
            },

            /**
             * Poll a version's processing status until terminal.
             * Returns "done" | "failed" | "timeout" (callers that only care
             * about the side effects may ignore the return value).
             */
            async pollVersion(versionId) {
                this.polling = true;
                const deadline = Date.now() + POLL_TIMEOUT_MS;
                try {
                    while (Date.now() < deadline) {
                        let status = null;
                        try {
                            status = await PF.request(PF.endpoints.versionStatus(versionId));
                        } catch (error) {
                            this.statusText = `Állapot lekérdezése sikertelen: ${error.message}`;
                        }
                        if (status) {
                            const state = String(status.status || "").toLowerCase();
                            this.errors = Array.isArray(status.errors) ? status.errors : [];
                            this.statusText = [state || "ismeretlen", status.stage]
                                .filter(Boolean)
                                .join(" · ");
                            const kind = PF.statusKind(state);
                            if (kind === "done") {
                                // The poller cannot see the findings: the status
                                // endpoint answers `{status, stage, errors}` only
                                // (backend `designs/services.py::version_status`),
                                // so the warnings this very render just wrote are
                                // not on this response. Re-read the version list,
                                // which carries the whole `validation_json`
                                // through `ModelVersionSerializer`, so the panel
                                // shows the finished render instead of the empty
                                // findings the row had while the job was queued.
                                await this.loadVersions();
                                return "done";
                            }
                            if (kind === "failed") {
                                this.error = "A generálás sikertelen.";
                                return "failed";
                            }
                        }
                        await sleep(POLL_INTERVAL_MS);
                    }
                    this.error = "Időtúllépés: a generálás állapota nem vált készre.";
                    return "timeout";
                } finally {
                    this.polling = false;
                }
            },

            // -------------------------------------------------------------
            // Visual-prompt annotations (docs/visual-editing.md 3.6)
            // -------------------------------------------------------------
            /**
             * The annotatable (rendered STL) viewer instance.
             *
             * The panel also hosts the read-only source-mesh canvas, so the
             * selector must exclude it: `querySelector` returns the first match
             * in document order and every canvas carries `x-stl-viewer`.
             */
            viewerInstance() {
                if (!window.PrintForgeViewer || !this.$el) return null;
                const canvas = this.$el.querySelector(
                    ".viewer-canvas:not(.viewer-canvas-source)"
                );
                return canvas ? window.PrintForgeViewer.get(canvas) : null;
            },

            toggleAnnotationMode() {
                if (!this.selectedVersionId) return;
                this.annotationMode = !this.annotationMode;
                if (!this.annotationMode) this.activeAnnotationId = null;
            },

            withInstruction(annotation) {
                return Object.assign({}, annotation, {
                    instruction: annotation.instruction || "",
                });
            },

            handleAnnotationAdded(event) {
                const annotation = event.detail && event.detail.annotation;
                if (!annotation) return;
                if (!this.annotations.some((item) => item.id === annotation.id)) {
                    this.annotations = this.annotations.concat([this.withInstruction(annotation)]);
                }
                this.activeAnnotationId = annotation.id;
            },

            handleAnnotationUpdated(event) {
                const annotation = event.detail && event.detail.annotation;
                if (!annotation) return;
                const index = this.annotations.findIndex((item) => item.id === annotation.id);
                const merged = this.withInstruction(annotation);
                if (index === -1) {
                    this.annotations = this.annotations.concat([merged]);
                } else {
                    // The viewer never sees the locally typed instruction.
                    merged.instruction = this.annotations[index].instruction || merged.instruction;
                    this.annotations.splice(index, 1, merged);
                }
                this.activeAnnotationId = annotation.id;
            },

            handleAnnotationRemoved(event) {
                const annotation = event.detail && event.detail.annotation;
                if (!annotation) return;
                this.annotations = this.annotations.filter((item) => item.id !== annotation.id);
                if (this.activeAnnotationId === annotation.id) this.activeAnnotationId = null;
            },

            handleSelectionChanged(event) {
                const selection = event.detail && event.detail.selection;
                this.activeAnnotationId = selection ? selection.id : null;
            },

            addAnnotationInstruction(annotation, value) {
                if (!annotation) return;
                annotation.instruction = value || "";
            },

            annotationKindLabel(annotation) {
                if (!annotation) return "";
                if (annotation.kind === "region") {
                    const count = (annotation.faces || []).length;
                    return `régió · ${count} háromszög`;
                }
                return "pont";
            },

            annotationPointLabel(annotation) {
                if (!annotation || !Array.isArray(annotation.point)) return "";
                const coords = annotation.point
                    .map((value) => Number(value).toFixed(1))
                    .join(", ");
                return `${coords} mm`;
            },

            removeAnnotation(annotation) {
                if (!annotation) return;
                const viewer = this.viewerInstance();
                if (viewer && typeof viewer.removeAnnotation === "function") {
                    viewer.removeAnnotation(annotation.id);
                }
                // Idempotent with the viewer's `viewer-annotation-removed` event.
                this.annotations = this.annotations.filter((item) => item.id !== annotation.id);
                if (this.activeAnnotationId === annotation.id) this.activeAnnotationId = null;
            },

            clearAnnotations() {
                const viewer = this.viewerInstance();
                if (viewer && typeof viewer.clearAnnotations === "function") {
                    viewer.clearAnnotations();
                }
                this.annotations = [];
                this.activeAnnotationId = null;
            },

            /** Overall edit prompt: explicit text, else the per-annotation ones. */
            annotationPromptText() {
                const overall = (this.annotationPrompt || "").trim();
                if (overall) return overall.slice(0, 2000);
                const instructions = this.annotations
                    .map((annotation) => (annotation.instruction || "").trim())
                    .filter(Boolean);
                const text = instructions.length
                    ? instructions.join("; ")
                    : "Annotáció-alapú szerkesztés.";
                return text.slice(0, 2000);
            },

            async submitAnnotations() {
                if (this.sendingAnnotations || !this.annotations.length) return;
                const baseVersionId = this.selectedVersionId;
                if (!baseVersionId) {
                    this.error = "Nincs kiválasztott verzió az annotációkhoz.";
                    return;
                }
                this.sendingAnnotations = true;
                this.error = "";
                this.notice = "";
                this.statusText = "Annotációk küldése…";
                try {
                    const response = await PF.request(ext.endpoints.annotationEdit(baseVersionId), {
                        method: "POST",
                        body: {
                            prompt: this.annotationPromptText(),
                            annotations: this.annotations.map(annotationPayload),
                        },
                    });
                    const parentId = response
                        && response.parent_version !== undefined
                        && response.parent_version !== null
                        ? response.parent_version
                        : baseVersionId;

                    this.statusText = "Az AI feldolgozza a jelöléseket…";
                    const created = await this.pollDerivedVersion(
                        parentId,
                        "Az AI dolgozik a szerkesztésen…"
                    );
                    if (!created) return;

                    this.annotations = [];
                    this.activeAnnotationId = null;
                    this.annotationPrompt = "";
                    this.annotationMode = false;
                    await this.loadVersions();
                    this.selectedVersionId = created.id;
                    this.viewerToken += 1;
                    // Surface the prompts this edit was built from, read-only.
                    this.showStoredAnnotations(created);
                    this.notice = `Az új verzió elkészült (v${created.version}).`;
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.polling = false;
                    this.sendingAnnotations = false;
                }
            },

            /**
             * Wait for a derived version (`parent_version == base`), shared by
             * the annotation edit and the history regenerate/edit flows. Only a
             * genuinely newer version is ever returned, so a failed run cannot
             * silently reuse the previous model.
             */
            async pollDerivedVersion(parentId, pendingMessage) {
                this.polling = true;
                const deadline = Date.now() + POLL_TIMEOUT_MS;
                try {
                    while (Date.now() < deadline) {
                        await sleep(POLL_INTERVAL_MS);
                        let versions = [];
                        try {
                            versions = await PF.api.listVersions(this.projectId);
                        } catch (error) {
                            this.statusText = `Verziólista lekérdezése sikertelen: ${error.message}`;
                            continue;
                        }
                        const derived = versions.filter(
                            (version) => Number(version.parent_version) === Number(parentId)
                        );
                        if (!derived.length) {
                            this.statusText = pendingMessage || "Az AI dolgozik…";
                            continue;
                        }
                        const created = derived.reduce((a, b) => (a.id > b.id ? a : b));
                        this.versions = versions;
                        const status = await this.pollVersion(created.id);
                        if (status !== "done") {
                            // A failed/timed-out derived version must not be
                            // selected: keep the previous model visible and make
                            // the failure explicit.
                            if (!this.error) {
                                this.error = "Az annotáció-alapú szerkesztés nem sikerült, a korábbi modell maradt kiválasztva.";
                            }
                            return null;
                        }
                        return created;
                    }
                    this.statusText = "Időtúllépés a szerkesztés közben.";
                    if (!this.error) {
                        this.error = "Időtúllépés: az annotáció-alapú szerkesztés nem fejeződött be, a korábbi modell maradt kiválasztva.";
                    }
                    return null;
                } finally {
                    this.polling = false;
                }
            },

            // -------------------------------------------------------------
            // Version history controls (docs/version-history-controls.md 4.)
            // -------------------------------------------------------------

            /** Explicit `origin`, or an inference for older serializer rows. */
            originLabel(version) {
                if (!version) return "";
                const origin = version.origin;
                if (origin && this.originLabels[origin]) return this.originLabels[origin];
                const annotations = Array.isArray(version.annotations_json)
                    ? version.annotations_json
                    : [];
                if (annotations.length) return this.originLabels.annotation;
                if (version.parent_version) return this.originLabels.regenerate;
                return this.originLabels.generate;
            },

            /** e.g. `v3 ← v2 (regenerate)`; plain `v1` for a root version. */
            chainLabel(version) {
                if (!version) return "";
                const base = `v${version.version}`;
                const parentId = version.parent_version;
                if (!parentId) return base;
                const parent = this.versions.find(
                    (item) => Number(item.id) === Number(parentId)
                );
                const parentTag = parent ? ` ← v${parent.version}` : ` ← #${parentId}`;
                return `${base}${parentTag} (${this.originLabel(version)})`;
            },

            originBadgeClass(version) {
                if (!version) return "";
                const origin = version.origin || "";
                if (origin === "regenerate") return "is-active";
                if (origin === "annotation") return "is-ok";
                if (origin === "manual") return "is-danger";
                return "";
            },

            /** `Újragenerálás`: same prompt, derived version, then poll. */
            async regenerateVersion(version) {
                if (!version || this.regenerateBusyId) return;
                this.regenerateBusyId = version.id;
                this.error = "";
                this.notice = "";
                this.errors = [];
                this.statusText = `v${version.version} újragenerálása…`;
                try {
                    // Empty body: reuse the base version's own prompt.
                    await PF.request(ext.endpoints.regenerate(version.id), { method: "POST" });
                    const created = await this.pollDerivedVersion(
                        version.id,
                        `v${version.version} újragenerálása…`
                    );
                    if (!created) return;
                    await this.loadVersions();
                    this.selectedVersionId = created.id;
                    this.viewerToken += 1;
                    this.showStoredAnnotations(created);
                    this.notice = `Az új verzió elkészült (${this.chainLabel(created)}).`;
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.regenerateBusyId = null;
                }
            },

            /**
             * Open the per-version prompt editor. `options.focus` additionally
             * puts the caret in the textarea: the render findings offer
             * "javítsd meg" for a version the user is not necessarily looking
             * at in the history list.
             */
            startEditVersion(version, options) {
                if (!version) return;
                this.editingVersionId = version.id;
                this.editPrompt = version.prompt || "";
                this.editError = "";
                this.notice = "";
                if (!options || !options.focus) return;
                // The edit form lives in an `x-if`, so the textarea does not
                // exist yet when this runs. Poll for it briefly instead of
                // guessing a single tick.
                //
                // `document.getElementById`, not `this.$el.querySelector`: inside
                // a method reached from a template `@click`, Alpine's `$el` is
                // the *bound* element (the button), not the component root, so a
                // scoped query would search the wrong subtree. The id is derived
                // from the version id and is unique on the page.
                const fieldId = "version-prompt-" + String(version.id);
                let attempts = 20;
                const focus = () => {
                    if (String(this.editingVersionId) !== String(version.id)) return;
                    const field = document.getElementById(fieldId);
                    if (!field) {
                        if (attempts <= 0) return;
                        attempts -= 1;
                        window.setTimeout(focus, 50);
                        return;
                    }
                    field.scrollIntoView({ block: "center" });
                    field.focus();
                };
                window.setTimeout(focus, 0);
            },

            cancelEditVersion() {
                this.editingVersionId = null;
                this.editPrompt = "";
                this.editError = "";
            },

            /** `Szerkesztés`: prompt edit then derived regenerate, then poll. */
            async saveEditVersion(version) {
                if (!version || this.regenerateBusyId) return;
                const prompt = this.editPrompt.trim();
                if (!prompt) {
                    this.editError = "A prompt nem lehet üres.";
                    return;
                }
                this.regenerateBusyId = version.id;
                this.error = "";
                this.notice = "";
                this.editError = "";
                this.statusText = `v${version.version} szerkesztett promptjának generálása…`;
                try {
                    await PF.request(ext.endpoints.regenerate(version.id), {
                        method: "POST",
                        body: { prompt },
                    });
                    const created = await this.pollDerivedVersion(
                        version.id,
                        `v${version.version} szerkesztett promptjának generálása…`
                    );
                    if (!created) return;
                    this.editingVersionId = null;
                    this.editPrompt = "";
                    await this.loadVersions();
                    this.selectedVersionId = created.id;
                    this.viewerToken += 1;
                    this.showStoredAnnotations(created);
                    this.notice = `Az új verzió elkészült (${this.chainLabel(created)}).`;
                } catch (error) {
                    this.editError = error.message || String(error);
                } finally {
                    this.regenerateBusyId = null;
                }
            },

            /** "Használt skillek: Süti kinyomó (auto)" (docs/skills.md 7.). */
            skillUsageLabel(skill) {
                if (!skill) return "";
                const name = skill.name || skill.slug || `#${skill.id}`;
                return skill.selection ? `${name} (${skill.selection})` : name;
            },

            handleViewerState(event) {
                const detail = event.detail || {};
                // The listener sits on `document`, so the read-only source-mesh
                // canvas reports through here too. It has its own state
                // (`handleSourceMeshState`); letting it through would overwrite
                // the main viewer's state and dimension readout.
                if (detail.readOnly) return;
                this.viewerState = detail.state || "empty";
                this.viewerMessage = detail.message || "";
                this.dimensions = detail.dimensions
                    ? `${detail.dimensions.x} × ${detail.dimensions.y} × ${detail.dimensions.z} mm`
                    : "";
            },
        };
        // Skill picker (docs/skills.md 6.). `Object.assign` copies only the
        // mixin's plain properties, so the accessors above stay live.
        if (ext.skillPickerState) Object.assign(component, ext.skillPickerState());
        return component;
    }

    document.addEventListener("alpine:init", () => {
        const Alpine = window.Alpine;
        if (!Alpine) return;
        Alpine.data("projectWorkspace", projectWorkspaceComponent);
    });
})();
