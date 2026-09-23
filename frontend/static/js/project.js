// PrintForge project detail enhancements (Phase 8 / #27 / #29).
//
//   GET/PATCH /api/v1/projects/{id}/                       (metadata + license + tags)
//   GET/POST  /api/v1/projects/{id}/versions/              (POST is multipart, #27)
//   GET       /api/v1/versions/{id}/status/
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
// Reuses the vendored Three.js viewer through `x-stl-viewer` (app.js) with the
// same `stlUrl` / `viewerToken` contract as the original component, plus the
// `annotationMode` flag (docs/visual-editing.md 3.6).
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
                return this.creatingVersion || this.polling;
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
                }
                this.selectedVersionId = version.id;
                this.showStoredAnnotations(version);
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
            viewerInstance() {
                if (!window.PrintForgeViewer || !this.$el) return null;
                const canvas = this.$el.querySelector("[x-stl-viewer], .viewer-canvas");
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

            startEditVersion(version) {
                if (!version) return;
                this.editingVersionId = version.id;
                this.editPrompt = version.prompt || "";
                this.editError = "";
                this.notice = "";
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
