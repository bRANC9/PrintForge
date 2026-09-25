import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from projects.services import create_project
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


def test_health_is_public():
    response = APIClient().get("/api/v1/health/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@override_settings(APP_GIT_SHA="0123456789abcdef0123456789abcdef01234567")
def test_health_reports_the_baked_in_commit():
    """The health probe doubles as the "which build is this?" endpoint."""
    response = APIClient().get("/api/v1/health/")
    assert response.json() == {
        "status": "ok",
        "git_sha": "0123456789abcdef0123456789abcdef01234567",
    }


@override_settings(APP_GIT_SHA="d767c82aaaaa")
def test_page_footer_shows_the_build_commit():
    """The footer shows the short SHA so a deployment is identifiable in the UI."""
    response = APIClient().get("/login/")
    assert response.status_code == 200
    assert "build: d767c82aaaaa" in response.content.decode()


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
