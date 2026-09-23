// PrintForge skills UI (docs/skills.md 6. fejezet).
//
//   GET    /api/v1/skills/            (paginated; filter/earch in the UI)
//   POST   /api/v1/skills/            (create a workspace skill)
//   GET    /api/v1/skills/{id}/
//   PATCH  /api/v1/skills/{id}/       (edit; built-in seeds are read-only)
//   DELETE /api/v1/skills/{id}/
//
// The same file also provides the shared skill-picker state used by the
// "Új elem" and generation forms (manual selection + auto-selection toggle).
//
// NOTE: the skills CRUD ViewSet and the `skills` field on ProjectSerializer
// are owned by `api-dev`; see the frontend report for the expected contract.
(function () {
    "use strict";

    const PF = window.PrintForge;
    const ext = window.PrintForgeExt;

    const KINDS = [
        { value: "guidance", label: "Guidance" },
        { value: "template", label: "Template" },
    ];

    function emptySkillForm() {
        return {
            name: "",
            description: "",
            kind: "guidance",
            object_kind: "",
            template_key: "",
            guidance: "",
            defaults_text: "{}",
            constraints_text: "{}",
            tagNames: [],
            tagInput: "",
            workspace: "",
            is_public: false,
        };
    }

    /** Follow DRF pagination and return every skill row. */
    async function fetchSkills() {
        let url = ext.endpoints.skills();
        const collected = [];
        while (url) {
            const payload = await PF.request(url);
            collected.push(...PF.unwrapList(payload));
            url = ext.sameOrigin(payload && payload.next);
        }
        return collected;
    }

    /**
     * Shared skill-picker state. Merged into `workspaceDetail` (workspaces.js)
     * and `projectWorkspace` (project.js) with `Object.assign`; it deliberately
     * exposes no getters so the host object's own accessors stay live.
     */
    function skillPickerState() {
        return {
            skills: [],
            skillsLoading: false,
            skillsError: "",
            selectedSkillIds: [],
            autoSkillSelection: true,

            async loadSkillCatalogue() {
                this.skillsLoading = true;
                this.skillsError = "";
                try {
                    this.skills = await fetchSkills();
                } catch (error) {
                    this.skillsError = error.message || String(error);
                    this.skills = [];
                } finally {
                    this.skillsLoading = false;
                }
            },

            toggleSkill(skillId) {
                if (this.autoSkillSelection) return;
                const index = this.selectedSkillIds.indexOf(skillId);
                if (index === -1) {
                    this.selectedSkillIds.push(skillId);
                } else {
                    this.selectedSkillIds.splice(index, 1);
                }
            },

            isSkillSelected(skillId) {
                return this.selectedSkillIds.includes(skillId);
            },

            /** Manual selection only; auto-selection sends an empty list. */
            skillIds() {
                return this.autoSkillSelection ? [] : this.selectedSkillIds.slice();
            },
        };
    }

    function skillsPageComponent() {
        return {
            skills: [],
            workspaces: [],
            tags: [],
            search: "",
            workspaceFilter: "",
            kindFilter: "",
            kinds: KINDS,
            loading: true,
            error: "",
            notice: "",
            showForm: false,
            editingId: null,
            saving: false,
            formError: "",
            form: emptySkillForm(),

            formatDate: PF.formatDate,

            get editingSkill() {
                return this.skills.find((skill) => skill.id === this.editingId) || null;
            },

            get isEditingBuiltin() {
                return Boolean(this.editingSkill && this.editingSkill.is_builtin);
            },

            get filteredSkills() {
                const term = this.search.trim().toLowerCase();
                return this.skills.filter((skill) => {
                    if (this.kindFilter && skill.kind !== this.kindFilter) return false;
                    if (!this.matchesWorkspaceFilter(skill)) return false;
                    if (!term) return true;
                    const haystack = [
                        skill.name,
                        skill.description,
                        skill.object_kind,
                        skill.template_key,
                        this.skillTags(skill).join(" "),
                    ]
                        .filter(Boolean)
                        .join(" ")
                        .toLowerCase();
                    return haystack.includes(term);
                });
            },

            matchesWorkspaceFilter(skill) {
                const filter = this.workspaceFilter;
                if (!filter) return true;
                if (filter === "builtin") return Boolean(skill.is_builtin);
                if (filter === "public") return Boolean(skill.is_public);
                if (filter === "global") {
                    return skill.workspace === null || skill.workspace === undefined || skill.workspace === "";
                }
                return Number(skill.workspace) === Number(filter);
            },

            kindLabel(kind) {
                const match = this.kinds.find((item) => item.value === kind);
                return match ? match.label : kind || "";
            },

            /** The API returns tag slugs; map them back to names when known. */
            skillTags(skill) {
                const tags = skill && Array.isArray(skill.tags) ? skill.tags : [];
                return tags.map((tag) => {
                    if (tag && typeof tag === "object") return tag.name || tag.slug;
                    const found = this.tags.find((item) => item.slug === tag || item.name === tag);
                    return found ? found.name : String(tag);
                });
            },

            workspaceLabel(skill) {
                if (skill.is_builtin) return "Beépített";
                if (skill.workspace === null || skill.workspace === undefined || skill.workspace === "") {
                    return "Globális";
                }
                const match = this.workspaces.find((item) => item.id === skill.workspace);
                return match ? match.name : `Workspace #${skill.workspace}`;
            },

            canEdit(skill) {
                return Boolean(skill) && !skill.is_builtin;
            },

            async init() {
                await Promise.all([this.load(), this.loadWorkspaces(), this.loadTags()]);
            },

            async load() {
                this.loading = true;
                this.error = "";
                try {
                    this.skills = await fetchSkills();
                } catch (error) {
                    this.error = error.message || String(error);
                } finally {
                    this.loading = false;
                }
            },

            async loadWorkspaces() {
                try {
                    this.workspaces = await PF.api.listWorkspaces();
                } catch (error) {
                    this.workspaces = [];
                }
            },

            /** Follow pagination so the tag datalist is complete. */
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
                    this.tags = [];
                }
            },

            startCreate() {
                this.editingId = null;
                this.form = emptySkillForm();
                this.form.workspace = this.workspaces.length ? String(this.workspaces[0].id) : "";
                this.formError = "";
                this.notice = "";
                this.showForm = true;
            },

            startEdit(skill) {
                if (!this.canEdit(skill)) return;
                this.editingId = skill.id;
                this.form = {
                    name: skill.name || "",
                    description: skill.description || "",
                    kind: skill.kind || "guidance",
                    object_kind: skill.object_kind || "",
                    template_key: skill.template_key || "",
                    guidance: skill.guidance || "",
                    defaults_text: JSON.stringify(skill.defaults_json || {}, null, 2),
                    constraints_text: JSON.stringify(skill.constraints_json || {}, null, 2),
                    tagNames: this.skillTags(skill).slice(),
                    tagInput: "",
                    workspace:
                        skill.workspace === null || skill.workspace === undefined
                            ? ""
                            : String(skill.workspace),
                    is_public: Boolean(skill.is_public),
                };
                this.formError = "";
                this.notice = "";
                this.showForm = true;
            },

            cancelForm() {
                this.showForm = false;
                this.editingId = null;
                this.form = emptySkillForm();
                this.formError = "";
            },

            addTag() {
                const raw = this.form.tagInput || "";
                raw.split(",")
                    .map((name) => name.trim())
                    .filter(Boolean)
                    .forEach((name) => {
                        const exists = this.form.tagNames.some(
                            (item) => item.toLowerCase() === name.toLowerCase()
                        );
                        if (!exists) this.form.tagNames.push(name);
                    });
                this.form.tagInput = "";
            },

            removeTag(index) {
                this.form.tagNames.splice(index, 1);
            },

            parseJsonField(text, label) {
                const value = (text || "").trim();
                if (!value) return {};
                try {
                    const parsed = JSON.parse(value);
                    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
                        throw new Error("objektum kell");
                    }
                    return parsed;
                } catch (error) {
                    throw new Error(`${label}: érvénytelen JSON (${error.message}).`);
                }
            },

            buildPayload() {
                const name = this.form.name.trim();
                if (!name) throw new Error("A név kötelező.");
                if (this.form.kind === "template" && !this.form.template_key.trim()) {
                    throw new Error("Template skillhez template_key kötelező.");
                }
                return {
                    name,
                    description: this.form.description.trim(),
                    kind: this.form.kind,
                    object_kind: this.form.object_kind.trim(),
                    template_key: this.form.kind === "template" ? this.form.template_key.trim() : "",
                    guidance: this.form.guidance,
                    defaults_json: this.parseJsonField(this.form.defaults_text, "defaults"),
                    constraints_json: this.parseJsonField(this.form.constraints_text, "constraints"),
                    tags: this.form.tagNames.slice(),
                    workspace: this.form.workspace === "" ? null : Number(this.form.workspace),
                    is_public: this.form.is_public,
                };
            },

            async save() {
                if (this.saving) return;
                let payload;
                try {
                    payload = this.buildPayload();
                } catch (error) {
                    this.formError = error.message || String(error);
                    return;
                }
                this.saving = true;
                this.formError = "";
                this.notice = "";
                try {
                    const url = this.editingId
                        ? ext.endpoints.skill(this.editingId)
                        : ext.endpoints.skills();
                    const method = this.editingId ? "PATCH" : "POST";
                    const saved = await PF.request(url, { method, body: payload });
                    this.showForm = false;
                    this.editingId = null;
                    await this.load();
                    this.notice = `Skill elmentve: ${(saved && saved.name) || payload.name}`;
                } catch (error) {
                    this.formError = error.message || String(error);
                } finally {
                    this.saving = false;
                }
            },

            async remove(skill) {
                if (!this.canEdit(skill)) return;
                const confirmed = window.confirm(`Törlöd a(z) „${skill.name}” skillt?`);
                if (!confirmed) return;
                this.error = "";
                this.notice = "";
                try {
                    await PF.request(ext.endpoints.skill(skill.id), { method: "DELETE" });
                    this.skills = this.skills.filter((item) => item.id !== skill.id);
                    if (this.editingId === skill.id) this.cancelForm();
                    this.notice = `Skill törölve: ${skill.name}`;
                } catch (error) {
                    this.error = error.message || String(error);
                }
            },
        };
    }

    ext.KINDS = KINDS;
    ext.skillPickerState = skillPickerState;

    document.addEventListener("alpine:init", () => {
        const Alpine = window.Alpine;
        if (!Alpine) return;
        Alpine.data("skillsPage", skillsPageComponent);
    });
})();
