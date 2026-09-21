import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from projects.services import create_project
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


def test_health_is_public():
    response = APIClient().get("/api/v1/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_creating_workspace_requires_authentication():
    response = APIClient().post("/api/v1/workspaces/", {"name": "x"}, format="json")
    assert response.status_code in (401, 403)


def test_authenticated_user_can_list_projects():
    user = User.objects.create_user(username="ada", email="ada@example.com", password="pw")
    workspace = create_workspace(name="Lab", owner=user)
    create_project(workspace=workspace, name="Bracket", created_by=user)

    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get("/api/v1/projects/")

    assert response.status_code == 200
    names = [item["name"] for item in response.json()["results"]]
    assert "Bracket" in names
