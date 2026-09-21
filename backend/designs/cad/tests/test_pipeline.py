"""Integration tests for the CAD pipeline orchestration + Celery task."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from designs import tasks
from designs.cad.base import GeneratedModel
from designs.cad.pipeline import render_version
from designs.services import create_next_version
from files.services import LocalStorage
from projects.services import create_project
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()

SPEC = {
    "object": "phone_holder",
    "dimensions": {"width": 70.6, "height": 147, "thickness": 7.6},
    "angle": 15,
    "wall_thickness": 4,
    "mounting": {"type": "M5", "count": 2},
}

FAKE_SCAD = b"// fake scad\ncube([1, 1, 1]);\n"
FAKE_STL = b"solid fake\nendsolid fake\n"


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.exported_format: str | None = None

    def generate(self, specification):  # noqa: ANN001
        return FAKE_SCAD.decode()

    def validate(self, model: GeneratedModel) -> list[str]:
        return []

    def export(self, model: GeneratedModel, format: str) -> bytes:
        self.exported_format = format
        return FAKE_STL


@pytest.fixture
def version():
    user = User.objects.create_user(username="ada", email="ada@example.com", password="pw")
    workspace = create_workspace(name="Lab", owner=user)
    project = create_project(workspace=workspace, name="Holder", created_by=user)
    return create_next_version(
        project=project, prompt="make it", created_by=user, specification=SPEC
    )


def test_render_version_stores_artifacts(version, tmp_path):
    backend = FakeBackend()
    storage = LocalStorage(root=tmp_path)

    result = render_version(version, backend=backend, storage=storage)

    version.refresh_from_db()
    assert result["status"] == "done"
    assert version.validation_json["status"] == "done"
    assert version.validation_json["errors"] == []
    assert version.scad_file.name.endswith("model.scad")
    assert version.stl_file.name.endswith("model.stl")
    assert backend.exported_format == "stl"
    assert storage.read_bytes(version.stl_file.name) == FAKE_STL
    assert storage.read_bytes(version.scad_file.name) == FAKE_SCAD


def test_render_version_persists_failure(version, tmp_path):
    class BoomBackend(FakeBackend):
        def export(self, model: GeneratedModel, format: str) -> bytes:
            raise RuntimeError("openscad exploded")

    storage = LocalStorage(root=tmp_path)
    with pytest.raises(RuntimeError):
        render_version(version, backend=BoomBackend(), storage=storage)

    version.refresh_from_db()
    assert version.validation_json["status"] == "failed"
    assert version.validation_json["stage"] == "failed"
    assert "openscad exploded" in version.validation_json["errors"][0]


def test_render_version_rejects_validation_problems(version, tmp_path):
    class ProblemBackend(FakeBackend):
        def validate(self, model: GeneratedModel) -> list[str]:
            return ["wall too thin"]

    storage = LocalStorage(root=tmp_path)
    with pytest.raises(ValueError):
        render_version(version, backend=ProblemBackend(), storage=storage)

    version.refresh_from_db()
    assert version.validation_json["status"] == "failed"
    assert "wall too thin" in version.validation_json["errors"][0]
    assert not storage.exists(version.stl_file.name or "model.stl")


def test_celery_task_renders_and_persists(version, tmp_path, monkeypatch):
    storage = LocalStorage(root=tmp_path)
    monkeypatch.setattr("designs.cad.pipeline.get_backend", lambda: FakeBackend())
    monkeypatch.setattr("designs.cad.pipeline.get_storage", lambda: storage)

    result = tasks.render_model_stl(version.pk)

    version.refresh_from_db()
    assert result["status"] == "done"
    assert version.validation_json["status"] == "done"
    assert storage.exists(version.stl_file.name)


def test_celery_task_handles_missing_version():
    result = tasks.render_model_stl(999_999)
    assert result == {"version_id": 999_999, "status": "missing"}


def test_task_name_is_stable():
    assert tasks.render_model_stl.name == "designs.render_model_stl"
