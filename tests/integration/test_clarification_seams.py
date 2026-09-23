"""Planner clarification at the task seam (docs/planner-clarification.md 4.).

The node-level routing is pinned by ``backend/agents/graph/tests/test_clarification.py``;
this module covers the *persistence* seam the docs describe:

* a blocking question under ``clarify_policy="ask"`` finishes the ``AgentRun``
  ``DONE`` with the questions in ``state_json`` and creates **no** ``ModelVersion``;
* an ``assumed`` clarification continues the run and stores the assumptions plus
  ``review_required`` on the version's ``validation_json``;
* the API ``clarify`` field is forwarded as ``clarify_policy`` (default
  ``"assume"``).

No live LLM/Ollama/OpenSCAD/Redis: the task dependencies are deterministic fakes
and the artifacts go to a per-test ``LocalStorage``.
"""

from __future__ import annotations

from typing import Any

import pytest
from factories import ProjectFactory

from agents.graph import WorkflowDeps
from agents.graph.tests.fakes import DEFAULT_SPEC, FakeCADBackend, FakeProvider
from agents.models import AgentRun, AgentRunStatus
from agents.tasks import run_agent_workflow
from files.services import LocalStorage

pytestmark = pytest.mark.django_db

BLOCKING = {
    "question": "Which phone model is the holder for?",
    "answer": "",
    "kind": "needs_user_input",
    "field": "dimensions",
}
ASSUMED = {
    "question": "No wall thickness given; assuming a printable 4 mm wall.",
    "answer": "4",
    "kind": "assumed",
    "field": "wall_thickness",
}


def _plan(*clarifications: dict[str, Any]) -> dict[str, Any]:
    return {
        "specification": DEFAULT_SPEC,
        "needs_research": False,
        "research_query": None,
        "clarifications": list(clarifications),
    }


def _install(monkeypatch, tmp_path, deps: WorkflowDeps) -> LocalStorage:
    monkeypatch.setattr("agents.tasks.build_dependencies", lambda: deps)
    storage = LocalStorage(root=tmp_path)
    monkeypatch.setattr("agents.tasks.get_storage", lambda: storage)
    return storage


@pytest.fixture
def enqueued(monkeypatch):
    """Capture the agent enqueue without a broker."""
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        run_agent_workflow, "delay", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    return calls


def _deps(provider: FakeProvider, cad: FakeCADBackend | None = None) -> WorkflowDeps:
    return WorkflowDeps(
        provider=provider,
        cad_backend=cad or FakeCADBackend(),
        retrieve_fn=lambda *args, **kwargs: [],
        preview_renderer=lambda _stl: b"",
        max_attempts=3,
    )


def test_ask_policy_persists_the_questions_without_creating_a_version(monkeypatch, tmp_path):
    project = ProjectFactory()
    provider = FakeProvider(plan=_plan(BLOCKING))
    _install(monkeypatch, tmp_path, _deps(provider))

    run_id = run_agent_workflow(
        project.pk,
        "make a holder for my phone",
        project.created_by_id,
        clarify_policy="ask",
    )

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert run.error == ""
    # No ModelVersion was finalised: nothing reached the CAD backend.
    assert not project.versions.exists()

    assert run.state_json["clarification_count"] == 1
    assert run.state_json["clarifications"] == [
        {"question": BLOCKING["question"], "field": "dimensions"}
    ]
    # The blocking summary never leaks the (empty) answer.
    assert "answer" not in run.state_json["clarifications"][0]
    assert run.state_json["assumptions"] == []


def test_assumed_clarification_continues_and_marks_the_version_for_review(monkeypatch, tmp_path):
    project = ProjectFactory()
    provider = FakeProvider(plan=_plan(ASSUMED))
    cad = FakeCADBackend()
    _install(monkeypatch, tmp_path, _deps(provider, cad))

    run_id = run_agent_workflow(
        project.pk,
        "make a holder",
        project.created_by_id,
        clarify_policy="assume",
    )

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert cad.generate_calls == 1

    version = project.versions.get()
    assert version.validation_json["review_required"] is True
    assert version.validation_json["assumptions"] == [
        {"field": "wall_thickness", "question": ASSUMED["question"], "answer": "4"}
    ]
    assert run.state_json["assumption_count"] == 1
    assert run.state_json["assumptions"] == [
        {"field": "wall_thickness", "question": ASSUMED["question"], "answer": "4"}
    ]


def test_assumed_and_blocking_split_under_ask_policy(monkeypatch, tmp_path):
    """Only the blocking question stops the run; the safe guess is still kept."""
    project = ProjectFactory()
    provider = FakeProvider(plan=_plan(ASSUMED, BLOCKING))
    cad = FakeCADBackend()
    _install(monkeypatch, tmp_path, _deps(provider, cad))

    run_id = run_agent_workflow(
        project.pk,
        "make a holder",
        project.created_by_id,
        clarify_policy="ask",
    )

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert not project.versions.exists()
    assert cad.generate_calls == 0
    assert [item["field"] for item in run.state_json["clarifications"]] == ["dimensions"]
    assert [item["field"] for item in run.state_json["assumptions"]] == ["wall_thickness"]


def test_api_forwards_the_clarify_policy_to_the_task(project, auth_client, enqueued):
    """``clarify="ask"`` on the generation payload reaches the workflow kwarg."""
    response = auth_client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "make a holder", "clarify": "ask"},
        format="json",
    )

    assert response.status_code == 202, response.content
    _args, kwargs = enqueued[0]
    assert kwargs["clarify_policy"] == "ask"


def test_api_defaults_the_clarify_policy_to_assume(project, auth_client, enqueued):
    response = auth_client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "make a holder"},
        format="json",
    )

    assert response.status_code == 202, response.content
    _args, kwargs = enqueued[0]
    assert kwargs["clarify_policy"] == "assume"
