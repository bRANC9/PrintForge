// PrintForge frontend bootstrap.
//
// Every JSON API URL is defined once in `endpoints` below so that a change on
// the backend (api-dev) only has to be applied in this single place.
(function () {
    "use strict";

    // ------------------------------------------------------------------
    // API configuration (single source of truth for fetch URLs)
    // ------------------------------------------------------------------
    const API_BASE = "/api/v1";

    const endpoints = {
        health: () => `${API_BASE}/health/`,
        workspaces: () => `${API_BASE}/workspaces/`,
        projects: () => `${API_BASE}/projects/`,
        project: (projectId) => `${API_BASE}/projects/${encodeURIComponent(projectId)}/`,
        versions: (projectId) => `${API_BASE}/projects/${encodeURIComponent(projectId)}/versions/`,
        versionStatus: (versionId) => `${API_BASE}/versions/${encodeURIComponent(versionId)}/status/`,
        artifact: (versionId, kind) =>
            `${API_BASE}/versions/${encodeURIComponent(versionId)}/artifact/${encodeURIComponent(kind)}/`,
    };

    const PROJECT_DETAIL_PATTERN = /\/projects\/(\d+)(?:\/|$)/;
    const POLL_INTERVAL_MS = 2000;
    const POLL_TIMEOUT_MS = 10 * 60 * 1000;

    const PENDING_STATUSES = [
        "",
        "pending",
        "queued",
        "queued_for_generation",
        "running",
        "processing",
        "in_progress",
        "generating",
        "started",
        "retrying",
    ];
    const DONE_STATUSES = ["done", "completed", "complete", "success", "succeeded", "ready"];
    const FAILED_STATUSES = ["failed", "error", "errored", "cancelled", "canceled", "timeout", "rejected"];

    // ------------------------------------------------------------------
    // Small helpers
    // ------------------------------------------------------------------
    function getCookie(name) {
        const needle = `${name}=`;
        const parts = document.cookie ? document.cookie.split(";") : [];
        for (const raw of parts) {
            const value = raw.trim();
            if (value.startsWith(needle)) {
                return decodeURIComponent(value.slice(needle.length));
            }
        }
        return "";
    }

    function unwrapList(payload) {
        if (Array.isArray(payload)) return payload;
        if (payload && Array.isArray(payload.results)) return payload.results;
        return [];
    }

    function formatDate(value) {
        if (!value) return "";
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return date.toLocaleString("hu-HU", {
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
        });
    }

    function sleep(ms) {
        return new Promise((resolve) => window.setTimeout(resolve, ms));
    }

    function projectIdFromLocation() {
        const match = window.location.pathname.match(PROJECT_DETAIL_PATTERN);
        return match ? match[1] : "";
    }

    async function describeError(response) {
        let detail = `${response.status} ${response.statusText}`.trim();
        try {
            const payload = await response.json();
            if (payload && typeof payload === "object") {
                if (typeof payload.detail === "string") {
                    detail = payload.detail;
                } else {
                    const entries = Object.keys(payload).map(
                        (key) => `${key}: ${[].concat(payload[key]).join(" ")}`
                    );
                    if (entries.length) detail = entries.join("; ");
                }
            }
        } catch (error) {
            // Response body was not JSON - keep the status line.
        }
        return detail;
    }

    async function request(url, options = {}) {
        const method = (options.method || "GET").toUpperCase();
        const config = {
            method,
            credentials: "same-origin",
            headers: Object.assign({ Accept: "application/json" }, options.headers || {}),
        };
        if (options.body !== undefined) {
            config.headers["Content-Type"] = "application/json";
            config.body = JSON.stringify(options.body);
        }
        if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
            const token = getCookie("csrftoken");
            if (token) config.headers["X-CSRFToken"] = token;
        }
        const response = await fetch(url, config);
        if (!response.ok) {
            throw new Error(await describeError(response));
        }
        if (response.status === 204) return null;
        const text = await response.text();
        if (!text) return null;
        try {
            return JSON.parse(text);
        } catch (error) {
            return null;
        }
    }

    const api = {
        listProjects: async () => unwrapList(await request(endpoints.projects())),
        getProject: (projectId) => request(endpoints.project(projectId)),
        listVersions: async (projectId) => unwrapList(await request(endpoints.versions(projectId))),
        createVersion: (projectId, prompt) =>
            request(endpoints.versions(projectId), { method: "POST", body: { prompt } }),
        versionStatus: (versionId) => request(endpoints.versionStatus(versionId)),
    };

    function statusKind(status) {
        const state = String(status || "").toLowerCase();
        if (DONE_STATUSES.includes(state)) return "done";
        if (FAILED_STATUSES.includes(state)) return "failed";
        if (PENDING_STATUSES.includes(state)) return "pending";
        return "unknown";
    }

    window.PrintForge = {
        API_BASE,
        endpoints,
        api,
        getCookie,
        request,
        unwrapList,
        formatDate,
        statusKind,
    };

    // ------------------------------------------------------------------
    // Alpine components
    // ------------------------------------------------------------------
    function projectListComponent() {
        return {
            projects: [],
            workspaces: [],
            search: "",
            loading: true,
            error: "",

            get visibleProjects() {
                const term = this.search.trim().toLowerCase();
                if (!term) return this.projects;
                return this.projects.filter((project) =>
                    `${project.name || ""} ${project.description || ""}`.toLowerCase().includes(term)
                );
            },

            workspaceName(workspaceId) {
                const workspace = this.workspaces.find((item) => item.id === workspaceId);
                return workspace ? workspace.name : "";
            },

            formatDate,

            async init() {
                this.loading = true;
                this.error = "";
                try {
                    const results = await Promise.all([api.listProjects(), this.loadWorkspaces()]);
                    this.projects = results[0];
                    this.workspaces = results[1];
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                }
            },

            async loadWorkspaces() {
                try {
                    return unwrapList(await request(endpoints.workspaces()));
                } catch (error) {
                    return [];
                }
            },
        };
    }

    function projectDetailComponent() {
        return {
            projectId: "",
            project: null,
            versions: [],
            selectedVersionId: null,
            prompt: "",
            loading: true,
            error: "",
            submitting: false,
            polling: false,
            stage: "",
            statusText: "",
            errors: [],
            viewerState: "empty",
            viewerMessage: "",
            dimensions: "",
            viewerToken: 0,

            get busy() {
                return this.submitting || this.polling;
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
                return this.selectedVersionId ? endpoints.artifact(this.selectedVersionId, "stl") : "";
            },

            get scadUrl() {
                return this.selectedVersionId ? endpoints.artifact(this.selectedVersionId, "scad") : "";
            },

            formatDate,

            formatError(value) {
                if (value === null || value === undefined) return "";
                return typeof value === "string" ? value : JSON.stringify(value);
            },

            async init() {
                const root = this.$el;
                const fromDom = root && root.dataset ? root.dataset.projectId || "" : "";
                this.projectId = fromDom || projectIdFromLocation();
                if (!this.projectId) {
                    this.error = "Hiányzó projekt azonosító az URL-ben.";
                    this.loading = false;
                    return;
                }
                await this.reload();
            },

            async reload() {
                this.loading = true;
                this.error = "";
                try {
                    this.project = await api.getProject(this.projectId);
                    await this.loadVersions();
                    if (!this.selectedVersionId && this.versions.length) {
                        this.selectVersion(this.versions[0]);
                    }
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                }
            },

            async loadVersions() {
                this.versions = await api.listVersions(this.projectId);
            },

            selectVersion(version) {
                if (!version) return;
                this.selectedVersionId = version.id;
            },

            async submitPrompt() {
                const prompt = this.prompt.trim();
                if (!prompt || this.busy) return;
                this.submitting = true;
                this.error = "";
                this.errors = [];
                this.stage = "";
                this.statusText = "Verzió létrehozása…";
                try {
                    const created = await api.createVersion(this.projectId, prompt);
                    this.prompt = "";
                    await this.loadVersions();
                    const createdId = created && created.id;
                    const fallbackId = this.versions.length ? this.versions[0].id : null;
                    const versionId = createdId || fallbackId;
                    if (versionId) {
                        this.selectedVersionId = versionId;
                        await this.pollVersion(versionId);
                        // The STL usually only exists once the worker is done:
                        // bump the token so the viewer retries the artifact URL.
                        this.viewerToken += 1;
                    }
                    await this.loadVersions();
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.submitting = false;
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
                            status = await api.versionStatus(versionId);
                        } catch (error) {
                            this.statusText = `Állapot lekérdezése sikertelen: ${error.message}`;
                        }
                        if (status) {
                            const state = String(status.status || "").toLowerCase();
                            this.stage = status.stage || "";
                            this.errors = Array.isArray(status.errors) ? status.errors : [];
                            const kind = statusKind(state);
                            this.statusText = [state || "ismeretlen", this.stage].filter(Boolean).join(" · ");
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

    /**
     * Alpine directive that keeps the imperative Three.js viewer in sync with
     * reactive state:  x-stl-viewer="{ stlUrl: stlUrl, token: viewerToken }"
     */
    function registerStlViewerDirective(Alpine) {
        Alpine.directive("stl-viewer", (el, { expression }, { evaluate, effect, cleanup }) => {
            let pendingConfig = null;

            const mount = () => {
                if (!window.PrintForgeViewer || !pendingConfig) return;
                window.PrintForgeViewer.mount(el, pendingConfig);
            };

            const onViewerReady = () => mount();
            window.addEventListener("printforge:viewer-ready", onViewerReady);

            effect(() => {
                pendingConfig = evaluate(expression) || {};
                mount();
            });

            cleanup(() => {
                window.removeEventListener("printforge:viewer-ready", onViewerReady);
                if (window.PrintForgeViewer) window.PrintForgeViewer.dispose(el);
            });
        });
    }

    document.addEventListener("alpine:init", () => {
        const Alpine = window.Alpine;
        if (!Alpine) return;
        Alpine.data("projectList", projectListComponent);
        Alpine.data("projectDetail", projectDetailComponent);
        registerStlViewerDirective(Alpine);
    });

    // ------------------------------------------------------------------
    // Legacy Phase 1 probe: keeps the health indicator on the home page alive.
    // ------------------------------------------------------------------
    document.addEventListener("DOMContentLoaded", async () => {
        const el = document.getElementById("api-status");
        if (!el) return;
        const url = el.dataset.healthUrl || endpoints.health();
        try {
            const response = await fetch(url, { headers: { Accept: "application/json" } });
            const data = await response.json();
            el.textContent = `API állapot: ${data.status}`;
        } catch (error) {
            el.textContent = "API állapot: nem elérhető";
            console.error(error);
        }
    });
})();
