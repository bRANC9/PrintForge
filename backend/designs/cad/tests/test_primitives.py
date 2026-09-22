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
def test_bad_type_is_rejected(primitive):
    with pytest.raises(SpecificationError):
        parse_primitives(spec(primitive))


@pytest.mark.parametrize("role", ["union", "remove", ""])
def test_bad_role_is_rejected(role):
    with pytest.raises(SpecificationError):
        parse_primitives(spec({**BOX, "role": role}))


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
def test_out_of_bounds_size_is_rejected(primitive, field, value):
    with pytest.raises(SpecificationError):
        parse_primitives(spec({**primitive, field: value}))


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
def test_out_of_bounds_position_or_rotation_is_rejected(field, value):
    with pytest.raises(SpecificationError):
        parse_primitives(spec({**BOX, field: value}))


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
    with pytest.raises(SpecificationError):
        parse_primitives(spec(incomplete))


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


def test_validate_reports_primitive_problems():
    model = GeneratedModel(
        specification={**BASE_SPEC, "primitives": [{**BOX, "type": "torus"}]},
        scad_source="cube([1, 1, 1]);",
    )
    problems = OpenSCADBackend().validate(model)
    assert any("primitives[0].type" in problem for problem in problems)


def test_validate_accepts_valid_primitives():
    source = OpenSCADBackend().generate(spec(BOX))
    model = GeneratedModel(specification=spec(BOX), scad_source=source)
    assert OpenSCADBackend().validate(model) == []
