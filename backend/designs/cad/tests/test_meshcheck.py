"""Tests for the dependency-light mesh printability report.

The report must stay deterministic and must never raise ``ImportError``: a
missing optional dependency (or the uninstalled ``scipy``/``networkx`` graph
engines trimesh would otherwise use) has to surface as
:class:`MeshCheckError` or as an unmeasured stat, never as a crash.

The source-size bound is covered at the *service* layer too
(``designs/tests/test_mesh_import.py``), because the bug it had -- two gates
resolving one policy through two different readers -- is only visible where both
gates run. The tests here pin the reader itself and the ``max_bytes`` seam.
"""

from __future__ import annotations

import struct

import pytest
from django.core.cache import cache
from django.test import override_settings

from designs.cad.meshcheck import (
    DEFAULT_MAX_SOURCE_BYTES,
    MAX_PRINT_MM,
    MAX_SOURCE_BYTES_SETTING,
    MIN_FEATURE_MM,
    SUPPORTED_MESH_FORMATS,
    MeshCheckError,
    _coerce_limit,
    _max_source_bytes,
    check_mesh,
    load_mesh,
)


def _box(extents=(10.0, 20.0, 30.0)) -> bytes:
    import trimesh

    return trimesh.creation.box(extents=extents).export(file_type="stl")


def _two_bodies() -> bytes:
    import trimesh

    return trimesh.util.concatenate(
        [
            trimesh.creation.box(extents=(10.0, 10.0, 10.0)),
            trimesh.creation.box(extents=(10.0, 10.0, 10.0)).apply_translation([50.0, 0.0, 0.0]),
        ]
    ).export(file_type="stl")


def _with_hole() -> bytes:
    """A box with one of its six faces removed (a single boundary loop)."""
    import trimesh

    box = trimesh.creation.box(extents=(10.0, 20.0, 30.0))
    holed = trimesh.Trimesh(
        vertices=box.vertices.copy(), faces=box.faces[:-1].copy(), process=False
    )
    return holed.export(file_type="stl")


def _inconsistent_winding() -> bytes:
    """A closed box with exactly one triangle wound the wrong way."""
    import trimesh

    box = trimesh.creation.box(extents=(10.0, 20.0, 30.0))
    faces = box.faces.copy()
    faces[0] = faces[0][::-1]
    flipped = trimesh.Trimesh(vertices=box.vertices.copy(), faces=faces, process=False)
    return flipped.export(file_type="stl")


def _non_finite() -> bytes:
    """A binary STL whose third vertex is ``inf`` (survives ``process=False``)."""
    facet = struct.pack(
        "<12fH",
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        float("inf"),
        0.0,
        0.0,
        0,
    )
    return b"\x00" * 80 + struct.pack("<I", 1) + facet


def _degenerate() -> bytes:
    """One zero-area triangle: parses, but encloses nothing."""
    import trimesh

    collapsed = trimesh.Trimesh(
        vertices=[[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
        faces=[[0, 1, 2]],
        process=False,
    )
    return collapsed.export(file_type="stl")


@pytest.fixture
def size_bound(db):
    """Resolve the source-size bound for real: a database and a clean cache.

    ``configuration.services`` memoises the ``AppSettings`` singleton in Django's
    cache backend, and the per-test transaction rollback does not undo that -- so
    without an explicit clear a row written by one test can silently change the
    limit the next one sees.

    ``db`` is requested deliberately. The reader's whole job is to consult
    ``get_setting``; a test that ran without database access would fall through
    to the ``django.conf.settings`` fallback and would therefore keep passing
    even if the runtime-aware path broke -- exactly the gap that let the two
    readers drift apart in the first place.
    """
    cache.clear()
    yield
    cache.clear()


def test_supported_formats_are_documented():
    assert SUPPORTED_MESH_FORMATS == ("stl", "obj", "glb")


def test_watertight_box_is_clean():
    report = check_mesh(_box(), "stl")

    assert report.ok
    assert report.problems == []
    assert report.warnings == []
    assert report.stats["format"] == "stl"
    assert report.stats["faces"] == 12
    assert report.stats["vertices"] == 8
    assert report.stats["watertight"] is True
    assert report.stats["winding_consistent"] is True
    assert report.stats["euler_number"] == 2
    assert report.stats["body_count"] == 1
    assert report.stats["volume_mm3"] == pytest.approx(10.0 * 20.0 * 30.0)
    assert report.stats["extents_mm"] == pytest.approx((10.0, 20.0, 30.0))
    assert report.stats["bounds_mm"] == (
        pytest.approx((-5.0, -10.0, -15.0)),
        pytest.approx((5.0, 10.0, 15.0)),
    )
    assert report.stats["degenerate_faces"] == 0


def test_box_with_a_hole_is_not_printable():
    report = check_mesh(_with_hole(), "stl")

    assert not report.ok
    assert any("not watertight" in problem for problem in report.problems)
    assert report.stats["watertight"] is False
    assert report.stats["euler_number"] == 1


def test_inconsistent_winding_is_not_printable():
    report = check_mesh(_inconsistent_winding(), "stl")

    assert not report.ok
    assert any("winding" in problem for problem in report.problems)
    assert report.stats["winding_consistent"] is False


def test_non_finite_coordinates_are_not_printable():
    report = check_mesh(_non_finite(), "stl")

    assert not report.ok
    assert any("non-finite" in problem for problem in report.problems)
    assert report.stats["watertight"] is None
    assert report.stats["volume_mm3"] is None


def test_zero_volume_mesh_is_not_printable():
    report = check_mesh(_degenerate(), "stl")

    assert not report.ok
    assert any("zero volume" in problem for problem in report.problems)
    assert report.stats["degenerate_faces"] == 1


def test_multiple_bodies_are_only_a_warning():
    report = check_mesh(_two_bodies(), "stl")

    assert report.ok
    assert report.stats["body_count"] == 2
    assert any("disconnected bodies" in warning for warning in report.warnings)


def test_dimensions_outside_the_envelope_are_only_a_warning():
    report = check_mesh(_box(extents=(0.01, 1.0, 1.0)), "stl")

    assert report.ok
    assert any("print minimum" in warning for warning in report.warnings)

    huge = check_mesh(_box(extents=(MAX_PRINT_MM * 2.0, 10.0, 10.0)), "stl")
    assert huge.ok
    assert any("print envelope" in warning for warning in huge.warnings)


def test_thin_features_are_only_a_warning():
    report = check_mesh(_box(extents=(10.0, 10.0, MIN_FEATURE_MM / 4.0)), "stl")

    assert report.ok
    assert any("printable feature size" in warning for warning in report.warnings)
    assert report.stats["min_edge_mm"] < MIN_FEATURE_MM


@pytest.mark.parametrize("payload", [b"", b"not a mesh", b"\x00\x01\x02\x03"])
def test_unusable_payloads_raise_mesh_check_error(payload):
    with pytest.raises(MeshCheckError):
        check_mesh(payload, "stl")


def test_unsupported_format_raises_mesh_check_error():
    with pytest.raises(MeshCheckError):
        check_mesh(_box(), "step")


def test_geometry_free_payload_raises_mesh_check_error():
    import trimesh

    with pytest.raises(MeshCheckError):
        check_mesh(b"\x00" * 80 + struct.pack("<I", 0), "stl")

    # An OBJ with only vertices loads as a point cloud, i.e. no triangle mesh.
    with pytest.raises(MeshCheckError):
        check_mesh(b"v 0 0 0\nv 1 0 0\nv 0 1 0\n", "obj")

    assert trimesh is not None


@override_settings(MESH_MAX_SOURCE_BYTES=512)
def test_oversized_source_is_rejected(size_bound):
    payload = _box()
    assert len(payload) > 512

    with pytest.raises(MeshCheckError) as excinfo:
        check_mesh(payload, "stl")
    assert "512 byte limit" in str(excinfo.value)


@override_settings(MESH_MAX_SOURCE_BYTES=1 << 20)
def test_size_limit_default_does_not_reject_a_small_mesh(size_bound):
    assert check_mesh(_box(), "stl").ok


# ---------------------------------------------------------------------------
# The size bound: one reader, and an explicit max_bytes seam
# ---------------------------------------------------------------------------


def test_the_setting_name_is_the_runtime_setting_meshcheck_resolves():
    """The registry entry is the contract, not a duplicated string literal."""
    from configuration.services import SETTING_NAMES

    assert MAX_SOURCE_BYTES_SETTING in SETTING_NAMES
    assert MAX_SOURCE_BYTES_SETTING == "mesh_max_source_bytes"


def test_reader_is_the_configuration_setting_not_the_django_attribute(size_bound):
    """A DB override wins over ``django.conf.settings`` -- the regression.

    This is the exact divergence that was reported: the checker used to read only
    ``django.conf.settings.MESH_MAX_SOURCE_BYTES``, so a *raised* DB override was
    silently capped by the Django value (effective limit ``min(DB, Django)``),
    and the rejection message quoted a limit the operator never set.
    """
    from configuration.services import update_settings

    update_settings(**{MAX_SOURCE_BYTES_SETTING: 1024 * 1024})
    with override_settings(MESH_MAX_SOURCE_BYTES=16):
        # The tighter Django tier loses: one policy, not the smaller of two.
        assert _max_source_bytes() == 1024 * 1024
        assert check_mesh(_box(), "stl").ok


def test_reader_tightening_wins_the_other_way_round(size_bound):
    """...and a *tighter* DB override is honoured even when the Django value is
    far more permissive. Both directions, so neither can regress silently."""
    from configuration.services import update_settings

    update_settings(**{MAX_SOURCE_BYTES_SETTING: 16})
    with override_settings(MESH_MAX_SOURCE_BYTES=64 * 1024 * 1024):
        assert _max_source_bytes() == 16
        with pytest.raises(MeshCheckError, match="16 byte limit"):
            check_mesh(_box(), "stl")


def test_reader_follows_a_db_override_changed_between_calls(size_bound):
    """Resolved per call, never memoised, so a Settings UI change applies at once."""
    from configuration.services import update_settings

    assert check_mesh(_box(), "stl").ok
    update_settings(**{MAX_SOURCE_BYTES_SETTING: 16})
    with pytest.raises(MeshCheckError, match="16 byte limit"):
        check_mesh(_box(), "stl")
    update_settings(**{MAX_SOURCE_BYTES_SETTING: None})
    assert check_mesh(_box(), "stl").ok


def test_reader_never_raises_and_never_returns_a_non_positive_bound(size_bound):
    """Even a hostile configuration value leaves a real bound behind."""
    for garbage in ("", "banana", 0, -1, None, object()):
        assert _coerce_limit(garbage) is None
    with override_settings(MESH_MAX_SOURCE_BYTES="banana"):
        resolved = _max_source_bytes()
    assert resolved == DEFAULT_MAX_SOURCE_BYTES > 0


def test_coerce_limit_accepts_the_types_the_bound_really_arrives_in():
    """``config.settings`` uses a plain ``env(...)`` (no cast), so a *string* is a
    normal arrival for this bound, not an edge case -- and an env var read from a
    shell or a compose file is routinely padded. ``"4096.0"`` is not an integer
    and must be treated as unusable, not silently truncated to 4096.
    """
    assert _coerce_limit(4096) == 4096
    assert _coerce_limit("4096") == 4096
    assert _coerce_limit("  4096  ") == 4096
    assert _coerce_limit(4.0 * 1000) == 4000
    assert _coerce_limit("4096.0") is None
    # Deliberately *not* special-cased: ``designs.services`` coerces the same way
    # (``int(get_setting(...))``), and two readers that disagree on a pathological
    # input are the very thing this module's single-source-of-truth rule exists
    # to prevent.
    assert _coerce_limit(True) == int(True)


def test_max_bytes_makes_the_limit_an_explicit_dependency(size_bound):
    """A caller that already resolved the limit can pass it in."""
    payload = _box()
    assert len(payload) > 16

    with override_settings(MESH_MAX_SOURCE_BYTES=1 << 20):
        # The permissive default accepts it; an explicit tighter bound does not.
        assert load_mesh(payload, "stl") is not None
        with pytest.raises(MeshCheckError, match="16 byte limit"):
            load_mesh(payload, "stl", max_bytes=16)
        with pytest.raises(MeshCheckError, match="16 byte limit"):
            check_mesh(payload, "stl", max_bytes=16)


def test_max_bytes_also_loosens_a_tighter_configured_limit(size_bound):
    """The override works in both directions, and is honoured verbatim."""
    with override_settings(MESH_MAX_SOURCE_BYTES=16):
        with pytest.raises(MeshCheckError, match="16 byte limit"):
            check_mesh(_box(), "stl")
        assert check_mesh(_box(), "stl", max_bytes=1 << 20).ok


def test_max_bytes_defaults_to_the_one_runtime_aware_reader(size_bound):
    payload = _box()
    with override_settings(MESH_MAX_SOURCE_BYTES=1 << 20):
        assert load_mesh(payload, "stl") is not None
        assert load_mesh(payload, "stl", max_bytes=None) is not None

    with override_settings(MESH_MAX_SOURCE_BYTES=16):
        for call in (
            lambda: load_mesh(payload, "stl"),
            lambda: load_mesh(payload, "stl", max_bytes=None),
            lambda: check_mesh(payload, "stl"),
            lambda: check_mesh(payload, "stl", max_bytes=None),
        ):
            with pytest.raises(MeshCheckError, match="16 byte limit"):
                call()


def test_a_nonsense_max_bytes_never_removes_the_bound(size_bound):
    """An unusable override degrades to the resolved limit, not to "no limit"."""
    with override_settings(MESH_MAX_SOURCE_BYTES=1 << 20):
        assert load_mesh(_box(), "stl", max_bytes=0) is not None
        assert load_mesh(_box(), "stl", max_bytes=-1) is not None
        assert load_mesh(_box(), "stl", max_bytes="banana") is not None

    # ...and the resolved limit it degrades to is still enforced.
    with override_settings(MESH_MAX_SOURCE_BYTES=16):
        for bad in (0, -1, "banana", ""):
            with pytest.raises(MeshCheckError, match="16 byte limit"):
                load_mesh(_box(), "stl", max_bytes=bad)


def test_glb_and_obj_sources_are_supported():
    import trimesh

    box = trimesh.creation.box(extents=(10.0, 20.0, 30.0))
    obj = box.export(file_type="obj")
    assert check_mesh(obj.encode("utf-8"), "obj").ok
    assert check_mesh(box.export(file_type="glb"), "glb").ok


def test_format_extension_with_a_leading_dot_is_accepted():
    assert check_mesh(_box(), ".STL").stats["format"] == "stl"


def test_load_mesh_keeps_raw_vertices_and_check_mesh_welds_them():
    raw = load_mesh(_box(), "stl")
    # ``process=False`` keeps STL's 3 vertices per triangle so non-finite
    # coordinates survive; ``check_mesh`` welds them before measuring topology.
    assert len(raw.vertices) == 36

    report = check_mesh(_box(), "stl")
    assert report.stats["vertices"] == 8
    assert report.stats["watertight"] is True


def test_report_is_deterministic():
    payload = _box()

    first = check_mesh(payload, "stl")
    second = check_mesh(payload, "stl")

    assert first.stats == second.stats
    assert first.problems == second.problems
    assert first.warnings == second.warnings


def test_stats_do_not_depend_on_a_graph_engine():
    """``scipy``/``networkx`` are not project dependencies, so body_count is numpy-only."""
    import numpy as np
    import trimesh

    from designs.cad.meshcheck import _connected_bodies

    box = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
    assert _connected_bodies(trimesh, np, box) == 1
    assert (
        _connected_bodies(
            trimesh,
            np,
            trimesh.util.concatenate(
                [box, box.copy().apply_translation([5.0, 0.0, 0.0]), box.copy()]
            ),
        )
        == 3
    )
    assert _connected_bodies(trimesh, np, trimesh.Trimesh()) == 0
