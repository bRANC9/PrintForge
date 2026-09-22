// PrintForge build-plate editor (#28).
//
//   GET/POST /api/v1/build-plates/                  (list is workspace-scoped)
//   GET      /api/v1/build-plates/{id}/items/
//   POST     /api/v1/build-plates/{id}/items/
//   DELETE   /api/v1/build-plates/{id}/items/        body {item_id}
//   GET      /api/v1/projects/{id}/versions/
//   POST     /api/v1/print-jobs/                     {project, build_plate, printer, ...}
//
// Printers / filaments / printer profiles are injected server-side by the page
// view because the JSON API has no list endpoint for them yet.
(function () {
    "use strict";

    const ext = window.PrintForgeExt;
    const PF = window.PrintForge;

    function readJSON(elementId, fallback) {
        const el = document.getElementById(elementId);
        if (!el) return fallback;
        try {
            const parsed = JSON.parse(el.textContent);
            return parsed || fallback;
        } catch (error) {
            return fallback;
        }
    }

    function numberOr(value, fallback) {
        const parsed = Number.parseFloat(value);
        return Number.isFinite(parsed) ? parsed : fallback;
    }

    function projectPlatesComponent() {
        return {
            projectId: "",
            plates: [],
            versions: [],
            selectedPlateId: null,
            items: [],
            printers: [],
            filaments: [],
            printerProfiles: [],

            plateForm: { name: "", printer_profile: "" },
            itemForm: {
                model_version: "",
                position_x: 0,
                position_y: 0,
                position_z: 0,
                rotation_z: 0,
                scale: 1,
            },
            jobForm: { printer: "", filament: "", priority: 0 },

            loading: true,
            error: "",
            notice: "",
            creatingPlate: false,
            addingItem: false,
            enqueuing: false,
            removingItemId: null,

            formatDate: PF.formatDate,

            get selectedPlate() {
                return this.plates.find((plate) => plate.id === this.selectedPlateId) || null;
            },

            async init() {
                this.projectId = (this.$el && this.$el.dataset
                    ? this.$el.dataset.projectId
                    : "") || "";
                this.printers = readJSON("plate-printers-data", []);
                this.filaments = readJSON("plate-filaments-data", []);
                this.printerProfiles = readJSON("plate-printer-profiles-data", []);
                if (!this.projectId) {
                    this.error = "Hiányzó projekt azonosító.";
                    this.loading = false;
                    return;
                }
                await this.loadAll();
            },

            async loadAll() {
                this.loading = true;
                this.error = "";
                try {
                    this.versions = await PF.api.listVersions(this.projectId);
                    await this.loadPlates();
                    if (this.plates.length) {
                        await this.selectPlate(this.plates[0]);
                    }
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                }
            },

            async loadPlates() {
                // The API is workspace-scoped and paginated; follow the pages
                // and narrow to this project client-side.
                let url = ext.endpoints.buildPlates();
                const collected = [];
                while (url) {
                    const payload = await PF.request(url);
                    collected.push(...PF.unwrapList(payload));
                    url = ext.sameOrigin(payload && payload.next);
                }
                this.plates = collected.filter(
                    (plate) => String(plate.project) === String(this.projectId)
                );
            },

            versionLabel(modelVersionId) {
                const version = this.versions.find((item) => item.id === modelVersionId);
                if (!version) return `#${modelVersionId}`;
                const prompt = version.prompt ? ` · ${version.prompt}` : "";
                return `v${version.version}${prompt}`;
            },

            async createPlate() {
                const name = this.plateForm.name.trim();
                if (!name || this.creatingPlate) return;
                this.creatingPlate = true;
                this.error = "";
                this.notice = "";
                const body = { project: Number(this.projectId), name };
                const profileId = Number.parseInt(this.plateForm.printer_profile, 10);
                if (Number.isFinite(profileId)) body.printer_profile = profileId;
                try {
                    const created = await PF.request(ext.endpoints.buildPlates(), {
                        method: "POST",
                        body,
                    });
                    this.plateForm.name = "";
                    await this.loadPlates();
                    if (created && created.id) await this.selectPlate(created);
                    this.notice = `Nyomtatási lap létrehozva: ${name}`;
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.creatingPlate = false;
                }
            },

            async selectPlate(plate) {
                if (!plate) return;
                this.selectedPlateId = plate.id;
                this.error = "";
                try {
                    this.items = await PF.request(ext.endpoints.plateItems(plate.id));
                } catch (error) {
                    this.items = [];
                    this.error = error.message || String(error);
                }
            },

            async addItem() {
                const plate = this.selectedPlate;
                const modelVersionId = Number.parseInt(this.itemForm.model_version, 10);
                if (!plate || !Number.isFinite(modelVersionId) || this.addingItem) return;
                this.addingItem = true;
                this.error = "";
                this.notice = "";
                try {
                    await PF.request(ext.endpoints.plateItems(plate.id), {
                        method: "POST",
                        body: {
                            model_version: modelVersionId,
                            position_x: numberOr(this.itemForm.position_x, 0),
                            position_y: numberOr(this.itemForm.position_y, 0),
                            position_z: numberOr(this.itemForm.position_z, 0),
                            rotation_z: numberOr(this.itemForm.rotation_z, 0),
                            scale: numberOr(this.itemForm.scale, 1),
                        },
                    });
                    this.items = await PF.request(ext.endpoints.plateItems(plate.id));
                    await this.loadPlates();
                    this.notice = "Elem hozzáadva a laphoz.";
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.addingItem = false;
                }
            },

            async removeItem(item) {
                const plate = this.selectedPlate;
                if (!plate || !item || this.removingItemId) return;
                this.removingItemId = item.id;
                this.error = "";
                try {
                    await PF.request(ext.endpoints.plateItems(plate.id), {
                        method: "DELETE",
                        body: { item_id: item.id },
                    });
                    this.items = this.items.filter((row) => row.id !== item.id);
                    await this.loadPlates();
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.removingItemId = null;
                }
            },

            async enqueue() {
                const plate = this.selectedPlate;
                const printerId = Number.parseInt(this.jobForm.printer, 10);
                if (!plate || !Number.isFinite(printerId) || this.enqueuing) return;
                this.enqueuing = true;
                this.error = "";
                this.notice = "";
                const body = {
                    project: Number(this.projectId),
                    build_plate: plate.id,
                    printer: printerId,
                    priority: numberOr(this.jobForm.priority, 0),
                };
                const filamentId = Number.parseInt(this.jobForm.filament, 10);
                if (Number.isFinite(filamentId)) body.filament = filamentId;
                try {
                    const job = await PF.request(ext.endpoints.printJobs(), {
                        method: "POST",
                        body,
                    });
                    this.notice = `Nyomtatási feladat sorba állítva (#${job && job.id}).`;
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.enqueuing = false;
                }
            },
        };
    }

    document.addEventListener("alpine:init", () => {
        const Alpine = window.Alpine;
        if (!Alpine) return;
        Alpine.data("projectPlates", projectPlatesComponent);
    });
})();
