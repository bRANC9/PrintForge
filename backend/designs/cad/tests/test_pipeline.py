"""Integration tests for the CAD pipeline orchestration + Celery task."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.db import models

from designs import tasks
from designs.cad.base import GeneratedModel
from designs.cad.mesh import MeshCADBackend
from designs.cad.openscad import SANDBOX_FLAGS, OpenSCADBackend
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


# ---------------------------------------------------------------------------
# Post-render dimension check
# ---------------------------------------------------------------------------
#
# ``designs.cad.dimensions`` compares the *rendered* bounding box with the
# requested ``dimensions`` and appends what it finds to
# ``validation_json["warnings"]`` -- advisory, never blocking. The unit tests
# live in ``test_dimensions.py``; these cover the wiring, the key-set contract
# and, at the bottom, the real OpenSCAD sandbox.


def _trimesh_stl(box: tuple[float, float, float], *, lift: float | None = None) -> bytes:
    """A real, parseable STL of a box, resting on the plate by default."""
    import trimesh

    mesh = trimesh.creation.box(extents=box)
    mesh.apply_translation([0.0, 0.0, (lift if lift is not None else box[2] / 2)])
    return mesh.export(file_type="stl")


class StlBackend(FakeBackend):
    """``FakeBackend`` that exports a real, measurable STL."""

    payload = b""

    def export(self, model: GeneratedModel, format: str) -> bytes:
        self.exported_format = format
        return self.payload


@pytest.fixture
def sized_version(version):
    """``version`` with a declared 40 x 60 x 10 mm envelope (a device, not a part)."""
    version.specification_json = {
        **SPEC,
        "dimensions": {"width": 40.0, "height": 60.0, "thickness": 10.0},
    }
    version.save(update_fields=["specification_json"])
    return version


def test_render_version_appends_a_dimension_warning(sized_version, tmp_path):
    backend = StlBackend()
    backend.payload = _trimesh_stl((100.0, 60.0, 10.0))

    result = render_version(sized_version, backend=backend, storage=LocalStorage(root=tmp_path))

    sized_version.refresh_from_db()
    assert result["status"] == "done"  # advisory: the job still succeeds
    assert sized_version.validation_json["status"] == "done"
    assert sized_version.validation_json["errors"] == []
    assert sized_version.validation_json["warnings"] == [
        "rendered X extent 100.0mm differs from the requested width 40.0mm by +150% "
        "(tolerance +-25%)"
    ]


def test_render_version_reports_a_floating_part(sized_version, tmp_path):
    """``min Z != 0`` is reported even when every dimension is right."""
    backend = StlBackend()
    backend.payload = _trimesh_stl((40.0, 60.0, 10.0), lift=10.0)

    render_version(sized_version, backend=backend, storage=LocalStorage(root=tmp_path))

    sized_version.refresh_from_db()
    assert sized_version.validation_json["warnings"] == [
        "rendered part starts at Z=5.000mm; the rest of the app assumes the part "
        "rests on the build plate (min Z = 0)"
    ]


def test_a_correct_part_adds_no_warnings_key(sized_version, tmp_path):
    """The documented ``validation_json`` shape must survive a *measurable* STL.

    ``test_openscad_generate_produces_untouched_validation_json_keys`` pins the
    key set for a backend whose STL cannot be parsed at all. This is the
    stronger version of the same contract: the check runs, finds nothing, and
    still leaves ``validation_json`` byte-identical -- no empty ``warnings`` list
    invented for every good render.
    """
    backend = StlBackend()
    backend.payload = _trimesh_stl((40.0, 60.0, 10.0))

    result = render_version(sized_version, backend=backend, storage=LocalStorage(root=tmp_path))

    sized_version.refresh_from_db()
    assert "warnings" not in sized_version.validation_json
    assert set(sized_version.validation_json) == {
        "status",
        "stage",
        "errors",
        "scad_file",
        "stl_file",
        "stl_bytes",
        "started_at",
        "completed_at",
    }
    assert set(result) == {"version_id", "status", "scad_file", "stl_file", "stl_bytes"}


def test_a_broken_dimension_check_cannot_fail_or_stall_a_render(
    sized_version, tmp_path, monkeypatch
):
    """The check runs on a render that already produced a valid artifact.

    A diagnostic must not be able to turn a finished render into a failed one --
    and, worse, into one stuck in ``status="running"``, which is what the API
    would then poll forever.
    """
    monkeypatch.setattr(
        "designs.cad.pipeline.dimension_warnings",
        lambda payload, dimensions: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    backend = StlBackend()
    backend.payload = _trimesh_stl((100.0, 60.0, 10.0))
    storage = LocalStorage(root=tmp_path)

    result = render_version(sized_version, backend=backend, storage=storage)

    sized_version.refresh_from_db()
    assert result["status"] == "done"
    assert sized_version.validation_json["status"] == "done"
    assert sized_version.validation_json["errors"] == []
    assert "warnings" not in sized_version.validation_json
    assert storage.exists(sized_version.stl_file.name)


def test_dimension_warnings_is_given_the_exported_bytes_and_the_dimensions(
    sized_version, tmp_path, monkeypatch
):
    """The seam the agent path is expected to reuse (see the module docstring)."""
    seen: list[tuple[bytes, object]] = []

    def spy(payload, dimensions):  # noqa: ANN001
        seen.append((payload, dimensions))
        return []

    monkeypatch.setattr("designs.cad.pipeline.dimension_warnings", spy)
    backend = StlBackend()
    backend.payload = _trimesh_stl((100.0, 60.0, 10.0))

    render_version(sized_version, backend=backend, storage=LocalStorage(root=tmp_path))

    assert seen == [(backend.payload, sized_version.specification_json["dimensions"])]


def test_an_unparseable_stl_is_not_a_render_failure(version, tmp_path):
    """``FAKE_STL`` cannot be measured: no findings, no key, no failure."""
    render_version(version, backend=FakeBackend(), storage=LocalStorage(root=tmp_path))

    version.refresh_from_db()
    assert "warnings" not in version.validation_json
    assert version.validation_json["status"] == "done"


def test_mesh_import_warnings_are_kept_when_the_check_appends(mesh_version):
    """``warnings`` is shared with ``designs.services``; append, never replace."""
    version, storage = mesh_version
    version.validation_json = {"status": "done", "warnings": ["mesh has 2 disconnected bodies"]}
    version.save(update_fields=["validation_json"])

    render_version(version, backend=MeshCADBackend(storage=storage), storage=storage)

    version.refresh_from_db()
    # The mesh specification has no ``dimensions`` and ``rest_on_plate`` put the
    # part on Z = 0, so only the pre-existing finding survives.
    assert version.validation_json["warnings"] == ["mesh has 2 disconnected bodies"]


def test_re_rendering_does_not_stack_identical_warnings(sized_version, tmp_path):
    backend = StlBackend()
    backend.payload = _trimesh_stl((100.0, 60.0, 10.0))

    storage = LocalStorage(root=tmp_path)
    render_version(sized_version, backend=backend, storage=storage)
    render_version(sized_version, backend=backend, storage=storage)

    sized_version.refresh_from_db()
    assert len(sized_version.validation_json["warnings"]) == 1


def test_a_mesh_render_gets_the_build_plate_check_without_dimensions(mesh_version):
    """A mesh specification has no ``dimensions``: only the plate check is possible."""
    import trimesh

    version, storage = mesh_version
    version.specification_json = {
        "generator": "mesh",
        "mesh": {"source": "reference_uploads/1/mesh.glb"},
        "transform": {"scale_mm": 120.0, "rest_on_plate": False, "center_xy": True},
    }
    version.save(update_fields=["specification_json"])
    source = "reference_uploads/1/floating.glb"
    box = trimesh.creation.box(extents=(1.0, 0.5, 0.25))
    box.apply_translation([0.0, 0.0, 1.0])
    storage.write_bytes(source, box.export(file_type="glb"))

    render_version(version, backend=MeshCADBackend(storage=storage), storage=storage)

    version.refresh_from_db()
    warnings = version.validation_json["warnings"]
    assert len(warnings) == 1
    assert warnings[0].startswith("rendered part starts at Z=")


# ---------------------------------------------------------------------------
# The generate step's own warnings (```GeneratedModel.warnings``)
# ---------------------------------------------------------------------------
#
# ``render_scad`` already knew what it had to skip or synthesise, but the note
# only ever became a ``// warning:`` comment inside the SCAD text -- invisible to
# the API and the UI, so a version that quietly fell back to a synthesised box
# looked finished. ``OpenS`GeneratedModel.warnings`` returns those notes
# alongside the source and ``render_version`` merges them into the same
# ``validation_json["warnings"]`` the dimension check and the mesh import use.
#
# These tests run the *real* generate step and stub only the sandboxed export,
# which is what keeps them out of the docker skip below: the warnings under test
# are produced by ``parse_primitives`` before any CLI call happens.


class UncompiledOpenSCADBackend(OpenSCADBackend):
    """The real OpenSCAD generate/validate, with only the CLI export replaced.

    Everything about the warnings travels unchanged -- ``generate_reported`` is
    the shipped implementation -- and ``export`` hands back a real STL so the
    pipeline still writes a measurable artifact without a container.
    """

    payload = b""

    def export(self, model, format):
        return self.payload


def _render_spec(version, specification, storage, *, extents=(40.0, 60.0, 10.0)):
    """Render ``version`` with the real OpenSCAD generate step and a fixed STL."""
    version.specification_json = specification
    version.save(update_fields=["specification_json"])
    backend = UncompiledOpenSCADBackend()
    backend.payload = _trimesh_stl(extents)
    render_version(version, backend=backend, storage=storage)
    version.refresh_from_db()
    return version.validation_json


#: A non-holder object with a declared envelope, so the box fallback is legal.
WIDGET = {
    "object": "widget",
    "dimensions": {"width": 40.0, "height": 60.0, "thickness": 10.0},
}

#: The exact notes the generate step collects, in the order it collects them.
SYNTHESIS_WARNING = "no usable primitives; synthesized a box from 'dimensions'"
SKIPPED_WARNING = (
    "skipped primitives[0]: primitives[0].type: must be one of "
    "['box', 'cylinder', 'sphere', 'cone', 'extrude'] (got 'torus')"
)


def test_render_version_records_the_generate_step_warnings(version, tmp_path):
    """A silent synthesised-box fallback becomes a visible advisory.

    Without this the only record that the model produced no usable geometry is
    a comment inside a ``.scad`` file the UI never opens.
    """
    validation = _render_spec(version, {**WIDGET, "primitives": []}, LocalStorage(root=tmp_path))

    assert validation["status"] == "done"
    assert validation["errors"] == []
    assert validation["warnings"] == [SYNTHESIS_WARNING]


def test_render_version_records_a_skipped_primitive_and_the_fallback(version, tmp_path):
    """Both notes travel, in order: the cause, then its consequence."""
    validation = _render_spec(
        version,
        {**WIDGET, "primitives": [{"type": "torus", "role": "add", "major_radius": 20.0}]},
        LocalStorage(root=tmp_path),
    )

    assert validation["warnings"] == [SKIPPED_WARNING, SYNTHESIS_WARNING]


def test_a_valid_primitive_spec_adds_no_warnings_key(version, tmp_path):
    """Good output must stay silent, in the merged key as much as in the hook.

    The synthesised box is exactly ``width x height x thickness``, so a spec with
    a matching primitive produces no generate note and no dimension finding --
    and ``validation_json`` keeps its documented shape with no ``warnings`` key.
    """
    validation = _render_spec(
        version,
        {
            **WIDGET,
            "primitives": [
                {
                    "type": "box",
                    "role": "add",
                    "width": 40.0,
                    "depth": 60.0,
                    "height": 10.0,
                    "position": {"x": 0.0, "y": 0.0, "z": 5.0},
                }
            ],
        },
        LocalStorage(root=tmp_path),
    )

    assert "warnings" not in validation
    assert set(validation) == {
        "status",
        "stage",
        "errors",
        "scad_file",
        "stl_file",
        "stl_bytes",
        "started_at",
        "completed_at",
    }


def test_generate_and_dimension_warnings_survive_in_the_same_list(version, tmp_path):
    """Two independent producers, one key: neither may replace the other.

    The generate step reports what it skipped; the dimension check reports what
    the export measured. Here both fire -- an unusable primitive *and* a mesh
    that does not match the declared envelope -- so a merge that lost either
    half would ship a version with an incomplete story.
    """
    # The fallback box is built from ``dimensions``, yet the exported mesh is
    # 100 mm wide: exactly the "synthesised something, and it is not what was
    # asked for" case both checks exist for.
    validation = _render_spec(
        version,
        {**WIDGET, "primitives": [{"type": "torus", "role": "add", "major_radius": 20.0}]},
        LocalStorage(root=tmp_path),
        extents=(100.0, 60.0, 10.0),
    )

    assert validation["warnings"] == [
        SKIPPED_WARNING,
        SYNTHESIS_WARNING,
        "rendered X extent 100.0mm differs from the requested width 40.0mm by +150% "
        "(tolerance +-25%)",
    ]


def test_a_backend_whose_model_carries_no_warnings_still_renders(version, tmp_path):
    """Duck-typed backends keep working: ``GeneratedModel.warnings`` is optional.

    The pipeline reads the notes with ``getattr(model, "warnings", ())``, so a
    backend that builds a model object of its own -- an external integration, a
    test double written before the field existed -- renders exactly as it did
    before. Reading the attribute unconditionally would break every such backend.
    """

    class BareModel:
        def __init__(self, specification, scad_source):
            self.specification = specification
            self.scad_source = scad_source

    class MinimalBackend:
        name = "minimal"

        def build_model(self, specification):
            return BareModel(specification, FAKE_SCAD.decode())

        def generate(self, specification):
            return FAKE_SCAD.decode()

        def validate(self, model):
            return []

        def export(self, model, format):
            return FAKE_STL

    assert not hasattr(BareModel("s", "src"), "warnings")

    storage = LocalStorage(root=tmp_path)
    result = render_version(version, backend=MinimalBackend(), storage=storage)

    version.refresh_from_db()
    assert result["status"] == "done"
    assert version.validation_json["status"] == "done"
    assert "warnings" not in version.validation_json
    assert storage.exists(version.stl_file.name)


# ---------------------------------------------------------------------------
# The real OpenSCAD sandbox
# ---------------------------------------------------------------------------
#
# Everything above fakes the mesh. These two render through the *real*
# ``OpenSCADBackend`` in ``docker`` mode (the terv.md 20. fejezet sandbox), which
# is the only way to prove the numbers come from a genuine CGAL mesh.
#
# They are skipped when there is no container runtime, no sandbox image, or the
# runtime cannot bind-mount the directory the backend writes the job into. That
# last case is a *harness* property, not a product behaviour: the sandbox is
# driven with ``-v <job-dir>/in:/work`` (``OpenSCADBackend.build_args``), so the
# runtime has to be able to see the path. On WSL the daemon is Windows-hosted and
# cannot see a WSL path, so ``TMPDIR`` has to point at a Windows-visible one
# (``/mnt/c/...``); on a native Linux host the default ``/tmp`` is fine. Without
# that, OpenSCAD exits 0 having written into a directory nobody else can see and
# the backend correctly reports "produced no output file".


def _docker_sandbox_available() -> tuple[bool, str]:
    import shutil
    import subprocess

    binary = shutil.which("docker")
    if binary is None:
        return False, "docker is not installed"
    image = OpenSCADBackend(mode="docker").config.image
    probe = subprocess.run(  # noqa: S603 - fixed argv, shell=False
        [binary, "image", "inspect", image],
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if probe.returncode != 0:
        return False, f"sandbox image {image} is missing"
    return _runtime_can_mount(binary, image)


def _runtime_can_mount(binary: str, image: str) -> tuple[bool, str]:
    """Can the runtime bind-mount the directory the backend will render into?

    Probed with the *real* invocation shape -- the sandbox flags and the same two
    mounts ``OpenSCADBackend.build_args`` uses -- and with a ``touch`` instead of
    a render, because the failure mode is silent: a runtime that cannot see the
    path mounts something else (or nothing), the container still exits 0, and
    OpenSCAD reports ``Can't open file "/out/model.stl" for export``. The marker
    has to show up **on this host** for the mount to count. Measured on this
    box: a WSL ``TMPDIR=/tmp`` leaves the container with the image's own ``/out``
    (``touch: Permission denied``), while ``TMPDIR=/mnt/c/...`` really mounts.
    """
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix="printforge-mount-probe-") as job:
        in_dir, out_dir = Path(job) / "in", Path(job) / "out"
        in_dir.mkdir()
        out_dir.mkdir()
        marker = out_dir / "probe"
        completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False
            [
                binary,
                "run",
                "--rm",
                *SANDBOX_FLAGS,
                "--entrypoint",
                "/bin/sh",
                "-v",
                f"{in_dir}:/work:ro",
                "-v",
                f"{out_dir}:/out:rw",
                image,
                "-c",
                "touch /out/probe",
            ],
            shell=False,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        mounted = marker.exists()
        if not mounted:
            return False, (
                f"the container runtime cannot bind-mount {tempfile.gettempdir()} "
                f"(exit {completed.returncode}: "
                f"{(completed.stderr or completed.stdout or '').strip()[:200]}); point "
                "TMPDIR at a path the runtime can see (on WSL: /mnt/c/...)"
            )
    return True, ""


_DOCKER, _DOCKER_REASON = _docker_sandbox_available()
needs_docker = pytest.mark.skipif(not _DOCKER, reason=_DOCKER_REASON or "docker unavailable")


def _render_with_sandbox(version, storage):
    render_version(
        version,
        backend=OpenSCADBackend(mode="docker"),
        storage=storage,
    )
    version.refresh_from_db()
    return version.validation_json


@needs_docker
def test_the_sandbox_render_of_an_oversized_primitive_warns(version, tmp_path):
    """A 100 mm plate declared as a 40 mm one: watertight, printable, wrong."""
    version.specification_json = {
        "object": "widget",
        "dimensions": {"width": 40.0, "height": 60.0, "thickness": 10.0},
        "primitives": [
            {
                "type": "box",
                "role": "add",
                "width": 100.0,
                "depth": 60.0,
                "height": 10.0,
                "position": {"x": 0.0, "y": 0.0, "z": 5.0},
            }
        ],
    }
    version.save(update_fields=["specification_json"])
    storage = LocalStorage(root=tmp_path)

    validation = _render_with_sandbox(version, storage)

    assert validation["status"] == "done"
    assert validation["errors"] == []
    assert validation["warnings"] == [
        "rendered X extent 100.0mm differs from the requested width 40.0mm by +150% "
        "(tolerance +-25%)"
    ]
    # The artifact itself is a fine STL -- that is the whole point of the warning.
    import trimesh

    mesh = trimesh.load(io.BytesIO(storage.read_bytes(version.stl_file.name)), file_type="stl")
    assert mesh.is_watertight
    assert float(mesh.bounds[0][2]) == pytest.approx(0.0, abs=1e-5)


@needs_docker
def test_the_sandbox_render_of_a_matching_primitive_is_silent(version, tmp_path):
    version.specification_json = {
        "object": "widget",
        "dimensions": {"width": 40.0, "height": 60.0, "thickness": 10.0},
        "primitives": [
            {
                "type": "box",
                "role": "add",
                "width": 40.0,
                "depth": 60.0,
                "height": 10.0,
                "position": {"x": 0.0, "y": 0.0, "z": 5.0},
            }
        ],
    }
    version.save(update_fields=["specification_json"])

    validation = _render_with_sandbox(version, LocalStorage(root=tmp_path))

    assert validation["status"] == "done"
    assert "warnings" not in validation
    assert set(validation) == {
        "status",
        "stage",
        "errors",
        "scad_file",
        "stl_file",
        "stl_bytes",
        "started_at",
        "completed_at",
    }


@needs_docker
def test_the_sandbox_render_of_a_centred_primitive_reports_the_build_plate(version, tmp_path):
    """A cylinder with no ``position`` is centred, so half of it is under Z = 0."""
    version.specification_json = {
        "object": "knob",
        "dimensions": {"width": 25.0, "height": 25.0, "thickness": 25.0},
        "primitives": [{"type": "cylinder", "role": "add", "diameter": 25.0, "height": 25.0}],
    }
    version.save(update_fields=["specification_json"])

    validation = _render_with_sandbox(version, LocalStorage(root=tmp_path))

    assert validation["status"] == "done"
    assert len(validation["warnings"]) == 1
    assert validation["warnings"][0].startswith("rendered part starts at Z=-12.")
