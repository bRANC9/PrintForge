"""Tests for version regeneration / derived versions.

Covers ``create_next_version`` provenance and ``regenerate_version``'s two
paths (agent regeneration and manual specification render), including the
broker-failure contract (``RenderEnqueueError`` -> HTTP 503 at the edge).
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from agents.tasks import run_agent_workflow
from designs.models import ModelVersionOrigin
from designs.services import (
    RenderEnqueueError,
    create_next_version,
    regenerate_version,
)
from designs.tasks import render_model_stl
from projects.services import create_project
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


@pytest.fixture
def project(user):
    workspace = create_workspace(name="Lab", owner=user)
    return create_project(workspace=workspace, name="Holder", created_by=user)


@pytest.fixture
def version(project, user):
    return create_next_version(project=project, prompt="make a holder", created_by=user)


@pytest.fixture
def broker(monkeypatch):
    """Record both enqueue paths without touching a real Celery broker."""
    calls: dict[str, list] = {"agent": [], "render": []}
    monkeypatch.setattr(run_agent_workflow, "delay", lambda **kwargs: calls["agent"].append(kwargs))
    monkeypatch.setattr(render_model_stl, "delay", lambda pk: calls["render"].append(pk))
    return calls


# ---------------------------------------------------------------------------
# create_next_version provenance
# ---------------------------------------------------------------------------


def test_create_next_version_defaults_to_generate_origin(project, user):
    version = create_next_version(project=project, prompt="x", created_by=user)

    assert version.origin == ModelVersionOrigin.GENERATE
    assert version.parent_version is None


def test_create_next_version_records_parent_and_origin(project, version, user):
    derived = create_next_version(
        project=project,
        prompt="x",
        created_by=user,
        parent_version=version,
        origin=ModelVersionOrigin.MANUAL,
    )

    assert derived.version == version.version + 1
    assert derived.parent_version_id == version.pk
    assert derived.origin == ModelVersionOrigin.MANUAL


# ---------------------------------------------------------------------------
# Agent regeneration (specification is None)
# ---------------------------------------------------------------------------


def test_regenerate_without_spec_enqueues_agent_with_base(project, version, user, broker):
    regenerate_version(base_version=version, created_by=user)

    assert len(broker["agent"]) == 1
    call = broker["agent"][0]
    assert call["project_id"] == project.pk
    assert call["prompt"] == "make a holder"
    assert call["user_id"] == user.pk
    assert call["base_version_id"] == version.pk
    # ``run_agent_workflow`` declares ``regenerate``: the flag is forwarded
    # unconditionally (no capability probe).
    assert call["regenerate"] is True
    assert broker["render"] == []
    # The agent task creates the derived version; no placeholder row is created.
    assert project.versions.count() == 1


def test_regenerate_flag_is_always_forwarded(version, user, broker, monkeypatch):
    """No signature probe: even a task whose ``run`` lacks the kwarg gets it."""

    def legacy_run(project_id, prompt, user_id=None, base_version_id=None):
        return 1

    monkeypatch.setattr(run_agent_workflow, "run", legacy_run)

    regenerate_version(base_version=version, created_by=user)

    assert broker["agent"][0]["regenerate"] is True


def test_regenerate_with_edited_prompt_overrides_the_base(version, user, broker):
    regenerate_version(base_version=version, prompt="make it bigger", created_by=user)

    assert broker["agent"][0]["prompt"] == "make it bigger"


def test_regenerate_without_created_by_passes_none(version, broker):
    regenerate_version(base_version=version)

    assert broker["agent"][0]["user_id"] is None


def test_regenerate_broker_failure_raises_render_enqueue_error(version, user, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(run_agent_workflow, "delay", boom)

    with pytest.raises(RenderEnqueueError, match="regeneration queue"):
        regenerate_version(base_version=version, created_by=user)


# ---------------------------------------------------------------------------
# Manual specification render (specification given)
# ---------------------------------------------------------------------------


def test_regenerate_with_specification_creates_manual_derived_version(
    project, version, user, broker
):
    regenerate_version(
        base_version=version,
        specification={"object": "cube"},
        created_by=user,
    )

    derived = project.versions.exclude(pk=version.pk).get()
    assert derived.parent_version_id == version.pk
    assert derived.origin == ModelVersionOrigin.MANUAL
    assert derived.specification_json == {"object": "cube"}
    assert derived.prompt == version.prompt
    assert broker["render"] == [derived.pk]
    assert broker["agent"] == []


def test_regenerate_with_specification_uses_the_edited_prompt(project, version, user, broker):
    regenerate_version(
        base_version=version,
        prompt="edited",
        specification={"object": "cube"},
        created_by=user,
    )

    derived = project.versions.exclude(pk=version.pk).get()
    assert derived.prompt == "edited"


def test_regenerate_manual_render_broker_failure_raises(version, user, monkeypatch):
    def boom(pk):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(render_model_stl, "delay", boom)

    with pytest.raises(RenderEnqueueError, match="render queue"):
        regenerate_version(
            base_version=version,
            specification={"object": "cube"},
            created_by=user,
        )
