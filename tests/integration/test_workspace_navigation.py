"""Workspace-first navigation routes and the ``?workspace=`` API filter.

Pins the docs/workspace-navigation.md contract end to end: the home page is the
workspace list, ``/workspaces/<id>/`` renders the workspace detail, the old flat
``/projects/`` list redirects home, and the projects API can be narrowed to one
workspace without weakening the membership scope.
"""

from __future__ import annotations

import pytest
from django.test import Client as DjangoClient
from factories import ProjectFactory, UserFactory, WorkspaceFactory
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def test_home_is_the_workspace_list_for_authenticated_users():
    client = DjangoClient()
    client.force_login(UserFactory())

    response = client.get("/")

    assert response.status_code == 200
    assert response.templates[0].name == "workspaces/list.html"


def test_home_requires_login():
    response = DjangoClient().get("/")

    assert response.status_code == 302
    assert "/login/" in response.url


def test_workspace_detail_page_passes_the_workspace_id():
    user = UserFactory()
    workspace = WorkspaceFactory(owner=user)
    client = DjangoClient()
    client.force_login(user)

    response = client.get(f"/workspaces/{workspace.pk}/")

    assert response.status_code == 200
    assert response.context["workspace_id"] == workspace.pk


def test_old_projects_list_redirects_to_home():
    response = DjangoClient().get("/projects/")

    assert response.status_code == 302
    assert response.url == "/"


def test_projects_api_can_be_filtered_by_workspace():
    user = UserFactory()
    first = WorkspaceFactory(owner=user)
    second = WorkspaceFactory(owner=user)
    project_a = ProjectFactory(workspace=first, created_by=user)
    project_b = ProjectFactory(workspace=second, created_by=user)

    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get(f"/api/v1/projects/?workspace={first.pk}")

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["results"]}
    assert ids == {project_a.pk}
    assert project_b.pk not in ids


def test_projects_api_workspace_filter_keeps_membership_scope():
    user = UserFactory()
    foreign = WorkspaceFactory()  # owned by someone else
    ProjectFactory(workspace=foreign)

    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get(f"/api/v1/projects/?workspace={foreign.pk}")

    assert response.status_code == 200
    assert response.json()["results"] == []
