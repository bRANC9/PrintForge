// PrintForge printer registry + live status (terv.md 13. fejezet).
//
//   GET    /api/v1/printers/                 (registry, paginated)
//   GET    /api/v1/printers/{id}/status/     (live snapshot + CFS slots)
//   POST   /api/v1/printers/                 (staff only)
//   PATCH  /api/v1/printers/{id}/            (staff only)
//   DELETE /api/v1/printers/{id}/            (staff only -> deactivate)
//
// A status request that fails (unreachable/locked printer) comes back as
// `online: false` with an `error` string, so the card can show it inline.
(function () {
    "use strict";

    const PF = window.PrintForge;

    const STATE_LABELS = {
        idle: "Készenlét",
        printing: "Nyomtatás",
        paused: "Szüneteltetve",
        offline: "Offline",
        error: "Hiba",
        unknown: "Ismeretlen",
    };

    function stateLabel(state) {
        return STATE_LABELS[String(state || "").toLowerCase()] || STATE_LABELS.unknown;
    }

    function stateKind(state) {
        const key = String(state || "").toLowerCase();
        return Object.prototype.hasOwnProperty.call(STATE_LABELS, key) ? key : "unknown";
    }

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

    function emptyForm() {
        return {
            id: null,
            name: "",
            backend: "",
            host: "",
            api_key: "",
            is_active: true,
        };
    }

    function printerStatusComponent() {
        return {
            printers: [],
            statuses: {},
            loading: true,
            refreshing: false,
            error: "",
            notice: "",
            autoRefresh: false,
            timer: null,
            isStaff: false,
            backends: [],
            form: emptyForm(),
            formOpen: false,
            saving: false,
            busyPrinterId: null,

            formatDate: PF.formatDate,
            stateLabel,
            stateKind,

            init() {
                this.isStaff = Boolean(this.$el && this.$el.dataset && this.$el.dataset.isStaff === "true");
                this.backends = readJSON("printer-backends-data", []);
                this.load();
                this.$watch("autoRefresh", () => this.schedule());
            },

            schedule() {
                window.clearInterval(this.timer);
                if (this.autoRefresh) {
                    this.timer = window.setInterval(() => this.refreshStatuses(), 5000);
                }
            },

            async load() {
                this.loading = true;
                this.error = "";
                try {
                    this.printers = await PF.api.listPrinters();
                    await this.refreshStatuses();
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                }
            },

            async refreshStatuses() {
                this.refreshing = true;
                try {
                    const rows = await Promise.all(
                        this.printers.map((printer) =>
                            PF.api
                                .printerStatus(printer.id)
                                .then((status) => [printer.id, status])
                                .catch((error) => [
                                    printer.id,
                                    {
                                        online: false,
                                        state: "offline",
                                        error: error.message || String(error),
                                        cfs_slots: [],
                                    },
                                ])
                        )
                    );
                    const map = {};
                    rows.forEach(([id, status]) => {
                        map[id] = status;
                    });
                    this.statuses = map;
                } finally {
                    this.refreshing = false;
                }
            },

            statusOf(printer) {
                return this.statuses[printer.id] || null;
            },

            progressPct(printer) {
                const status = this.statusOf(printer);
                if (!status || status.progress === null || status.progress === undefined) return null;
                return Math.round(status.progress * 100);
            },

            slotLabel(slot) {
                if (!slot || slot.empty) return "Üres";
                const parts = [slot.material, slot.name].filter(Boolean);
                return parts.length ? parts.join(" · ") : "Ismeretlen";
            },

            // -- staff management ------------------------------------------

            startCreate() {
                this.form = emptyForm();
                this.formOpen = true;
                this.error = "";
                this.notice = "";
            },

            startEdit(printer) {
                this.form = {
                    id: printer.id,
                    name: printer.name || "",
                    backend: printer.backend || "",
                    host: printer.host || "",
                    api_key: "",
                    is_active: Boolean(printer.is_active),
                };
                this.formOpen = true;
                this.error = "";
                this.notice = "";
            },

            cancelForm() {
                this.form = emptyForm();
                this.formOpen = false;
            },

            async savePrinter() {
                if (this.saving || !this.form.name.trim()) return;
                this.saving = true;
                this.error = "";
                this.notice = "";
                const body = {
                    name: this.form.name.trim(),
                    backend: this.form.backend,
                    host: this.form.host.trim(),
                    is_active: Boolean(this.form.is_active),
                };
                // An empty key means "leave unchanged" (PATCH) / "no key" (POST).
                if (this.form.api_key) body.api_key = this.form.api_key;
                try {
                    if (this.form.id) {
                        await PF.api.updatePrinter(this.form.id, body);
                        this.notice = "Nyomtató frissítve.";
                    } else {
                        await PF.api.createPrinter(body);
                        this.notice = "Nyomtató létrehozva.";
                    }
                    this.cancelForm();
                    await this.load();
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.saving = false;
                }
            },

            async deactivate(printer) {
                if (this.busyPrinterId) return;
                this.busyPrinterId = printer.id;
                this.error = "";
                this.notice = "";
                try {
                    await PF.api.deactivatePrinter(printer.id);
                    this.notice = `${printer.name} deaktiválva.`;
                    await this.load();
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.busyPrinterId = null;
                }
            },

            async reactivate(printer) {
                if (this.busyPrinterId) return;
                this.busyPrinterId = printer.id;
                this.error = "";
                this.notice = "";
                try {
                    await PF.api.updatePrinter(printer.id, { is_active: true });
                    this.notice = `${printer.name} aktiválva.`;
                    await this.load();
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.busyPrinterId = null;
                }
            },
        };
    }

    document.addEventListener("alpine:init", () => {
        const Alpine = window.Alpine;
        if (!Alpine) return;
        Alpine.data("printerStatus", printerStatusComponent);
    });
})();
