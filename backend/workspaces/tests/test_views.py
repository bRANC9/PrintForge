"""Tests for the workspace-first navigation pages (docs/workspace-navigation.md)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from projects.services import create_project
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


@pytest.fixture
def workspace(user):
    return create_workspace(name="Lab", owner=user)


@pytest.fixture
def auth_client(user):
    client = Client()
    client.force_login(user)
    return client


def test_home_redirects_anonymous_to_login():
    response = Client().get("/")

    assert response.status_code == 302
    assert "/login/" in response.url


def test_home_renders_the_workspace_list(auth_client):
    response = auth_client.get("/")

    assert response.status_code == 200
    assert "Workspaces" in response.content.decode()
    assert response.templates[0].name == "workspaces/list.html"


def test_workspace_detail_renders_and_passes_the_id(auth_client, workspace):
    response = auth_client.get(f"/workspaces/{workspace.pk}/")

    assert response.status_code == 200
    assert response.context["workspace_id"] == workspace.pk
    assert response.templates[0].name == "workspaces/detail.html"


def test_workspace_detail_requires_login(workspace):
    response = Client().get(f"/workspaces/{workspace.pk}/")

    assert response.status_code == 302
    assert "/login/" in response.url


def test_old_projects_list_redirects_home():
    response = Client().get("/projects/")

    assert response.status_code == 302
    assert response.url == "/"


def test_project_detail_is_still_routed(user):
    workspace = create_workspace(name="Lab", owner=user)
    project = create_project(workspace=workspace, name="Holder", created_by=user)

    response = Client().get(f"/projects/{project.pk}/")

    assert response.status_code == 200
    assert response.templates[0].name == "projects/detail.html"
