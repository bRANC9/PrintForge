"""Endpoint tests for per-run skill selection on version generation.

``POST /api/v1/projects/{id}/versions/`` accepts ``skill_ids`` and
``auto_skill_selection`` (docs/skills.md 3./6.). The matching
``run_agent_workflow`` kwargs have now landed, so the view's signature probe
forwards them to the real task; these tests also pin the probe's fallback for a
task that declares neither kwarg (forwarding one would only crash in the
worker).
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from agents.tasks import run_agent_workflow
from designs.tasks import render_model_stl
from projects.services import create_project
from skills.models import Skill
from skills.services import create_skill
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture(autouse=True)
def _clear_seeded_skills():
    """Drop the seeded built-ins so test skills never collide on ``slug``."""
    Skill.objects.all().delete()


def _run_with_skill_kwargs(
    project_id,
    prompt,
    user_id=None,
    reference_image_name="",
    reference_note="",
    skill_ids=None,
    auto_skill_selection=False,
):
    """Stand-in for the future task signature (agent-orchestrator)."""
    return 1


def _run_with_extra_kwargs(
    project_id,
    prompt,
    user_id=None,
    reference_image_name="",
    reference_note="",
    **kwargs,
):
    """Stand-in for a task that accepts arbitrary kwargs."""
    return 1


@pytest.fixture
def user():
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


@pytest.fixture
def client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def workspace(user):
    return create_workspace(name="Lab", owner=user)


@pytest.fixture
def project(workspace, user):
    return create_project(workspace=workspace, name="Holder", created_by=user)


@pytest.fixture(autouse=True)
def broker(monkeypatch):
    """Record the agent/render enqueues without touching a real broker."""
    calls: dict[str, list] = {"agent": [], "render": []}
    monkeypatch.setattr(
        run_agent_workflow,
        "delay",
        lambda *args, **kwargs: calls["agent"].append((args, kwargs)),
    )
    monkeypatch.setattr(render_model_stl, "delay", lambda pk: calls["render"].append(pk))
    return calls


def test_skill_kwargs_are_dropped_when_the_task_declares_neither(
    client, project, workspace, user, broker, monkeypatch
):
    """The probe still guards a task that declares neither kwarg nor **kwargs."""

    def legacy_run(project_id, prompt, user_id=None, reference_image_name="", reference_note=""):
        return 1

    monkeypatch.setattr(run_agent_workflow, "run", legacy_run)
    skill = create_skill(name="Cookie cutter", workspace=workspace, created_by=user)

    response = client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "make a cookie cutter", "skill_ids": [skill.pk], "auto_skill_selection": True},
        format="json",
    )

    assert response.status_code == 202, response.content
    _args, kwargs = broker["agent"][0]
    # A signature without the kwargs (and without **kwargs) must not receive
    # them: forwarding would only crash inside the worker.
    assert "skill_ids" not in kwargs
    assert "auto_skill_selection" not in kwargs
    assert kwargs == {"reference_image_name": "", "reference_note": ""}


def test_real_task_now_receives_the_skill_kwargs(client, project, workspace, user, broker):
    """With the agent-orchestrator kwargs landed, the real task gets them."""
    skill = create_skill(name="Cookie cutter", workspace=workspace, created_by=user)

    response = client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "make a cookie cutter", "skill_ids": [skill.pk], "auto_skill_selection": True},
        format="json",
    )

    assert response.status_code == 202, response.content
    _args, kwargs = broker["agent"][0]
    assert kwargs["skill_ids"] == [skill.pk]
    assert kwargs["auto_skill_selection"] is True


def test_skill_kwargs_are_forwarded_when_task_declares_them(
    client, project, workspace, user, broker, monkeypatch
):
    monkeypatch.setattr(run_agent_workflow, "run", _run_with_skill_kwargs)
    skill = create_skill(name="Cookie cutter", workspace=workspace, created_by=user)

    response = client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "make a cookie cutter", "skill_ids": [skill.pk], "auto_skill_selection": True},
        format="json",
    )

    assert response.status_code == 202, response.content
    args, kwargs = broker["agent"][0]
    assert args == (project.pk, "make a cookie cutter", user.pk)
    assert kwargs["skill_ids"] == [skill.pk]
    assert kwargs["auto_skill_selection"] is True


def test_skill_kwargs_are_forwarded_when_task_accepts_extra_kwargs(
    client, project, workspace, user, broker, monkeypatch
):
    monkeypatch.setattr(run_agent_workflow, "run", _run_with_extra_kwargs)
    skill = create_skill(name="Cookie cutter", workspace=workspace, created_by=user)

    response = client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "x", "skill_ids": [skill.pk], "auto_skill_selection": False},
        format="json",
    )

    assert response.status_code == 202, response.content
    _args, kwargs = broker["agent"][0]
    assert kwargs["skill_ids"] == [skill.pk]
    assert kwargs["auto_skill_selection"] is False


def test_multipart_repeated_skill_ids_are_parsed_as_integers(
    client, project, workspace, user, broker, monkeypatch
):
    monkeypatch.setattr(run_agent_workflow, "run", _run_with_skill_kwargs)
    first = create_skill(name="First", workspace=workspace, created_by=user)
    second = create_skill(name="Second", workspace=workspace, created_by=user)

    response = client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {
            "prompt": "x",
            "skill_ids": [first.pk, second.pk],
            "auto_skill_selection": True,
        },
        format="multipart",
    )

    assert response.status_code == 202, response.content
    _args, kwargs = broker["agent"][0]
    assert kwargs["skill_ids"] == [first.pk, second.pk]
    assert kwargs["auto_skill_selection"] is True


def test_generation_without_skill_fields_still_works(client, project, broker):
    response = client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "make it"},
        format="json",
    )

    assert response.status_code == 202, response.content
    _args, kwargs = broker["agent"][0]
    # The real task now declares the skill kwargs; no skills means empty/false.
    assert kwargs == {
        "reference_image_name": "",
        "reference_note": "",
        "skill_ids": [],
        "auto_skill_selection": False,
        "clarify_policy": "assume",
    }
