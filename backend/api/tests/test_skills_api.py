"""Endpoint tests for the skills CRUD API (docs/skills.md 6.)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from skills.models import Skill
from skills.services import create_skill
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture(autouse=True)
def _clear_seeded_skills():
    """Drop the seeded built-ins so list assertions are exact."""
    Skill.objects.all().delete()


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
def client(owner):
    client = APIClient()
    client.force_authenticate(user=owner)
    return client


def test_create_workspace_skill(client, workspace, owner):
    response = client.post(
        "/api/v1/skills/",
        {
            "name": "Cookie cutter",
            "description": "thin wall",
            "kind": "guidance",
            "object_kind": "cookie_cutter",
            "workspace": workspace.pk,
            "tags": ["cookie", "kitchen"],
        },
        format="json",
    )

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["slug"] == "cookie-cutter"
    assert body["created_by"] == owner.pk
    assert body["is_builtin"] is False
    assert set(body["tags"]) == {"cookie", "kitchen"}
    assert Skill.objects.get(pk=body["id"]).workspace_id == workspace.pk


def test_list_excludes_foreign_private_skills(client, workspace, owner, outsider):
    create_skill(name="Mine", workspace=workspace, created_by=owner)
    create_skill(name="Foreign", created_by=outsider)
    public = Skill.objects.create(name="Public", slug="public", is_public=True)
    builtin = Skill.objects.create(name="Seed", slug="seed", is_builtin=True)

    response = client.get("/api/v1/skills/")

    assert response.status_code == 200
    names = {item["name"] for item in response.json()["results"]}
    assert names == {"Mine", "Public", "Seed"}
    assert {public.pk, builtin.pk} <= {item["id"] for item in response.json()["results"]}


def test_list_filters_by_kind_and_query(client, workspace, owner):
    create_skill(name="Cookie cutter", kind="guidance", workspace=workspace, created_by=owner)
    create_skill(name="Phone holder", kind="template", workspace=workspace, created_by=owner)

    by_kind = client.get("/api/v1/skills/?kind=template").json()["results"]
    by_query = client.get("/api/v1/skills/?q=cookie").json()["results"]

    assert [item["name"] for item in by_kind] == ["Phone holder"]
    assert [item["name"] for item in by_query] == ["Cookie cutter"]


def test_update_and_delete_a_workspace_skill(client, workspace, owner):
    skill = create_skill(name="Old", workspace=workspace, created_by=owner)

    patched = client.patch(
        f"/api/v1/skills/{skill.pk}/",
        {"name": "New", "tags": ["metal"]},
        format="json",
    )
    assert patched.status_code == 200, patched.content
    assert patched.json()["name"] == "New"
    assert patched.json()["tags"] == ["metal"]

    deleted = client.delete(f"/api/v1/skills/{skill.pk}/")
    assert deleted.status_code == 204
    assert not Skill.objects.filter(pk=skill.pk).exists()


def test_builtin_skills_are_read_only(client):
    builtin = Skill.objects.create(name="Seed", slug="seed", is_builtin=True)

    patched = client.patch(f"/api/v1/skills/{builtin.pk}/", {"name": "Nope"}, format="json")
    deleted = client.delete(f"/api/v1/skills/{builtin.pk}/")

    assert patched.status_code == 403
    assert deleted.status_code == 403
    builtin.refresh_from_db()
    assert builtin.name == "Seed"


def test_non_member_cannot_create_into_a_foreign_workspace(outsider, workspace):
    client = APIClient()
    client.force_authenticate(user=outsider)

    response = client.post(
        "/api/v1/skills/",
        {"name": "Intruder", "workspace": workspace.pk},
        format="json",
    )

    assert response.status_code == 403
    assert not Skill.objects.filter(name="Intruder").exists()


def test_viewer_cannot_create_a_workspace_skill(workspace, owner):
    viewer = User.objects.create_user(username="viewer", email="v@example.com", password="pw")
    add_member(workspace=workspace, user=viewer, role=WorkspaceRole.VIEWER)
    client = APIClient()
    client.force_authenticate(user=viewer)

    response = client.post(
        "/api/v1/skills/",
        {"name": "Nope", "workspace": workspace.pk},
        format="json",
    )

    assert response.status_code == 403


def test_skills_require_authentication():
    assert APIClient().get("/api/v1/skills/").status_code in (401, 403)
