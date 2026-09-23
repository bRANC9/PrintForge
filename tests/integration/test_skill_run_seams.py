"""Skills wired through the agent task: selection -> prompt -> version provenance.

Colocated coverage note: ``backend/skills/tests`` pins the service-level CRUD /
visibility / ``select_skills`` order and ``backend/agents/graph/tests/test_skills.py``
pins the node-level injection. This module covers the remaining *seam*: the real
``run_agent_workflow`` task (with fakes) resolves the workspace-scoped skills,
injects them into the Planner system prompt and records them on the persisted
``ModelVersion.validation_json`` (docs/skills.md 3., 4., 7.).

No live LLM/Ollama, no broker, no OpenSCAD: the task dependencies are the
deterministic fakes from ``agents.graph.tests.fakes`` and the artifact storage is
a per-test ``LocalStorage``.
"""

from __future__ import annotations

from typing import Any

import pytest
from factories import ProjectFactory, UserFactory

from agents.graph import WorkflowDeps, run_workflow
from agents.graph.planner import PLANNER_SYSTEM_PROMPT
from agents.graph.skills import ACTIVE_SKILLS_HEADER
from agents.graph.tests.fakes import FakeCADBackend, FakeProvider
from agents.tasks import run_agent_workflow
from files.services import LocalStorage
from skills.models import Skill
from skills.services import create_skill

pytestmark = pytest.mark.django_db

PROMPT = "please make a cookie cutter for the kitchen"


@pytest.fixture(autouse=True)
def _clear_seeded_skills():
    """Drop the seeded built-ins so only the skills created here are candidates."""
    Skill.objects.all().delete()


def _deps(
    *,
    provider: FakeProvider | None = None,
    cad: FakeCADBackend | None = None,
    **overrides: Any,
) -> WorkflowDeps:
    """Build fake workflow dependencies (never a live backend)."""
    overrides.setdefault("retrieve_fn", lambda *args, **kwargs: [])
    overrides.setdefault("preview_renderer", lambda _stl: b"")
    return WorkflowDeps(
        provider=provider or FakeProvider(),
        cad_backend=cad or FakeCADBackend(),
        max_attempts=3,
        **overrides,
    )


def _install(monkeypatch, tmp_path, deps: WorkflowDeps) -> None:
    monkeypatch.setattr("agents.tasks.build_dependencies", lambda: deps)
    monkeypatch.setattr("agents.tasks.get_storage", lambda: LocalStorage(root=tmp_path))


def test_manual_skill_ids_flow_from_the_api_through_the_task_to_the_version(
    monkeypatch, tmp_path, project, auth_client
):
    """API ``skill_ids`` -> captured task kwargs -> real task -> version provenance."""
    skill = create_skill(
        name="Kitchen cutter",
        object_kind="cookie_cutter",
        tags=["cookie"],
        workspace=project.workspace,
        created_by=project.created_by,
    )
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        run_agent_workflow, "delay", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    response = auth_client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": PROMPT, "skill_ids": [skill.pk]},
        format="json",
    )

    assert response.status_code == 202, response.content
    args, kwargs = calls[0]
    assert kwargs["skill_ids"] == [skill.pk]
    assert kwargs["auto_skill_selection"] is False

    provider = FakeProvider()
    _install(monkeypatch, tmp_path, _deps(provider=provider))
    run_id = run_agent_workflow(*args, **kwargs)

    version = project.versions.get()
    assert version.validation_json["skills"] == [
        {"slug": skill.slug, "name": skill.name, "selection": "manual"}
    ]
    assert version.validation_json["skill_selection"] == "manual"
    # The "Aktív skillek" block actually reached the Planner system prompt.
    assert ACTIVE_SKILLS_HEADER in provider.last_system
    assert skill.name in provider.last_system
    run = project.agent_runs.get(pk=run_id)
    assert run.state_json["skills"] == [
        {"slug": skill.slug, "name": skill.name, "selection": "manual"}
    ]


def test_auto_selection_matches_a_workspace_skill_and_records_provenance(
    monkeypatch, tmp_path, project
):
    """``auto_skill_selection`` resolves the real service against the workspace."""
    skill = create_skill(
        name="Kitchen cutter",
        tags=["cookie"],
        workspace=project.workspace,
        created_by=project.created_by,
    )
    provider = FakeProvider()
    _install(monkeypatch, tmp_path, _deps(provider=provider))

    run_agent_workflow(project.pk, PROMPT, project.created_by_id, auto_skill_selection=True)

    version = project.versions.get()
    assert version.validation_json["skills"] == [
        {"slug": skill.slug, "name": skill.name, "selection": "auto"}
    ]
    assert ACTIVE_SKILLS_HEADER in provider.last_system


def test_embedding_fallback_uses_the_injected_ranker_at_the_graph_seam(monkeypatch):
    """No tag/object_kind match -> the semantic fallback selects the skill.

    The graph's production selector (``deps.select_skills_fn is None``) lazily
    imports ``skills.services.select_skills``; the default ranker is replaced so
    no embedding model/Ollama is ever touched.
    """
    project = ProjectFactory()
    skill = create_skill(
        name="Generic gadget",
        workspace=project.workspace,
        created_by=project.created_by,
    )

    def fake_ranker(*, prompt: str, candidates: list[Any], limit: int) -> list[int]:
        assert prompt == "an entirely unrelated wish"
        assert {candidate.pk for candidate in candidates} == {skill.pk}
        return [skill.pk]

    monkeypatch.setattr("skills.services._default_embedding_ranker", fake_ranker)
    provider = FakeProvider()

    state = run_workflow(
        "an entirely unrelated wish",
        deps=_deps(provider=provider),
        company_id=str(project.workspace_id),
        auto_skill_selection=True,
    )

    assert state["status"] == "done"
    assert state["skill_selection"] == "auto"
    assert [item["slug"] for item in state["skills"]] == [skill.slug]
    assert ACTIVE_SKILLS_HEADER in provider.last_system


def test_planner_prompt_is_unchanged_without_skills(monkeypatch):
    """Regression guard: no selected skills means the plain system prompt."""
    provider = FakeProvider()

    state = run_workflow("make a plain box", deps=_deps(provider=provider))

    assert state["skills"] == []
    assert ACTIVE_SKILLS_HEADER not in provider.last_system
    assert provider.last_system == PLANNER_SYSTEM_PROMPT


def test_skill_ids_from_another_workspace_are_not_selected(monkeypatch, tmp_path):
    """A foreign/private skill id must not leak into a run (docs/skills.md 3.)."""
    mine = ProjectFactory()
    foreign_skill = create_skill(name="Secret recipe", created_by=mine.created_by)
    target = ProjectFactory()
    provider = FakeProvider()
    _install(monkeypatch, tmp_path, _deps(provider=provider))

    run_agent_workflow(
        target.pk,
        "make something",
        target.created_by_id,
        skill_ids=[foreign_skill.pk],
    )

    version = target.versions.get()
    assert "skills" not in version.validation_json
    assert ACTIVE_SKILLS_HEADER not in provider.last_system


def test_skill_crud_visibility_is_workspace_builtin_and_public(project, auth_client):
    """API list: own workspace + built-ins + public, never a foreign private skill."""
    own = create_skill(name="Own", workspace=project.workspace, created_by=project.created_by)
    public = Skill.objects.create(name="Public", slug="public", is_public=True)
    builtin = Skill.objects.create(name="Builtin", slug="builtin", is_builtin=True)
    foreign = create_skill(name="Foreign", created_by=UserFactory())

    body = auth_client.get("/api/v1/skills/").json()["results"]
    ids = {item["id"] for item in body}

    assert ids == {own.pk, public.pk, builtin.pk}
    assert foreign.pk not in ids


def test_builtin_skill_is_read_only_through_the_api(auth_client):
    builtin = Skill.objects.create(name="Builtin", slug="builtin", is_builtin=True)

    patched = auth_client.patch(f"/api/v1/skills/{builtin.pk}/", {"name": "Nope"}, format="json")
    deleted = auth_client.delete(f"/api/v1/skills/{builtin.pk}/")

    assert patched.status_code == 403
    assert deleted.status_code == 403
    builtin.refresh_from_db()
    assert builtin.name == "Builtin"


def test_attaching_a_skill_to_a_project_persists_the_m2m(project, auth_client):
    skill = create_skill(
        name="Attach me", workspace=project.workspace, created_by=project.created_by
    )

    response = auth_client.patch(
        f"/api/v1/projects/{project.pk}/", {"skills": [skill.pk]}, format="json"
    )

    assert response.status_code == 200, response.content
    assert list(project.skills.values_list("pk", flat=True)) == [skill.pk]
