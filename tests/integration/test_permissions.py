"""Permission / ownership tests (terv.md 20., 26.3).

Covers the Phase-1 contract:

* anonymous callers may read but never write;
* ``owner`` / ``created_by`` are taken from ``request.user`` and can never be
  forged through the request payload (they are read-only at the serializer and
  set by the view / service layer);
* workspace membership and role basics behind ``workspaces.services``.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from factories import UserFactory, WorkspaceFactory

from api.serializers import ProjectSerializer, WorkspaceSerializer
from projects.models import Project
from workspaces.models import Workspace, WorkspaceMember, WorkspaceRole
from workspaces.services import add_member, create_workspace, workspaces_for_user

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Anonymous access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url", "data"),
    [
        ("post", "/api/v1/workspaces/", {"name": "Intruder"}),
        ("put", "/api/v1/workspaces/{ws}/", {"name": "Intruder"}),
        ("patch", "/api/v1/workspaces/{ws}/", {"name": "Intruder"}),
        ("delete", "/api/v1/workspaces/{ws}/", None),
        ("post", "/api/v1/projects/", {"workspace": "{ws}", "name": "Intruder"}),
        ("put", "/api/v1/projects/{proj}/", {"workspace": "{ws}", "name": "Intruder"}),
        ("patch", "/api/v1/projects/{proj}/", {"name": "Intruder"}),
        ("delete", "/api/v1/projects/{proj}/", None),
    ],
)
def test_unauthenticated_writes_are_rejected(api_client, workspace, project, method, url, data):
    url = url.format(ws=workspace.id, proj=project.id)

    response = getattr(api_client, method)(url, data, format="json")

    assert response.status_code in (401, 403), response.content
    assert Workspace.objects.filter(name="Intruder").exists() is False
    assert Project.objects.filter(name="Intruder").exists() is False
    assert Workspace.objects.filter(pk=workspace.pk).exists()
    assert Project.objects.filter(pk=project.pk).exists()


def test_unauthenticated_reads_are_allowed(api_client, workspace, project):
    for url in ("/api/v1/workspaces/", "/api/v1/projects/"):
        response = api_client.get(url)
        assert response.status_code == 200

    assert api_client.get(f"/api/v1/workspaces/{workspace.id}/").status_code == 200
    assert api_client.get(f"/api/v1/projects/{project.id}/").status_code == 200


# ---------------------------------------------------------------------------
# owner / created_by come from request.user, never the payload
# ---------------------------------------------------------------------------


def test_workspace_create_sets_owner_from_request_user(auth_client, user, other_user):
    response = auth_client.post(
        "/api/v1/workspaces/",
        {"name": "Lab", "owner": other_user.id},
        format="json",
    )

    assert response.status_code == 201, response.content
    payload = response.json()
    assert payload["owner"] == user.id

    created = Workspace.objects.get(pk=payload["id"])
    assert created.owner_id == user.id
    assert created.owner_id != other_user.id
    # create_workspace also records the owner as an OWNER member
    assert created.members.get(user=user).role == WorkspaceRole.OWNER


def test_project_create_sets_created_by_from_request_user(auth_client, user, workspace, other_user):
    response = auth_client.post(
        "/api/v1/projects/",
        {"workspace": workspace.id, "name": "Bracket", "created_by": other_user.id},
        format="json",
    )

    assert response.status_code == 201, response.content
    payload = response.json()
    assert payload["created_by"] == user.id

    created = Project.objects.get(pk=payload["id"])
    assert created.created_by_id == user.id
    assert created.created_by_id != other_user.id
    assert created.workspace_id == workspace.id


def test_workspace_update_ignores_owner_from_payload(auth_client, user, workspace, other_user):
    response = auth_client.patch(
        f"/api/v1/workspaces/{workspace.id}/",
        {"name": "Renamed", "owner": other_user.id},
        format="json",
    )

    assert response.status_code == 200, response.content
    workspace.refresh_from_db()
    assert workspace.name == "Renamed"
    assert workspace.owner_id == user.id


def test_project_update_ignores_created_by_from_payload(auth_client, user, project, other_user):
    response = auth_client.patch(
        f"/api/v1/projects/{project.id}/",
        {"name": "Renamed", "created_by": other_user.id},
        format="json",
    )

    assert response.status_code == 200, response.content
    project.refresh_from_db()
    assert project.name == "Renamed"
    assert project.created_by_id == user.id


def test_workspace_serializer_marks_owner_read_only(workspace, other_user):
    serializer = WorkspaceSerializer(
        workspace, data={"name": "x", "owner": other_user.id}, partial=True
    )

    assert serializer.is_valid(), serializer.errors
    assert "owner" not in serializer.validated_data


def test_project_serializer_marks_created_by_read_only(project, other_user):
    serializer = ProjectSerializer(
        project, data={"name": "x", "created_by": other_user.id}, partial=True
    )

    assert serializer.is_valid(), serializer.errors
    assert "created_by" not in serializer.validated_data


# ---------------------------------------------------------------------------
# Workspace membership / role basics
# ---------------------------------------------------------------------------


def test_create_workspace_adds_owner_membership():
    user = UserFactory()
    workspace = create_workspace(name="Lab", owner=user)

    member = workspace.members.get(user=user)
    assert member.role == WorkspaceRole.OWNER


def test_add_member_defaults_to_member(workspace, other_user):
    member = add_member(workspace=workspace, user=other_user)

    assert member.role == WorkspaceRole.MEMBER
    assert workspace.members.filter(user=other_user).count() == 1


def test_add_member_updates_role_without_creating_a_duplicate(workspace, other_user):
    add_member(workspace=workspace, user=other_user, role=WorkspaceRole.VIEWER)
    add_member(workspace=workspace, user=other_user, role=WorkspaceRole.ADMIN)

    assert workspace.members.filter(user=other_user).count() == 1
    assert workspace.members.get(user=other_user).role == WorkspaceRole.ADMIN


def test_workspace_membership_is_unique_per_user(workspace, other_user):
    WorkspaceMember.objects.create(workspace=workspace, user=other_user)

    with pytest.raises(IntegrityError), transaction.atomic():
        WorkspaceMember.objects.create(workspace=workspace, user=other_user)


def test_workspaces_for_user_only_returns_memberships(user):
    mine = WorkspaceFactory(owner=user)
    WorkspaceFactory(owner=UserFactory())  # owned by someone else, not a member

    assert set(workspaces_for_user(user)) == {mine}
