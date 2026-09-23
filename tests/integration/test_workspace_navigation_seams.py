"""Workspace-first navigation edge cases (docs/workspace-navigation.md).

The main contract (home = workspace list, ``/workspaces/<id>/`` detail, the old
``/projects/`` redirect and the ``?workspace=`` API scoping) is pinned by
``tests/integration/test_workspace_navigation.py``. This file only adds the
remaining edges: the workspace detail page is login-protected and a malformed
``?workspace=`` filter degrades to an empty result set instead of erroring.
"""

from __future__ import annotations

import pytest
from django.test import Client as DjangoClient
from factories import ProjectFactory, UserFactory, WorkspaceFactory
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def test_workspace_detail_requires_login():
    workspace = WorkspaceFactory()

    response = DjangoClient().get(f"/workspaces/{workspace.pk}/")

    assert response.status_code == 302
    assert "/login/" in response.url


def test_non_numeric_workspace_filter_returns_no_projects_but_keeps_membership():
    user = UserFactory()
    workspace = WorkspaceFactory(owner=user)
    ProjectFactory(workspace=workspace, created_by=user)
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get("/api/v1/projects/?workspace=not-a-number")

    assert response.status_code == 200
    assert response.json()["results"] == []


def test_workspace_list_api_only_returns_the_callers_workspaces():
    user = UserFactory()
    mine = WorkspaceFactory(owner=user)
    WorkspaceFactory()  # owned by someone else
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get("/api/v1/workspaces/")

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["results"]}
    assert ids == {mine.pk}
