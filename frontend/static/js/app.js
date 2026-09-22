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
        // Phase 7 (multi-user): print history + notifications.
        printJobs: () => `${API_BASE}/print-jobs/`,
        printJobStart: (jobId) => `${API_BASE}/print-jobs/${encodeURIComponent(jobId)}/start/`,
        printJobCancel: (jobId) => `${API_BASE}/print-jobs/${encodeURIComponent(jobId)}/cancel/`,
        printJobTransition: (jobId) =>
            `${API_BASE}/print-jobs/${encodeURIComponent(jobId)}/transition/`,
        // Printer registry + live status (terv.md 13.).
        printers: () => `${API_BASE}/printers/`,
        printer: (printerId) => `${API_BASE}/printers/${encodeURIComponent(printerId)}/`,
        printerStatus: (printerId) =>
            `${API_BASE}/printers/${encodeURIComponent(printerId)}/status/`,
        notifications: () => `${API_BASE}/notifications/`,
        notificationRead: (notificationId) =>
            `${API_BASE}/notifications/${encodeURIComponent(notificationId)}/read/`,
        // Runtime settings (staff-only).
        settings: () => `${API_BASE}/settings/`,
        settingsTestOllama: () => `${API_BASE}/settings/test-ollama/`,
        // Ollama model management (staff-only).
        ollamaModels: () => `${API_BASE}/ollama/models/`,
        ollamaModelDelete: (name) => `${API_BASE}/ollama/models/?name=${encodeURIComponent(name)}`,
        ollamaModelUse: () => `${API_BASE}/ollama/models/use/`,
        ollamaModelPull: () => `${API_BASE}/ollama/models/pull/`,
        ollamaPulls: () => `${API_BASE}/ollama/pulls/`,
        ollamaPull: (pullId) => `${API_BASE}/ollama/pulls/${encodeURIComponent(pullId)}/`,
    };

    const NOTIFICATION_POLL_MS = 60000;
    const OLLAMA_POLL_MS = 2000;
    const POLL_INTERVAL_MS = 2000;
    const POLL_TIMEOUT_MS = 10 * 60 * 1000;
    const PULL_PENDING_STATUSES = ["PENDING", "RUNNING"];
    const PULL_TERMINAL_STATUSES = ["DONE", "FAILED"];

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

    /**
     * Fill a Django `{% url %}` template that was reversed with `pk=0` (a
     * `projects:detail` URL such as "/projects/0/") with a real primary key.
     * Anchored on the trailing segment so a port number or a version prefix is
     * never touched.
     */
    function fillPkTemplate(template, pk) {
        if (!template) return "#";
        return template.replace(/0(\/?)$/, `${pk}$1`);
    }

    /** Last numeric path segment - fallback when no data-project-id is set. */
    function idFromLocation() {
        const segments = window.location.pathname.split("/").filter(Boolean);
        const last = segments.length ? segments[segments.length - 1] : "";
        return /^\d+$/.test(last) ? last : "";
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
            const error = new Error(await describeError(response));
            // Keep the HTTP status so callers can react to 403/404 explicitly.
            error.status = response.status;
            throw error;
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
        createProject: (payload) => request(endpoints.projects(), { method: "POST", body: payload }),
        listWorkspaces: async () => unwrapList(await request(endpoints.workspaces())),
        createWorkspace: (name) => request(endpoints.workspaces(), { method: "POST", body: { name } }),
        listVersions: async (projectId) => unwrapList(await request(endpoints.versions(projectId))),
        createVersion: (projectId, prompt) =>
            request(endpoints.versions(projectId), { method: "POST", body: { prompt } }),
        versionStatus: (versionId) => request(endpoints.versionStatus(versionId)),
        listPrintJobs: async () => unwrapList(await request(endpoints.printJobs())),
        startPrintJob: (jobId) => request(endpoints.printJobStart(jobId), { method: "POST" }),
        cancelPrintJob: (jobId) => request(endpoints.printJobCancel(jobId), { method: "POST" }),
        transitionPrintJob: (jobId, status) =>
            request(endpoints.printJobTransition(jobId), { method: "POST", body: { status } }),
        listPrinters: async () => unwrapList(await request(endpoints.printers())),
        createPrinter: (payload) => request(endpoints.printers(), { method: "POST", body: payload }),
        updatePrinter: (printerId, patch) =>
            request(endpoints.printer(printerId), { method: "PATCH", body: patch }),
        deactivatePrinter: (printerId) =>
            request(endpoints.printer(printerId), { method: "DELETE" }),
        printerStatus: (printerId) => request(endpoints.printerStatus(printerId)),
        listNotifications: async () => unwrapList(await request(endpoints.notifications())),
        markNotificationRead: (notificationId) =>
            request(endpoints.notificationRead(notificationId), { method: "POST" }),
        getSettings: () => request(endpoints.settings()),
        updateSettings: (patch) => request(endpoints.settings(), { method: "PATCH", body: patch }),
        testOllama: () => request(endpoints.settingsTestOllama(), { method: "POST" }),
        listOllamaModels: () => request(endpoints.ollamaModels()),
        pullOllamaModel: (name) =>
            request(endpoints.ollamaModelPull(), { method: "POST", body: { name } }),
        listOllamaPulls: async () => unwrapList(await request(endpoints.ollamaPulls())),
        deleteOllamaModel: (name) => request(endpoints.ollamaModelDelete(name), { method: "DELETE" }),
        useOllamaModel: (name) =>
            request(endpoints.ollamaModelUse(), { method: "POST", body: { name } }),
    };

    function statusKind(status) {
        const state = String(status || "").toLowerCase();
        if (DONE_STATUSES.includes(state)) return "done";
        if (FAILED_STATUSES.includes(state)) return "failed";
        if (PENDING_STATUSES.includes(state)) return "pending";
        return "unknown";
    }

    /** Human label for a FK field that may arrive as an id or as a nested object. */
    function labelFor(value, fallback) {
        if (value === null || value === undefined || value === "") return fallback || "—";
        if (typeof value === "object") return value.name || value.title || value.id || fallback || "—";
        return String(value);
    }

    /**
     * Prefer an API display field (`project_name`, `printer_name`, ...) and fall
     * back to the raw FK value (id or nested object) when it is null/empty.
     */
    function displayName(value, fallback) {
        if (value !== null && value !== undefined && value !== "") return String(value);
        return labelFor(fallback);
    }

    /** Maps a PrintJob status (terv.md 14.) to a badge modifier class. */
    function printStatusClass(status) {
        const state = String(status || "").toLowerCase();
        if (state === "completed") return "is-ok";
        if (state === "failed" || state === "cancelled") return "is-danger";
        if (state === "printing" || state === "paused" || state === "ready") return "is-active";
        return "is-muted";
    }

    /** Byte count -> "1.4 GB". Returns "—" for missing/invalid values. */
    function formatBytes(value) {
        if (value === null || value === undefined || value === "") return "—";
        let size = Number(value);
        if (!Number.isFinite(size)) return "—";
        const units = ["B", "KB", "MB", "GB", "TB"];
        let index = 0;
        while (size >= 1024 && index < units.length - 1) {
            size /= 1024;
            index += 1;
        }
        return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
    }

    /** Maps an OllamaPull status to a badge modifier class. */
    function pullStatusClass(status) {
        const state = String(status || "").toUpperCase();
        if (state === "DONE") return "is-ok";
        if (state === "FAILED") return "is-danger";
        if (state === "RUNNING") return "is-active";
        return "is-muted";
    }

    function isPullPending(status) {
        return PULL_PENDING_STATUSES.includes(String(status || "").toUpperCase());
    }

    function isPullTerminal(status) {
        return PULL_TERMINAL_STATUSES.includes(String(status || "").toUpperCase());
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
        fillPkTemplate,
        labelFor,
        displayName,
        printStatusClass,
        formatBytes,
        pullStatusClass,
        isPullPending,
        isPullTerminal,
    };

    // ------------------------------------------------------------------
    // Alpine components
    // ------------------------------------------------------------------

    /**
     * Shared `detailUrl(pk)` helper. The page stores a reversed
     * `projects:detail` template (`pk=0`) in `data-project-detail-url`, so no
     * URL path is ever hardcoded in JS or in an Alpine expression.
     */
    function projectDetailUrlMixin() {
        return {
            get detailUrlTemplate() {
                return this.$el && this.$el.dataset ? this.$el.dataset.projectDetailUrl || "" : "";
            },
            detailUrl(pk) {
                return fillPkTemplate(this.detailUrlTemplate, pk);
            },
        };
    }

    /**
     * Copy own properties - **including accessors** - from `source` onto `target`.
     *
     * `Object.assign` invokes getters and copies their current *value*, which
     * would freeze `get visibleProjects()` to the empty array it returned
     * before `projects` was loaded and would freeze `get detailUrlTemplate()`
     * to `""` (it read `this.$el` on the bare source object). Copying the
     * property descriptors keeps them live and reactive.
     */
    function mergeLiveProperties(target, source) {
        return Object.defineProperties(target, Object.getOwnPropertyDescriptors(source));
    }

    function projectListComponent() {
        return mergeLiveProperties(projectDetailUrlMixin(), {
            projects: [],
            workspaces: [],
            search: "",
            loading: true,
            error: "",

            // Create forms (Feature: create project / workspace from the UI).
            showProjectForm: false,
            showWorkspaceForm: false,
            projectForm: { name: "", description: "", workspace: "" },
            workspaceForm: { name: "" },
            creatingProject: false,
            creatingWorkspace: false,
            projectError: "",
            workspaceError: "",
            notice: "",

            get hasWorkspaces() {
                return this.workspaces.length > 0;
            },

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
                    const results = await Promise.all([this.loadProjects(), this.loadWorkspaces()]);
                    this.projects = results[0];
                    this.workspaces = results[1];
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                }
                this.syncForms();
            },

            /** Open the form the user actually needs on first load. */
            syncForms() {
                if (!this.hasWorkspaces) {
                    this.showWorkspaceForm = true;
                    this.showProjectForm = false;
                    return;
                }
                if (!this.projectForm.workspace) {
                    this.projectForm.workspace = this.workspaces[0].id;
                }
                this.showProjectForm = true;
                this.showWorkspaceForm = false;
            },

            toggleProjectForm() {
                if (!this.hasWorkspaces) {
                    this.showWorkspaceForm = true;
                    return;
                }
                this.showProjectForm = !this.showProjectForm;
            },

            toggleWorkspaceForm() {
                this.showWorkspaceForm = !this.showWorkspaceForm;
            },

            async loadProjects() {
                return api.listProjects();
            },

            async loadWorkspaces() {
                try {
                    return await api.listWorkspaces();
                } catch (error) {
                    return [];
                }
            },

            async reloadProjects() {
                try {
                    this.projects = await this.loadProjects();
                } catch (error) {
                    this.error = error.message || String(error);
                }
            },

            async createProject() {
                const name = this.projectForm.name.trim();
                const workspaceId = Number.parseInt(this.projectForm.workspace, 10);
                if (!name || !Number.isFinite(workspaceId) || this.creatingProject) return;

                this.creatingProject = true;
                this.projectError = "";
                this.notice = "";
                try {
                    const created = await api.createProject({
                        workspace: workspaceId,
                        name,
                        description: this.projectForm.description.trim(),
                    });
                    this.projectForm.name = "";
                    this.projectForm.description = "";
                    this.showProjectForm = false;
                    await this.reloadProjects();
                    this.notice = `Projekt létrehozva: ${(created && created.name) || name}`;
                } catch (error) {
                    this.projectError = error.message || String(error);
                } finally {
                    this.creatingProject = false;
                }
            },

            async createWorkspace() {
                const name = this.workspaceForm.name.trim();
                if (!name || this.creatingWorkspace) return;

                this.creatingWorkspace = true;
                this.workspaceError = "";
                this.notice = "";
                try {
                    const created = await api.createWorkspace(name);
                    this.workspaceForm.name = "";
                    this.workspaces = await this.loadWorkspaces();
                    if (created && created.id) this.projectForm.workspace = created.id;
                    this.syncForms();
                    this.notice = `Workspace létrehozva: ${(created && created.name) || name}`;
                } catch (error) {
                    this.workspaceError = error.message || String(error);
                } finally {
                    this.creatingWorkspace = false;
                }
            },
        });
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
                this.projectId = fromDom || idFromLocation();
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
     * Nav notifications bell (terv.md 21. fejezet, Phase 7).
     * GET /api/v1/notifications/ + POST /api/v1/notifications/{id}/read/
     */
    function notificationsBellComponent() {
        return {
            notifications: [],
            open: false,
            loading: false,
            loaded: false,
            error: "",
            pollTimer: null,

            get unreadCount() {
                return this.notifications.filter((item) => !item.is_read).length;
            },

            formatDate,

            async init() {
                await this.refresh();
                this.pollTimer = window.setInterval(() => {
                    if (!document.hidden) this.refresh();
                }, NOTIFICATION_POLL_MS);
            },

            async refresh() {
                this.loading = true;
                try {
                    this.notifications = await api.listNotifications();
                    this.error = "";
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                    this.loaded = true;
                }
            },

            toggle() {
                this.open = !this.open;
                if (this.open && !this.loaded) this.refresh();
            },

            close() {
                this.open = false;
            },

            async markRead(notification) {
                if (!notification || notification.is_read) return;
                const previous = notification.is_read;
                notification.is_read = true; // optimistic, rolled back on failure
                try {
                    await api.markNotificationRead(notification.id);
                    this.error = "";
                } catch (error) {
                    notification.is_read = previous;
                    this.error = error.message || String(error);
                }
            },
        };
    }

    /**
     * Print history page (terv.md 14. + Phase 7 "print history").
     * GET /api/v1/print-jobs/ (paginated, includes *_name display fields).
     */
    function printHistoryComponent() {
        const CANCELLABLE = ["QUEUED", "PREPARING", "SLICING", "READY", "PRINTING", "PAUSED"];
        const REQUEUEABLE = ["FAILED", "CANCELLED"];

        return mergeLiveProperties(projectDetailUrlMixin(), {
            jobs: [],
            loading: true,
            error: "",
            notice: "",
            actionBusyId: null,

            formatDate,
            displayName,
            statusClass: printStatusClass,

            versionLabel(job) {
                if (job && job.version !== null && job.version !== undefined) return `v${job.version}`;
                return labelFor(job ? job.model_version : null);
            },

            canStart(job) {
                return job.status === "READY";
            },
            canComplete(job) {
                return job.status === "PRINTING";
            },
            canCancel(job) {
                return CANCELLABLE.includes(job.status);
            },
            canRequeue(job) {
                return REQUEUEABLE.includes(job.status);
            },

            replaceJob(updated) {
                const index = this.jobs.findIndex((row) => row.id === updated.id);
                if (index >= 0) this.jobs.splice(index, 1, updated);
            },

            /** ``action`` is ``start`` / ``cancel`` or a target status. */
            async runAction(job, action) {
                if (this.actionBusyId) return;
                this.actionBusyId = job.id;
                this.error = "";
                this.notice = "";
                try {
                    let updated;
                    if (action === "start") {
                        updated = await api.startPrintJob(job.id);
                    } else if (action === "cancel") {
                        updated = await api.cancelPrintJob(job.id);
                    } else {
                        updated = await api.transitionPrintJob(job.id, action);
                    }
                    this.replaceJob(updated);
                    this.notice = `#${job.id} állapota: ${updated.status}`;
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.actionBusyId = null;
                }
            },

            async init() {
                try {
                    this.jobs = await api.listPrintJobs();
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                }
            },
        });
    }

    /**
     * Settings page (staff-only).
     *
     * GET/PATCH /api/v1/settings/  -> {effective, overrides, sources, read_only, updated_at}
     * POST       /api/v1/settings/test-ollama/ -> {ok, detail}
     *
     * The form only sends the fields the operator actually changed (a PATCH
     * diff against the last loaded `effective` values), so untouched
     * env/default-sourced settings are never turned into DB overrides.
     */
    function settingsPageComponent() {
        function emptyForm() {
            return {
                ollama_base_url: "",
                ollama_model: "",
                embedding_model: "",
                rag_enabled: false,
                openscad_mode: "",
                openscad_timeout_sec: "",
                openscad_memory_limit: "",
                openscad_cpu_limit: "",
                slicer_mode: "",
                slicer_timeout_sec: "",
                storage_backend: "",
            };
        }

        function formFromEffective(effective) {
            const data = effective || {};
            return {
                ollama_base_url: data.ollama_base_url || "",
                ollama_model: data.ollama_model || "",
                embedding_model: data.embedding_model || "",
                rag_enabled: Boolean(data.rag_enabled),
                openscad_mode: data.openscad_mode || "",
                openscad_timeout_sec:
                    data.openscad_timeout_sec === null || data.openscad_timeout_sec === undefined
                        ? ""
                        : data.openscad_timeout_sec,
                openscad_memory_limit: data.openscad_memory_limit || "",
                openscad_cpu_limit: data.openscad_cpu_limit || "",
                slicer_mode: data.slicer_mode || "",
                slicer_timeout_sec:
                    data.slicer_timeout_sec === null || data.slicer_timeout_sec === undefined
                        ? ""
                        : data.slicer_timeout_sec,
                storage_backend: data.storage_backend || "",
            };
        }

        /** "" / null -> null (meaning "clear the override"). */
        function serialize(value) {
            if (typeof value === "boolean") return value;
            if (value === "" || value === null || value === undefined) return null;
            return value;
        }

        return {
            loading: true,
            saving: false,
            testing: false,
            forbidden: false,
            error: "",
            notice: "",
            testResult: null,
            effective: {},
            overrides: {},
            sources: {},
            readOnly: [],
            updatedAt: "",
            form: emptyForm(),
            baseline: emptyForm(),
            models: [],
            modelsLoading: false,
            modelsError: "",

            readOnlyNotes: {
                embedding_dim:
                    "Megváltoztatása a pgvector séma és az összes embedding újragenerálását igényli.",
            },

            source(name) {
                return this.sources[name] || "default";
            },

            /** Read-only fields are rendered straight from `effective`. */
            effectiveValue(name) {
                const value = this.effective ? this.effective[name] : null;
                return value === null || value === undefined ? "" : value;
            },

            /** True when Ollama reported capabilities (>= 0.34); else show all. */
            hasModelCapabilities() {
                return this.models.some(
                    (model) => Array.isArray(model.capabilities) && model.capabilities.length
                );
            },

            /**
             * Dropdown options for a model kind ("completion" / "embedding").
             * Falls back to the full list when capabilities are unavailable, and
             * always keeps the current value so an existing override is never
             * hidden (even when that model is not installed).
             */
            modelOptionsFor(current, kind) {
                let options = Array.isArray(this.models) ? this.models.slice() : [];
                if (this.hasModelCapabilities()) {
                    options = options.filter((model) => {
                        const caps = Array.isArray(model.capabilities) ? model.capabilities : [];
                        if (kind === "embedding") return caps.includes("embedding");
                        return (
                            caps.includes("completion") ||
                            caps.includes("tools") ||
                            caps.includes("insert")
                        );
                    });
                }
                const names = options.map((model) => model.name);
                if (current && !names.includes(current)) {
                    options.unshift({ name: current, missing: true });
                }
                return options;
            },

            get ollamaModelOptions() {
                return this.modelOptionsFor(this.form.ollama_model, "completion");
            },

            get embeddingModelOptions() {
                return this.modelOptionsFor(this.form.embedding_model, "embedding");
            },

            formatDate,

            async init() {
                await this.load();
            },

            async load() {
                this.loading = true;
                this.error = "";
                this.forbidden = false;
                try {
                    const [data] = await Promise.all([api.getSettings(), this.loadModels()]);
                    this.applyResponse(data);
                } catch (error) {
                    if (error.status === 403) this.forbidden = true;
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                }
            },

            /** Populate the model dropdowns from the installed Ollama models. */
            async loadModels() {
                this.modelsLoading = true;
                this.modelsError = "";
                try {
                    const payload = (await api.listOllamaModels()) || {};
                    this.models = Array.isArray(payload.models) ? payload.models : [];
                    if (payload.error) this.modelsError = payload.error;
                } catch (error) {
                    // The settings endpoint already decides the forbidden view;
                    // here a failure only means the suggestions are unavailable.
                    this.modelsError = error.message || String(error);
                } finally {
                    this.modelsLoading = false;
                }
            },

            applyResponse(data) {
                const payload = data || {};
                this.effective = payload.effective || {};
                this.overrides = payload.overrides || {};
                this.sources = payload.sources || {};
                this.readOnly = Array.isArray(payload.read_only) ? payload.read_only : [];
                this.updatedAt = payload.updated_at || "";
                this.form = formFromEffective(this.effective);
                this.baseline = formFromEffective(this.effective);
            },

            /** Only the changed fields, as a PATCH body. */
            buildPatch() {
                const patch = {};
                for (const name of Object.keys(this.form)) {
                    const next = serialize(this.form[name]);
                    const previous = serialize(this.baseline[name]);
                    if (!Object.is(next, previous)) patch[name] = next;
                }
                return patch;
            },

            async save() {
                if (this.saving || this.forbidden) return;
                const patch = this.buildPatch();
                this.error = "";
                this.notice = "";
                if (!Object.keys(patch).length) {
                    this.notice = "Nincs mentendő változás.";
                    return;
                }
                this.saving = true;
                try {
                    this.applyResponse(await api.updateSettings(patch));
                    this.notice = "Beállítások elmentve.";
                } catch (error) {
                    if (error.status === 403) this.forbidden = true;
                    this.error = error.message || String(error);
                } finally {
                    this.saving = false;
                }
            },

            async testOllama() {
                if (this.testing || this.forbidden) return;
                this.testing = true;
                this.testResult = null;
                this.error = "";
                try {
                    this.testResult = await api.testOllama();
                } catch (error) {
                    this.testResult = { ok: false, detail: error.message || String(error) };
                } finally {
                    this.testing = false;
                }
            },

            reset() {
                this.form = formFromEffective(this.effective);
                this.baseline = formFromEffective(this.effective);
                this.notice = "";
                this.error = "";
            },
        };
    }

    /**
     * Ollama model management page (staff-only).
     *
     * GET    /api/v1/ollama/models/            -> {base_url, version, models, error}
     * POST   /api/v1/ollama/models/pull/       -> 202 OllamaPull
     * GET    /api/v1/ollama/pulls/             -> newest-first OllamaPull list
     * DELETE /api/v1/ollama/models/?name=<m>   -> 204
     * POST   /api/v1/ollama/models/use/        -> 200 (sets ollama_model)
     */
    function ollamaModelsComponent() {
        return {
            loading: true,
            forbidden: false,
            error: "",
            notice: "",
            baseUrl: "",
            version: "",
            serverError: "",
            models: [],
            activeModel: "",
            pullName: "",
            pulling: false,
            pullError: "",
            pulls: [],
            busyModel: "",
            confirmDelete: "",
            pollTimer: null,

            formatDate,
            formatBytes,

            async init() {
                this.boundVisibility = () => this.handleVisibility();
                document.addEventListener("visibilitychange", this.boundVisibility);
                await this.loadAll();
            },

            destroy() {
                this.stopPolling();
                if (this.boundVisibility) {
                    document.removeEventListener("visibilitychange", this.boundVisibility);
                }
            },

            async loadAll() {
                this.loading = true;
                this.error = "";
                this.forbidden = false;
                try {
                    await Promise.all([this.loadModels(), this.refreshPulls(), this.loadActiveModel()]);
                } finally {
                    this.loading = false;
                }
                this.ensurePolling();
            },

            async loadModels() {
                try {
                    const payload = (await api.listOllamaModels()) || {};
                    this.baseUrl = payload.base_url || "";
                    this.version = payload.version || "";
                    this.serverError = payload.error || "";
                    this.models = Array.isArray(payload.models) ? payload.models : [];
                } catch (error) {
                    if (error.status === 403) this.forbidden = true;
                    this.error = error.message || String(error);
                }
            },

            /** The active model lives in the settings payload. */
            async loadActiveModel() {
                try {
                    const payload = await api.getSettings();
                    const effective = (payload && payload.effective) || {};
                    this.activeModel = effective.ollama_model || "";
                } catch (error) {
                    if (error.status === 403) this.forbidden = true;
                }
            },

            /**
             * Refresh the pull list. Returns `false` when the request failed so
             * callers never restart polling on top of a failing endpoint.
             */
            async refreshPulls() {
                try {
                    this.pulls = await api.listOllamaPulls();
                    return true;
                } catch (error) {
                    if (error.status === 403) this.forbidden = true;
                    return false;
                }
            },

            // ---------------------------------------------------------------
            // Polling: every 2 s, only while a pull is pending, never hidden
            // ---------------------------------------------------------------
            get hasPendingPulls() {
                return this.pulls.some((pull) => isPullPending(pull.status));
            },

            ensurePolling() {
                if (this.hasPendingPulls && !document.hidden) this.startPolling();
                else this.stopPolling();
            },

            startPolling() {
                if (this.pollTimer) return;
                this.pollTimer = window.setInterval(() => {
                    if (document.hidden) {
                        this.stopPolling();
                        return;
                    }
                    this.refreshPulls().then((ok) => {
                        // A failed poll stops polling instead of hammering the
                        // endpoint; "Frissítés" restarts it on demand.
                        if (ok) this.ensurePolling();
                        else this.stopPolling();
                    });
                }, OLLAMA_POLL_MS);
            },

            stopPolling() {
                if (!this.pollTimer) return;
                window.clearInterval(this.pollTimer);
                this.pollTimer = null;
            },

            handleVisibility() {
                if (document.hidden) {
                    this.stopPolling();
                    return;
                }
                if (this.hasPendingPulls) {
                    this.refreshPulls().then((ok) => {
                        if (ok) this.ensurePolling();
                    });
                }
            },

            isPending(pull) {
                return isPullPending(pull && pull.status);
            },

            pullStatusClass(status) {
                return pullStatusClass(status);
            },

            progressPercent(pull) {
                if (!pull) return 0;
                if (typeof pull.progress_percent === "number") return pull.progress_percent;
                const total = Number(pull.total_bytes);
                const done = Number(pull.completed_bytes);
                if (Number.isFinite(total) && total > 0 && Number.isFinite(done)) {
                    return Math.min(100, Math.round((done / total) * 100));
                }
                return 0;
            },

            bytesLabel(pull) {
                if (!pull) return "";
                const done = formatBytes(pull.completed_bytes);
                const total = formatBytes(pull.total_bytes);
                if (done === "—" && total === "—") return "";
                if (total === "—") return done;
                return `${done} / ${total}`;
            },

            /** "llama · 30B · Q4_K_M" style summary for a model row. */
            modelMeta(model) {
                if (!model) return "—";
                const parts = [model.family, model.parameter_size, model.quantization].filter(Boolean);
                return parts.length ? parts.join(" · ") : "—";
            },

            dismiss(pull) {
                this.pulls = this.pulls.filter((item) => item.id !== pull.id);
            },

            // ---------------------------------------------------------------
            // Actions
            // ---------------------------------------------------------------
            async pull() {
                const name = this.pullName.trim();
                if (!name || this.pulling) return;
                this.pulling = true;
                this.pullError = "";
                this.notice = "";
                try {
                    const created = await api.pullOllamaModel(name);
                    this.pullName = "";
                    if (created && created.id) {
                        this.pulls = [created].concat(this.pulls.filter((item) => item.id !== created.id));
                    }
                    this.notice = `Letöltés elindítva: ${name}`;
                    await this.refreshPulls();
                    this.ensurePolling();
                } catch (error) {
                    if (error.status === 403) this.forbidden = true;
                    this.pullError = error.message || String(error);
                } finally {
                    this.pulling = false;
                }
            },

            async useModel(model) {
                if (!model || !model.name || this.busyModel) return;
                this.busyModel = model.name;
                this.error = "";
                this.notice = "";
                try {
                    await api.useOllamaModel(model.name);
                    this.activeModel = model.name;
                    this.notice = `Aktív modell: ${model.name}`;
                } catch (error) {
                    if (error.status === 403) this.forbidden = true;
                    this.error = error.message || String(error);
                } finally {
                    this.busyModel = "";
                }
            },

            async deleteModel(model) {
                if (!model || !model.name || this.busyModel) return;
                this.confirmDelete = "";
                this.busyModel = model.name;
                this.error = "";
                this.notice = "";
                try {
                    await api.deleteOllamaModel(model.name);
                    if (this.activeModel === model.name) this.activeModel = "";
                    this.notice = `Törölve: ${model.name}`;
                    await this.loadModels();
                } catch (error) {
                    if (error.status === 403) this.forbidden = true;
                    this.error = error.message || String(error);
                } finally {
                    this.busyModel = "";
                }
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
        Alpine.data("notificationsBell", notificationsBellComponent);
        Alpine.data("printHistory", printHistoryComponent);
        Alpine.data("settingsPage", settingsPageComponent);
        Alpine.data("ollamaModels", ollamaModelsComponent);
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
