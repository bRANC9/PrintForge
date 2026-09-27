"""Tests for the mesh-import service layer (externally generated .stl/.obj/.glb).

The mesh is validated *before* anything is written, so these tests also pin the
"nothing survives a rejected upload" contract: no version row and no file.
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings

from configuration.services import get_setting, invalidate_settings_cache, update_settings
from designs.cad import meshcheck
from designs.cad.pipeline import (
    ARTIFACT_FILENAMES,
    ARTIFACT_SIZE_KEYS,
    ARTIFACT_TARGETS,
)
from designs.models import ModelVersion, ModelVersionOrigin
from designs.services import (
    ARTIFACT_CONTENT_TYPES,
    ARTIFACT_KINDS,
    MESH_SOURCE_FORMATS,
    MeshNotPrintableError,
    artifact_path,
    attach_source_mesh_to_version,
    create_next_version,
    create_next_version_from_mesh,
    delete_version,
    read_artifact,
    read_source_mesh,
    source_mesh_format,
    source_mesh_path,
    source_mesh_warnings,
)
from files.services import LocalStorage
from projects.services import create_project
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()

#: The exact blocking strings ``designs.cad.meshcheck`` produces. They are the
#: payload of a ``400`` response, so they are asserted verbatim rather than with
#: a substring match that would survive a reworded or truncated message.
PROBLEM_NOT_WATERTIGHT = "mesh is not watertight (open edges would print as holes)"
PROBLEM_ZERO_VOLUME = "mesh encloses zero volume (degenerate or single-sided geometry)"
PROBLEM_NON_FINITE = "mesh contains non-finite coordinates (NaN or infinity)"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """The ``AppSettings`` singleton is cached process-wide; never leak it.

    ``configuration.services`` memoises the row in Django's cache backend, which
    the per-test transaction rollback does not undo. Without this, an override
    written by one test would still be in effect for the next one.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def project(db):
    user = User.objects.create_user(username="ada", email="ada@example.com", password="pw")
    workspace = create_workspace(name="Lab", owner=user)
    return create_project(workspace=workspace, name="Holder", created_by=user)


@pytest.fixture
def media(settings, tmp_path) -> LocalStorage:
    settings.MEDIA_ROOT = str(tmp_path)
    return LocalStorage(root=tmp_path)


def box_stl() -> bytes:
    """A watertight 10x20x30 mm box as binary STL bytes."""
    import trimesh

    return bytes(trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl"))


def box_glb() -> bytes:
    """The same solid as binary GLB (the format a viewer usually exports)."""
    import trimesh

    return bytes(trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="glb"))


def box_obj() -> bytes:
    """The same solid as Wavefront OBJ (a text format, hence the ``encode``)."""
    import trimesh

    exported = trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="obj")
    return exported.encode("utf-8") if isinstance(exported, str) else bytes(exported)


def open_box_stl() -> bytes:
    """A box with one triangle removed: parses, but is not watertight."""
    import trimesh

    box = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    holed = trimesh.Trimesh(vertices=box.vertices.copy(), faces=box.faces[:-1], process=False)
    return bytes(holed.export(file_type="stl"))


def flat_plane_stl() -> bytes:
    """A single triangle: not watertight *and* encloses no volume.

    Two independent blocking problems in one payload, which is what makes it the
    fixture for the "every problem is reported, not just the first" assertion.
    """
    import numpy as np
    import trimesh

    plane = trimesh.Trimesh(
        vertices=np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 10.0, 0.0]]),
        faces=np.array([[0, 1, 2]]),
        process=False,
    )
    return bytes(plane.export(file_type="stl"))


def nan_mesh_stl() -> bytes:
    """A mesh carrying a NaN vertex: finite-geometry check fails first."""
    import numpy as np
    import trimesh

    broken = trimesh.Trimesh(
        vertices=np.array(
            [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 10.0, 0.0], [np.nan, 1.0, 1.0]]
        ),
        faces=np.array([[0, 1, 2], [0, 1, 3]]),
        process=False,
    )
    return bytes(broken.export(file_type="stl"))


# ---------------------------------------------------------------------------
# Contract: the source mesh is an input, never an artifact
# ---------------------------------------------------------------------------


def test_source_mesh_is_not_an_artifact_kind():
    """The artifact registry stays in sync with ``pipeline.ARTIFACT_TARGETS``."""
    assert MESH_SOURCE_FORMATS == ("stl", "obj", "glb")
    assert set(ARTIFACT_KINDS) == {"scad", "stl", "preview"}
    assert set(ARTIFACT_CONTENT_TYPES) == {"scad", "stl", "preview"}
    assert "source_mesh" not in ARTIFACT_KINDS
    assert "mesh" not in ARTIFACT_KINDS


def test_artifact_kinds_order_is_pinned():
    """``ARTIFACT_KINDS`` is a public tuple: its order is part of the contract."""
    assert ARTIFACT_KINDS == ("scad", "stl", "preview")


def test_source_mesh_is_unreachable_through_the_artifact_registry(project, media):
    """The invariant, enforced end to end: no artifact kind resolves a mesh.

    ``artifact_path`` is a pure lookup into ``_ARTIFACT_FIELDS``, so this is the
    single seam that would have to change for the source mesh to start being
    served as a render output. ``read_artifact`` and the ``{kind}_file`` status
    key are pinned too, because ``render_version`` is what writes those.
    """
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )

    assert version.source_mesh.name
    assert artifact_path(version, "source_mesh") is None
    assert read_artifact(version, "source_mesh") is None
    # It is genuinely not a *missing* artifact either -- it simply has no kind.
    assert artifact_path(version, "mesh") is None
    assert "source_mesh" not in (version.validation_json or {})
    # ...while the input reader does resolve it.
    assert read_source_mesh(version) is not None


def test_source_mesh_format_never_leaks_into_the_artifact_maps():
    assert set(ARTIFACT_TARGETS) == {"scad", "stl"}
    assert "source_mesh" not in ARTIFACT_TARGETS
    assert "source_mesh" not in ARTIFACT_FILENAMES
    assert "source_mesh" not in ARTIFACT_SIZE_KEYS


# ---------------------------------------------------------------------------
# create_next_version_from_mesh
# ---------------------------------------------------------------------------


def test_create_next_version_from_mesh_stores_mesh_and_specification(project, media):
    payload = box_stl()

    version = create_next_version_from_mesh(
        project=project,
        mesh_bytes=payload,
        filename="from-image.STL",
        created_by=project.created_by,
    )

    assert version.source_mesh.name == f"projects/{project.pk}/v1/source.stl"
    assert media.read_bytes(version.source_mesh.name) == payload
    assert version.specification_json == {
        "generator": "mesh",
        "mesh": {"source": f"projects/{project.pk}/v1/source.stl"},
    }
    assert version.origin == ModelVersionOrigin.MANUAL
    assert version.prompt == ""
    assert source_mesh_path(version) == version.source_mesh.name
    assert source_mesh_warnings(version) == []


def test_create_next_version_from_mesh_uses_the_next_version_number(project, media):
    create_next_version(project=project, prompt="v1")

    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )

    assert version.version == 2
    assert version.source_mesh.name == f"projects/{project.pk}/v2/source.stl"


def test_create_next_version_from_mesh_forwards_transform_and_repair(project, media):
    version = create_next_version_from_mesh(
        project=project,
        mesh_bytes=box_glb(),
        filename="box.glb",
        scale_mm=42.0,
        rotate_deg=[0, 0, 90],
        repair=False,
        reference_note="generated from a photo",
    )

    assert version.source_mesh.name.endswith("source.glb")
    assert version.specification_json == {
        "generator": "mesh",
        "mesh": {"source": version.source_mesh.name},
        "transform": {"scale_mm": 42.0, "rotate_deg": [0.0, 0.0, 90.0]},
        "repair": {"enabled": False},
    }
    assert version.reference_note == "generated from a photo"


def test_create_next_version_from_mesh_keeps_the_warnings(project, media):
    # A solid built in a unitless source scale lands far below the print
    # envelope, which is exactly the non-blocking hint worth surfacing.
    import trimesh

    payload = bytes(trimesh.creation.box(extents=(0.01, 0.02, 0.03)).export(file_type="stl"))

    version = create_next_version_from_mesh(
        project=project, mesh_bytes=payload, filename="tiny.stl"
    )

    warnings = source_mesh_warnings(version)
    assert warnings
    assert any("print" in warning for warning in warnings)
    # Only warnings: the import is accepted.
    assert ModelVersion.objects.filter(pk=version.pk).exists()


def test_create_next_version_from_mesh_rejects_an_unsupported_suffix(project, media):
    with pytest.raises(ValueError, match="stl, obj, glb"):
        create_next_version_from_mesh(project=project, mesh_bytes=box_stl(), filename="model.3mf")

    assert not ModelVersion.objects.exists()
    assert not media.exists(f"projects/{project.pk}/v1/source.3mf")


def test_create_next_version_from_mesh_rejects_an_empty_upload(project, media):
    with pytest.raises(ValueError, match="empty"):
        create_next_version_from_mesh(project=project, mesh_bytes=b"", filename="box.stl")

    assert not ModelVersion.objects.exists()


def test_create_next_version_from_mesh_rejects_an_oversized_upload(project, media):
    with (
        override_settings(MESH_MAX_SOURCE_BYTES=16),
        pytest.raises(ValueError, match="above the 16 byte limit"),
    ):
        create_next_version_from_mesh(project=project, mesh_bytes=box_stl(), filename="box.stl")

    assert not ModelVersion.objects.exists()


def test_create_next_version_from_mesh_rejects_a_non_printable_mesh(project, media):
    with pytest.raises(MeshNotPrintableError) as excinfo:
        create_next_version_from_mesh(
            project=project, mesh_bytes=open_box_stl(), filename="broken.stl"
        )

    assert any("not watertight" in problem for problem in excinfo.value.problems)
    assert "not watertight" in str(excinfo.value)
    assert isinstance(excinfo.value, ValueError)
    # Nothing survives the rejection: no version, no file.
    assert not ModelVersion.objects.exists()
    assert not media.exists(f"projects/{project.pk}/v1/source.stl")


def test_create_next_version_from_mesh_rejects_a_mislabelled_mesh(project, media):
    """The suffix picks the parser, so a GLB named ``.stl`` must not sneak in."""
    with pytest.raises(meshcheck.MeshCheckError):
        create_next_version_from_mesh(project=project, mesh_bytes=box_glb(), filename="box.stl")

    # ``MeshCheckError`` is a ``ValueError``, so the API maps it to a 400 too.
    assert issubclass(meshcheck.MeshCheckError, ValueError)
    assert not ModelVersion.objects.exists()


# ---------------------------------------------------------------------------
# attach_source_mesh_to_version
# ---------------------------------------------------------------------------


def test_attach_source_mesh_to_version_replaces_the_previous_mesh(project, media):
    first = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="first.stl"
    )
    first_path = f"projects/{project.pk}/v1/source.stl"

    version = attach_source_mesh_to_version(
        version=first,
        mesh_bytes=box_glb(),
        filename="second.glb",
        scale_mm=10.0,
        reference_note="better scan",
    )

    assert version.pk == first.pk
    assert version.source_mesh.name == f"projects/{project.pk}/v1/source.glb"
    assert not media.exists(first_path)
    assert version.specification_json["transform"] == {"scale_mm": 10.0}
    assert version.reference_note == "better scan"
    assert version.origin == ModelVersionOrigin.MANUAL
    assert ModelVersion.objects.filter(pk=first.pk).count() == 1


def test_attach_source_mesh_to_version_keeps_a_version_without_a_mesh(project, media):
    version = create_next_version(project=project, prompt="parametric", origin="generate")

    version = attach_source_mesh_to_version(
        version=version, mesh_bytes=box_stl(), filename="box.stl"
    )

    assert version.source_mesh.name == f"projects/{project.pk}/v1/source.stl"
    assert version.specification_json["generator"] == "mesh"


def test_attach_source_mesh_to_version_leaves_the_version_untouched_when_rejected(project, media):
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )
    before = ModelVersion.objects.get(pk=version.pk)

    with pytest.raises(MeshNotPrintableError):
        attach_source_mesh_to_version(
            version=version, mesh_bytes=open_box_stl(), filename="broken.stl"
        )

    before.refresh_from_db()
    assert before.source_mesh.name == f"projects/{project.pk}/v1/source.stl"
    assert before.specification_json == version.specification_json
    assert media.exists(before.source_mesh.name)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def test_read_source_mesh_returns_the_stored_bytes(project, media):
    payload = box_stl()
    version = create_next_version_from_mesh(project=project, mesh_bytes=payload, filename="box.stl")

    assert read_source_mesh(version) == (version.source_mesh.name, payload)


def test_read_source_mesh_is_none_without_a_mesh(project, media):
    version = create_next_version(project=project, prompt="parametric")

    assert source_mesh_path(version) is None
    assert read_source_mesh(version) is None


def test_read_source_mesh_is_none_when_the_file_is_gone(project, media):
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )
    media.delete(version.source_mesh.name)

    assert read_source_mesh(version) is None


def test_delete_version_removes_the_source_mesh_file(project, media):
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )
    path = version.source_mesh.name

    delete_version(version)

    assert not ModelVersion.objects.filter(pk=version.pk).exists()
    assert not media.exists(path)


def test_delete_version_removes_every_stored_file(project, media, tmp_path):
    """No orphan in storage: the mesh and every artifact go with the row.

    ``delete_version`` walks a hand-maintained list of model fields, so a new
    file field would silently leak unless it is added to that list. Populating
    every one of them and then asserting the version directory is *empty* (not
    merely that the known paths are gone) catches a field that is stored but
    never cleaned up.
    """
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )
    fields = ("scad_file", "stl_file", "glb_file", "preview_image", "reference_image")
    for field_name in fields:
        relative = f"projects/{project.pk}/v1/{field_name}.bin"
        media.write_bytes(relative, b"payload")
        getattr(version, field_name).name = relative
    version.save(update_fields=[*fields])
    stored = [getattr(version, field).name for field in (*fields, "source_mesh")]

    assert all(media.exists(name) for name in stored)

    delete_version(version)

    version_dir = tmp_path / "projects" / str(project.pk) / "v1"
    remaining = sorted(p.name for p in version_dir.iterdir() if p.is_file())
    assert remaining == [], f"delete_version left files behind: {remaining}"


# ---------------------------------------------------------------------------
# MeshNotPrintableError.problems -- the payload of the 400
# ---------------------------------------------------------------------------
#
# ``problems`` is what the API answers with, so its *contents* matter, not just
# the fact that something was raised: a truncated or reworded message would tell
# the user the wrong thing about their model.


def test_not_printable_error_carries_the_exact_problems(project, media):
    with pytest.raises(MeshNotPrintableError) as excinfo:
        create_next_version_from_mesh(
            project=project, mesh_bytes=open_box_stl(), filename="broken.stl"
        )

    assert excinfo.value.problems == [PROBLEM_NOT_WATERTIGHT]


def test_not_printable_error_reports_every_problem_not_just_the_first(project, media):
    """A single bad mesh can have several independent problems."""
    with pytest.raises(MeshNotPrintableError) as excinfo:
        create_next_version_from_mesh(
            project=project, mesh_bytes=flat_plane_stl(), filename="plane.stl"
        )

    assert excinfo.value.problems == [PROBLEM_NOT_WATERTIGHT, PROBLEM_ZERO_VOLUME]
    # The message is exactly the problems joined, which is what the 400 body is.
    assert str(excinfo.value) == (
        f"The source mesh is not printable: {PROBLEM_NOT_WATERTIGHT}; {PROBLEM_ZERO_VOLUME}"
    )


def test_not_printable_error_problems_are_exactly_the_checker_report(project, media):
    """The service forwards ``check_mesh``'s blocking problems verbatim."""
    report = meshcheck.check_mesh(nan_mesh_stl(), "stl")

    with pytest.raises(MeshNotPrintableError) as excinfo:
        create_next_version_from_mesh(
            project=project, mesh_bytes=nan_mesh_stl(), filename="nan.stl"
        )

    assert excinfo.value.problems == report.problems == [PROBLEM_NON_FINITE]


def test_warnings_are_never_promoted_to_problems(project, media):
    """Only ``problems`` block; advisory findings must not reject an upload."""
    import trimesh

    # Two disjoint solids: perfectly printable, but worth telling the user about.
    first = trimesh.creation.box(extents=(5.0, 5.0, 5.0))
    second = trimesh.creation.box(extents=(5.0, 5.0, 5.0)).apply_translation([40.0, 0.0, 0.0])
    payload = bytes(trimesh.util.concatenate([first, second]).export(file_type="stl"))

    report = meshcheck.check_mesh(payload, "stl")
    assert report.problems == []
    assert any("disconnected bodies" in warning for warning in report.warnings)

    version = create_next_version_from_mesh(project=project, mesh_bytes=payload, filename="two.stl")

    assert source_mesh_warnings(version) == report.warnings
    assert ModelVersion.objects.filter(pk=version.pk).exists()


def test_problems_is_a_fresh_list_the_caller_owns(project, media):
    """``problems`` is a copy, and the message is a snapshot taken at construction.

    Both matter because the message is what the API returns: a caller that trims
    ``problems`` for its own logging must not be able to change the 400 body, and
    must not be able to reach back into the checker's report.
    """
    with pytest.raises(MeshNotPrintableError) as excinfo:
        create_next_version_from_mesh(
            project=project, mesh_bytes=open_box_stl(), filename="broken.stl"
        )

    excinfo.value.problems.clear()

    assert excinfo.value.problems == []
    assert str(excinfo.value) == f"The source mesh is not printable: {PROBLEM_NOT_WATERTIGHT}"
    # A fresh check still reports the real problem: nothing was corrupted.
    assert meshcheck.check_mesh(open_box_stl(), "stl").problems == [PROBLEM_NOT_WATERTIGHT]


# ---------------------------------------------------------------------------
# mesh_max_source_bytes: ONE limit, resolved from ONE source of truth
# ---------------------------------------------------------------------------
#
# The upload is gated twice -- ``designs.services._max_source_bytes`` before
# anything is written, and ``designs.cad.meshcheck`` again inside
# ``load_mesh`` -- so both have to resolve the *same* policy or the effective
# bound is some combination of the two instead of what the operator set.
#
# Every test below goes through ``create_next_version_from_mesh``: that is the
# layer the API (and MCP) calls, and the layer the bug was invisible from. A
# per-module unit test could not have caught it.


def test_max_source_bytes_every_gate_reads_the_same_source_of_truth():
    """No private second reader: the service limit and the checker limit agree.

    ``designs.services`` resolves DB -> settings -> env -> default through
    ``configuration.services.get_setting``; ``designs.cad.meshcheck`` must resolve
    the same chain or the two gates can disagree.
    """
    from designs.services import _max_source_bytes as service_limit

    assert service_limit() == get_setting("mesh_max_source_bytes")
    assert meshcheck._max_source_bytes() == service_limit()

    update_settings(mesh_max_source_bytes=4096)

    assert get_setting("mesh_max_source_bytes") == 4096
    assert service_limit() == 4096
    # The same reader, not a second one: this assertion is the regression guard.
    assert meshcheck._max_source_bytes() == 4096


def test_max_source_bytes_db_override_rejects_an_oversized_upload(project, media):
    update_settings(mesh_max_source_bytes=16)

    with pytest.raises(ValueError, match="above the 16 byte limit"):
        create_next_version_from_mesh(project=project, mesh_bytes=box_stl(), filename="box.stl")

    # Nothing survives the rejection, and the reported limit is the DB value.
    assert not ModelVersion.objects.exists()
    assert not media.exists(f"projects/{project.pk}/v1/source.stl")


def test_max_source_bytes_db_override_tightens_below_the_django_setting(project, media):
    """Tightening works even when the Django setting is far more permissive."""
    update_settings(mesh_max_source_bytes=16)

    with (
        override_settings(MESH_MAX_SOURCE_BYTES=64 * 1024 * 1024),
        pytest.raises(ValueError, match="above the 16 byte limit"),
    ):
        create_next_version_from_mesh(project=project, mesh_bytes=box_stl(), filename="box.stl")

    assert not ModelVersion.objects.exists()


def test_max_source_bytes_db_override_loosens_above_the_django_setting(project, media):
    """The regression: raising the limit in the Settings UI actually raises it.

    While ``meshcheck`` had a private ``django.conf.settings`` reader, the
    effective bound was ``min(DB override, Django setting)``. An operator who
    raised ``mesh_max_source_bytes`` to 1 MiB was still capped at the 16 bytes
    configured for this test, and the rejection quoted a limit they never set.
    """
    update_settings(mesh_max_source_bytes=1024 * 1024)
    payload = box_stl()
    # Precondition: the payload the tightened tier would have rejected.
    assert len(payload) > 16

    with override_settings(MESH_MAX_SOURCE_BYTES=16):
        assert meshcheck._max_source_bytes() == 1024 * 1024
        version = create_next_version_from_mesh(
            project=project, mesh_bytes=payload, filename="box.stl"
        )

    assert version.source_mesh.name == f"projects/{project.pk}/v1/source.stl"
    assert media.read_bytes(version.source_mesh.name) == payload


def test_max_source_bytes_is_read_at_call_time(project, media):
    """The limit is resolved per call, never cached at import time."""
    create_next_version_from_mesh(project=project, mesh_bytes=box_stl(), filename="first.stl")
    update_settings(mesh_max_source_bytes=16)

    with pytest.raises(ValueError, match="above the 16 byte limit"):
        create_next_version_from_mesh(project=project, mesh_bytes=box_stl(), filename="second.stl")


def test_max_source_bytes_db_override_is_cleared_by_an_empty_value(project, media):
    update_settings(mesh_max_source_bytes=16)
    update_settings(mesh_max_source_bytes=None)

    assert get_setting("mesh_max_source_bytes") == meshcheck.DEFAULT_MAX_SOURCE_BYTES
    version = create_next_version_from_mesh(
        project=project, mesh_bytes=box_stl(), filename="box.stl"
    )
    assert version.source_mesh.name


def test_max_source_bytes_env_value_bounds_the_upload_without_a_db_row(project, media):
    """``MESH_MAX_SOURCE_BYTES`` from the environment still works.

    ``config/settings.py`` reads it with a plain ``env(...)`` rather than
    ``env.int(...)``, so when an operator sets it the value reaches the limit as a
    **string** -- which is why the reader casts defensively. Asserted with no DB
    override present, so the env tier is unambiguously the one in play.
    """
    assert get_setting("mesh_max_source_bytes") == meshcheck.DEFAULT_MAX_SOURCE_BYTES

    with override_settings(MESH_MAX_SOURCE_BYTES="16"):
        with pytest.raises(ValueError, match="above the 16 byte limit"):
            create_next_version_from_mesh(project=project, mesh_bytes=box_stl(), filename="box.stl")

    assert not ModelVersion.objects.exists()


def test_max_source_bytes_os_environ_value_bounds_the_upload(monkeypatch, project, media):
    """The raw ``os.environ`` tier, bypassing the Django attribute entirely.

    ``config/settings.py`` snapshots the variable at startup, so the only way to
    reach the ``os.environ`` rung of ``get_setting``'s chain is to remove the
    Django attribute as well. Deleting it inside ``override_settings`` is what
    makes this the genuine env tier rather than a second copy of the test above.
    """
    limit = len(box_stl()) - 1
    monkeypatch.setenv("MESH_MAX_SOURCE_BYTES", str(limit))

    with override_settings():
        del settings.MESH_MAX_SOURCE_BYTES
        invalidate_settings_cache()
        try:
            assert meshcheck._max_source_bytes() == limit
            with pytest.raises(ValueError, match=f"above the {limit} byte limit"):
                create_next_version_from_mesh(
                    project=project, mesh_bytes=box_stl(), filename="box.stl"
                )
        finally:
            invalidate_settings_cache()

    assert not ModelVersion.objects.exists()


@pytest.mark.parametrize("garbage", ["", "banana", 0, -1, None])
def test_max_source_bytes_garbage_falls_back_to_the_default(garbage, project, media):
    """A nonsense value degrades to the default.

    It must never raise, and it must never become "no limit at all" -- a
    non-positive bound would accept every upload, which is the opposite of a size
    gate. Both gates have to land on the *same* default, or the operator is back
    to a silent ``min()`` of two readers.
    """
    from designs.services import _max_source_bytes as service_limit

    with override_settings(MESH_MAX_SOURCE_BYTES=garbage):
        assert meshcheck._max_source_bytes() == meshcheck.DEFAULT_MAX_SOURCE_BYTES > 0
        assert service_limit() == meshcheck.DEFAULT_MAX_SOURCE_BYTES
        # The bound is still a real one: the box is accepted, and nothing blows up
        # on the way in.
        version = create_next_version_from_mesh(
            project=project, mesh_bytes=box_stl(), filename="box.stl"
        )

    assert version.source_mesh.name


# ---------------------------------------------------------------------------
# source_mesh_format (the read-only projection used by the serializer)
# ---------------------------------------------------------------------------


def test_source_mesh_format_reports_the_stored_suffix(project, media):
    """Each container format round-trips: the suffix picks the parser, and it
    is the same suffix ``source_mesh_format`` reports back."""
    for filename, payload, expected in (
        ("box.stl", box_stl(), "stl"),
        ("box.STL", box_stl(), "stl"),
        ("model.obj", box_obj(), "obj"),
        ("model.glb", box_glb(), "glb"),
    ):
        version = create_next_version_from_mesh(
            project=project, mesh_bytes=payload, filename=filename
        )
        assert source_mesh_format(version) == expected
        # The stored name and the projection can never disagree.
        assert version.source_mesh.name.endswith(f"source.{expected}")


def test_source_mesh_format_is_empty_without_a_mesh(project, media):
    version = create_next_version(project=project, prompt="parametric")

    assert source_mesh_format(version) == ""


def test_source_mesh_format_ignores_a_foreign_suffix(project, media):
    """A read-only projection never raises, even on a hand-written name."""
    version = create_next_version(project=project, prompt="parametric")
    version.source_mesh.name = "projects/1/v1/source.step"
    version.save(update_fields=["source_mesh"])

    assert source_mesh_format(version) == ""
