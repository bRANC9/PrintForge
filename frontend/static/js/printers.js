// PrintForge printer registry + live status (terv.md 13. fejezet).
//
//   GET /api/v1/printers/                 (registry, paginated)
//   GET /api/v1/printers/{id}/status/     (live snapshot + CFS slots)
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

    function printerStatusComponent() {
        return {
            printers: [],
            statuses: {},
            loading: true,
            refreshing: false,
            error: "",
            autoRefresh: false,
            timer: null,

            formatDate: PF.formatDate,
            stateLabel,
            stateKind,

            init() {
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
        };
    }

    document.addEventListener("alpine:init", () => {
        const Alpine = window.Alpine;
        if (!Alpine) return;
        Alpine.data("printerStatus", printerStatusComponent);
    });
})();
