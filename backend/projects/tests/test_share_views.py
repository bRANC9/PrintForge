"""Tests for the public token share pages (``/share/<token>/``)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from designs.services import create_next_version
from files.services import get_storage
from projects.models import ProjectShare
from projects.services import create_project, create_share
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def owner():
    return User.objects.create_user(username="owner", email="owner@example.com", password="pw")


@pytest.fixture
def project(owner):
    workspace = create_workspace(name="Lab", owner=owner)
    return create_project(workspace=workspace, name="Holder", created_by=owner)


def _token_link(project, owner):
    return create_share(project, create_link=True, created_by=owner)


def test_share_page_renders_for_a_valid_token(project, owner):
    share = _token_link(project, owner)

    response = Client().get(f"/share/{share.token}/")

    assert response.status_code == 200
    assert response.templates[0].name == "projects/shared.html"
    assert response.context["project"] == project


def test_share_page_unknown_token_404(project):
    response = Client().get("/share/does-not-exist/")

    assert response.status_code == 404


def test_share_page_expired_token_404(project, owner):
    share = create_share(
        project,
        create_link=True,
        created_by=owner,
        expires_at=timezone.now() - timedelta(hours=1),
    )

    response = Client().get(f"/share/{share.token}/")

    assert response.status_code == 404


def test_share_page_ignores_user_scoped_tokens(project, owner):
    """Only anonymous token rows are public links, even if a token is set."""
    share = ProjectShare.objects.create(
        project=project, shared_with=owner, token="user-token", created_by=owner
    )

    response = Client().get(f"/share/{share.token}/")

    assert response.status_code == 404


def test_share_download_without_stl_404(project, owner):
    share = _token_link(project, owner)

    response = Client().get(f"/share/{share.token}/stl/")

    assert response.status_code == 404


def test_share_download_streams_the_latest_stl(project, owner):
    share = _token_link(project, owner)
    version = create_next_version(project=project, prompt="make it", created_by=owner)
    relative_path = f"projects/{project.pk}/v{version.version}/model.stl"
    get_storage().write_bytes(relative_path, b"solid test")
    version.stl_file.name = relative_path
    version.save(update_fields=["stl_file"])

    response = Client().get(f"/share/{share.token}/stl/")

    assert response.status_code == 200
    assert response["Content-Type"] == "model/stl"
    assert response.content == b"solid test"
