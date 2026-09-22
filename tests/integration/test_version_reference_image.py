"""Optional reference image/note on version creation (terv.md 27. fejezet).

The upload path is multipart; the JSON path must keep working unchanged. No
broker is contacted (the render task is stubbed) and the image is written under
a per-test ``MEDIA_ROOT``.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from factories import ProjectFactory
from PIL import Image
from rest_framework.test import APIClient

from designs.tasks import render_model_stl

pytestmark = pytest.mark.django_db


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    queued: list[int] = []
    monkeypatch.setattr(render_model_stl, "delay", queued.append)
    return queued


@pytest.fixture
def project():
    return ProjectFactory()


def _auth(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_multipart_version_create_stores_reference_image_and_note(project, settings, tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile

    settings.MEDIA_ROOT = str(tmp_path)
    upload = SimpleUploadedFile("reference.png", _png_bytes(), content_type="image/png")

    response = _auth(project.created_by).post(
        f"/api/v1/projects/{project.pk}/versions/",
        {
            "prompt": "make a holder for this",
            "reference_note": "70 mm wide",
            "reference_image": upload,
        },
        format="multipart",
    )

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["reference_note"] == "70 mm wide"
    assert body["reference_image"]  # a storage URL was produced

    version = project.versions.get()
    assert version.reference_note == "70 mm wide"
    assert version.reference_image.name.endswith("reference.png")
    assert version.reference_image.storage.exists(version.reference_image.name)


def test_json_version_create_leaves_reference_fields_empty(project):
    response = _auth(project.created_by).post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "plain prompt"},
        format="json",
    )

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["reference_note"] == ""
    assert body["reference_image"] is None

    version = project.versions.get()
    assert not version.reference_image
    assert version.reference_note == ""


def test_multipart_without_image_is_still_accepted(project, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)

    response = _auth(project.created_by).post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "text only", "reference_note": "no photo"},
        format="multipart",
    )

    assert response.status_code == 201, response.content
    version = project.versions.get()
    assert version.reference_note == "no photo"
    assert not version.reference_image


def test_version_detail_exposes_reference_fields(project, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    version = project.versions.create(
        version=1,
        prompt="x",
        reference_note="from the detail view",
        created_by=project.created_by,
    )

    response = _auth(project.created_by).get(f"/api/v1/versions/{version.pk}/")

    assert response.status_code == 200
    body = response.json()
    assert body["reference_note"] == "from the detail view"
    assert "reference_image" in body
    assert body["reference_image"] is None
