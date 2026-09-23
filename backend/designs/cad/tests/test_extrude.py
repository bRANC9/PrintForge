"""Tests for the 2D ``extrude`` primitive (docs/skills.md 5).

Covers the bounded parser (profile / height / wall / rounding), the
``linear_extrude`` renderer (solid, hollow wall, rounded outline), the blocking
``validate`` checks and that extrude primitives mix with box/cylinder/sphere/cone
without emitting any forbidden file-reading construct.
"""

from __future__ import annotations

import pytest

from designs.cad.base import GeneratedModel
from designs.cad.openscad import (
    OpenSCADBackend,
    parse_primitives,
    validate_scad_source,
)

BASE_SPEC = {
    "object": "cookie_cutter",
    "dimensions": {"width": 70.6, "height": 147, "thickness": 7.6},
    "angle": 15,
    "wall_thickness": 4,
    "mounting": {"type": "M5", "count": 2},
}

BOX = {
    "type": "box",
    "role": "add",
    "position": {"x": 0.0, "y": 0.0, "z": 5.0},
    "width": 20.0,
    "depth": 10.0,
    "height": 5.0,
}

TRIANGLE = [{"x": 0.0, "y": 0.0}, {"x": 10.0, "y": 0.0}, {"x": 5.0, "y": 8.0}]
SQUARE = [
    {"x": 0.0, "y": 0.0},
    {"x": 20.0, "y": 0.0},
    {"x": 20.0, "y": 20.0},
    {"x": 0.0, "y": 20.0},
]

SOLID = {
    "type": "extrude",
    "role": "add",
    "position": {"x": 5.0, "y": 0.0, "z": 0.0},
    "profile": TRIANGLE,
    "height": 5.0,
}

HOLLOW = {
    "type": "extrude",
    "role": "add",
    "profile": SQUARE,
    "height": 10.0,
    "wall_thickness": 2.0,
}


def spec(*primitives: dict) -> dict:
    return {**BASE_SPEC, "primitives": list(primitives)}


def model_for(specification: dict) -> GeneratedModel:
    source = OpenSCADBackend().generate(specification)
    return GeneratedModel(specification=specification, scad_source=source)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_solid_extrude_parses_profile_height_and_defaults():
    primitive = parse_primitives(spec(SOLID))[0]

    assert primitive.type == "extrude"
    assert primitive.role == "add"
    assert primitive.position == (5.0, 0.0, 0.0)
    assert primitive.profile == ((0.0, 0.0), (10.0, 0.0), (5.0, 8.0))
    assert primitive.height == pytest.approx(5.0)
    assert primitive.wall_thickness is None
    assert primitive.inner_profile is None
    assert primitive.round_radius is None
    assert primitive.is_additive is True


def test_hollow_wall_offsets_the_profile_inward_in_python():
    primitive = parse_primitives(spec(HOLLOW))[0]

    assert primitive.wall_thickness == pytest.approx(2.0)
    assert primitive.inner_profile == (
        (2.0, 2.0),
        (18.0, 2.0),
        (18.0, 18.0),
        (2.0, 18.0),
    )


def test_round_radius_is_kept_when_it_fits_the_profile():
    rounded = {**SOLID, "round_radius": 1.5}
    primitive = parse_primitives(spec(rounded))[0]

    assert primitive.round_radius == pytest.approx(1.5)


def test_round_radius_larger_than_the_profile_is_dropped_with_warning():
    # The triangle's smaller span is 8mm, so a 20mm radius cannot be honoured.
    rounded = {**SOLID, "round_radius": 20.0}
    warnings: list[str] = []
    primitive = parse_primitives(spec(rounded), warnings=warnings)[0]

    assert primitive.round_radius is None
    assert any("round_radius" in warning for warning in warnings)


@pytest.mark.parametrize(
    "bad_profile",
    [
        "not-a-list",
        [{"x": 0.0, "y": 0.0}, {"x": 10.0, "y": 0.0}],  # too few points
        [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, "nope"],
        [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, {"x": 2000.0, "y": 0.0}],
        [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, {"y": 2.0}],
        [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}, {"x": True, "y": 2.0}],
    ],
)
def test_invalid_profile_is_skipped_with_warning(bad_profile):
    warnings: list[str] = []
    parsed = parse_primitives(spec({**SOLID, "profile": bad_profile}), warnings=warnings)

    assert all(primitive.type != "extrude" for primitive in parsed)
    assert warnings


def test_missing_height_is_skipped_with_warning():
    incomplete = {key: value for key, value in SOLID.items() if key != "height"}
    warnings: list[str] = []
    parsed = parse_primitives(spec(incomplete), warnings=warnings)

    assert all(primitive.type != "extrude" for primitive in parsed)
    assert any("height" in warning for warning in warnings)


def test_extrude_does_not_borrow_the_dimensions_size_fallback():
    # Unlike box/cylinder, an extrude must not invent its height from
    # ``dimensions.thickness``.
    incomplete = {key: value for key, value in SOLID.items() if key != "height"}
    warnings: list[str] = []
    parsed = parse_primitives(spec(incomplete), warnings=warnings)

    assert all(primitive.type != "extrude" for primitive in parsed)
    assert warnings


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_solid_extrude_renders_linear_extrude_polygon():
    source = OpenSCADBackend().generate(spec(SOLID))

    assert (
        "translate([5.0, 0.0, 0.0]) rotate([0.0, 0.0, 0.0]) "
        "linear_extrude(height=5.0) "
        "polygon(points=[[0.0, 0.0], [10.0, 0.0], [5.0, 8.0]]);"
    ) in source
    assert validate_scad_source(source) == []


def test_hollow_extrude_renders_difference_of_outer_and_inner():
    source = OpenSCADBackend().generate(spec(HOLLOW))

    assert (
        "linear_extrude(height=10.0) difference() { "
        "polygon(points=[[0.0, 0.0], [20.0, 0.0], [20.0, 20.0], [0.0, 20.0]]); "
        "polygon(points=[[2.0, 2.0], [18.0, 2.0], [18.0, 18.0], [2.0, 18.0]]); }"
    ) in source
    assert validate_scad_source(source) == []


def test_rounded_extrude_wraps_the_profile_in_offset():
    rounded = {**SOLID, "round_radius": 1.5}
    source = OpenSCADBackend().generate(spec(rounded))

    assert (
        "linear_extrude(height=5.0) "
        "offset(r=1.5) polygon(points=[[0.0, 0.0], [10.0, 0.0], [5.0, 8.0]])"
    ) in source
    assert validate_scad_source(source) == []


def test_over_thick_wall_degrades_to_a_solid_extrusion():
    too_thick = {**HOLLOW, "wall_thickness": 50.0}
    warnings: list[str] = []
    primitive = parse_primitives(spec(too_thick), warnings=warnings)[0]

    assert primitive.inner_profile is None
    assert primitive.wall_thickness == pytest.approx(50.0)
    assert any("too thick" in warning for warning in warnings)

    source = OpenSCADBackend().generate(spec(too_thick))
    assert "linear_extrude(height=10.0) difference()" not in source
    assert "linear_extrude(height=10.0) polygon(points=[[0.0, 0.0], [20.0, 0.0]" in source


def test_box_and_extrude_mix_by_role():
    subtract = {**SOLID, "role": "subtract"}
    source = OpenSCADBackend().generate(spec(BOX, subtract))

    assert "cube([20.0, 10.0, 5.0])" in source
    assert "linear_extrude(height=5.0) polygon(" in source
    # Additive box unions first; the subtractive extrude cuts afterwards.
    assert source.index("// primitive: box") < source.index("// primitive: extrude")
    assert validate_scad_source(source) == []


def test_non_extrude_primitives_never_emit_extrude_constructs():
    source = OpenSCADBackend().generate(spec(BOX))

    assert "linear_extrude" not in source
    assert "polygon(" not in source


@pytest.mark.parametrize(
    "primitive",
    [SOLID, HOLLOW, {**SOLID, "round_radius": 1.0}],
)
def test_extrude_output_is_sandbox_safe(primitive):
    source = OpenSCADBackend().generate(spec(primitive))

    assert validate_scad_source(source) == []
    assert "import(" not in source
    assert "surface(" not in source
    assert "include <" not in source
    assert "use <" not in source


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


def test_validate_accepts_a_valid_extrude():
    assert OpenSCADBackend().validate(model_for(spec(SOLID))) == []
    assert OpenSCADBackend().validate(model_for(spec(HOLLOW))) == []


def test_validate_reports_too_few_profile_points():
    bad_spec = spec({**SOLID, "profile": TRIANGLE[:2]})
    problems = OpenSCADBackend().validate(model_for(bad_spec))

    assert any("profile" in problem and "extrude" in problem for problem in problems)


def test_validate_reports_missing_height():
    incomplete = {key: value for key, value in SOLID.items() if key != "height"}
    problems = OpenSCADBackend().validate(model_for(spec(incomplete)))

    assert any("height" in problem for problem in problems)


def test_validate_reports_non_positive_height():
    bad_spec = spec({**SOLID, "height": 0.0})
    problems = OpenSCADBackend().validate(model_for(bad_spec))

    assert any("height" in problem for problem in problems)


def test_validate_reports_wall_thicker_than_the_shape_allows():
    too_thick = {**HOLLOW, "wall_thickness": 50.0}
    problems = OpenSCADBackend().validate(model_for(spec(too_thick)))

    assert any("wall_thickness" in problem and "too thick" in problem for problem in problems)
