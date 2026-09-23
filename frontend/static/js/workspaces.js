// PrintForge workspace-first navigation (docs/workspace-navigation.md 4.).
//
//   GET  /api/v1/workspaces/                       (workspace cards)
//   POST /api/v1/workspaces/                       (create; body {name})
//   GET  /api/v1/workspaces/{id}/                  (detail header)
//   GET  /api/v1/projects/?workspace={id}          (items of one workspace)
//   POST /api/v1/projects/                         (create an item)
//   GET  /api/v1/skills/                           (skill picker, docs/skills.md)
//
// `projectList` (app.js) stays untouched for the deprecated `/projects/` page;
// the workspace-specific state lives here so the two do not drift.
(function () {
    "use strict";

    const PF = window.PrintForge;
    const ext = window.PrintForgeExt;

    /** Copy own descriptors so accessors stay live (see app.js mergeLiveProperties). */
    function live(target, source) {
        return Object.defineProperties(target, Object.getOwnPropertyDescriptors(source));
    }

    /** Follow DRF pagination and return every row. */
    async function fetchAll(url) {
        const collected = [];
        let next = url;
        while (next) {
            const payload = await PF.request(next);
            collected.push(...PF.unwrapList(payload));
            next = ext.sameOrigin(payload && payload.next);
        }
        return collected;
    }

    function idFromLocation() {
        const segments = window.location.pathname.split("/").filter(Boolean);
        const last = segments.length ? segments[segments.length - 1] : "";
        return /^\d+$/.test(last) ? last : "";
    }

    /** `/workspaces/{id}/` links are built from a reversed `pk=0` template. */
    function workspaceDetailUrlMixin() {
        return {
            get workspaceDetailUrlTemplate() {
                const root = this.$root;
                return root && root.dataset ? root.dataset.workspaceDetailUrl || "" : "";
            },
            workspaceUrl(pk) {
                return PF.fillPkTemplate(this.workspaceDetailUrlTemplate, pk);
            },
        };
    }

    function workspaceListComponent() {
        return live(workspaceDetailUrlMixin(), {
            workspaces: [],
            projects: [],
            search: "",
            loading: true,
            error: "",
            notice: "",
            showCreateForm: false,
            form: { name: "" },
            creating: false,
            createError: "",

            formatDate: PF.formatDate,

            get visibleWorkspaces() {
                const term = this.search.trim().toLowerCase();
                if (!term) return this.workspaces;
                return this.workspaces.filter((workspace) =>
                    `${workspace.name || ""}`.toLowerCase().includes(term)
                );
            },

            get hasWorkspaces() {
                return this.workspaces.length > 0;
            },

            memberCount(workspace) {
                return Array.isArray(workspace && workspace.members) ? workspace.members.length : 0;
            },

            projectCount(workspaceId) {
                return this.projects.filter(
                    (project) => Number(project.workspace) === Number(workspaceId)
                ).length;
            },

            async init() {
                this.loading = true;
                this.error = "";
                try {
                    const [workspaces, projects] = await Promise.all([
                        PF.api.listWorkspaces(),
                        fetchAll(PF.endpoints.projects()),
                    ]);
                    this.workspaces = workspaces;
                    this.projects = projects;
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                }
            },

            async createWorkspace() {
                const name = this.form.name.trim();
                if (!name || this.creating) return;
                this.creating = true;
                this.createError = "";
                this.notice = "";
                try {
                    const created = await PF.api.createWorkspace(name);
                    this.form.name = "";
                    this.showCreateForm = false;
                    this.workspaces = await PF.api.listWorkspaces();
                    this.notice = `Workspace létrehozva: ${(created && created.name) || name}`;
                } catch (error) {
                    this.createError = error.message || String(error);
                } finally {
                    this.creating = false;
                }
            },
        });
    }

    function workspaceDetailComponent() {
        const state = {
            workspaceId: "",
            workspace: null,
            projects: [],
            search: "",
            loading: true,
            error: "",
            notice: "",
            showCreateForm: false,
            form: { name: "", description: "" },
            creating: false,
            createError: "",

            formatDate: PF.formatDate,

            get workspaceName() {
                return this.workspace ? this.workspace.name : "";
            },

            get visibleProjects() {
                const term = this.search.trim().toLowerCase();
                if (!term) return this.projects;
                return this.projects.filter((project) =>
                    `${project.name || ""} ${project.description || ""}`.toLowerCase().includes(term)
                );
            },

            /** `/projects/{id}/` links come from a reversed `pk=0` template. */
            get projectDetailUrlTemplate() {
                const root = this.$root;
                return root && root.dataset ? root.dataset.projectDetailUrl || "" : "";
            },

            projectUrl(pk) {
                return PF.fillPkTemplate(this.projectDetailUrlTemplate, pk);
            },

            init() {
                this.workspaceId =
                    (this.$el && this.$el.dataset ? this.$el.dataset.workspaceId : "") ||
                    idFromLocation();
                if (!this.workspaceId) {
                    this.error = "Hiányzó workspace azonosító az URL-ben.";
                    this.loading = false;
                    return;
                }
                this.reload();
                if (ext.skillPickerState) this.loadSkillCatalogue();
            },

            async reload() {
                this.loading = true;
                this.error = "";
                try {
                    this.workspace = await PF.request(ext.endpoints.workspace(this.workspaceId));
                    await this.loadProjects();
                } catch (error) {
                    if (ext.isPermissionError(error)) {
                        this.error = "Nincs hozzáférésed ehhez a workspace-hez.";
                    } else {
                        this.error = error.message || String(error);
                    }
                } finally {
                    this.loading = false;
                }
            },

            async loadProjects() {
                // Ask the API for one workspace; the membership-scoped filter
                // is re-applied client-side as a safety net.
                const rows = await fetchAll(ext.endpoints.projectsByWorkspace(this.workspaceId));
                this.projects = rows.filter(
                    (project) => Number(project.workspace) === Number(this.workspaceId)
                );
            },

            async createProject() {
                const name = this.form.name.trim();
                if (!name || this.creating) return;
                this.creating = true;
                this.createError = "";
                this.notice = "";
                try {
                    const created = await PF.api.createProject({
                        workspace: Number(this.workspaceId),
                        name,
                        description: this.form.description.trim(),
                        // Forward-looking (docs/skills.md): ignored by the
                        // current serializer, consumed once api-dev adds it.
                        skills: this.skillIds ? this.skillIds() : [],
                        auto_skill_selection: Boolean(this.autoSkillSelection),
                    });
                    this.form.name = "";
                    this.form.description = "";
                    this.showCreateForm = false;
                    await this.loadProjects();
                    this.notice = `Elem létrehozva: ${(created && created.name) || name}`;
                } catch (error) {
                    this.createError = error.message || String(error);
                } finally {
                    this.creating = false;
                }
            },
        };
        // Manual skill selection (docs/skills.md) when skills.js is loaded.
        if (ext.skillPickerState) Object.assign(state, ext.skillPickerState());
        return state;
    }

    document.addEventListener("alpine:init", () => {
        const Alpine = window.Alpine;
        if (!Alpine) return;
        Alpine.data("workspaceList", workspaceListComponent);
        Alpine.data("workspaceDetail", workspaceDetailComponent);
    });
})();
