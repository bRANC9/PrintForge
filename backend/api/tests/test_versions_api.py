"""Endpoint tests for the version / agent-run JSON API contract."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from agents.models import AgentRun
from agents.tasks import run_agent_workflow
from designs.services import create_next_version
from designs.tasks import render_model_stl
from files.services import LocalStorage
from projects.services import create_project
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


@pytest.fixture
def client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def project(user):
    workspace = create_workspace(name="Lab", owner=user)
    return create_project(workspace=workspace, name="Holder", created_by=user)


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    """Never talk to a real Celery broker during these tests.

    Records both enqueue paths: ``render`` (manual specification) and ``agent``
    (prompt-only AI workflow).
    """
    calls: dict[str, list] = {"render": [], "agent": []}
    monkeypatch.setattr(render_model_stl, "delay", lambda pk: calls["render"].append(pk))
    monkeypatch.setattr(
        run_agent_workflow,
        "delay",
        lambda *args, **kwargs: calls["agent"].append((args, kwargs)),
    )
    return calls


def test_project_create_takes_created_by_from_session(client, user):
    workspace = create_workspace(name="Lab", owner=user)
    response = client.post(
        "/api/v1/projects/",
        {"workspace": workspace.pk, "name": "Bracket", "created_by": 999999},
        format="json",
    )
    assert response.status_code == 201
    assert response.json()["created_by"] == user.pk


def test_versions_are_listed_newest_first(client, project, user):
    create_next_version(project=project, prompt="v1", created_by=user)
    create_next_version(project=project, prompt="v2", created_by=user)

    response = client.get(f"/api/v1/projects/{project.pk}/versions/")

    assert response.status_code == 200
    body = response.json()
    assert [item["version"] for item in body] == [2, 1]
    assert body[0]["prompt"] == "v2"


def test_create_version_with_specification_renders_directly(client, project, no_broker):
    response = client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "make it", "specification_json": {"object": "cube"}},
        format="json",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["version"] == 1
    assert body["prompt"] == "make it"
    assert body["created_by"] is not None
    assert body["specification_json"] == {"object": "cube"}
    assert no_broker["render"] == [body["id"]]
    assert no_broker["agent"] == []

    status_response = client.get(f"/api/v1/versions/{body['id']}/status/")
    assert status_response.json() == {"status": "queued", "stage": "queued", "errors": []}


def test_prompt_only_version_enqueues_the_agent(client, project, user, no_broker):
    response = client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "make it"},
        format="json",
    )

    assert response.status_code == 202
    assert response.json() == {"status": "queued", "mode": "agent"}
    # No version is created here: the agent creates it when it finishes.
    assert not project.versions.exists()
    assert no_broker["render"] == []
    assert no_broker["agent"] == [
        (
            (project.pk, "make it", user.pk),
            {
                "reference_image_name": "",
                "reference_note": "",
                "skill_ids": [],
                "auto_skill_selection": False,
                "clarify_policy": "assume",
            },
        )
    ]


def test_create_version_requires_authentication(project):
    response = APIClient().post(
        f"/api/v1/projects/{project.pk}/versions/", {"prompt": "x"}, format="json"
    )
    assert response.status_code in (401, 403)


def test_create_version_returns_503_when_broker_is_down(client, project, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(run_agent_workflow, "delay", boom)

    response = client.post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "make it"},
        format="json",
    )

    assert response.status_code == 503
    assert "queue" in response.json()["detail"].lower()
    assert not project.versions.exists()


def test_version_detail_exposes_specification_and_validation(client, project, user):
    version = create_next_version(
        project=project,
        prompt="make it",
        created_by=user,
        specification={"object": "cube"},
    )

    response = client.get(f"/api/v1/versions/{version.pk}/")

    assert response.status_code == 200
    body = response.json()
    assert body["specification_json"] == {"object": "cube"}
    assert body["validation_json"] == {}
    assert body["status"] == "pending"


def test_artifact_is_streamed_through_storage(client, project, user, tmp_path):
    version = create_next_version(project=project, prompt="make it", created_by=user)
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        storage = LocalStorage(root=tmp_path)
        relative = f"projects/{project.pk}/v{version.version}/model.scad"
        storage.write_bytes(relative, b"cube([1,1,1]);")
        version.scad_file.name = relative
        version.save(update_fields=["scad_file"])

        response = client.get(f"/api/v1/versions/{version.pk}/artifact/scad/")

    assert response.status_code == 200
    assert response.content == b"cube([1,1,1]);"
    assert response["Content-Type"].startswith("text/plain")
    assert "model.scad" in response["Content-Disposition"]


def test_missing_artifact_returns_404(client, project, user):
    version = create_next_version(project=project, prompt="make it", created_by=user)

    response = client.get(f"/api/v1/versions/{version.pk}/artifact/stl/")
    assert response.status_code == 404

    invalid_kind = client.get(f"/api/v1/versions/{version.pk}/artifact/nope/")
    assert invalid_kind.status_code == 404


def test_agent_runs_list_and_detail(client, project):
    run = AgentRun.objects.create(project=project, user_prompt="hello")

    list_response = client.get("/api/v1/agent-runs/")
    assert list_response.status_code == 200
    results = list_response.json()["results"]
    assert [item["id"] for item in results] == [run.pk]

    detail_response = client.get(f"/api/v1/agent-runs/{run.pk}/")
    assert detail_response.status_code == 200
    assert detail_response.json()["user_prompt"] == "hello"
