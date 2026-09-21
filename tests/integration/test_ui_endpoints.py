"""UI-supporting API flows for the create-workspace / create-project forms.

The forms on ``/projects/`` POST to the JSON API, so this pins the exact flow
they depend on: any authenticated user may create a workspace (and becomes its
OWNER), a MEMBER may create a project inside it, and the new project shows up in
the scoped list. A VIEWER may not create projects.
"""

from __future__ import annotations

import pytest
from factories import UserFactory, WorkspaceFactory
from rest_framework.test import APIClient

from projects.models import Project
from workspaces.models import WorkspaceRole
from workspaces.services import add_member

pytestmark = pytest.mark.django_db


def auth(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_authenticated_user_can_create_a_workspace_and_project_then_list_it():
    user = UserFactory()
    client = auth(user)

    workspace = client.post("/api/v1/workspaces/", {"name": "Studio"}, format="json")
    assert workspace.status_code == 201, workspace.content
    workspace_body = workspace.json()
    assert workspace_body["owner"] == user.id
    workspace_id = workspace_body["id"]

    # The new workspace is immediately visible to its creator.
    workspaces = client.get("/api/v1/workspaces/").json()["results"]
    assert [item["id"] for item in workspaces] == [workspace_id]

    project = client.post(
        "/api/v1/projects/",
        {"workspace": workspace_id, "name": "Phone holder", "description": "from the form"},
        format="json",
    )
    assert project.status_code == 201, project.content
    assert project.json()["created_by"] == user.id

    listing = client.get("/api/v1/projects/").json()["results"]
    assert "Phone holder" in [item["name"] for item in listing]


def test_viewer_cannot_create_a_project(workspace):
    viewer = UserFactory()
    add_member(workspace=workspace, user=viewer, role=WorkspaceRole.VIEWER)

    response = auth(viewer).post(
        "/api/v1/projects/",
        {"workspace": workspace.id, "name": "Nope"},
        format="json",
    )

    assert response.status_code == 403
    assert not Project.objects.filter(name="Nope").exists()


def test_viewer_can_still_create_their_own_workspace():
    # Workspace creation is open to every authenticated user (they become its
    # OWNER); being a VIEWER of someone else's workspace does not block it.
    user = UserFactory()
    foreign = WorkspaceFactory()
    add_member(workspace=foreign, user=user, role=WorkspaceRole.VIEWER)

    response = auth(user).post("/api/v1/workspaces/", {"name": "My own"}, format="json")

    assert response.status_code == 201
    assert response.json()["owner"] == user.id
