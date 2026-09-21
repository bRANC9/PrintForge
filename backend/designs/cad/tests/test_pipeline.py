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


class RecordingNotifier:
    """Records ``notify_project_members`` calls for assertions."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> list:
        self.calls.append(kwargs)
        return []


class BoomBackend(FakeBackend):
    """Fails during generation with a configurable error."""

    def __init__(self, message: str = "openscad exploded") -> None:
        super().__init__()
        self.message = message

    def generate(self, specification):  # noqa: ANN001
        raise RuntimeError(self.message)


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


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def _use_backend(monkeypatch, backend, storage):
    monkeypatch.setattr("designs.cad.pipeline.get_backend", lambda: backend)
    monkeypatch.setattr("designs.cad.pipeline.get_storage", lambda: storage)


def test_task_notifies_model_ready_on_success(version, tmp_path, monkeypatch):
    storage = LocalStorage(root=tmp_path)
    _use_backend(monkeypatch, FakeBackend(), storage)
    notifier = RecordingNotifier()
    monkeypatch.setattr(tasks, "notify_project_members", notifier)

    result = tasks.render_model_stl(version.pk)

    assert result["status"] == "done"
    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["kind"] == tasks.NotificationKind.MODEL_READY
    assert call["project"].pk == version.project_id
    assert call["url"] == f"/projects/{version.project_id}/"
    assert "Holder" in call["message"]
    assert f"v{version.version}" in call["message"]


def test_task_notifies_model_failed_on_failure(version, tmp_path, monkeypatch):
    storage = LocalStorage(root=tmp_path)
    _use_backend(monkeypatch, BoomBackend(), storage)
    notifier = RecordingNotifier()
    monkeypatch.setattr(tasks, "notify_project_members", notifier)

    with pytest.raises(RuntimeError, match="openscad exploded"):
        tasks.render_model_stl(version.pk)

    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["kind"] == tasks.NotificationKind.MODEL_FAILED
    assert call["project"].pk == version.project_id
    assert call["url"] == f"/projects/{version.project_id}/"
    assert "openscad exploded" in call["message"]


def test_failure_notification_truncates_reason(version, tmp_path, monkeypatch):
    storage = LocalStorage(root=tmp_path)
    _use_backend(monkeypatch, BoomBackend("E" * 500), storage)
    notifier = RecordingNotifier()
    monkeypatch.setattr(tasks, "notify_project_members", notifier)

    with pytest.raises(RuntimeError):
        tasks.render_model_stl(version.pk)

    message = notifier.calls[0]["message"]
    assert "E" * 200 in message
    assert "E" * 201 not in message


def test_notify_exception_does_not_break_success(version, tmp_path, monkeypatch):
    storage = LocalStorage(root=tmp_path)
    _use_backend(monkeypatch, FakeBackend(), storage)

    def boom(**kwargs):
        raise RuntimeError("notification backend down")

    monkeypatch.setattr(tasks, "notify_project_members", boom)

    result = tasks.render_model_stl(version.pk)

    assert result["status"] == "done"
    version.refresh_from_db()
    assert version.validation_json["status"] == "done"


def test_notify_exception_does_not_mask_failure(version, tmp_path, monkeypatch):
    storage = LocalStorage(root=tmp_path)
    _use_backend(monkeypatch, BoomBackend("openscad exploded"), storage)

    def boom(**kwargs):
        raise RuntimeError("notification backend down")

    monkeypatch.setattr(tasks, "notify_project_members", boom)

    # The original render error must survive, not the notification error.
    with pytest.raises(RuntimeError, match="openscad exploded"):
        tasks.render_model_stl(version.pk)

    version.refresh_from_db()
    assert version.validation_json["status"] == "failed"
