"""Tests for prompt-driven CSG primitives (docs/cad-primitives.md 2).

Covers the bounded ``parse_primitives`` parser, the ``difference``/``union`` CSG
base renderer, the composition with the annotation-driven ``operations`` layer
and the ``no primitives`` backward-compatibility guarantee.
"""

from __future__ import annotations

import pytest

from designs.cad.base import GeneratedModel, SpecificationError
from designs.cad.openscad import (
    MAX_PRIMITIVES,
    OpenSCADBackend,
    RenderPrimitive,
    build_parameters,
    parse_primitives,
    render_scad,
    validate_scad_source,
)

BASE_SPEC = {
    "object": "phone_holder",
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

CYLINDER = {
    "type": "cylinder",
    "role": "subtract",
    "position": {"x": 0.0, "y": 0.0, "z": 0.0},
    "diameter": 4.0,
    "height": 20.0,
}

SPHERE = {"type": "sphere", "diameter": 8.0}
CONE = {"type": "cone", "diameter": 10.0, "height": 12.0}

BOSS_OPERATION = {
    "kind": "boss",
    "origin": {"x": 10.0, "y": 10.0, "z": 0.0},
    "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
    "depth": 5.0,
    "diameter": 6.0,
}


def spec(*primitives: dict) -> dict:
    return {**BASE_SPEC, "primitives": list(primitives)}


def bare_spec(*primitives: dict) -> dict:
    """A specification without ``dimensions``: no usable size fallback exists."""
    return {"object": "widget", "primitives": list(primitives)}


# ---------------------------------------------------------------------------
# Backward compatibility: no primitives renders the holder part only
# ---------------------------------------------------------------------------


def test_parse_primitives_defaults_to_empty():
    assert parse_primitives(BASE_SPEC) == []
    assert parse_primitives({**BASE_SPEC, "primitives": []}) == []
    assert parse_primitives(None) == []


def test_no_primitives_is_byte_identical_to_holder():
    backend = OpenSCADBackend()
    without = backend.generate(BASE_SPEC)
    with_empty = backend.generate({**BASE_SPEC, "primitives": []})
    assert without == with_empty
    # Exactly the historical holder render, byte for byte.
    assert without == render_scad(build_parameters(BASE_SPEC))
    assert without.rstrip().endswith("holder();")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_returns_render_primitive():
    primitives = parse_primitives(spec(BOX))

    assert len(primitives) == 1
    primitive = primitives[0]
    assert isinstance(primitive, RenderPrimitive)
    assert primitive.type == "box"
    assert primitive.role == "add"
    assert primitive.position == (0.0, 0.0, 5.0)
    assert primitive.rotation == (0.0, 0.0, 0.0)
    assert primitive.width == pytest.approx(20.0)
    assert primitive.depth == pytest.approx(10.0)
    assert primitive.height == pytest.approx(5.0)
    assert primitive.diameter is None
    assert primitive.is_additive is True


def test_all_primitive_types_parse():
    primitives = parse_primitives(spec(BOX, CYLINDER, SPHERE, CONE))

    assert [primitive.type for primitive in primitives] == [
        "box",
        "cylinder",
        "sphere",
        "cone",
    ]
    assert primitives[0].is_additive is True
    assert primitives[1].is_additive is False


def test_role_and_position_default_when_omitted():
    primitive = parse_primitives(spec(SPHERE))[0]
    assert primitive.role == "add"
    assert primitive.position == (0.0, 0.0, 0.0)
    assert primitive.rotation == (0.0, 0.0, 0.0)


def test_primitives_must_be_a_list():
    with pytest.raises(SpecificationError):
        parse_primitives({**BASE_SPEC, "primitives": "box"})


def test_too_many_primitives_are_rejected():
    with pytest.raises(SpecificationError):
        parse_primitives(spec(*([BOX] * (MAX_PRIMITIVES + 1))))


@pytest.mark.parametrize(
    "primitive",
    [
        {**BOX, "type": "torus"},
        {**BOX, "type": None},
        {key: value for key, value in BOX.items() if key != "type"},
        "not-an-object",
        [],
    ],
)
def test_bad_type_is_skipped_with_warning(primitive):
    warnings: list[str] = []
    assert parse_primitives(spec(primitive), warnings=warnings) == []
    assert warnings
    assert "skipped primitives[0]" in warnings[0]


@pytest.mark.parametrize("role", ["union", "remove", ""])
def test_bad_role_is_skipped_with_warning(role):
    warnings: list[str] = []
    assert parse_primitives(spec({**BOX, "role": role}), warnings=warnings) == []
    assert warnings


@pytest.mark.parametrize(
    ("primitive", "field", "value"),
    [
        (BOX, "width", 0.3),
        (BOX, "width", 1000.1),
        (BOX, "depth", 0.0),
        (BOX, "height", float("nan")),
        (BOX, "height", float("inf")),
        (BOX, "width", "not-a-number"),
        (BOX, "width", True),
        (CYLINDER, "diameter", 0.3),
        (CYLINDER, "diameter", 1000.1),
        (SPHERE, "diameter", -1.0),
    ],
)
def test_out_of_bounds_size_is_skipped(primitive, field, value):
    warnings: list[str] = []
    assert parse_primitives(spec({**primitive, field: value}), warnings=warnings) == []
    assert warnings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("position", {"x": 1001.0, "y": 0, "z": 0}),
        ("position", {"x": -1001.0, "y": 0, "z": 0}),
        ("position", {"x": 0, "y": 0}),
        ("rotation", {"x": 0, "y": 361.0, "z": 0}),
        ("rotation", {"x": 0, "y": 0, "z": -361.0}),
        ("position", {"x": float("nan"), "y": 0, "z": 0}),
    ],
)
def test_out_of_bounds_position_or_rotation_is_skipped(field, value):
    warnings: list[str] = []
    assert parse_primitives(spec({**BOX, field: value}), warnings=warnings) == []
    assert warnings


@pytest.mark.parametrize(
    ("primitive", "field"),
    [
        (BOX, "width"),
        (BOX, "depth"),
        (BOX, "height"),
        (CYLINDER, "diameter"),
        (CYLINDER, "height"),
        (SPHERE, "diameter"),
        (CONE, "diameter"),
        (CONE, "height"),
    ],
)
def test_missing_required_size_is_rejected(primitive, field):
    incomplete = {key: value for key, value in primitive.items() if key != field}
    # No ``dimensions`` at all: the weak-model fallback cannot repair the field.
    with pytest.raises(SpecificationError):
        parse_primitives(bare_spec(incomplete))


# ---------------------------------------------------------------------------
# Weak-model size fallback (docs/cad-primitives.md 2)
# ---------------------------------------------------------------------------


def test_box_missing_sizes_fall_back_to_dimensions():
    # A weak model emitted a plate-like box with only ``width`` set.
    dimensions = BASE_SPEC["dimensions"]
    parsed = parse_primitives(spec({"type": "box", "width": 20.0}))[0]

    assert parsed.width == pytest.approx(20.0)  # explicit primitive value wins
    assert parsed.depth == pytest.approx(dimensions["height"])
    assert parsed.height == pytest.approx(dimensions["thickness"])


@pytest.mark.parametrize("kind", ["cylinder", "sphere", "cone"])
def test_diameter_missing_falls_back_to_smallest_dimension(kind):
    dimensions = BASE_SPEC["dimensions"]
    expected = min(dimensions["width"], dimensions["height"], dimensions["thickness"])
    primitive = {"type": kind, "diameter": None}
    if kind != "sphere":
        primitive["height"] = 10.0

    parsed = parse_primitives(spec(primitive))[0]

    assert parsed.diameter == pytest.approx(expected)


def test_cylinder_missing_height_falls_back_to_thickness():
    parsed = parse_primitives(spec({"type": "cylinder", "diameter": 6.0}))[0]

    assert parsed.diameter == pytest.approx(6.0)
    assert parsed.height == pytest.approx(BASE_SPEC["dimensions"]["thickness"])


def test_explicit_primitive_values_are_never_overridden():
    parsed = parse_primitives(spec(BOX))[0]

    assert parsed.width == pytest.approx(20.0)
    assert parsed.depth == pytest.approx(10.0)
    assert parsed.height == pytest.approx(5.0)


def test_box_without_size_or_dimensions_is_rejected():
    with pytest.raises(SpecificationError):
        parse_primitives(bare_spec({"type": "box"}))


def test_unusable_dimensions_do_not_invent_sizes():
    # Malformed/partial dimensions leave the field missing and still raise for a
    # non-holder object (a holder-like/legacy spec would fall back to the
    # template instead of synthesizing).
    with pytest.raises(SpecificationError):
        parse_primitives(
            {"object": "widget", "dimensions": "not-an-object", "primitives": [{"type": "box"}]}
        )
    with pytest.raises(SpecificationError):
        parse_primitives(
            {"object": "widget", "dimensions": {"height": 10.0}, "primitives": [{"type": "box"}]}
        )
    with pytest.raises(SpecificationError):
        parse_primitives(
            {
                "object": "widget",
                "dimensions": {"width": 20.0, "height": "nope", "thickness": 5.0},
                "primitives": [{"type": "sphere"}],
            }
        )


# ---------------------------------------------------------------------------
# Weak-model no-primitives fallback (synthesized box / holder template)
# ---------------------------------------------------------------------------


def test_non_holder_without_primitives_synthesizes_box_from_dimensions():
    spec_without_primitives = {
        "object": "wall_bracket",
        "dimensions": {"width": 40.0, "height": 30.0, "thickness": 5.0},
    }
    warnings: list[str] = []
    primitives = parse_primitives(spec_without_primitives, warnings=warnings)

    assert len(primitives) == 1
    box = primitives[0]
    assert isinstance(box, RenderPrimitive)
    assert box.type == "box"
    assert box.role == "add"
    assert box.width == pytest.approx(40.0)
    assert box.depth == pytest.approx(30.0)  # depth <- dimensions.height
    assert box.height == pytest.approx(5.0)  # height <- dimensions.thickness
    assert box.position == pytest.approx((0.0, 0.0, 2.5))  # sits on the plate
    assert any("synthesized" in warning for warning in warnings)


def test_synthesized_box_renders_on_build_plate():
    spec_without_primitives = {
        "object": "wall_bracket",
        "dimensions": {"width": 40.0, "height": 30.0, "thickness": 5.0},
    }
    source = OpenSCADBackend().generate(spec_without_primitives)

    assert "// warning: " in source
    assert "translate([-20.0, -15.0, -2.5]) cube([40.0, 30.0, 5.0]);" in source
    assert validate_scad_source(source) == []


def test_non_holder_with_empty_primitives_also_synthesizes():
    spec_with_empty = {
        "object": "wall_bracket",
        "dimensions": {"width": 40.0, "height": 30.0, "thickness": 5.0},
        "primitives": [],
    }
    assert len(parse_primitives(spec_with_empty)) == 1


def test_holder_without_primitives_keeps_holder_template():
    source = OpenSCADBackend().generate(BASE_SPEC)

    assert source.rstrip().endswith("holder();")
    assert "// warning:" not in source
    assert "// primitive:" not in source


def test_legacy_spec_without_object_keeps_holder_template():
    legacy = {key: value for key, value in BASE_SPEC.items() if key != "object"}
    source = OpenSCADBackend().generate(legacy)

    assert source.rstrip().endswith("holder();")
    assert "// warning:" not in source


def test_non_holder_without_primitives_or_dimensions_raises():
    with pytest.raises(SpecificationError):
        parse_primitives({"object": "wall_bracket"})
    with pytest.raises(SpecificationError):
        parse_primitives({"object": "wall_bracket", "dimensions": "not-an-object"})
    with pytest.raises(SpecificationError):
        parse_primitives({"object": "wall_bracket", "dimensions": {"width": 40.0}})


def test_skipped_primitive_warning_is_surfaced_in_source():
    spec_with_bad_primitive = {
        "object": "wall_bracket",
        "dimensions": {"width": 40.0, "height": 30.0, "thickness": 5.0},
        "primitives": [{**BOX, "type": "torus"}],
    }
    source = OpenSCADBackend().generate(spec_with_bad_primitive)

    assert "// warning: skipped primitives[0]" in source
    # The dimensions fallback box still renders.
    assert "cube([40.0, 30.0, 5.0])" in source
    assert validate_scad_source(source) == []


def test_weak_model_spec_generates_instead_of_failing():
    """The live e2e finding: bad mounting + no primitives + incomplete pocket."""
    weak_spec = {
        "object": "wall_mount",
        "dimensions": {"width": 60.0, "height": 40.0, "thickness": 6.0},
        "mounting": {"type": "standard_6mm"},
        "operations": [
            {
                "kind": "pocket",  # missing width/height -> skipped
                "origin": {"x": 0.0, "y": 0.0, "z": 0.0},
                "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
                "depth": 3.0,
            }
        ],
    }
    backend = OpenSCADBackend()
    source = backend.generate(weak_spec)

    assert build_parameters(weak_spec).hole_diameter == pytest.approx(6.3)
    assert "// warning: skipped operations[0]" in source
    assert "cube([60.0, 40.0, 6.0])" in source  # synthesized from dimensions
    model = GeneratedModel(specification=weak_spec, scad_source=source)
    assert backend.validate(model) == []


def test_warning_comments_cannot_inject_code():
    source = render_scad(
        build_parameters(BASE_SPEC),
        warnings=['evil\nimport("/etc/passwd")'],
    )

    assert "// warning: evil import" in source
    assert "\nimport(" not in source
    assert validate_scad_source(source) == []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_box_primitive_renders_cube_inside_difference_union():
    source = OpenSCADBackend().generate(spec(BOX))

    assert (
        "difference() {\n"
        "    union() {\n"
        "        // primitive: box\n"
        "        translate([0.0, 0.0, 5.0]) rotate([0.0, 0.0, 0.0]) "
        "translate([-10.0, -5.0, -2.5]) cube([20.0, 10.0, 5.0]);\n"
        "    }\n"
        "}"
    ) in source
    assert validate_scad_source(source) == []


def test_add_and_subtract_primitives_render_both():
    source = OpenSCADBackend().generate(spec(BOX, CYLINDER))

    assert "// primitive: box" in source
    assert "// primitive: cylinder" in source
    assert "cube([20.0, 10.0, 5.0])" in source
    assert "cylinder(d=4.0, h=20.0, center=true)" in source
    # Additive shapes union first, subtractive ones cut afterwards.
    assert source.index("// primitive: box") < source.index("// primitive: cylinder")
    assert validate_scad_source(source) == []


def test_sphere_and_cone_render():
    source = OpenSCADBackend().generate(spec(SPHERE, CONE))

    assert "sphere(d=8.0)" in source
    assert "cylinder(d1=10.0, d2=0, h=12.0, center=true)" in source
    assert validate_scad_source(source) == []


def test_operations_layer_applies_on_top_of_primitives():
    source = OpenSCADBackend().generate(
        {**BASE_SPEC, "primitives": [BOX], "operations": [BOSS_OPERATION]}
    )

    assert "cube([20.0, 10.0, 5.0])" in source
    assert "multmatrix(" in source
    assert "// operation: boss" in source
    # The primitive CSG is nested inside the operations union, before the boss.
    assert source.index("// primitive: box") < source.index("// operation: boss")
    assert validate_scad_source(source) == []


def test_primitive_label_cannot_inject_code():
    primitive = {**BOX, "label": 'x"); import("/etc/passwd");//'}
    source = OpenSCADBackend().generate(spec(primitive))

    assert validate_scad_source(source) == []
    assert "import(" not in source


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


def test_validate_reports_structural_primitive_problems():
    # A non-list is a structural error and still blocks.
    model = GeneratedModel(
        specification={**BASE_SPEC, "primitives": "box"},
        scad_source="cube([1, 1, 1]);",
    )
    problems = OpenSCADBackend().validate(model)
    assert any("primitives" in problem for problem in problems)


def test_validate_tolerates_individual_bad_primitives():
    # A skipped primitive is simply absent: the render still validates.
    bad_spec = spec({**BOX, "type": "torus"})
    source = OpenSCADBackend().generate(bad_spec)
    model = GeneratedModel(specification=bad_spec, scad_source=source)
    assert OpenSCADBackend().validate(model) == []


def test_validate_accepts_valid_primitives():
    source = OpenSCADBackend().generate(spec(BOX))
    model = GeneratedModel(specification=spec(BOX), scad_source=source)
    assert OpenSCADBackend().validate(model) == []
