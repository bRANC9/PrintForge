"""Endpoint tests for Planner clarifications and provenance exposure.

Covers docs/planner-clarification.md 5.:

* ``POST /api/v1/agent-runs/{id}/clarifications/`` (202 / 409 / 400 / 503 and
  workspace-role enforcement);
* the read-only ``clarifications`` / ``assumptions`` / ``status`` fields on
  ``AgentRunSerializer`` and ``assumptions`` / ``review_required`` on
  ``ModelVersionSerializer``;
* the ``clarify`` flag on version generation forwarded as ``clarify_policy``.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from agents.models import AgentRun, AgentRunStatus
from agents.tasks import run_agent_workflow
from designs.services import create_next_version
from projects.services import create_project
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def owner():
    return User.objects.create_user(username="owner", email="owner@example.com", password="pw")


@pytest.fixture
def outsider():
    return User.objects.create_user(username="outsider", email="out@example.com", password="pw")


@pytest.fixture
def workspace(owner):
    return create_workspace(name="Lab", owner=owner)


@pytest.fixture
def project(workspace, owner):
    return create_project(workspace=workspace, name="Holder", created_by=owner)


@pytest.fixture
def client(owner):
    client = APIClient()
    client.force_authenticate(user=owner)
    return client


@pytest.fixture(autouse=True)
def broker(monkeypatch):
    """Record the agent enqueue without touching a real Celery broker.

    Generation calls ``.delay`` with positional args, the clarification service
    with keyword args, so both are captured.
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        run_agent_workflow,
        "delay",
        lambda *args, **kwargs: calls.append({"args": args, "kwargs": kwargs}),
    )
    return calls


def _clarification_run(project, prompt="make a holder"):
    return AgentRun.objects.create(
        project=project,
        user_prompt=prompt,
        status=AgentRunStatus.DONE,
        state_json={
            "status": "clarification",
            "clarifications": [{"question": "How wide?", "field": "dimensions.width"}],
            "assumptions": [{"field": "height", "question": "How tall?", "answer": "20"}],
        },
    )


# ---------------------------------------------------------------------------
# POST /api/v1/agent-runs/{id}/clarifications/
# ---------------------------------------------------------------------------


def test_answer_clarifications_queues_a_follow_up_run(client, project, owner, broker):
    run = _clarification_run(project)

    response = client.post(
        f"/api/v1/agent-runs/{run.pk}/clarifications/",
        {"answers": [{"field": "dimensions.width", "answer": "40"}]},
        format="json",
    )

    assert response.status_code == 202
    assert response.json() == {"queued": True, "parent_run": run.pk}
    assert len(broker) == 1
    call = broker[0]["kwargs"]
    assert call["project_id"] == project.pk
    assert call["user_id"] == owner.pk
    assert call["clarify_policy"] == "assume"
    assert "Felhasználói válaszok:" in call["prompt"]
    assert "- How wide?: 40" in call["prompt"]
    # No new run row is created here: the task does that.
    assert AgentRun.objects.count() == 1


def test_answer_clarifications_returns_409_without_pending_questions(client, project, broker):
    run = AgentRun.objects.create(
        project=project,
        user_prompt="x",
        status=AgentRunStatus.DONE,
        state_json={"status": "done"},
    )

    response = client.post(
        f"/api/v1/agent-runs/{run.pk}/clarifications/",
        {"answers": [{"field": "f", "answer": "a"}]},
        format="json",
    )

    assert response.status_code == 409
    assert broker == []


def test_answer_clarifications_rejects_empty_answers(client, project):
    run = _clarification_run(project)

    response = client.post(
        f"/api/v1/agent-runs/{run.pk}/clarifications/",
        {"answers": []},
        format="json",
    )

    assert response.status_code == 400


def test_answer_clarifications_rejects_a_missing_answers_key(client, project):
    run = _clarification_run(project)

    response = client.post(
        f"/api/v1/agent-runs/{run.pk}/clarifications/",
        {},
        format="json",
    )

    assert response.status_code == 400


def test_answer_clarifications_rejects_blank_answer(client, project):
    run = _clarification_run(project)

    response = client.post(
        f"/api/v1/agent-runs/{run.pk}/clarifications/",
        {"answers": [{"field": "dimensions.width", "answer": ""}]},
        format="json",
    )

    assert response.status_code == 400


def test_answer_clarifications_returns_503_when_broker_is_down(client, project, monkeypatch):
    run = _clarification_run(project)

    def boom(**kwargs):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(run_agent_workflow, "delay", boom)

    response = client.post(
        f"/api/v1/agent-runs/{run.pk}/clarifications/",
        {"answers": [{"field": "dimensions.width", "answer": "40"}]},
        format="json",
    )

    assert response.status_code == 503
    assert "queue" in response.json()["detail"].lower()


def test_answer_clarifications_non_member_gets_404(outsider, project):
    run = _clarification_run(project)
    client = APIClient()
    client.force_authenticate(user=outsider)

    response = client.post(
        f"/api/v1/agent-runs/{run.pk}/clarifications/",
        {"answers": [{"field": "dimensions.width", "answer": "40"}]},
        format="json",
    )

    assert response.status_code == 404


def test_answer_clarifications_viewer_is_forbidden(workspace, project):
    viewer = User.objects.create_user(username="viewer", email="v@example.com", password="pw")
    add_member(workspace=workspace, user=viewer, role=WorkspaceRole.VIEWER)
    run = _clarification_run(project)
    client = APIClient()
    client.force_authenticate(user=viewer)

    response = client.post(
        f"/api/v1/agent-runs/{run.pk}/clarifications/",
        {"answers": [{"field": "dimensions.width", "answer": "40"}]},
        format="json",
    )

    assert response.status_code == 403


def test_answer_clarifications_requires_authentication(project):
    run = _clarification_run(project)

    response = APIClient().post(
        f"/api/v1/agent-runs/{run.pk}/clarifications/",
        {"answers": [{"field": "dimensions.width", "answer": "40"}]},
        format="json",
    )

    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Read-only provenance
# ---------------------------------------------------------------------------


def test_agent_run_serializer_surfaces_clarification_status(client, project):
    run = _clarification_run(project)

    response = client.get(f"/api/v1/agent-runs/{run.pk}/")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "clarification"
    assert body["clarifications"] == [{"question": "How wide?", "field": "dimensions.width"}]
    assert body["assumptions"] == [{"field": "height", "question": "How tall?", "answer": "20"}]


def test_agent_run_serializer_keeps_the_db_status_otherwise(client, project):
    run = AgentRun.objects.create(project=project, user_prompt="x")

    response = client.get(f"/api/v1/agent-runs/{run.pk}/")

    body = response.json()
    assert body["status"] == AgentRunStatus.PENDING
    assert body["clarifications"] == []
    assert body["assumptions"] == []


def test_version_serializer_exposes_assumptions_and_review_required(client, project, owner):
    version = create_next_version(project=project, prompt="x", created_by=owner)
    version.validation_json = {
        "status": "done",
        "assumptions": [{"field": "height", "question": "How tall?", "answer": "20"}],
        "review_required": True,
    }
    version.save(update_fields=["validation_json"])

    response = client.get(f"/api/v1/versions/{version.pk}/")

    assert response.status_code == 200
    body = response.json()
    assert body["assumptions"] == [{"field": "height", "question": "How tall?", "answer": "20"}]
    assert body["review_required"] is True


def test_version_serializer_defaults_to_no_assumptions(client, project, owner):
    version = create_next_version(project=project, prompt="x", created_by=owner)

    body = client.get(f"/api/v1/versions/{version.pk}/").json()

    assert body["assumptions"] == []
    assert body["review_required"] is False


# ---------------------------------------------------------------------------
# clarify flag -> clarify_policy
# ---------------------------------------------------------------------------


def test_generation_forwards_clarify_ask(client, project, broker):
    response = client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "make it", "clarify": "ask"},
        format="json",
    )

    assert response.status_code == 202, response.content
    assert broker[0]["kwargs"]["clarify_policy"] == "ask"


def test_generation_defaults_to_clarify_assume(client, project, broker):
    response = client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "make it"},
        format="json",
    )

    assert response.status_code == 202, response.content
    assert broker[0]["kwargs"]["clarify_policy"] == "assume"


def test_generation_rejects_an_unknown_clarify_value(client, project):
    response = client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "make it", "clarify": "maybe"},
        format="json",
    )

    assert response.status_code == 400
