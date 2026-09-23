"""Endpoint tests for the print-job slicing preview.

``GET /api/v1/print-jobs/{id}/preview/`` streams the PNG the slicer worker
records under ``PrintJob.slicing_json["preview"]`` through the storage backend;
it answers ``404`` when no preview (or no file) exists, and is workspace-scoped
like the other ``PrintJob`` actions. The serializer exposes the read-only
``has_preview`` / ``preview_url`` fields built from that endpoint.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from designs.services import create_next_version
from files.services import LocalStorage
from printers.models import Printer
from printers.services import enqueue_job
from projects.services import create_project
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()

PNG = b"\x89PNG\r\n\x1a\npreview-bytes"
PREVIEW_PATH = "print_jobs/previews/job.png"


def make_user(username, **extra):
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="pw",
        **extra,
    )


def auth(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def owner():
    return make_user("owner")


@pytest.fixture
def outsider():
    return make_user("outsider")


@pytest.fixture
def workspace(owner):
    return create_workspace(name="Lab", owner=owner)


@pytest.fixture
def project(workspace, owner):
    return create_project(workspace=workspace, name="Holder", created_by=owner)


@pytest.fixture
def version(project, owner):
    return create_next_version(project=project, prompt="make it", created_by=owner)


@pytest.fixture
def printer():
    return Printer.objects.create(name="K2 Pro")


@pytest.fixture
def job(project, version, printer, owner):
    return enqueue_job(
        project=project,
        model_version=version,
        printer=printer,
        created_by=owner,
    )


def _store_preview(job, tmp_path, *, data: bytes = PNG, path: str = PREVIEW_PATH) -> None:
    """Write the preview to ``tmp_path`` and record its path on ``job``."""
    LocalStorage(root=tmp_path).write_bytes(path, data)
    job.slicing_json = {"preview": path}
    job.save(update_fields=["slicing_json"])


def test_preview_streams_the_png_when_present(owner, job, tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        _store_preview(job, tmp_path)
        response = auth(owner).get(f"/api/v1/print-jobs/{job.pk}/preview/")

    assert response.status_code == 200
    assert response.content == PNG
    assert response["Content-Type"] == "image/png"
    assert response["Content-Disposition"].startswith("inline")
    assert "job.png" in response["Content-Disposition"]


def test_preview_is_404_when_no_preview_is_recorded(owner, job):
    response = auth(owner).get(f"/api/v1/print-jobs/{job.pk}/preview/")

    assert response.status_code == 404


def test_preview_is_404_when_the_recorded_file_is_missing(owner, job, tmp_path):
    job.slicing_json = {"preview": PREVIEW_PATH}
    job.save(update_fields=["slicing_json"])

    with override_settings(MEDIA_ROOT=str(tmp_path)):
        response = auth(owner).get(f"/api/v1/print-jobs/{job.pk}/preview/")

    assert response.status_code == 404


def test_preview_ignores_a_non_string_preview_value(owner, job):
    job.slicing_json = {"preview": {"unexpected": True}}
    job.save(update_fields=["slicing_json"])

    assert auth(owner).get(f"/api/v1/print-jobs/{job.pk}/preview/").status_code == 404


def test_preview_requires_authentication(job):
    response = APIClient().get(f"/api/v1/print-jobs/{job.pk}/preview/")

    assert response.status_code in (401, 403)


def test_preview_is_404_for_a_non_member(outsider, job, tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        _store_preview(job, tmp_path)
        response = auth(outsider).get(f"/api/v1/print-jobs/{job.pk}/preview/")

    assert response.status_code == 404


def test_viewer_can_read_the_preview(workspace, job, tmp_path):
    viewer = make_user("viewer")
    add_member(workspace=workspace, user=viewer, role=WorkspaceRole.VIEWER)

    with override_settings(MEDIA_ROOT=str(tmp_path)):
        _store_preview(job, tmp_path)
        response = auth(viewer).get(f"/api/v1/print-jobs/{job.pk}/preview/")

    assert response.status_code == 200
    assert response.content == PNG


def test_serializer_exposes_has_preview_and_preview_url(owner, job, tmp_path):
    detail = auth(owner).get(f"/api/v1/print-jobs/{job.pk}/")
    assert detail.status_code == 200
    assert detail.json()["has_preview"] is False
    assert detail.json()["preview_url"] is None

    with override_settings(MEDIA_ROOT=str(tmp_path)):
        _store_preview(job, tmp_path)
        detail = auth(owner).get(f"/api/v1/print-jobs/{job.pk}/")

    body = detail.json()
    assert body["has_preview"] is True
    assert body["preview_url"] == f"/api/v1/print-jobs/{job.pk}/preview/"
