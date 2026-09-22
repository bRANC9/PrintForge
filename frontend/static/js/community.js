// PrintForge community library (Phase 8).
//
//   GET /api/v1/community/projects/?q=&tag=&license=&ordering=  (paginated)
//   GET /api/v1/community/projects/{id}/
//   GET /api/v1/tags/                                            (paginated)
//   POST /api/v1/projects/{id}/download/
//
// Both components are public: no authentication is required for browsing.
(function () {
    "use strict";

    const ext = window.PrintForgeExt;
    const PF = window.PrintForge;

    function ratingText(rating) {
        if (!rating || rating.count === 0 || rating.average === null || rating.average === undefined) {
            return "Még nincs értékelés";
        }
        return `${Number(rating.average).toFixed(1)} ★ · ${rating.count} értékelés`;
    }

    function communityListComponent() {
        return {
            q: "",
            tag: "",
            license: "",
            ordering: "newest",
            licenses: ext.LICENSES,
            tags: [],
            projects: [],
            loading: true,
            loadingMore: false,
            error: "",
            count: 0,
            page: 1,
            hasMore: false,
            detailTemplate: "",
            searchTimer: null,

            formatDate: PF.formatDate,
            ratingText,

            init() {
                this.detailTemplate = (this.$el && this.$el.dataset
                    ? this.$el.dataset.communityDetailUrl
                    : "") || "";
                this.loadTags();
                this.load(true);
            },

            detailUrl(pk) {
                return PF.fillPkTemplate(this.detailTemplate, pk);
            },

            /** Debounced search box handler. */
            scheduleSearch() {
                window.clearTimeout(this.searchTimer);
                this.searchTimer = window.setTimeout(() => this.load(true), 300);
            },

            onFilterChange() {
                this.load(true);
            },

            toggleTag(slug) {
                this.tag = this.tag === slug ? "" : slug;
                this.load(true);
            },

            /** Follow DRF pagination so every tag is available for the filter. */
            async loadTags() {
                try {
                    let url = ext.endpoints.tags();
                    const collected = [];
                    while (url) {
                        const payload = await PF.request(url);
                        collected.push(...PF.unwrapList(payload));
                        url = ext.sameOrigin(payload && payload.next);
                    }
                    this.tags = collected;
                } catch (error) {
                    // The filter simply stays empty when the catalogue fails.
                    this.tags = [];
                }
            },

            async load(reset) {
                if (reset) {
                    this.loading = true;
                    this.page = 1;
                } else {
                    this.loadingMore = true;
                }
                this.error = "";
                try {
                    const payload = await PF.request(
                        ext.endpoints.communityProjects({
                            q: this.q.trim(),
                            tag: this.tag,
                            license: this.license,
                            ordering: this.ordering,
                            page: this.page,
                        })
                    );
                    const rows = PF.unwrapList(payload);
                    this.projects = reset ? rows : this.projects.concat(rows);
                    this.count = payload && typeof payload.count === "number"
                        ? payload.count
                        : this.projects.length;
                    this.hasMore = Boolean(payload && payload.next);
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                    this.loadingMore = false;
                }
            },

            async loadMore() {
                if (this.loadingMore || !this.hasMore) return;
                this.page += 1;
                await this.load(false);
            },
        };
    }

    function communityDetailComponent() {
        return {
            projectId: "",
            project: null,
            loading: true,
            error: "",
            notice: "",
            downloadBusy: false,

            formatDate: PF.formatDate,
            ratingText,

            get rating() {
                return (this.project && this.project.rating) || { average: null, count: 0 };
            },

            init() {
                this.projectId = (this.$el && this.$el.dataset
                    ? this.$el.dataset.projectId
                    : "") || "";
                if (!this.projectId) {
                    this.error = "Hiányzó projekt azonosító.";
                    this.loading = false;
                    return;
                }
                this.load();
            },

            async load() {
                this.loading = true;
                this.error = "";
                try {
                    this.project = await PF.request(ext.endpoints.communityProject(this.projectId));
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                }
            },

            async download() {
                if (this.downloadBusy) return;
                this.downloadBusy = true;
                this.error = "";
                this.notice = "";
                try {
                    const data = await PF.request(ext.endpoints.download(this.projectId), {
                        method: "POST",
                        body: {},
                    });
                    if (data && data.url) {
                        if (this.project) this.project.download_count = data.download_count;
                        window.location.assign(data.url);
                    } else {
                        this.error = "Ehhez a projekthez még nincs letölthető STL.";
                    }
                } catch (error) {
                    // Anonymous visitors are not workspace members, so the
                    // download endpoint answers 401/403/404.
                    if (ext.isPermissionError(error)) {
                        this.notice = "A letöltéshez jelentkezz be, és legyél tagja a workspace-nek.";
                    } else {
                        this.error = error.message || String(error);
                    }
                } finally {
                    this.downloadBusy = false;
                }
            },
        };
    }

    document.addEventListener("alpine:init", () => {
        const Alpine = window.Alpine;
        if (!Alpine) return;
        Alpine.data("communityList", communityListComponent);
        Alpine.data("communityDetail", communityDetailComponent);
    });
})();
