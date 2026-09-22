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
//
// Reuses the vendored Three.js viewer through `x-stl-viewer` (app.js) with the
// same `stlUrl` / `viewerToken` contract as the original component.
(function () {
    "use strict";

    const ext = window.PrintForgeExt;
    const PF = window.PrintForge;
    const POLL_INTERVAL_MS = 2000;
    const POLL_TIMEOUT_MS = 10 * 60 * 1000;

    function sleep(ms) {
        return new Promise((resolve) => window.setTimeout(resolve, ms));
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
            creatingVersion: false,
            polling: false,
            statusText: "",
            errors: [],

            viewerState: "empty",
            viewerMessage: "",
            dimensions: "",
            viewerToken: 0,

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

            sourceLabel(source) {
                return this.sourceLabels[source] || source || "üres";
            },

            init() {
                this.projectId = (this.$el && this.$el.dataset
                    ? this.$el.dataset.projectId
                    : "") || idFromLocation();
                if (!this.projectId) {
                    this.error = "Hiányzó projekt azonosító az URL-ben.";
                    this.loading = false;
                    return;
                }
                this.reload();
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
                        this.notice = `Letöltés naplózva (összesen ${data.download_count}).`;
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
