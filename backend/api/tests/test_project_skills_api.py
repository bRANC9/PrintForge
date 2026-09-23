"""Endpoint tests for the writable ``skills`` field on projects (docs/skills.md 2.).

A project carries a persistent skill assignment (``Project.skills``). The field
is writable through ``/api/v1/projects/`` but scoped to the skills the caller
can actually see: built-ins + public + the caller's workspace skills.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from projects.models import Project
from projects.services import create_project
from skills.models import Skill
from skills.services import create_skill
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture(autouse=True)
def _clear_seeded_skills():
    """Drop the seeded built-ins so visibility assertions are exact."""
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
def project(workspace, owner):
    return create_project(workspace=workspace, name="Holder", created_by=owner)


@pytest.fixture
def client(owner):
    client = APIClient()
    client.force_authenticate(user=owner)
    return client


def test_project_create_attaches_a_workspace_skill(client, workspace, owner):
    skill = create_skill(name="Cookie cutter", workspace=workspace, created_by=owner)

    response = client.post(
        "/api/v1/projects/",
        {"workspace": workspace.pk, "name": "Cutter", "skills": [skill.pk]},
        format="json",
    )

    assert response.status_code == 201, response.content
    assert response.json()["skills"] == [skill.pk]
    project = Project.objects.get(pk=response.json()["id"])
    assert list(project.skills.values_list("pk", flat=True)) == [skill.pk]


def test_project_update_replaces_skills(client, workspace, owner, project):
    first = create_skill(name="First", workspace=workspace, created_by=owner)
    second = create_skill(name="Second", workspace=workspace, created_by=owner)
    project.skills.set([first])

    response = client.patch(
        f"/api/v1/projects/{project.pk}/",
        {"skills": [second.pk]},
        format="json",
    )

    assert response.status_code == 200, response.content
    assert list(project.skills.values_list("pk", flat=True)) == [second.pk]
    assert response.json()["skills"] == [second.pk]


def test_project_can_attach_builtin_and_public_skills(client, workspace, owner):
    builtin = Skill.objects.create(name="Seed", slug="seed", is_builtin=True)
    public = Skill.objects.create(name="Shared", slug="shared", is_public=True)

    response = client.post(
        "/api/v1/projects/",
        {
            "workspace": workspace.pk,
            "name": "Bracket",
            "skills": [builtin.pk, public.pk],
        },
        format="json",
    )

    assert response.status_code == 201, response.content
    assert set(response.json()["skills"]) == {builtin.pk, public.pk}


def test_project_cannot_attach_a_foreign_private_skill(client, workspace, owner, outsider):
    foreign_workspace = create_workspace(name="Other", owner=outsider)
    foreign = create_skill(name="Foreign", workspace=foreign_workspace, created_by=outsider)

    response = client.post(
        "/api/v1/projects/",
        {"workspace": workspace.pk, "name": "Intruder", "skills": [foreign.pk]},
        format="json",
    )

    assert response.status_code == 400
    assert "skills" in response.json()
    assert not Project.objects.filter(name="Intruder").exists()


def test_project_cannot_update_to_a_foreign_private_skill(
    client, workspace, owner, outsider, project
):
    foreign_workspace = create_workspace(name="Other", owner=outsider)
    foreign = create_skill(name="Foreign", workspace=foreign_workspace, created_by=outsider)

    response = client.patch(
        f"/api/v1/projects/{project.pk}/",
        {"skills": [foreign.pk]},
        format="json",
    )

    assert response.status_code == 400
    assert not project.skills.exists()


def test_project_skills_default_to_empty(client, workspace):
    response = client.post(
        "/api/v1/projects/",
        {"workspace": workspace.pk, "name": "Plain"},
        format="json",
    )

    assert response.status_code == 201, response.content
    assert response.json()["skills"] == []
