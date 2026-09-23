"""Endpoint tests for per-run skill selection on version generation.

``POST /api/v1/projects/{id}/versions/`` accepts ``skill_ids`` and
``auto_skill_selection`` (docs/skills.md 3./6.). ``run_agent_workflow`` declares
the matching kwargs, so the view forwards them unconditionally -- there is no
signature probe any more.
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


def test_skill_kwargs_are_always_forwarded_without_a_signature_probe(
    client, project, workspace, user, broker, monkeypatch
):
    """The view forwards the kwargs unconditionally (the probe is gone)."""

    def legacy_run(project_id, prompt, user_id=None, reference_image_name="", reference_note=""):
        return 1

    # Monkeypatching the underlying ``run`` would have changed the old probe's
    # decision; now it must not influence the forwarded kwargs at all.
    monkeypatch.setattr(run_agent_workflow, "run", legacy_run)
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


def test_real_task_receives_the_skill_kwargs(client, project, workspace, user, broker):
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


def test_multipart_repeated_skill_ids_are_parsed_as_integers(
    client, project, workspace, user, broker
):
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
    # The task declares the skill kwargs; no skills means empty/false.
    assert kwargs == {
        "reference_image_name": "",
        "reference_note": "",
        "skill_ids": [],
        "auto_skill_selection": False,
        "clarify_policy": "assume",
    }
