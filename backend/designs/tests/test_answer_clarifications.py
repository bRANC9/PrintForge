"""Tests for ``designs.services.answer_clarifications``.

Covers the follow-up-run contract of docs/planner-clarification.md 3-5.: a
stopped Planner run is answered by enqueueing a *new* run whose prompt is the
original prompt plus a deterministic, bounded „Felhasználói válaszok” block,
including the ``ClarificationError`` / ``RenderEnqueueError`` failure modes.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from agents.models import AgentRun, AgentRunStatus
from agents.tasks import run_agent_workflow
from designs.services import (
    ClarificationError,
    RenderEnqueueError,
    answer_clarifications,
)
from projects.services import create_project
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()

_ANSWERS_HEADING = "Felhasználói válaszok:"


@pytest.fixture
def user():
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


@pytest.fixture
def project(user):
    workspace = create_workspace(name="Lab", owner=user)
    return create_project(workspace=workspace, name="Holder", created_by=user)


@pytest.fixture
def broker(monkeypatch):
    """Record the enqueue without touching a real Celery broker."""
    calls: list[dict] = []
    monkeypatch.setattr(run_agent_workflow, "delay", lambda **kwargs: calls.append(kwargs))
    return calls


def _clarification_run(project, prompt="make a holder"):
    return AgentRun.objects.create(
        project=project,
        user_prompt=prompt,
        status=AgentRunStatus.DONE,
        state_json={
            "status": "clarification",
            "clarifications": [
                {"question": "How wide?", "field": "dimensions.width"},
                {"question": "Which material?", "field": ""},
            ],
            "assumptions": [],
        },
    )


def test_answer_clarifications_enqueues_a_new_run(project, user, broker):
    run = _clarification_run(project)

    result = answer_clarifications(
        run=run,
        answers=[{"field": "dimensions.width", "answer": "40"}],
        created_by=user,
    )

    assert result == {"queued": True, "parent_run": run.pk}
    assert len(broker) == 1
    call = broker[0]
    assert call["project_id"] == project.pk
    assert call["user_id"] == user.pk
    assert call["clarify_policy"] == "assume"
    assert call["prompt"] == (f"make a holder\n\n{_ANSWERS_HEADING}\n- How wide?: 40")
    # The stopped run is immutable; the worker creates the follow-up row.
    assert AgentRun.objects.count() == 1


def test_answer_clarifications_pairs_the_question_by_field(project, broker):
    run = _clarification_run(project)

    answer_clarifications(
        run=run,
        answers=[
            {"field": "dimensions.width", "answer": "40"},
            {"field": "", "answer": "PLA"},
        ],
    )

    prompt = broker[0]["prompt"]
    assert f"{_ANSWERS_HEADING}" in prompt
    assert "- How wide?: 40" in prompt
    # A field-less answer cannot be paired with a question; it gets a stable label.
    assert "- kerdes 2: PLA" in prompt


def test_answer_clarifications_prefers_the_state_prompt(project, broker):
    run = AgentRun.objects.create(
        project=project,
        user_prompt="fallback prompt",
        status=AgentRunStatus.DONE,
        state_json={
            "status": "clarification",
            "prompt": "state prompt",
            "clarifications": [{"question": "Q", "field": "f"}],
        },
    )

    answer_clarifications(run=run, answers=[{"field": "f", "answer": "a"}])

    assert broker[0]["prompt"].startswith("state prompt")


def test_answer_clarifications_without_created_by_passes_none(project, broker):
    run = _clarification_run(project)

    answer_clarifications(run=run, answers=[{"field": "dimensions.width", "answer": "40"}])

    assert broker[0]["user_id"] is None


def test_answer_clarifications_bounds_and_truncates_answers(project, broker):
    run = _clarification_run(project)
    answers = [{"field": f"field_{index}", "answer": "x" * 1000} for index in range(40)]

    answer_clarifications(run=run, answers=answers)

    prompt = broker[0]["prompt"]
    answer_lines = [line for line in prompt.splitlines() if line.startswith("- ")]
    assert len(answer_lines) == 16  # capped
    assert "x" * 601 not in prompt  # each answer truncated
    assert "x" * 600 in prompt


def test_answer_clarifications_requires_pending_questions(project, broker):
    run = AgentRun.objects.create(
        project=project,
        user_prompt="x",
        status=AgentRunStatus.DONE,
        state_json={"status": "done"},
    )

    with pytest.raises(ClarificationError, match="not waiting"):
        answer_clarifications(run=run, answers=[{"field": "f", "answer": "a"}])

    assert broker == []


def test_answer_clarifications_rejects_blank_answers(project, broker):
    run = _clarification_run(project)

    with pytest.raises(ClarificationError, match="usable reply"):
        answer_clarifications(run=run, answers=[{"field": "dimensions.width", "answer": "   "}])

    assert broker == []


def test_answer_clarifications_broker_failure_raises(project, user, monkeypatch):
    run = _clarification_run(project)

    def boom(**kwargs):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(run_agent_workflow, "delay", boom)

    with pytest.raises(RenderEnqueueError, match="clarification queue"):
        answer_clarifications(
            run=run,
            answers=[{"field": "dimensions.width", "answer": "40"}],
            created_by=user,
        )
