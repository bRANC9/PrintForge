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
        return {
            projectId: "",
            project: null,
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
                    await this.loadPublicRating();
                    await this.loadVersions();
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
                try {
                    await navigator.clipboard.writeText(share.token);
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
                    const baselineRunId = await this.latestAgentRunId();
                    const formData = new FormData();
                    formData.append("prompt", prompt);
                    if (this.referenceNote.trim()) {
                        formData.append("reference_note", this.referenceNote.trim());
                    }
                    if (this.referenceImage) {
                        formData.append("reference_image", this.referenceImage);
                    }
                    const created = await ext.requestForm(
                        PF.endpoints.versions(this.projectId),
                        formData
                    );
                    this.prompt = "";
                    this.referenceNote = "";
                    this.clearReference();

                    if (created && created.mode === "agent") {
                        // The AI workflow builds the specification and creates the
                        // version itself; wait for the run, then load the result.
                        await this.pollAgentRun(baselineRunId);
                        await this.loadVersions();
                        if (this.versions.length) {
                            this.selectedVersionId = this.versions[0].id;
                            this.viewerToken += 1;
                        }
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

            /** Poll the AI run started by this submit until it is terminal. */
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
                            this.statusText = "A modell elkészült.";
                            return;
                        }
                        if (state === "failed") {
                            this.errors = run.error ? [run.error] : ["A generálás nem sikerült."];
                            this.statusText = "A generálás nem sikerült.";
                            return;
                        }
                    }
                    this.statusText = "Időtúllépés a generálás közben.";
                } finally {
                    this.polling = false;
                }
            },

            async pollVersion(versionId) {
                this.polling = true;
                const deadline = Date.now() + POLL_TIMEOUT_MS;
                let finished = false;
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
                                finished = true;
                                break;
                            }
                            if (kind === "failed") {
                                this.error = "A generálás sikertelen.";
                                finished = true;
                                break;
                            }
                        }
                        await sleep(POLL_INTERVAL_MS);
                    }
                    if (!finished) {
                        this.error = "Időtúllépés: a generálás állapota nem vált készre.";
                    }
                    return finished;
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
                    const created = await this.pollAnnotationVersion(parentId);
                    if (!created) return;

                    this.annotations = [];
                    this.activeAnnotationId = null;
                    this.annotationPrompt = "";
                    this.annotationMode = false;
                    await this.loadVersions();
                    this.selectedVersionId = created.id;
                    this.viewerToken += 1;
                    this.notice = `Az új verzió elkészült (v${created.version}).`;
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.polling = false;
                    this.sendingAnnotations = false;
                }
            },

            /** Wait for the derived version (`parent_version == base`). */
            async pollAnnotationVersion(parentId) {
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
                            this.statusText = "Az AI dolgozik a szerkesztésen…";
                            continue;
                        }
                        const created = derived.reduce((a, b) => (a.id > b.id ? a : b));
                        this.versions = versions;
                        const ready = await this.pollVersion(created.id);
                        return ready ? created : null;
                    }
                    this.statusText = "Időtúllépés a szerkesztés közben.";
                    return null;
                } finally {
                    this.polling = false;
                }
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
    }

    document.addEventListener("alpine:init", () => {
        const Alpine = window.Alpine;
        if (!Alpine) return;
        Alpine.data("projectWorkspace", projectWorkspaceComponent);
    });
})();
