"""Reference image/note on generation requests (terv.md 27. fejezet).

Prompt-only requests now go through the AI workflow (202): the upload is stored
first and its storage path is handed to ``run_agent_workflow``, which creates the
version itself. The manual ``specification_json`` path still creates the version
synchronously. No broker is contacted (both tasks are stubbed) and the image is
written under a per-test ``MEDIA_ROOT``.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from factories import ProjectFactory
from PIL import Image
from rest_framework.test import APIClient

from agents.tasks import run_agent_workflow
from designs.tasks import render_model_stl
from files.services import LocalStorage

pytestmark = pytest.mark.django_db


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    calls: dict[str, list] = {"render": [], "agent": []}
    monkeypatch.setattr(render_model_stl, "delay", lambda pk: calls["render"].append(pk))
    monkeypatch.setattr(
        run_agent_workflow,
        "delay",
        lambda *args, **kwargs: calls["agent"].append((args, kwargs)),
    )
    return calls


@pytest.fixture
def project():
    return ProjectFactory()


def _auth(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_multipart_prompt_stores_the_reference_upload_for_the_agent(
    project, settings, tmp_path, no_broker
):
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

    assert response.status_code == 202, response.content
    assert response.json() == {"status": "queued", "mode": "agent"}
    assert not project.versions.exists()

    ((args, kwargs),) = no_broker["agent"]
    assert args == (project.pk, "make a holder for this", project.created_by_id)
    assert kwargs["reference_note"] == "70 mm wide"
    stored_name = kwargs["reference_image_name"]
    assert stored_name.startswith(f"reference_uploads/{project.pk}/")
    assert stored_name.endswith("reference.png")
    assert LocalStorage(root=tmp_path).read_bytes(stored_name) == _png_bytes()


def test_prompt_only_json_enqueues_the_agent_without_a_reference(project, no_broker):
    response = _auth(project.created_by).post(
        f"/api/v1/projects/{project.pk}/versions/",
        {"prompt": "plain prompt"},
        format="json",
    )

    assert response.status_code == 202, response.content
    ((args, kwargs),) = no_broker["agent"]
    assert kwargs == {
        "reference_image_name": "",
        "reference_note": "",
        "skill_ids": [],
        "auto_skill_selection": False,
        "clarify_policy": "assume",
    }
    assert not project.versions.exists()


def test_manual_specification_still_creates_the_version_with_a_reference_image(
    project, settings, tmp_path, no_broker
):
    settings.MEDIA_ROOT = str(tmp_path)
    upload = SimpleUploadedFile("reference.png", _png_bytes(), content_type="image/png")

    response = _auth(project.created_by).post(
        f"/api/v1/projects/{project.pk}/versions/",
        {
            "prompt": "make it",
            "specification_json": '{"object": "cube"}',
            "reference_note": "from a photo",
            "reference_image": upload,
        },
        format="multipart",
    )

    assert response.status_code == 201, response.content
    version = project.versions.get()
    assert version.reference_note == "from a photo"
    assert version.reference_image.name.endswith("reference.png")
    assert version.reference_image.storage.exists(version.reference_image.name)
    assert no_broker["render"] == [version.pk]
    assert no_broker["agent"] == []


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
