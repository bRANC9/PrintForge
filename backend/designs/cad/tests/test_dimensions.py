"""Unit tests for the post-render dimension check.

Pure numbers in, list of advisory strings out: no mesh, no trimesh, no Django and
no render. The integration through ``render_version`` (and therefore through the
real OpenSCAD sandbox) is in ``test_pipeline.py``.
"""

from __future__ import annotations

import pytest

from designs.cad.dimensions import (
    AXIS_DIMENSION_KEYS,
    DIMENSION_TOLERANCE,
    MIN_Z_EPSILON_MM,
    dimension_issues,
)

DIMS = {"width": 40.0, "height": 60.0, "thickness": 10.0}


# ---------------------------------------------------------------------------
# Constants / contract
# ---------------------------------------------------------------------------


def test_axis_mapping_is_width_x_height_y_thickness_z():
    assert AXIS_DIMENSION_KEYS == (("X", "width"), ("Y", "height"), ("Z", "thickness"))


def test_tolerance_and_plate_epsilon_are_the_documented_ones():
    assert DIMENSION_TOLERANCE == 0.25
    assert MIN_Z_EPSILON_MM == 0.01


def test_module_imports_without_trimesh(monkeypatch):
    """The whole point of the split: numbers in, no mesh library needed.

    ``builtins.__import__`` is stubbed so *any* import of ``trimesh``/``numpy``
    reached during the call raises, which is stronger than checking that the
    module namespace happens to be free of them.
    """
    import builtins

    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if name.split(".")[0] in {"trimesh", "numpy"}:
            raise AssertionError(f"dimension_issues must not import {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    assert dimension_issues((40.0, 60.0, 10.0), DIMS) == []
    assert dimension_issues((100.0, 60.0, 10.0), DIMS, min_z_mm=1.0) != []


def test_is_deterministic():
    first = dimension_issues((100.0, 60.0, 10.0), DIMS, min_z_mm=3.0)
    second = dimension_issues((100.0, 60.0, 10.0), DIMS, min_z_mm=3.0)
    assert first == second
    assert len(first) == 2


# ---------------------------------------------------------------------------
# In tolerance
# ---------------------------------------------------------------------------


def test_exact_match_is_clean():
    assert dimension_issues((40.0, 60.0, 10.0), DIMS) == []


@pytest.mark.parametrize("factor", [0.80, 0.9, 1.0, 1.1, 1.2])
def test_within_tolerance_is_clean(factor):
    assert dimension_issues(tuple(value * factor for value in (40.0, 60.0, 10.0)), DIMS) == []


def test_undershoot_inside_tolerance_is_clean():
    # A 25% *smaller* part is the same "legitimately differs" case as an
    # overshoot, and the check is symmetric.
    assert dimension_issues((30.0, 45.0, 10.0), DIMS) == []


# ---------------------------------------------------------------------------
# Out of tolerance, per axis
# ---------------------------------------------------------------------------


def test_x_overshoot_is_reported_with_both_numbers_and_the_percentage():
    issues = dimension_issues((100.0, 60.0, 10.0), DIMS)
    assert len(issues) == 1
    assert "X" in issues[0]
    assert "width" in issues[0]
    assert "100.0mm" in issues[0]
    assert "40.0mm" in issues[0]
    assert "+150%" in issues[0]


def test_y_overshoot_is_reported():
    issues = dimension_issues((40.0, 195.0, 10.0), DIMS)
    assert len(issues) == 1
    assert "Y extent 195.0mm" in issues[0]
    assert "height 60.0mm" in issues[0]
    assert "+225%" in issues[0]


def test_z_undershoot_is_reported():
    """The measured phi4:14b tree case: a 4.5mm plate for a 90mm request."""
    issues = dimension_issues((90.0, 90.0, 4.5), {"width": 90, "height": 90, "thickness": 90})
    assert len(issues) == 1
    assert "Z extent 4.5mm" in issues[0]
    assert "thickness 90.0mm" in issues[0]
    assert "-95%" in issues[0]


def test_every_axis_can_fire_independently():
    issues = dimension_issues((100.0, 200.0, 1.0), DIMS)
    assert [issue.split()[1] for issue in issues] == ["X", "Y", "Z"]
    assert issues == sorted(issues, key=lambda text: "XYZ".index(text.split()[1]))


# ---------------------------------------------------------------------------
# Tolerance edge: the comparison is strictly greater-than
# ---------------------------------------------------------------------------


def test_exactly_at_the_tolerance_is_not_flagged():
    at_edge = tuple(value * (1 + DIMENSION_TOLERANCE) for value in (40.0, 60.0, 10.0))
    assert dimension_issues(at_edge, DIMS) == []


def test_just_past_the_tolerance_is_flagged():
    past = tuple(value * (1 + DIMENSION_TOLERANCE + 0.001) for value in (40.0, 60.0, 10.0))
    assert len(dimension_issues(past, DIMS)) == 3


def test_exactly_at_the_undershoot_edge_is_not_flagged():
    at_edge = tuple(value * (1 - DIMENSION_TOLERANCE) for value in (40.0, 60.0, 10.0))
    assert dimension_issues(at_edge, DIMS) == []


# ---------------------------------------------------------------------------
# Unusable requested values are skipped, never flagged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, 0, 0.0, "", "   ", "not a number", [], {}, float("nan")])
def test_an_unusable_requested_value_is_skipped(value):
    issues = dimension_issues((40.0, 60.0, 10.0), {"width": value, "height": 60, "thickness": 10})
    assert issues == []


def test_a_missing_key_is_skipped():
    issues = dimension_issues((40.0, 60.0, 10.0), {"width": 40})
    assert issues == []


@pytest.mark.parametrize("requested", [None, {}, {"dimensions": {}}, "nope", 7])
def test_an_absent_or_unusable_requested_mapping_is_skipped(requested):
    assert dimension_issues((40.0, 60.0, 10.0), requested) == []


def test_a_numeric_string_is_usable():
    """A hand-written spec may quote its numbers; that is comparable."""
    assert (
        dimension_issues((100.0, 60.0, 10.0), {"width": "40", "height": 60, "thickness": 10}) != []
    )
    assert (
        dimension_issues((40.0, 60.0, 10.0), {"width": "40", "height": 60, "thickness": 10}) == []
    )


def test_a_boolean_is_not_a_measurement():
    assert (
        dimension_issues((40.0, 60.0, 10.0), {"width": True, "height": 60, "thickness": 10}) == []
    )


def test_a_negative_request_is_skipped_not_flagged():
    assert dimension_issues((40.0, 60.0, 10.0), {"width": -10, "height": 60, "thickness": 10}) == []


# ---------------------------------------------------------------------------
# Degenerate input
# ---------------------------------------------------------------------------


def test_a_short_extent_sequence_compares_only_what_it_has():
    issues = dimension_issues((100.0,), DIMS)
    assert len(issues) == 1
    assert issues[0].startswith("rendered X extent")


def test_no_extents_still_reports_the_build_plate():
    assert dimension_issues((), DIMS, min_z_mm=-12.5) == [
        "rendered part starts at Z=-12.500mm; the rest of the app assumes the part "
        "rests on the build plate (min Z = 0)"
    ]


@pytest.mark.parametrize("extents", [None, "40,60,10", [float("nan"), 60, 10], [0, 60, 10]])
def test_an_unusable_extent_is_not_compared(extents):
    """Nothing to compare against means no claim, not a bogus one."""
    assert dimension_issues(extents, DIMS) == []


# ---------------------------------------------------------------------------
# The build plate
# ---------------------------------------------------------------------------


def test_a_part_resting_on_the_plate_is_clean():
    assert dimension_issues((40.0, 60.0, 10.0), DIMS, min_z_mm=0.0) == []


def test_float32_dust_below_the_epsilon_is_clean():
    """A metre-wide STL rounds to ~6e-5 mm; that must not be a warning."""
    assert dimension_issues((40.0, 60.0, 10.0), DIMS, min_z_mm=MIN_Z_EPSILON_MM / 2) == []
    assert dimension_issues((40.0, 60.0, 10.0), DIMS, min_z_mm=-MIN_Z_EPSILON_MM / 2) == []


def test_a_sunken_part_is_flagged():
    """The measured cylinder case: a centred primitive sinks into the plate.

    ``trimesh``/the renderer place a primitive on its *centre*, so a cylinder
    with no ``position`` is half buried in the plate. The envelope matches, so the
    build plate is the only thing wrong.
    """
    issues = dimension_issues(
        (25.0, 25.0, 25.0), {"width": 25, "height": 25, "thickness": 25}, min_z_mm=-12.5
    )
    assert issues == [
        "rendered part starts at Z=-12.500mm; the rest of the app assumes the part "
        "rests on the build plate (min Z = 0)"
    ]


def test_the_measured_centred_cylinder_fails_both_checks():
    """Both findings, exactly as the 2026-09-27 measurement produced them."""
    issues = dimension_issues(
        (25.0, 25.0, 25.0), {"width": 40, "height": 40, "thickness": 25}, min_z_mm=-12.5
    )
    assert len(issues) == 3
    assert "rendered X extent 25.0mm" in issues[0]
    assert "rendered Y extent 25.0mm" in issues[1]
    assert "Z=-12.500mm" in issues[2]


def test_a_floating_part_is_flagged_with_its_actual_value():
    issues = dimension_issues((40.0, 60.0, 10.0), DIMS, min_z_mm=0.5)
    assert len(issues) == 1
    assert "Z=0.500mm" in issues[0]


def test_the_plate_check_runs_without_any_requested_dimension():
    """A mesh-import specification has no ``dimensions`` and still gets checked."""
    issues = dimension_issues((120.0, 90.0, 30.0), None, min_z_mm=0.02)
    assert len(issues) == 1
    assert "Z=0.020mm" in issues[0]


@pytest.mark.parametrize("min_z", [None, "not a number", [0]])
def test_an_unusable_min_z_is_treated_as_zero(min_z):
    assert dimension_issues((40.0, 60.0, 10.0), DIMS, min_z_mm=min_z) == []


# ---------------------------------------------------------------------------
# The pipeline's byte-level wrapper
# ---------------------------------------------------------------------------


def test_dimension_warnings_measures_a_real_export():
    """The wrapper is the only trimesh-aware part; check it on a real box."""
    trimesh = pytest.importorskip("trimesh")

    from designs.cad.pipeline import dimension_warnings

    # ``trimesh.creation.box`` is centred on the origin: lift it onto the plate
    # the way every PrintForge backend does (``min Z = 0``).
    box = trimesh.creation.box(extents=(40.0, 60.0, 10.0))
    box.apply_translation([0.0, 0.0, 5.0])
    payload = box.export(file_type="stl")
    assert dimension_warnings(payload, {"width": 40, "height": 60, "thickness": 10}) == []

    # The same part, 2 mm too big on X and floating 5 mm above the plate.
    over = trimesh.creation.box(extents=(100.0, 60.0, 10.0))
    over.apply_translation([0.0, 0.0, 10.0])
    issues = dimension_warnings(over.export(file_type="stl"), DIMS)
    assert len(issues) == 2
    assert issues[0].startswith("rendered X extent 100.0mm")
    assert "Z=5.000mm" in issues[1]


def test_dimension_warnings_never_raises_on_junk():
    from designs.cad.pipeline import dimension_warnings

    assert dimension_warnings(b"solid fake\nendsolid fake\n", DIMS) == []
    assert dimension_warnings(b"", DIMS) == []
    assert dimension_warnings(b"\x00\x01\x02", DIMS) == []


def test_a_mesh_only_specification_gets_the_plate_check_alone():
    """``{"generator": "mesh", ...}`` has no ``dimensions`` -- no axis is comparable."""
    trimesh = pytest.importorskip("trimesh")

    from designs.cad.pipeline import dimension_warnings

    mesh = trimesh.creation.box(extents=(120.0, 90.0, 30.0))
    mesh.apply_translation([0.0, 0.0, 15.0])
    assert dimension_warnings(mesh.export(file_type="stl"), None) == []
    mesh.apply_translation([0.0, 0.0, 0.75])
    assert len(dimension_warnings(mesh.export(file_type="stl"), None)) == 1
