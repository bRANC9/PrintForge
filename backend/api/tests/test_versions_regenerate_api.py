"""Endpoint tests for ``POST /api/v1/versions/{id}/regenerate/``."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from agents.tasks import run_agent_workflow
from designs.models import ModelVersionOrigin
from designs.services import create_next_version
from designs.tasks import render_model_stl
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
def version(project, owner):
    return create_next_version(project=project, prompt="make a holder", created_by=owner)


@pytest.fixture
def client(owner):
    client = APIClient()
    client.force_authenticate(user=owner)
    return client


@pytest.fixture(autouse=True)
def broker(monkeypatch):
    calls: dict[str, list] = {"agent": [], "render": []}
    monkeypatch.setattr(run_agent_workflow, "delay", lambda **kwargs: calls["agent"].append(kwargs))
    monkeypatch.setattr(render_model_stl, "delay", lambda pk: calls["render"].append(pk))
    return calls


def test_regenerate_queues_the_agent(client, version, broker):
    response = client.post(f"/api/v1/versions/{version.pk}/regenerate/", {}, format="json")

    assert response.status_code == 202
    assert response.json() == {"queued": True, "parent_version": version.pk}
    assert broker["agent"][0]["base_version_id"] == version.pk
    assert broker["agent"][0]["prompt"] == version.prompt
    assert broker["render"] == []


def test_regenerate_accepts_an_edited_prompt(client, version, broker):
    response = client.post(
        f"/api/v1/versions/{version.pk}/regenerate/", {"prompt": "bigger"}, format="json"
    )

    assert response.status_code == 202
    assert broker["agent"][0]["prompt"] == "bigger"


def test_regenerate_with_specification_creates_a_manual_version(client, project, version, broker):
    response = client.post(
        f"/api/v1/versions/{version.pk}/regenerate/",
        {"specification_json": {"object": "cube"}},
        format="json",
    )

    assert response.status_code == 202
    derived = project.versions.exclude(pk=version.pk).get()
    assert derived.parent_version_id == version.pk
    assert derived.origin == ModelVersionOrigin.MANUAL
    assert broker["render"] == [derived.pk]
    assert broker["agent"] == []


def test_regenerate_returns_503_when_broker_is_down(client, version, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(run_agent_workflow, "delay", boom)

    response = client.post(f"/api/v1/versions/{version.pk}/regenerate/", {}, format="json")

    assert response.status_code == 503
    assert "queue" in response.json()["detail"].lower()


def test_regenerate_non_member_gets_404(outsider, version):
    client = APIClient()
    client.force_authenticate(user=outsider)

    response = client.post(f"/api/v1/versions/{version.pk}/regenerate/", {}, format="json")

    assert response.status_code == 404


def test_regenerate_viewer_is_forbidden(workspace, version):
    viewer = User.objects.create_user(username="viewer", email="v@example.com", password="pw")
    add_member(workspace=workspace, user=viewer, role=WorkspaceRole.VIEWER)
    client = APIClient()
    client.force_authenticate(user=viewer)

    response = client.post(f"/api/v1/versions/{version.pk}/regenerate/", {}, format="json")

    assert response.status_code == 403


def test_regenerate_requires_authentication(version):
    response = APIClient().post(f"/api/v1/versions/{version.pk}/regenerate/", {}, format="json")

    assert response.status_code in (401, 403)


def test_version_serializer_exposes_origin_and_parent(client, project, version, owner):
    derived = create_next_version(
        project=project,
        prompt="x",
        created_by=owner,
        parent_version=version,
        origin=ModelVersionOrigin.REGENERATE,
    )

    response = client.get(f"/api/v1/versions/{derived.pk}/")

    assert response.status_code == 200
    body = response.json()
    assert body["origin"] == "regenerate"
    assert body["parent_version"] == version.pk
