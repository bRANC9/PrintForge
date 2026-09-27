"""Tests for the mesh CAD backend and the CAD backend registry.

The transform maths is verified from the *exported STL* (not from internal
state), so the assertions cover what actually lands in storage: the part rests
on the build plate at ``min Z == 0``, X/Y are centred and the largest
bounding-box edge equals ``transform.scale_mm``.
"""

from __future__ import annotations

import io
import logging
import sys

import pytest
from django.test import override_settings

from designs.cad.base import CADBackend, GeneratedModel, SpecificationError, UnsupportedFormatError
from designs.cad.mesh import MeshCADBackend
from designs.cad.openscad import OpenSCADBackend
from designs.cad.pipeline import (
    DEFAULT_CAD_BACKEND,
    get_backend,
    resolve_backend_name,
)
from files.services import LocalStorage

SOURCE = "reference_uploads/1/abc123_model.glb"

#: A cube is 12 triangles, so a 4-face budget is always over budget.
OVER_BUDGET_REPAIR = {"enabled": True, "target_faces": 4}


def _write_source(storage: LocalStorage, payload: bytes, path: str = SOURCE) -> str:
    storage.write_bytes(path, payload)
    return path


@pytest.fixture
def storage(tmp_path) -> LocalStorage:
    return LocalStorage(root=tmp_path)


@pytest.fixture
def source_glb(storage: LocalStorage) -> str:
    import trimesh

    # ``file_type`` is required: trimesh 5 treats a bare ``export("glb")`` as a
    # file object and writes a junk file named ``glb`` into the cwd.
    return _write_source(
        storage,
        trimesh.creation.box(extents=(1.0, 0.5, 0.25)).export(file_type="glb"),
    )


def _load(payload: bytes):
    import trimesh

    return trimesh.load(io.BytesIO(payload), file_type="stl")


def _spec(path: str, **transform) -> dict:
    return {"generator": "mesh", "mesh": {"source": path}, "transform": transform}


class _BlockImport:
    """A ``sys.meta_path`` finder that makes importing one module fail.

    Returning ``None`` would just let the next finder try, so this *raises*
    :class:`ImportError` from ``find_spec`` -- which is exactly what CPython does
    when a module is not installed.
    """

    def __init__(self, blocked: str) -> None:
        self._blocked = blocked

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001, ARG002
        if fullname == self._blocked or fullname.startswith(f"{self._blocked}."):
            raise ImportError(f"{self._blocked} is blocked for this test")
        return None


@pytest.fixture
def without_fast_simplification(monkeypatch):
    """Simulate an install where ``fast-simplification`` is unavailable.

    ``fast-simplification`` is a *required* dependency now, so the degraded path
    no longer happens in CI and would otherwise go untested. It is reached
    through ``trimesh.Trimesh.simplify_quadric_decimation``, which does a
    **function-local** ``from fast_simplification import simplify``
    (``trimesh/base.py``) -- so the lookup is re-resolved on every call and a
    finder is enough to break it. The catch is ``sys.modules``: the import system
    answers from that cache before consulting ``sys.meta_path``, so an
    already-imported ``fast_simplification`` has to be evicted too or the finder
    is never consulted and the test would pass for the wrong reason.

    ``monkeypatch`` restores both ``sys.modules`` and ``sys.meta_path``.
    """
    for name in [name for name in sys.modules if name.startswith("fast_simplification")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_BlockImport("fast_simplification"), *sys.meta_path])

    # Assert the precondition, so a silently ineffective blocker fails the test
    # instead of quietly making it a second copy of the happy-path test.
    with pytest.raises(ImportError, match="fast_simplification is blocked"):
        import fast_simplification  # noqa: F401


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_backend_declares_its_artifacts():
    assert MeshCADBackend.name == "mesh"
    assert MeshCADBackend.supported_formats == ("stl",)
    assert MeshCADBackend.produced_artifacts == ("stl",)
    assert MeshCADBackend.produces_source_code is False
    assert issubclass(MeshCADBackend, CADBackend)


def test_openscad_backend_contract_is_unchanged():
    assert OpenSCADBackend.produced_artifacts == ("scad", "stl")
    assert OpenSCADBackend.produces_source_code is True


def test_generate_returns_a_manifest_and_build_model_carries_the_mesh(storage, source_glb):
    backend = MeshCADBackend(storage=storage)

    manifest = backend.generate(_spec(source_glb, scale_mm=120.0))
    model = backend.build_model(_spec(source_glb, scale_mm=120.0))

    assert manifest == model.scad_source
    assert SOURCE in manifest
    assert "faces: 12 -> 12" in manifest
    assert model.mesh_format == "stl"
    assert model.mesh_bytes
    assert b"solid" not in model.mesh_bytes  # binary STL, not ASCII


def test_missing_mesh_source_raises_specification_error(storage):
    backend = MeshCADBackend(storage=storage)

    with pytest.raises(SpecificationError):
        backend.generate({"mesh": {}})
    with pytest.raises(SpecificationError):
        backend.generate({})


def test_unknown_mesh_source_format_raises_specification_error(storage):
    backend = MeshCADBackend(storage=storage)

    with pytest.raises(SpecificationError):
        backend.generate(_spec("uploads/thing.step"))


def test_unreadable_mesh_source_raises_specification_error(storage):
    backend = MeshCADBackend(storage=storage)

    with pytest.raises(SpecificationError):
        backend.generate(_spec("reference_uploads/1/missing.glb"))


def test_flat_source_mesh_field_is_accepted(storage, source_glb):
    backend = MeshCADBackend(storage=storage)

    model = backend.build_model({"source_mesh": source_glb, "transform": {"scale_mm": 40.0}})

    assert model.mesh_bytes


# ---------------------------------------------------------------------------
# Transform maths (verified from the exported STL)
# ---------------------------------------------------------------------------


def test_largest_edge_equals_scale_mm(storage, source_glb):
    backend = MeshCADBackend(storage=storage)

    model = backend.build_model(_spec(source_glb, scale_mm=120.0))
    out = _load(model.mesh_bytes)

    assert float(out.extents.max()) == pytest.approx(120.0, abs=1e-4)
    # The source is 1.0 x 0.5 x 0.25, so the aspect ratio is preserved exactly.
    assert list(out.extents) == pytest.approx([120.0, 60.0, 30.0], abs=1e-4)


def test_min_z_is_zero_and_xy_is_centred(storage, source_glb):
    backend = MeshCADBackend(storage=storage)

    model = backend.build_model(
        _spec(source_glb, scale_mm=90.0, rest_on_plate=True, center_xy=True)
    )
    out = _load(model.mesh_bytes)

    assert float(out.bounds[0][2]) == pytest.approx(0.0, abs=1e-5)
    assert out.bounds[0][0] == pytest.approx(-out.bounds[1][0], abs=1e-4)
    assert out.bounds[0][1] == pytest.approx(-out.bounds[1][1], abs=1e-4)
    # The source is 1.0 x 0.5 x 0.25, so 90mm of largest edge is 90 x 45 x 22.5.
    assert float(out.bounds[1][2]) == pytest.approx(22.5, abs=1e-4)


def test_rest_on_plate_can_be_disabled(storage, source_glb):
    backend = MeshCADBackend(storage=storage)

    model = backend.build_model(
        _spec(source_glb, scale_mm=90.0, rest_on_plate=False, center_xy=True)
    )
    out = _load(model.mesh_bytes)

    assert float(out.bounds[0][2]) < 0.0


def test_rotation_is_xyz_degrees_about_the_bounding_box_centre(storage, source_glb):
    import numpy as np

    backend = MeshCADBackend(storage=storage)
    # Scaling off, so the raw source extents (1.0 x 0.5 x 0.25) are observable.
    upright = _load(backend.build_model(_spec(source_glb, scale_mm=0.0)).mesh_bytes)
    turned = _load(
        backend.build_model(_spec(source_glb, scale_mm=0.0, rotate_deg=[0.0, 0.0, 45.0])).mesh_bytes
    )

    # A 1.0 x 0.5 rectangle turned 45 degrees about Z is a square of side
    # (1.0 + 0.5) / sqrt(2) in both X and Y; Z is untouched.
    assert float(upright.extents[0]) == pytest.approx(1.0, abs=1e-6)
    diagonal = 1.5 / np.sqrt(2.0)
    assert float(turned.extents[0]) == pytest.approx(diagonal, abs=1e-6)
    assert float(turned.extents[1]) == pytest.approx(diagonal, abs=1e-6)
    assert float(turned.extents[2]) == pytest.approx(0.25, abs=1e-6)
    # Still resting on the plate after rotating.
    assert float(turned.bounds[0][2]) == pytest.approx(0.0, abs=1e-6)


def test_scaling_happens_after_rotation(storage, source_glb):
    backend = MeshCADBackend(storage=storage)

    model = backend.build_model(_spec(source_glb, scale_mm=60.0, rotate_deg=[0.0, 0.0, 45.0]))
    out = _load(model.mesh_bytes)

    # The rotated footprint is the largest edge, so the result is 60 x 60 x 14.14.
    assert float(out.extents.max()) == pytest.approx(60.0, abs=1e-4)
    assert list(out.extents[:2]) == pytest.approx([60.0, 60.0], abs=1e-4)
    assert float(out.bounds[0][2]) == pytest.approx(0.0, abs=1e-5)


def test_default_scale_comes_from_the_setting(storage, source_glb):
    backend = MeshCADBackend(storage=storage)

    with override_settings(MESH_DEFAULT_SCALE_MM=75.0):
        model = backend.build_model({"mesh": {"source": source_glb}})

    assert float(_load(model.mesh_bytes).extents.max()) == pytest.approx(75.0, abs=1e-4)


def test_non_positive_scale_keeps_the_source_units(storage, source_glb):
    backend = MeshCADBackend(storage=storage)

    model = backend.build_model(_spec(source_glb, scale_mm=0.0))

    assert float(_load(model.mesh_bytes).extents.max()) == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def test_repair_reports_what_it_did(storage, source_glb):
    backend = MeshCADBackend(storage=storage)

    manifest = backend.generate(_spec(source_glb, scale_mm=50.0))

    assert "fix_normals" in manifest
    assert "fill_holes" in manifest
    assert "decimation" not in manifest


def test_repair_can_be_disabled(storage, source_glb):
    backend = MeshCADBackend(storage=storage)

    manifest = backend.generate(
        {
            "mesh": {"source": source_glb},
            "repair": {"enabled": False},
            "transform": {"scale_mm": 50.0},
        }
    )

    assert "repair: repair disabled" in manifest


def test_decimation_over_budget_reduces_the_faces_to_the_budget(storage, source_glb):
    """The success path: ``fast_simplification`` is present, so the mesh shrinks."""
    backend = MeshCADBackend(storage=storage)

    model = backend.build_model(
        _spec(source_glb, scale_mm=50.0, rest_on_plate=True) | {"repair": OVER_BUDGET_REPAIR}
    )

    # The cube is 12 faces and the budget is 4, so decimation really ran and
    # really reached the budget -- it did not merely return a "skipped" note.
    assert "decimated 12 -> 4 faces" in model.scad_source
    assert "decimation skipped" not in model.scad_source
    assert len(_load(model.mesh_bytes).faces) == 4


def test_decimation_over_budget_never_fails_the_render(
    storage, source_glb, without_fast_simplification, caplog
):
    """The degraded path: no ``fast_simplification``, so decimate loudly.

    The valuable property is the second half: an over-budget mesh must **never**
    fail the render, because a missing decimation extra is an environment
    problem, not a bad part. The mesh is still exported, and -- because nothing
    was simplified -- it is still the original watertight solid.
    """
    backend = MeshCADBackend(storage=storage)

    with caplog.at_level(logging.INFO, logger="designs.cad.mesh"):
        model = backend.build_model(
            _spec(source_glb, scale_mm=50.0, rest_on_plate=True) | {"repair": OVER_BUDGET_REPAIR}
        )

    # Loud: the shortfall is logged for the operator, not just recorded in the
    # manifest, so a silent over-budget part is visible in the worker logs.
    assert any("mesh decimation unavailable" in record.message for record in caplog.records)

    # Specific and non-fatal: the manifest names the shortfall and the exception
    # type instead of silently producing an over-budget part.
    assert "decimation skipped, 12 faces above the 4 budget" in model.scad_source
    assert "ImportError" in model.scad_source

    assert model.mesh_bytes
    out = _load(model.mesh_bytes)
    assert out.is_watertight
    # Nothing was decimated, so the render is the full original 12-face solid.
    assert len(out.faces) == 12

    # The render is still valid: the blocking gate passes on the unsimplified mesh.
    assert backend.validate(model) == []


def test_target_faces_below_the_minimum_is_clamped(storage, source_glb):
    from designs.cad.mesh import MIN_TARGET_FACES, _parse_repair

    assert _parse_repair({"target_faces": 0}).target_faces == MIN_TARGET_FACES
    assert _parse_repair({"target_faces": 100}).target_faces == 100
    assert _parse_repair(None).target_faces > 0


# ---------------------------------------------------------------------------
# validate / export
# ---------------------------------------------------------------------------


def test_validate_accepts_a_healthy_mesh(storage, source_glb):
    backend = MeshCADBackend(storage=storage)
    model = backend.build_model(_spec(source_glb, scale_mm=60.0))

    assert backend.validate(model) == []


def test_validate_reports_blocking_problems(storage):
    import trimesh

    box = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    holed = trimesh.Trimesh(vertices=box.vertices.copy(), faces=box.faces[:-1], process=False)
    path = _write_source(storage, holed.export(file_type="stl"), "reference_uploads/1/broken.stl")

    backend = MeshCADBackend(storage=storage)
    model = backend.build_model(_spec(path, scale_mm=60.0))

    problems = backend.validate(model)
    assert problems
    assert any("not watertight" in problem for problem in problems)


def test_validate_without_mesh_bytes_is_blocking(storage):
    backend = MeshCADBackend(storage=storage)
    model = GeneratedModel(specification={}, scad_source="manifest")

    assert backend.validate(model) == ["mesh backend produced no mesh bytes"]


def test_export_returns_binary_stl_and_rejects_other_formats(storage, source_glb):
    backend = MeshCADBackend(storage=storage)
    model = backend.build_model(_spec(source_glb, scale_mm=60.0))

    assert backend.export(model, "stl") == model.mesh_bytes
    assert backend.export(model, "STL") == model.mesh_bytes

    with pytest.raises(UnsupportedFormatError):
        backend.export(model, "obj")
    with pytest.raises(UnsupportedFormatError):
        backend.export(model, "")


def test_export_without_a_mesh_raises(storage):
    from designs.cad.base import CADError

    backend = MeshCADBackend(storage=storage)
    model = GeneratedModel(specification={})

    with pytest.raises(CADError):
        backend.export(model, "stl")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_default_backend_is_openscad():
    assert DEFAULT_CAD_BACKEND == "openscad"
    assert resolve_backend_name() == "openscad"
    assert isinstance(get_backend(), OpenSCADBackend)


def test_registry_maps_every_alias():
    assert isinstance(get_backend("openscad"), OpenSCADBackend)
    assert isinstance(get_backend("scad"), OpenSCADBackend)
    assert isinstance(get_backend("Mesh"), MeshCADBackend)


def test_explicit_argument_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("MESH_BACKEND", "mesh")

    assert resolve_backend_name("openscad") == "openscad"
    assert resolve_backend_name("  MESH  ") == "mesh"
    assert isinstance(get_backend("openscad"), OpenSCADBackend)


def test_mesh_backend_env_override(monkeypatch):
    monkeypatch.setenv("MESH_BACKEND", "mesh")

    assert resolve_backend_name() == "mesh"
    assert isinstance(get_backend(), MeshCADBackend)


def test_whitespace_in_the_env_value_is_ignored(monkeypatch):
    monkeypatch.setenv("MESH_BACKEND", "   ")

    assert resolve_backend_name() == "openscad"


def test_django_setting_is_used_when_the_env_is_unset(settings):
    settings.MESH_BACKEND = "mesh"

    assert resolve_backend_name() == "mesh"
    assert isinstance(get_backend(), MeshCADBackend)


def test_runtime_setting_is_the_last_tier(monkeypatch, settings):
    monkeypatch.delenv("MESH_BACKEND", raising=False)
    settings.MESH_BACKEND = ""

    monkeypatch.setattr(
        "configuration.services.get_setting",
        lambda name: "mesh" if name == "mesh_backend" else None,
    )

    assert resolve_backend_name() == "mesh"


def test_unknown_runtime_setting_key_is_skipped(monkeypatch, settings):
    """``cad_backend`` is not a registered key, so the lookup must just be ignored."""
    monkeypatch.delenv("MESH_BACKEND", raising=False)
    settings.MESH_BACKEND = ""

    def boom(name: str) -> str:
        if name == "mesh_backend":
            return ""
        raise KeyError(name)

    monkeypatch.setattr("configuration.services.get_setting", boom)

    assert resolve_backend_name() == "openscad"


def test_unknown_backend_name_raises(monkeypatch):
    from designs.cad.base import CADError

    monkeypatch.setenv("MESH_BACKEND", "freecad")

    with pytest.raises(CADError) as excinfo:
        get_backend()
    assert "freecad" in str(excinfo.value)
    assert "mesh" in str(excinfo.value)
    assert "openscad" in str(excinfo.value)
