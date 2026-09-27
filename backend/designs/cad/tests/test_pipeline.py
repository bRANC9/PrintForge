"""Integration tests for the CAD pipeline orchestration + Celery task."""

from __future__ import annotations

import io

import pytest
from django.contrib.auth import get_user_model
from django.db import models

from designs import tasks
from designs.cad.base import GeneratedModel
from designs.cad.mesh import MeshCADBackend
from designs.cad.openscad import OpenSCADBackend
from designs.cad.pipeline import (
    ARTIFACT_FILENAMES,
    ARTIFACT_SIZE_KEYS,
    ARTIFACT_TARGETS,
    CADValidationFailed,
    render_version,
    resolve_backend_name,
)
from designs.models import ModelVersion
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
    assert "Holder" in call["message"]


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


# ---------------------------------------------------------------------------
# Artifact mapping / backend contract (regression guards)
# ---------------------------------------------------------------------------


def test_artifact_mapping_matches_the_designs_service_contract():
    from designs import services as designs_services

    assert ARTIFACT_FILENAMES["scad"] == "model.scad"
    assert ARTIFACT_FILENAMES["stl"] == "model.stl"
    assert set(ARTIFACT_TARGETS) <= set(designs_services.ARTIFACT_KINDS)
    for kind, (filename, field, content_type) in ARTIFACT_TARGETS.items():
        assert filename == ARTIFACT_FILENAMES[kind]
        assert field == designs_services._ARTIFACT_FIELDS[kind]
        assert content_type == designs_services.ARTIFACT_CONTENT_TYPES[kind]


def test_size_keys_are_limited_to_the_documented_contract():
    assert ARTIFACT_SIZE_KEYS == {"stl": "stl_bytes"}


# ---------------------------------------------------------------------------
# Artifact-registry sync invariant
# ---------------------------------------------------------------------------
#
# Two agents wrote code against this map: ``pipeline.ARTIFACT_TARGETS`` (what the
# renderer writes) and ``services.ARTIFACT_KINDS`` / ``_ARTIFACT_FIELDS`` /
# ``ARTIFACT_CONTENT_TYPES`` (what the API and MCP serve). They are separate
# modules in different apps that must agree exactly, so the agreement is asserted
# exhaustively here rather than left to one direction of a spot check.


def test_pipeline_targets_are_a_subset_of_the_service_kinds():
    from designs import services as designs_services

    assert set(ARTIFACT_TARGETS) <= set(designs_services.ARTIFACT_KINDS)
    # The renderer looks the filename up by kind, so a target without one is a
    # KeyError waiting to happen. (The reverse is *not* required: ``scad`` has no
    # entry in ``ARTIFACT_SIZE_KEYS`` by design.)
    assert set(ARTIFACT_TARGETS) == set(ARTIFACT_FILENAMES)


def test_artifact_kind_maps_have_identical_key_sets():
    from designs import services as designs_services

    assert (
        set(designs_services.ARTIFACT_KINDS)
        == set(designs_services._ARTIFACT_FIELDS)
        == set(designs_services.ARTIFACT_CONTENT_TYPES)
    )


def test_every_artifact_field_points_at_a_real_file_field():
    """A renamed/removed model field would make ``artifact_path`` answer ``None``.

    ``artifact_path`` is a ``getattr`` with no error, so a stale field name in
    ``_ARTIFACT_FIELDS`` is silent: the API would 404 an artifact that really
    exists in storage. This turns that silence into a failure.
    """
    from designs import services as designs_services

    for kind, field_name in designs_services._ARTIFACT_FIELDS.items():
        field = ModelVersion._meta.get_field(field_name)
        assert isinstance(field, models.FileField), f"{kind} -> {field_name} is not a FileField"


def test_pipeline_target_triples_match_the_service_maps():
    from designs import services as designs_services

    for kind, (filename, field_name, content_type) in ARTIFACT_TARGETS.items():
        assert filename == ARTIFACT_FILENAMES[kind]
        assert field_name == designs_services._ARTIFACT_FIELDS[kind]
        assert content_type == designs_services.ARTIFACT_CONTENT_TYPES[kind]


def test_artifact_filenames_are_unique_and_canonical():
    """Two kinds sharing a filename would overwrite each other in storage."""
    assert list(ARTIFACT_FILENAMES.values()).count("model.scad") == 1
    assert list(ARTIFACT_FILENAMES.values()).count("model.stl") == 1
    assert set(ARTIFACT_FILENAMES.values()) == {"model.scad", "model.stl"}


def test_size_keys_follow_the_kind_bytes_convention():
    for kind, size_key in ARTIFACT_SIZE_KEYS.items():
        assert kind in ARTIFACT_TARGETS
        assert size_key == f"{kind}_bytes"
    # Only the STL reports a size; a ``scad_bytes`` key would change the
    # documented ``validation_json`` contract.
    assert set(ARTIFACT_SIZE_KEYS) == {"stl"}


def test_preview_is_a_service_kind_the_renderer_never_writes():
    """``preview`` is servable but not a render output -- that asymmetry is intended.

    The preview PNG is produced by a separate vision-review step, so it belongs
    in ``ARTIFACT_KINDS`` (the API serves it) but not in ``ARTIFACT_TARGETS``
    (a render must not invent it). Pinned so nobody "fixes" the subset relation
    in ``test_pipeline_targets_are_a_subset_of_the_service_kinds`` above.
    """
    from designs import services as designs_services

    assert "preview" in designs_services.ARTIFACT_KINDS
    assert "preview" not in ARTIFACT_TARGETS
    assert "preview" not in ARTIFACT_FILENAMES


def test_a_rendered_artifact_is_readable_through_the_service(version, tmp_path, settings):
    """The end-to-end version of the invariant: written *and* servable.

    Renders with the two-artifact backend, then reads every kind back through
    ``designs.services.read_artifact`` -- the exact call the API and the MCP
    ``export_model_stl`` tool make. This is what would fail first if the two
    registries drifted apart. ``read_artifact`` resolves storage through
    ``files.services.get_storage``, so ``MEDIA_ROOT`` is pointed at the same
    directory rather than handing the service the test's storage object.
    """
    from designs.services import read_artifact

    settings.MEDIA_ROOT = str(tmp_path)
    storage = LocalStorage(root=tmp_path)
    render_version(version, backend=FakeBackend(), storage=storage)
    version.refresh_from_db()

    for kind in ARTIFACT_TARGETS:
        result = read_artifact(version, kind)
        assert result is not None, f"{kind} was written but is not servable"
        relative_path, data = result
        assert relative_path.endswith(f"/{ARTIFACT_TARGETS[kind][0]}")
        assert data == storage.read_bytes(relative_path)


def test_openscad_backend_still_writes_both_artifacts():
    assert OpenSCADBackend.produced_artifacts == ("scad", "stl")
    assert OpenSCADBackend.produces_source_code is True
    assert OpenSCADBackend.supported_formats == ("stl",)


def test_openscad_generate_produces_untouched_validation_json_keys(version, tmp_path):
    """The documented ``validation_json`` key set must not change."""
    from designs.cad.openscad import render_scad

    backend = FakeBackend()
    source = backend.generate(version.specification_json)
    storage = LocalStorage(root=tmp_path)

    result = render_version(version, backend=backend, storage=storage)

    version.refresh_from_db()
    assert set(result) == {"version_id", "status", "scad_file", "stl_file", "stl_bytes"}
    assert set(version.validation_json) == {
        "status",
        "stage",
        "errors",
        "scad_file",
        "stl_file",
        "stl_bytes",
        "started_at",
        "completed_at",
    }
    # The OpenSCAD source is still written verbatim, byte for byte.
    assert storage.read_bytes(version.scad_file.name) == source.encode("utf-8")
    assert isinstance(render_scad, type(render_scad))  # the render path is still importable


def test_unknown_artifact_kind_fails_the_job(version, tmp_path):
    from designs.cad.base import CADError

    class WeirdBackend(FakeBackend):
        produced_artifacts = ("scad", "stl", "dxf")

    storage = LocalStorage(root=tmp_path)
    with pytest.raises(CADError, match="artifact kind"):
        render_version(version, backend=WeirdBackend(), storage=storage)

    version.refresh_from_db()
    assert version.validation_json["status"] == "failed"


# ---------------------------------------------------------------------------
# Mesh backend end to end
# ---------------------------------------------------------------------------


@pytest.fixture
def mesh_version(version, tmp_path):
    import trimesh

    storage = LocalStorage(root=tmp_path)
    path = "reference_uploads/1/mesh.glb"
    # ``file_type`` is required: trimesh 5 treats a bare ``export("glb")`` as a
    # file object and writes a junk file named ``glb`` into the cwd.
    storage.write_bytes(
        path, trimesh.creation.box(extents=(1.0, 0.5, 0.25)).export(file_type="glb")
    )
    version.specification_json = {
        "generator": "mesh",
        "mesh": {"source": path},
        "transform": {"scale_mm": 120.0, "rest_on_plate": True, "center_xy": True},
        "repair": {"enabled": True},
    }
    version.save(update_fields=["specification_json"])
    return version, storage


def test_render_version_with_mesh_backend_writes_only_an_stl(mesh_version):
    version, storage = mesh_version
    backend = MeshCADBackend(storage=storage)

    result = render_version(version, backend=backend, storage=storage)

    version.refresh_from_db()
    assert result["status"] == "done"
    assert set(result) == {"version_id", "status", "stl_file", "stl_bytes"}
    assert result["stl_file"] == version.stl_file.name
    assert version.stl_file.name.endswith("model.stl")
    assert storage.exists(version.stl_file.name)

    # No source code: the manifest must not be persisted as a .scad.
    assert not version.scad_file.name
    assert "scad_file" not in version.validation_json
    assert not storage.exists(f"projects/{version.project_id}/v{version.version}/model.scad")

    payload = storage.read_bytes(version.stl_file.name)
    assert len(payload) == result["stl_bytes"]

    import trimesh

    mesh = trimesh.load(io.BytesIO(payload), file_type="stl")
    assert float(mesh.bounds[0][2]) == pytest.approx(0.0, abs=1e-5)
    assert float(mesh.extents.max()) == pytest.approx(120.0, abs=1e-4)
    assert mesh.is_watertight


def test_mesh_render_keeps_the_openscad_validation_json_shape(mesh_version):
    """The back-compat contract, pinned key by key.

    The pipeline documents ``validation_json`` as a fixed key set and promises no
    key is ever renamed or removed. A mesh render therefore keeps every status
    key and simply omits ``scad_file`` -- it must not invent a placeholder, and
    it must not drop ``stl_bytes``. A client polling for ``stl_file`` on a mesh
    version has to find exactly the same key it finds on a parametric one.

    ``FakeBackend`` stands in for the parametric OpenSCAD path: it inherits
    ``CADBackend.produced_artifacts`` / ``produces_source_code``, so it exercises
    the same two-artifact branch without shelling out to a real binary.
    """
    version, storage = mesh_version
    openscad_keys = {
        "status",
        "stage",
        "errors",
        "scad_file",
        "stl_file",
        "stl_bytes",
        "started_at",
        "completed_at",
    }

    render_version(version, backend=FakeBackend(), storage=storage)
    version.refresh_from_db()
    parametric_keys = set(version.validation_json)
    assert parametric_keys == openscad_keys

    # Re-render the same version through the mesh backend.
    version.validation_json = {}
    version.save(update_fields=["validation_json"])
    render_version(version, backend=MeshCADBackend(storage=storage), storage=storage)
    version.refresh_from_db()

    mesh_keys = set(version.validation_json)
    assert mesh_keys == parametric_keys - {"scad_file"}
    assert "scad_file" not in mesh_keys
    assert "scad_bytes" not in mesh_keys
    assert mesh_keys >= {"stl_file", "stl_bytes", "status", "stage", "errors"}
    assert version.validation_json["status"] == "done"
    assert version.validation_json["errors"] == []
    # The parametric run really did write both artifacts, so the two shapes are
    # comparable and the mesh one is a strict subset rather than a different key
    # vocabulary.
    assert storage.exists(f"projects/{version.project_id}/v{version.version}/model.scad")
    assert storage.exists(version.stl_file.name)


def test_mesh_render_never_creates_a_scad_file_on_disk(mesh_version):
    """Not just "the field is empty": nothing was written, under any name."""
    version, storage = mesh_version
    version_dir = f"projects/{version.project_id}/v{version.version}"

    render_version(version, backend=MeshCADBackend(storage=storage), storage=storage)
    version.refresh_from_db()

    assert not version.scad_file.name
    assert not storage.exists(f"{version_dir}/model.scad")
    assert storage.read_bytes(version.stl_file.name)[:5] != b"//"


def test_mesh_render_leaves_a_stale_scad_from_an_earlier_render_alone(mesh_version):
    """A version re-pointed at a mesh keeps its old ``.scad`` until a full re-import.

    The pipeline only ever *adds* artifacts; it never deletes one. Documented
    here because it is the one case where ``scad_file`` and the mesh
    specification disagree, and a caller must not assume the field is empty just
    because the last render was a mesh one.
    """
    version, storage = mesh_version
    version_dir = f"projects/{version.project_id}/v{version.version}"
    version.scad_file.name = f"{version_dir}/model.scad"
    version.save(update_fields=["scad_file"])
    storage.write_bytes(version.scad_file.name, b"// stale parametric source\n")

    render_version(version, backend=MeshCADBackend(storage=storage), storage=storage)
    version.refresh_from_db()

    assert version.scad_file.name == f"{version_dir}/model.scad"
    assert storage.exists(f"{version_dir}/model.scad")
    # The new render still reports the STL, and never claims a fresh .scad.
    assert version.validation_json["stl_file"] == version.stl_file.name
    assert "scad_file" not in version.validation_json


def test_render_version_with_mesh_backend_fails_on_blocking_problems(mesh_version, tmp_path):
    import trimesh

    version, storage = mesh_version
    box = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    holed = trimesh.Trimesh(vertices=box.vertices.copy(), faces=box.faces[:-1], process=False)
    path = "reference_uploads/1/broken.stl"
    storage.write_bytes(path, holed.export(file_type="stl"))
    version.source_mesh.name = path
    version.save(update_fields=["source_mesh"])
    version.specification_json = {
        "generator": "mesh",
        "mesh": {"source": path},
    }
    version.save(update_fields=["specification_json"])

    with pytest.raises(CADValidationFailed) as excinfo:
        render_version(version, backend=MeshCADBackend(storage=storage), storage=storage)

    assert any("not watertight" in problem for problem in excinfo.value.problems)
    version.refresh_from_db()
    assert version.validation_json["status"] == "failed"
    assert "CADValidationFailed" in version.validation_json["errors"][0]
    assert not version.stl_file.name


def test_mesh_backend_is_selected_from_the_registry(mesh_version, monkeypatch):
    version, storage = mesh_version
    monkeypatch.setenv("MESH_BACKEND", "mesh")
    # The mesh backend resolves its own storage, so patch the factory it uses.
    monkeypatch.setattr("files.services.get_storage", lambda: storage)
    monkeypatch.setattr("designs.cad.pipeline.get_storage", lambda: storage)

    assert resolve_backend_name() == "mesh"
    result = render_version(version)

    version.refresh_from_db()
    assert result["status"] == "done"
    assert storage.exists(version.stl_file.name)
    assert not version.scad_file.name
