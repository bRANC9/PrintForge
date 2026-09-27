// PrintForge own-profile page (/profile/).
//
//   GET   /api/v1/me/            -> account fields + aggregated statistics
//   PATCH /api/v1/me/            -> editable profile fields
//   POST  /api/v1/me/password/   -> password change (Django PasswordChangeForm)
//
// The page is a thin shell: everything below is loaded from the API, so the
// server-rendered HTML carries no user data of its own.
(function () {
    "use strict";

    const PF = window.PrintForge;

    /** Stat tiles, grouped; `key` is the path into `stats`. */
    const GROUPS = [
        {
            title: "Munkaterületek",
            tiles: [
                { key: "workspaces.owned", label: "Saját workspace", kind: "accent" },
                { key: "workspaces.joined", label: "Tag vagyok" },
            ],
        },
        {
            title: "Tartalom",
            tiles: [
                { key: "content.projects", label: "Projekt" },
                { key: "content.versions", label: "Verzió" },
                { key: "content.skills", label: "Skill" },
                { key: "content.public_projects", label: "Nyilvános projekt" },
                { key: "content.shared_with_me", label: "Rám osztott projekt" },
            ],
        },
        {
            title: "Nyomtatás",
            tiles: [
                { key: "printing.jobs", label: "Nyomtatási feladat" },
                { key: "printing.completed", label: "Kész", kind: "ok" },
                { key: "printing.active", label: "Fut / sorban", kind: "accent" },
                { key: "printing.failed", label: "Sikertelen", kind: "danger" },
            ],
        },
        {
            title: "Hatás",
            tiles: [
                { key: "reach.downloads", label: "Letöltés" },
                { key: "reach.prints", label: "Kinyomtatva" },
                {
                    key: "reach.rating_average",
                    label: "Értékelés (átlag)",
                    format: "rating",
                },
                { key: "reach.rating_count", label: "Értékelés darab" },
            ],
        },
    ];

    /** Read a dotted path out of a plain object. */
    function dig(source, path) {
        return path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), source);
    }

    function profilePageComponent() {
        return {
            loading: true,
            savingProfile: false,
            savingPassword: false,
            error: "",
            notice: "",
            profileError: {},
            passwordError: {},

            profile: null,
            stats: null,
            groups: GROUPS,

            form: { display_name: "", email: "", first_name: "", last_name: "" },
            password: { old_password: "", new_password1: "", new_password2: "" },

            formatDate: PF.formatDate,

            /** "2026-09" -> "szept." for the bar axis label. */
            monthLabel(value) {
                const date = this.parseMonth(value);
                if (!date) return "";
                return date.toLocaleDateString("hu-HU", { month: "short" });
            },

            /** "2026-09" -> "2026. szept." for the tooltip (the axis has no
                room for the year, so it lives here instead). */
            monthLabelLong(value) {
                const date = this.parseMonth(value);
                if (!date) return "";
                return date.toLocaleDateString("hu-HU", { year: "numeric", month: "short" });
            },

            parseMonth(value) {
                if (!value) return null;
                const date = new Date(`${value}-01T00:00:00`);
                return Number.isNaN(date.getTime()) ? null : date;
            },

            get displayName() {
                if (!this.profile) return "";
                return this.profile.display_name || this.profile.username;
            },

            get activity() {
                const series = (this.stats && this.stats.activity) || [];
                return series.map((row) => ({
                    month: row.month,
                    label: this.monthLabel(row.month),
                    fullLabel: this.monthLabelLong(row.month),
                    print_jobs: row.print_jobs || 0,
                    downloads: row.downloads || 0,
                    total: (row.print_jobs || 0) + (row.downloads || 0),
                }));
            },

            /** Tallest bar in the series, so the bars scale against real data. */
            get activityPeak() {
                return this.activity.reduce((max, row) => Math.max(max, row.total), 0);
            },

            /** Bar height as a percentage of the series peak (0 when flat). */
            barHeight(value) {
                const peak = this.activityPeak;
                if (!peak) return 0;
                return Math.max(2, Math.round((Number(value || 0) / peak) * 100));
            },

            /**
             * One tile's rendered text. A dash (not a zero) marks "nothing
             * recorded", which is a different fact from "counted as zero".
             */
            tileValue(group) {
                const raw = dig(this.stats || {}, group.key);
                if (group.format === "rating") {
                    return raw === null || raw === undefined ? "—" : Number(raw).toFixed(1);
                }
                if (raw === null || raw === undefined) return "—";
                return new Intl.NumberFormat("hu-HU").format(Number(raw));
            },

            async init() {
                await this.load();
            },

            async load() {
                this.loading = true;
                this.error = "";
                try {
                    this.apply(await PF.api.getProfile());
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                }
            },

            /** Adopt a `GET`/`PATCH` payload: profile, stats and form in one go. */
            apply(payload) {
                if (!payload) return;
                this.profile = payload;
                this.stats = payload.stats || null;
                this.form = {
                    display_name: payload.display_name || "",
                    email: payload.email || "",
                    first_name: payload.first_name || "",
                    last_name: payload.last_name || "",
                };
            },

            /** Flatten a DRF field-error object into "field: message" lines. */
            fieldErrors(errors) {
                if (!errors || typeof errors !== "object") return [];
                if (typeof errors.detail === "string") return [errors.detail];
                return Object.keys(errors).map((field) => {
                    const value = errors[field];
                    const message = Array.isArray(value) ? value.join(" ") : String(value);
                    const label = {
                        old_password: "Jelenlegi jelszó",
                        new_password1: "Új jelszó",
                        new_password2: "Új jelszó megerősítése",
                        display_name: "Megjelenített név",
                        email: "E-mail",
                        first_name: "Keresztnév",
                        last_name: "Vezetéknév",
                    };
                    return `${label[field] || field}: ${message}`;
                });
            },

            async saveProfile() {
                if (this.savingProfile) return;
                this.savingProfile = true;
                this.error = "";
                this.notice = "";
                this.profileError = {};
                try {
                    this.apply(await PF.api.updateProfile({ ...this.form }));
                    this.notice = "A profil mentve.";
                } catch (error) {
                    this.profileError = error;
                    this.error = this.fieldErrors(error).join(" ") || error.message || String(error);
                } finally {
                    this.savingProfile = false;
                }
            },

            async savePassword() {
                if (this.savingPassword) return;
                this.savingPassword = true;
                this.error = "";
                this.notice = "";
                this.passwordError = {};
                try {
                    await PF.api.changePassword({ ...this.password });
                    this.password = { old_password: "", new_password1: "", new_password2: "" };
                    this.notice = "A jelszó frissítve.";
                } catch (error) {
                    this.passwordError = error;
                    this.error =
                        this.fieldErrors(error).join(" ") || error.message || String(error);
                } finally {
                    this.savingPassword = false;
                }
            },
        };
    }

    document.addEventListener("alpine:init", () => {
        const Alpine = window.Alpine;
        if (!Alpine) return;
        Alpine.data("profilePage", profilePageComponent);
    });
})();
