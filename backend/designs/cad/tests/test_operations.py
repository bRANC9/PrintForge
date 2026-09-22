"""Tests for annotation-driven parametric operations.

Covers docs/visual-editing.md 3.1 / 3.3: the bounded ``parse_operations`` parser,
the ``multmatrix`` local-frame placement, the additive/subtractive combination
and the extended ``validate`` problems. The base render must stay unchanged when
no operations are present.
"""

from __future__ import annotations

import pytest

from designs.cad.base import GeneratedModel, SpecificationError
from designs.cad.openscad import (
    MAX_OPERATIONS,
    OpenSCADBackend,
    RenderOperation,
    parse_operations,
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

HOLE = {
    "kind": "hole",
    "origin": {"x": 35.0, "y": 12.5, "z": 8.0},
    "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
    "depth": 4.0,
    "diameter": 4.0,
    "label": "4 mm through hole",
}

BOSS = {
    "kind": "boss",
    "origin": {"x": 10.0, "y": 10.0, "z": 0.0},
    "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
    "depth": 5.0,
    "diameter": 6.0,
}

POCKET = {
    "kind": "pocket",
    "origin": {"x": 20.0, "y": 20.0, "z": 5.0},
    "normal": {"x": 1.0, "y": 0.0, "z": 0.0},
    "depth": 3.0,
    "width": 12.0,
    "height": 4.0,
}


def spec(*operations: dict) -> dict:
    return {**BASE_SPEC, "operations": list(operations)}


# ---------------------------------------------------------------------------
# Backward compatibility: no operations renders the base part only
# ---------------------------------------------------------------------------


def test_parse_operations_defaults_to_empty():
    assert parse_operations(BASE_SPEC) == []
    assert parse_operations({**BASE_SPEC, "operations": []}) == []
    assert parse_operations(None) == []


def test_no_operations_renders_base_only():
    source = OpenSCADBackend().generate(BASE_SPEC)

    assert source.rstrip().endswith("holder();")
    assert "multmatrix" not in source
    assert validate_scad_source(source) == []


def test_no_operations_matches_direct_render():
    from designs.cad.openscad import build_parameters

    backend = OpenSCADBackend()
    assert backend.generate(BASE_SPEC) == render_scad(build_parameters(BASE_SPEC))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_operations_returns_render_operation():
    operations = parse_operations(spec(HOLE))

    assert len(operations) == 1
    operation = operations[0]
    assert isinstance(operation, RenderOperation)
    assert operation.kind == "hole"
    assert operation.origin == (35.0, 12.5, 8.0)
    assert operation.normal == (0.0, 0.0, 1.0)
    assert operation.depth == pytest.approx(4.0)
    assert operation.diameter == pytest.approx(4.0)
    assert operation.width is None
    assert operation.height is None
    assert operation.length is None
    assert operation.label == "4 mm through hole"
    assert operation.is_additive is False


def test_all_operation_kinds_parse():
    operations = parse_operations(
        spec(
            HOLE,
            {**BOSS, "kind": "add", "width": 5.0, "height": 3.0},
            {**POCKET, "kind": "cut"},
            {**HOLE, "kind": "slot", "length": 10.0},
            BOSS,
        )
    )
    assert [operation.kind for operation in operations] == [
        "hole",
        "add",
        "cut",
        "slot",
        "boss",
    ]


def test_operations_must_be_a_list():
    with pytest.raises(SpecificationError):
        parse_operations({**BASE_SPEC, "operations": "hole"})


def test_too_many_operations_are_rejected():
    with pytest.raises(SpecificationError):
        parse_operations(spec(*([HOLE] * (MAX_OPERATIONS + 1))))


@pytest.mark.parametrize(
    "operation",
    [
        {**HOLE, "kind": "engrave"},
        {**HOLE, "kind": None},
        "not-an-object",
        [],
    ],
)
def test_malformed_operations_are_rejected(operation):
    with pytest.raises(SpecificationError):
        parse_operations(spec(operation))


@pytest.mark.parametrize(
    "normal",
    [
        {"x": 0.0, "y": 0.0, "z": 0.0},
        {"x": 0, "y": 0, "z": 0},
    ],
)
def test_zero_length_normal_is_rejected(normal):
    with pytest.raises(SpecificationError):
        parse_operations(spec({**HOLE, "normal": normal}))


def test_generate_rejects_zero_length_normal():
    with pytest.raises(SpecificationError):
        OpenSCADBackend().generate(spec({**HOLE, "normal": {"x": 0, "y": 0, "z": 0}}))


@pytest.mark.parametrize(
    "operation",
    [
        {**HOLE, "depth": 0.3},
        {**HOLE, "depth": 200.1},
        {**HOLE, "depth": float("nan")},
        {**HOLE, "depth": float("inf")},
        {**HOLE, "depth": "not-a-number"},
        {**HOLE, "depth": True},
        {**HOLE, "diameter": 0.3},
        {**HOLE, "diameter": 200.1},
        {**POCKET, "width": 0.3},
        {**POCKET, "height": 500.1},
        {**HOLE, "kind": "slot", "length": 500.1, "diameter": 3.0},
    ],
)
def test_out_of_bounds_operation_values_are_rejected(operation):
    with pytest.raises(SpecificationError):
        parse_operations(spec(operation))


@pytest.mark.parametrize("origin", [{"x": 1e12, "y": 0, "z": 0}, {"x": 0, "z": 0}])
def test_invalid_origin_is_rejected(origin):
    with pytest.raises(SpecificationError):
        parse_operations(spec({**HOLE, "origin": origin}))


@pytest.mark.parametrize("kind", ["hole", "boss"])
def test_cylindrical_kinds_require_diameter(kind):
    with pytest.raises(SpecificationError):
        parse_operations(spec({**HOLE, "kind": kind, "diameter": None}))


@pytest.mark.parametrize("kind", ["pocket", "cut", "add"])
def test_rectangular_kinds_require_width_and_height(kind):
    with pytest.raises(SpecificationError):
        parse_operations(spec({**POCKET, "kind": kind, "width": None}))
    with pytest.raises(SpecificationError):
        parse_operations(spec({**POCKET, "kind": kind, "height": None}))


def test_slot_requires_length_and_diameter():
    slot = {**HOLE, "kind": "slot", "length": 10.0}
    parse_operations(spec(slot))  # valid
    with pytest.raises(SpecificationError):
        parse_operations(spec({key: value for key, value in slot.items() if key != "length"}))
    with pytest.raises(SpecificationError):
        parse_operations(spec({key: value for key, value in slot.items() if key != "diameter"}))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_hole_operation_renders_multmatrix_and_cylinder():
    source = OpenSCADBackend().generate(spec(HOLE))

    assert "multmatrix(" in source
    assert "cylinder(" in source
    assert "difference() {" in source
    assert "union() {" in source
    # The origin ends up in the translation column of the emitted matrix.
    assert "35.0" in source
    assert "12.5" in source
    assert "8.0" in source
    assert validate_scad_source(source) == []


def test_orthonormal_frame_is_right_handed_for_axis_normals():
    # An x-axis normal must still produce a valid, self-contained transform.
    source = OpenSCADBackend().generate(spec(POCKET))
    assert "multmatrix(" in source
    assert validate_scad_source(source) == []


def test_additive_and_subtractive_operations_render_both():
    source = OpenSCADBackend().generate(spec(BOSS, POCKET))

    assert source.count("multmatrix(") == 2
    assert "operation: boss" in source
    assert "operation: pocket" in source
    # Additive features go inside the union, subtractive ones after it.
    assert source.index("operation: boss") < source.index("operation: pocket")
    assert validate_scad_source(source) == []


def test_slot_renders_hull():
    slot = {**HOLE, "kind": "slot", "length": 10.0, "diameter": 3.0}
    source = OpenSCADBackend().generate(spec(slot))
    assert "hull()" in source
    assert validate_scad_source(source) == []


def test_add_operation_renders_cube():
    add = {**BOSS, "kind": "add", "width": 5.0, "height": 3.0}
    source = OpenSCADBackend().generate(spec(add))
    assert "cube(" in source
    assert validate_scad_source(source) == []


def test_label_with_forbidden_tokens_stays_safe():
    operation = {**HOLE, "label": 'x"); import("/etc/passwd");//'}
    source = OpenSCADBackend().generate(spec(operation))

    assert validate_scad_source(source) == []
    assert "import(" not in source


def test_label_newlines_cannot_break_out_of_comment():
    operation = {**HOLE, "label": 'ok\nimport("/etc/passwd")'}
    source = OpenSCADBackend().generate(spec(operation))
    assert validate_scad_source(source) == []
    assert "import(" not in source


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


def test_validate_reports_operation_problems():
    model = GeneratedModel(
        specification=spec({**HOLE, "normal": {"x": 0, "y": 0, "z": 0}}),
        scad_source="cube([1, 1, 1]);",
    )
    problems = OpenSCADBackend().validate(model)
    assert any("normal" in problem for problem in problems)


def test_validate_accepts_valid_operations():
    source = OpenSCADBackend().generate(spec(HOLE))
    model = GeneratedModel(specification=spec(HOLE), scad_source=source)
    assert OpenSCADBackend().validate(model) == []
