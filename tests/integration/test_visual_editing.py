"""Visual-prompt editing (docs/visual-editing.md): annotations API + agent task.

Integration coverage for the annotation edit flow through the DRF API and the
Celery task body. No broker, no live LLM/Ollama and no OpenSCAD: the agent
enqueue is recorded and the task runs with the deterministic fakes from
``agents.graph.tests.fakes``.

Graph-level routing coverage already lives in
``backend/agents/graph/tests/test_workflow.py`` (``test_edit_mode_routes_start_through_the_editor``)
and ``backend/agents/graph/tests/test_editor.py``; this module adds the
integration-level assertion that the *task* stores the Editor's ``operations``
on the derived version instead of duplicating those unit tests.
"""

from __future__ import annotations

from typing import Any

import pytest
from factories import ModelVersionFactory, ProjectFactory, UserFactory
from rest_framework.test import APIClient

from agents.graph import WorkflowDeps
from agents.graph.tests.fakes import FakeCADBackend, FakeProvider
from agents.models import AgentRun, AgentRunStatus
from agents.spec import ModelSpecification
from agents.tasks import run_agent_workflow
from designs.tasks import render_model_stl
from files.services import LocalStorage
from workspaces.models import WorkspaceRole
from workspaces.services import add_member

pytestmark = pytest.mark.django_db

PROMPT = "A kijelölt peremre tegyél egy 4 mm-es lyukat."
ANNOTATIONS: list[dict[str, Any]] = [
    {
        "id": "c1",
        "kind": "point",
        "point": [35.0, 12.5, 8.0],
        "normal": [0.0, 0.0, 1.0],
        "faces": [],
        "instruction": "4 mm-es átmenő lyuk",
    }
]

#: ``AnnotationSerializer`` fills the optional defaults, so the payload the
#: service enqueues carries ``region: {}`` even when the client omitted it.
NORMALISED_ANNOTATIONS: list[dict[str, Any]] = [{**ANNOTATIONS[0], "region": {}}]

#: The Editor's expected output: the base spec plus one validated operation.
EDIT_SPEC: dict[str, Any] = {
    **ModelSpecification.example(),
    "operations": [
        {
            "kind": "hole",
            "origin": {"x": 35.0, "y": 12.5, "z": 8.0},
            "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
            "depth": 8.0,
            "diameter": 4.0,
            "label": "4 mm hole",
        }
    ],
}


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    """Record both enqueue paths; never touch a real Celery broker."""
    calls: dict[str, list] = {"agent": [], "render": []}
    monkeypatch.setattr(render_model_stl, "delay", lambda pk: calls["render"].append(pk))
    monkeypatch.setattr(
        run_agent_workflow,
        "delay",
        lambda *args, **kwargs: calls["agent"].append((args, kwargs)),
    )
    return calls


def auth(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _add_member(workspace, role: str) -> Any:
    member = UserFactory()
    add_member(workspace=workspace, user=member, role=role)
    return member


def _deps(*, provider=None, cad=None) -> WorkflowDeps:
    return WorkflowDeps(
        provider=provider or FakeProvider(),
        cad_backend=cad or FakeCADBackend(),
        retrieve_fn=lambda *args, **kwargs: [],
        max_attempts=3,
    )


def _install_task(monkeypatch, tmp_path, deps: WorkflowDeps) -> LocalStorage:
    monkeypatch.setattr("agents.tasks.build_dependencies", lambda: deps)
    storage = LocalStorage(root=tmp_path)
    monkeypatch.setattr("agents.tasks.get_storage", lambda: storage)
    return storage


# ---------------------------------------------------------------------------
# API: POST /api/v1/versions/{id}/annotations/
# ---------------------------------------------------------------------------


def test_member_enqueues_the_annotation_edit(workspace, project, version, no_broker):
    member = _add_member(workspace, WorkspaceRole.MEMBER)

    response = auth(member).post(
        f"/api/v1/versions/{version.id}/annotations/",
        {"prompt": PROMPT, "annotations": ANNOTATIONS},
        format="json",
    )

    assert response.status_code == 202, response.content
    assert response.json() == {"queued": True, "parent_version": version.id}

    # The service only enqueues; the agent task creates the derived version.
    ((args, kwargs),) = no_broker["agent"]
    assert args == ()
    assert kwargs["project_id"] == project.id
    assert kwargs["prompt"] == PROMPT
    assert kwargs["user_id"] == member.id
    assert kwargs["base_version_id"] == version.id
    assert kwargs["annotations"] == NORMALISED_ANNOTATIONS
    assert no_broker["render"] == []


def test_viewer_cannot_enqueue_the_annotation_edit(workspace, version, no_broker):
    viewer = _add_member(workspace, WorkspaceRole.VIEWER)

    response = auth(viewer).post(
        f"/api/v1/versions/{version.id}/annotations/",
        {"prompt": PROMPT, "annotations": ANNOTATIONS},
        format="json",
    )

    assert response.status_code == 403
    assert no_broker["agent"] == []


def test_non_member_gets_404_for_the_annotation_edit(version, other_user, no_broker):
    response = auth(other_user).post(
        f"/api/v1/versions/{version.id}/annotations/",
        {"prompt": PROMPT, "annotations": ANNOTATIONS},
        format="json",
    )

    assert response.status_code == 404
    assert no_broker["agent"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"annotations": ANNOTATIONS},  # missing prompt
        {"prompt": PROMPT, "annotations": []},  # empty annotations
        # Bad kind (must be point|region).
        {"prompt": PROMPT, "annotations": [{**ANNOTATIONS[0], "kind": "engrave"}]},
        # point / normal must be exactly length 3.
        {"prompt": PROMPT, "annotations": [{**ANNOTATIONS[0], "point": [1.0, 2.0]}]},
        {
            "prompt": PROMPT,
            "annotations": [{**ANNOTATIONS[0], "normal": [1.0, 2.0, 3.0, 4.0]}],
        },
    ],
)
def test_annotation_edit_request_validation(version, auth_client, payload):
    response = auth_client.post(
        f"/api/v1/versions/{version.id}/annotations/", payload, format="json"
    )

    assert response.status_code == 400, response.content


def test_version_detail_exposes_the_edit_chain_fields(version, auth_client):
    response = auth_client.get(f"/api/v1/versions/{version.id}/")

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["parent_version"] is None
    assert body["annotations_json"] == []


def test_edit_chain_fields_are_read_only(version, auth_client):
    # Only the detail route is registered, so the read-only fields are not
    # writable through the API at all.
    response = auth_client.patch(
        f"/api/v1/versions/{version.id}/",
        {"parent_version": version.id, "annotations_json": ANNOTATIONS},
        format="json",
    )

    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Task: run_agent_workflow(base_version_id=..., annotations=...)
# ---------------------------------------------------------------------------


def test_task_persists_the_edit_version(monkeypatch, tmp_path):
    project = ProjectFactory()
    base = ModelVersionFactory(project=project, version=1)
    _install_task(monkeypatch, tmp_path, _deps())

    run_id = run_agent_workflow(
        project.pk,
        PROMPT,
        project.created_by_id,
        base_version_id=base.pk,
        annotations=ANNOTATIONS,
    )

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.DONE
    derived = project.versions.get(version=2)
    assert derived.parent_version_id == base.id
    assert derived.annotations_json == ANNOTATIONS


class RecordingProvider(FakeProvider):
    """Fake provider that also records the prompts handed to the Editor."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.prompts: list[str] = []

    def structured(self, prompt, schema, **kwargs):  # noqa: ANN001
        self.prompts.append(prompt)
        return super().structured(prompt, schema, **kwargs)


def test_edit_mode_task_runs_the_editor_and_persists_operations(monkeypatch, tmp_path):
    """Integration-level edit-mode assertion.

    The ``START -> editor`` routing itself is pinned by
    ``backend/agents/graph/tests/test_workflow.py::test_edit_mode_routes_start_through_the_editor``;
    here we verify that the task seeds the Editor with the base specification +
    annotations and stores the produced ``operations`` on the derived version.
    """
    project = ProjectFactory()
    base = ModelVersionFactory(project=project, version=1)
    provider = RecordingProvider(
        plan={"specification": EDIT_SPEC, "needs_research": False, "research_query": None}
    )
    _install_task(monkeypatch, tmp_path, _deps(provider=provider))

    run_id = run_agent_workflow(
        project.pk,
        PROMPT,
        project.created_by_id,
        base_version_id=base.pk,
        annotations=ANNOTATIONS,
    )

    assert AgentRun.objects.get(pk=run_id).status == AgentRunStatus.DONE
    # The Editor (not the Planner) ran: its prompt embeds the annotation payload.
    assert "Annotations (JSON)" in provider.prompts[0]

    derived = project.versions.get(version=2)
    assert derived.parent_version_id == base.id
    assert derived.annotations_json == ANNOTATIONS
    operations = derived.specification_json["operations"]
    assert operations[0]["kind"] == "hole"
    assert operations[0]["diameter"] == 4.0
