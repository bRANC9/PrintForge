// PrintForge Phase 8 API helpers: community library, publish/rating/shares,
// AI description (#29), reference images (#27) and build plates (#28).
//
// All JSON calls still go through `window.PrintForge.request` (defined in
// app.js). This file only adds the new URL builders plus a multipart helper
// (the version-create endpoint accepts `reference_image`, so it must NOT be
// sent as JSON).
(function () {
    "use strict";

    const PF = window.PrintForge;
    const API_BASE = PF.API_BASE;

    function withQuery(path, params) {
        const query = new URLSearchParams();
        Object.keys(params || {}).forEach((key) => {
            const value = params[key];
            if (value !== undefined && value !== null && String(value) !== "") {
                query.set(key, value);
            }
        });
        const qs = query.toString();
        return qs ? `${path}?${qs}` : path;
    }

    /**
     * Normalise a DRF pagination `next` link to a same-origin path so it can be
     * fetched even when the request Host header does not match `location.host`.
     */
    function sameOrigin(url) {
        if (!url) return "";
        try {
            const parsed = new URL(url, window.location.origin);
            if (parsed.origin === window.location.origin) return parsed.pathname + parsed.search;
            return parsed.pathname + parsed.search;
        } catch (error) {
            return url;
        }
    }

    const endpoints = {
        // Workspace-first navigation (docs/workspace-navigation.md 4.).
        workspace: (id) => `${API_BASE}/workspaces/${encodeURIComponent(id)}/`,
        // The flat /projects/ list filtered to one workspace (the server-side
        // `?workspace=` filter, docs/workspace-navigation.md 3.). The UI keeps a
        // client-side filter as a safety net.
        projectsByWorkspace: (workspaceId) =>
            withQuery(`${API_BASE}/projects/`, { workspace: workspaceId }),
        // Reusable generation recipes (docs/skills.md 6.).
        skills: (params) => withQuery(`${API_BASE}/skills/`, params),
        skill: (id) => `${API_BASE}/skills/${encodeURIComponent(id)}/`,
        // Version history controls (docs/version-history-controls.md 3./4.).
        regenerate: (versionId) =>
            `${API_BASE}/versions/${encodeURIComponent(versionId)}/regenerate/`,
        // Planner clarification (docs/planner-clarification.md 5.): answer the
        // blocking questions of a run that stopped with `status="clarification"`.
        // The run router is registered as `agent-runs`, so the action lives at
        // `/agent-runs/{id}/clarifications/`.
        runClarifications: (runId) =>
            `${API_BASE}/agent-runs/${encodeURIComponent(runId)}/clarifications/`,
        // Phase 8 community library (AllowAny).
        communityProjects: (params) => withQuery(`${API_BASE}/community/projects/`, params),
        communityProject: (id) => `${API_BASE}/community/projects/${encodeURIComponent(id)}/`,
        // Tag catalogue (AllowAny, paginated).
        tags: (params) => withQuery(`${API_BASE}/tags/`, params),
        // Project actions (MEMBER+).
        publish: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/publish/`,
        unpublish: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/unpublish/`,
        download: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/download/`,
        printProject: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/print/`,
        rate: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/rate/`,
        shares: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/share/`,
        share: (id, shareId) =>
            `${API_BASE}/projects/${encodeURIComponent(id)}/shares/${encodeURIComponent(shareId)}/`,
        descriptionAi: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/description-ai/`,
        // Visual-prompt model editing (docs/visual-editing.md 3.4/3.6).
        annotationEdit: (versionId) =>
            `${API_BASE}/versions/${encodeURIComponent(versionId)}/annotations/`,
        // Build plates (#28).
        buildPlates: (params) => withQuery(`${API_BASE}/build-plates/`, params),
        buildPlate: (id) => `${API_BASE}/build-plates/${encodeURIComponent(id)}/`,
        plateItems: (id) => `${API_BASE}/build-plates/${encodeURIComponent(id)}/items/`,
        printJobs: () => `${API_BASE}/print-jobs/`,
    };

    // `clarify` value on the generation payload (docs/planner-clarification.md
    // 5.): "assume" (default) lets the Planner guess missing values, "ask" makes
    // it stop with `status="clarification"` instead of guessing a risky value.
    const CLARIFY_POLICIES = { ask: "ask", assume: "assume" };

    // Mirrors projects.models.ProjectLicense (no choices endpoint exists).
    const LICENSES = [
        { value: "CC0-1.0", label: "CC0 1.0" },
        { value: "CC-BY-4.0", label: "CC BY 4.0" },
        { value: "CC-BY-SA-4.0", label: "CC BY-SA 4.0" },
        { value: "MIT", label: "MIT" },
        { value: "Apache-2.0", label: "Apache 2.0" },
        { value: "GPL-3.0", label: "GPL 3.0" },
        { value: "proprietary", label: "Proprietary" },
    ];

    function licenseLabel(value) {
        const match = LICENSES.find((item) => item.value === value);
        return match ? match.label : value || "Nincs licenc";
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
            // Body was not JSON - keep the status line.
        }
        return detail;
    }

    /**
     * POST/PATCH a `FormData` body. Deliberately does not set Content-Type so
     * the browser adds the multipart boundary; only the CSRF header is added.
     */
    async function requestForm(url, formData, method) {
        const headers = { Accept: "application/json" };
        const token = PF.getCookie("csrftoken");
        if (token) headers["X-CSRFToken"] = token;
        const response = await fetch(url, {
            method: method || "POST",
            credentials: "same-origin",
            headers,
            body: formData,
        });
        if (!response.ok) {
            const error = new Error(await describeError(response));
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

    /** Colours a 4xx as "no access" instead of a generic error. */
    function isPermissionError(error) {
        return Boolean(error && (error.status === 401 || error.status === 403 || error.status === 404));
    }

    window.PrintForgeExt = {
        endpoints,
        CLARIFY_POLICIES,
        LICENSES,
        licenseLabel,
        withQuery,
        sameOrigin,
        requestForm,
        describeError,
        isPermissionError,
    };
})();
