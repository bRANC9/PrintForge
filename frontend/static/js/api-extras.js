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
        // Phase 8 community library (AllowAny).
        communityProjects: (params) => withQuery(`${API_BASE}/community/projects/`, params),
        communityProject: (id) => `${API_BASE}/community/projects/${encodeURIComponent(id)}/`,
        // Tag catalogue (AllowAny, paginated).
        tags: (params) => withQuery(`${API_BASE}/tags/`, params),
        // Project actions (MEMBER+).
        publish: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/publish/`,
        unpublish: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/unpublish/`,
        download: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/download/`,
        rate: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/rate/`,
        shares: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/share/`,
        share: (id, shareId) =>
            `${API_BASE}/projects/${encodeURIComponent(id)}/shares/${encodeURIComponent(shareId)}/`,
        descriptionAi: (id) => `${API_BASE}/projects/${encodeURIComponent(id)}/description-ai/`,
        // Build plates (#28).
        buildPlates: (params) => withQuery(`${API_BASE}/build-plates/`, params),
        buildPlate: (id) => `${API_BASE}/build-plates/${encodeURIComponent(id)}/`,
        plateItems: (id) => `${API_BASE}/build-plates/${encodeURIComponent(id)}/items/`,
        printJobs: () => `${API_BASE}/print-jobs/`,
    };

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
        LICENSES,
        licenseLabel,
        withQuery,
        sameOrigin,
        requestForm,
        describeError,
        isPermissionError,
    };
})();
