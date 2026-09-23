"""Validation tests for the structured specification (terv.md 8. fejezet)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from agents.spec import (
    Clarification,
    Dimensions,
    EditOperation,
    ModelSpecification,
    Mounting,
    Primitive,
    ReviewResult,
    Vec2,
    Vec3,
)

VALID: dict[str, Any] = {
    "object": "phone_holder",
    "dimensions": {"width": 70.6, "height": 147, "thickness": 7.6},
    "angle": 15,
    "wall_thickness": 4,
    "mounting": {"type": "M5", "count": 2},
    "material": "PETG",
}

VALID_HOLE: dict[str, Any] = {
    "kind": "hole",
    "origin": {"x": 35.0, "y": 12.5, "z": 8.0},
    "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
    "depth": 4.0,
    "diameter": 4.0,
    "label": "4 mm through hole",
}


def spec(**overrides: Any) -> dict[str, Any]:
    """Return a copy of VALID with top-level/nested overrides applied."""
    payload = copy.deepcopy(VALID)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            payload[key].update(value)
        else:
            payload[key] = value
    return payload


def test_valid_spec_parses_and_exposes_fields():
    model = ModelSpecification.model_validate(VALID)
    assert model.object == "phone_holder"
    assert model.dimensions.width == pytest.approx(70.6)
    assert model.dimensions.height == pytest.approx(147.0)
    assert model.dimensions.thickness == pytest.approx(7.6)
    assert model.angle == pytest.approx(15.0)
    assert model.wall_thickness == pytest.approx(4.0)
    assert model.mounting.type == "M5"
    assert model.mounting.count == 2
    assert model.material == "PETG"


def test_model_dump_matches_terv_example_shape():
    # A base spec has no features, so the empty ``operations`` list is omitted
    # from the serialised payload and the terv.md 8. example round-trips exactly.
    assert ModelSpecification.model_validate(VALID).model_dump() == VALID


def test_integral_dimensions_are_coerced_to_float():
    model = ModelSpecification.model_validate(spec(dimensions={"height": 147}))
    assert isinstance(model.dimensions.height, float)
    assert model.dimensions.height == pytest.approx(147.0)


def test_material_is_normalised_to_uppercase():
    assert ModelSpecification.model_validate(spec(material=" petg ")).material == "PETG"


def test_example_helper_is_valid_and_normalised():
    example = ModelSpecification.example()
    assert example["object"] == "phone_holder"
    assert example["material"] == "PETG"


def test_to_json_schema_lists_all_fields():
    schema = ModelSpecification.to_json_schema()
    assert schema["title"] == "ModelSpecification"
    for field in (
        "object",
        "dimensions",
        "angle",
        "wall_thickness",
        "mounting",
        "material",
        "operations",
        "primitives",
    ):
        assert field in schema["properties"]


def test_angle_bounds_are_inclusive():
    assert ModelSpecification.model_validate(spec(angle=0)).angle == 0
    assert ModelSpecification.model_validate(spec(angle=180)).angle == 180


def test_wall_thickness_minimum_is_printable():
    assert ModelSpecification.model_validate(spec(wall_thickness=0.4)).wall_thickness == 0.4


def test_mounting_count_zero_is_allowed():
    assert ModelSpecification.model_validate(spec(mounting={"count": 0})).mounting.count == 0


INVALID_PAYLOADS: list[dict[str, Any]] = [
    spec(object=""),
    spec(object="x" * 129),
    spec(dimensions={"width": 0}),
    spec(dimensions={"width": -5}),
    spec(dimensions={"width": 1001}),
    spec(dimensions={"height": 0}),
    spec(dimensions={"thickness": 0}),
    spec(angle=-1),
    spec(angle=181),
    spec(wall_thickness=0),
    spec(wall_thickness=0.3),
    spec(wall_thickness=101),
    spec(mounting={"type": ""}),
    spec(mounting={"type": "x" * 65}),
    spec(mounting={"count": -1}),
    spec(mounting={"count": 65}),
    spec(material=""),
    spec(extra="not allowed"),
    {"object": "phone_holder"},
    {},
]


@pytest.mark.parametrize("payload", INVALID_PAYLOADS)
def test_invalid_specs_are_rejected(payload):
    with pytest.raises(ValidationError):
        ModelSpecification.model_validate(payload)


def test_unknown_nested_field_is_rejected():
    with pytest.raises(ValidationError):
        Dimensions.model_validate({"width": 1, "height": 1, "thickness": 1, "depth": 1})


def test_mounting_count_must_be_integer():
    with pytest.raises(ValidationError):
        Mounting.model_validate({"type": "M5", "count": 1.5})


# ---------------------------------------------------------------------------
# Annotation-driven edit operations (docs/visual-editing.md 3.1).
# ---------------------------------------------------------------------------


def test_spec_without_operations_defaults_to_empty_list():
    model = ModelSpecification.model_validate(VALID)
    assert model.operations == []
    # Empty features are omitted from the payload (canonical terv.md example).
    assert "operations" not in model.model_dump()


def test_spec_with_valid_hole_operation_validates():
    model = ModelSpecification.model_validate(spec(operations=[VALID_HOLE]))
    assert len(model.operations) == 1
    operation = model.operations[0]
    assert operation.kind == "hole"
    assert operation.origin.x == pytest.approx(35.0)
    assert operation.origin.y == pytest.approx(12.5)
    assert operation.origin.z == pytest.approx(8.0)
    assert operation.normal.z == pytest.approx(1.0)
    assert operation.depth == pytest.approx(4.0)
    assert operation.diameter == pytest.approx(4.0)
    assert operation.width is None
    assert operation.height is None
    assert operation.length is None
    assert operation.label == "4 mm through hole"
    # A non-empty feature list is serialised for the CAD backend.
    dumped = model.model_dump()
    assert dumped["operations"][0]["kind"] == "hole"
    assert dumped["operations"][0]["origin"] == {"x": 35.0, "y": 12.5, "z": 8.0}


def test_all_operation_kinds_are_accepted():
    for kind in ("hole", "pocket", "boss", "slot", "cut", "add"):
        operation = EditOperation.model_validate({**VALID_HOLE, "kind": kind})
        assert operation.kind == kind


def test_operation_with_bad_kind_is_rejected():
    with pytest.raises(ValidationError):
        ModelSpecification.model_validate(spec(operations=[{**VALID_HOLE, "kind": "engrave"}]))


@pytest.mark.parametrize("depth", [0.0, 0.3, 200.1, 10_000.0])
def test_operation_depth_out_of_bounds_is_rejected(depth):
    with pytest.raises(ValidationError):
        EditOperation.model_validate({**VALID_HOLE, "depth": depth})


def test_operation_depth_bounds_are_inclusive():
    assert EditOperation.model_validate({**VALID_HOLE, "depth": 0.4}).depth == pytest.approx(0.4)
    assert EditOperation.model_validate({**VALID_HOLE, "depth": 200.0}).depth == pytest.approx(
        200.0
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("width", 0.3),
        ("height", 501.0),
        ("length", 500.1),
        ("diameter", 200.1),
    ],
)
def test_operation_optional_dimension_bounds_are_enforced(field, value):
    with pytest.raises(ValidationError):
        EditOperation.model_validate({**VALID_HOLE, field: value})


def test_operation_label_max_length_is_enforced():
    with pytest.raises(ValidationError):
        EditOperation.model_validate({**VALID_HOLE, "label": "x" * 121})


def test_vec3_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Vec3.model_validate({"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0})


def test_edit_operation_forbids_extra_fields():
    with pytest.raises(ValidationError):
        EditOperation.model_validate({**VALID_HOLE, "radius": 2.0})


def test_json_schema_exposes_bounded_edit_operation():
    schema = ModelSpecification.to_json_schema()
    assert "operations" in schema["properties"]
    operation_schema = schema["$defs"]["EditOperation"]
    assert operation_schema["additionalProperties"] is False
    assert operation_schema["properties"]["depth"] == {
        "minimum": 0.4,
        "maximum": 200.0,
        "description": "Cut depth / boss height in mm; required for every operation.",
        "title": "Depth",
        "type": "number",
    }
    kinds = operation_schema["properties"]["kind"]["enum"]
    assert kinds == ["hole", "pocket", "boss", "slot", "cut", "add"]


def test_json_schema_documents_per_kind_field_requirements():
    """The schema the LLM sees must spell out which kinds need which fields.

    Regression guard for the editor LLM emitting a ``slot`` operation without
    ``diameter`` (the CAD parser then failed with "operations[0].diameter is
    required for a 'slot' operation"): the requirement has to be explicit in
    the structured-output schema, not only enforced downstream.
    """
    properties = ModelSpecification.to_json_schema()["$defs"]["EditOperation"]["properties"]
    assert properties["diameter"]["description"] == (
        "Required for kinds hole, boss and slot; ignored otherwise."
    )
    assert properties["length"]["description"] == ("Required for the slot kind; ignored otherwise.")
    assert properties["width"]["description"] == (
        "Required for kinds pocket, cut and add; ignored otherwise."
    )
    assert properties["height"]["description"] == (
        "Required for kinds pocket, cut and add; ignored otherwise."
    )
    assert properties["depth"]["description"] == (
        "Cut depth / boss height in mm; required for every operation."
    )


def test_example_helper_is_unchanged_by_operations_field():
    assert ModelSpecification.example() == VALID


# ---------------------------------------------------------------------------
# Prompt-driven CSG primitives (docs/cad-primitives.md 1).
# ---------------------------------------------------------------------------

VALID_BOX: dict[str, Any] = {
    "type": "box",
    "role": "add",
    "position": {"x": 10.0, "y": 20.0, "z": 5.0},
    "rotation": {"x": 0.0, "y": 0.0, "z": 45.0},
    "width": 40.0,
    "depth": 30.0,
    "height": 10.0,
    "label": "base plate",
}

VALID_CYLINDER: dict[str, Any] = {
    "type": "cylinder",
    "role": "subtract",
    "position": {"x": 0.0, "y": 0.0, "z": 3.0},
    "diameter": 4.0,
    "height": 8.0,
    "label": "M4 hole",
}


def test_spec_without_primitives_defaults_to_empty_list():
    model = ModelSpecification.model_validate(VALID)
    assert model.primitives == []
    # Empty primitives are omitted from the payload exactly like empty operations.
    assert "primitives" not in model.model_dump()


def test_spec_with_valid_box_primitive_validates():
    model = ModelSpecification.model_validate(spec(primitives=[VALID_BOX]))
    assert len(model.primitives) == 1
    primitive = model.primitives[0]
    assert primitive.type == "box"
    assert primitive.role == "add"
    assert primitive.position.x == pytest.approx(10.0)
    assert primitive.position.y == pytest.approx(20.0)
    assert primitive.position.z == pytest.approx(5.0)
    assert primitive.rotation.z == pytest.approx(45.0)
    assert primitive.width == pytest.approx(40.0)
    assert primitive.depth == pytest.approx(30.0)
    assert primitive.height == pytest.approx(10.0)
    assert primitive.diameter is None
    assert primitive.label == "base plate"
    # A non-empty primitive list is serialised for the CAD backend.
    dumped = model.model_dump()
    assert dumped["primitives"][0]["type"] == "box"
    assert dumped["primitives"][0]["position"] == {"x": 10.0, "y": 20.0, "z": 5.0}


def test_spec_with_valid_cylinder_primitive_validates():
    model = ModelSpecification.model_validate(spec(primitives=[VALID_CYLINDER]))
    primitive = model.primitives[0]
    assert primitive.type == "cylinder"
    assert primitive.role == "subtract"
    assert primitive.position.z == pytest.approx(3.0)
    assert primitive.diameter == pytest.approx(4.0)
    assert primitive.height == pytest.approx(8.0)
    assert primitive.width is None
    assert primitive.depth is None


def test_primitive_defaults_are_centred_and_additive():
    primitive = Primitive.model_validate({"type": "sphere", "diameter": 20.0})
    assert primitive.role == "add"
    assert primitive.position.model_dump() == {"x": 0.0, "y": 0.0, "z": 0.0}
    assert primitive.rotation.model_dump() == {"x": 0.0, "y": 0.0, "z": 0.0}
    assert primitive.label == ""


def test_primitive_with_bad_type_is_rejected():
    with pytest.raises(ValidationError):
        Primitive.model_validate({"type": "torus", "diameter": 5.0})


def test_primitive_with_bad_role_is_rejected():
    with pytest.raises(ValidationError):
        Primitive.model_validate({**VALID_BOX, "role": "cut"})


@pytest.mark.parametrize("field", ["width", "depth", "height", "diameter"])
@pytest.mark.parametrize("value", [0.0, 0.3, 1000.1, 10_000.0])
def test_primitive_size_out_of_bounds_is_rejected(field, value):
    with pytest.raises(ValidationError):
        Primitive.model_validate({"type": "box", field: value})


def test_primitive_size_bounds_are_inclusive():
    small = Primitive.model_validate({"type": "box", "width": 0.4, "depth": 0.4, "height": 0.4})
    assert small.width == pytest.approx(0.4)
    large = Primitive.model_validate({"type": "cylinder", "diameter": 1000.0, "height": 1000.0})
    assert large.diameter == pytest.approx(1000.0)


def test_primitive_label_max_length_is_enforced():
    with pytest.raises(ValidationError):
        Primitive.model_validate({**VALID_BOX, "label": "x" * 121})


def test_primitive_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Primitive.model_validate({**VALID_BOX, "radius": 2.0})


def test_json_schema_exposes_bounded_primitive():
    schema = ModelSpecification.to_json_schema()
    assert "primitives" in schema["properties"]
    primitive_schema = schema["$defs"]["Primitive"]
    assert primitive_schema["additionalProperties"] is False
    assert primitive_schema["properties"]["type"]["enum"] == [
        "box",
        "cylinder",
        "sphere",
        "cone",
        "extrude",
    ]
    assert primitive_schema["properties"]["role"]["enum"] == ["add", "subtract"]
    # Optional numeric sizes are exposed as ``anyOf`` (number | null).
    number_schema = primitive_schema["properties"]["width"]["anyOf"][0]
    assert number_schema["minimum"] == 0.4
    assert number_schema["maximum"] == 1000.0
    assert primitive_schema["properties"]["label"]["maxLength"] == 120
    assert schema["properties"]["primitives"]["maxItems"] == 64


def test_json_schema_documents_per_type_primitive_field_requirements():
    """The schema the LLM sees must spell out which primitive types need which sizes.

    Regression guard for a local model emitting a ``box`` primitive without
    ``depth``/``height``: the requirement has to be explicit in the
    structured-output schema, not only enforced downstream.
    """
    properties = ModelSpecification.to_json_schema()["$defs"]["Primitive"]["properties"]
    assert properties["width"]["description"] == "Box size along X in mm (required for type box)."
    assert properties["depth"]["description"] == "Box size along Y in mm (required for type box)."
    assert properties["height"]["description"] == (
        "Box size along Z, or the cylinder/cone height, in mm "
        "(required for box, cylinder and cone)."
    )
    assert properties["diameter"]["description"] == (
        "Cylinder/sphere/cone diameter in mm (required for cylinder, sphere and cone)."
    )
    assert properties["position"]["description"] == (
        "Primitive centre in mm; the part rests on the build plate (minimum Z = 0)."
    )
    assert properties["rotation"]["description"] == "XYZ rotation in degrees."
    assert properties["role"]["description"] == "add adds material, subtract cuts it away."


def test_example_helper_is_unchanged_by_primitives_field():
    assert ModelSpecification.example() == VALID


# ---------------------------------------------------------------------------
# Vision self-check review contract (docs/vision-self-check.md 3).
# ---------------------------------------------------------------------------

VALID_REVIEW: dict[str, Any] = {
    "matches": False,
    "issues": ["wall is too thin", "mounting holes are missing"],
    "summary": "The preview does not match: walls and mounting holes differ.",
}


def test_valid_review_round_trips():
    review = ReviewResult.model_validate(VALID_REVIEW)
    assert review.matches is False
    assert review.issues == ["wall is too thin", "mounting holes are missing"]
    assert review.summary == "The preview does not match: walls and mounting holes differ."
    assert review.model_dump() == VALID_REVIEW


def test_review_defaults_are_empty():
    review = ReviewResult.model_validate({"matches": True})
    assert review.matches is True
    assert review.issues == []
    assert review.summary == ""
    assert review.model_dump() == {"matches": True, "issues": [], "summary": ""}


def test_review_strips_whitespace():
    review = ReviewResult.model_validate({"matches": True, "summary": "  ok  "})
    assert review.summary == "ok"


def test_review_missing_matches_is_rejected():
    with pytest.raises(ValidationError):
        ReviewResult.model_validate({"issues": ["x"], "summary": "y"})


def test_review_too_many_issues_is_rejected():
    with pytest.raises(ValidationError):
        ReviewResult.model_validate({"matches": False, "issues": ["x"] * 21})


def test_review_issue_count_boundary_is_inclusive():
    review = ReviewResult.model_validate({"matches": False, "issues": ["x"] * 20})
    assert len(review.issues) == 20


def test_review_summary_too_long_is_rejected():
    with pytest.raises(ValidationError):
        ReviewResult.model_validate({"matches": False, "summary": "x" * 501})


def test_review_forbids_extra_fields():
    with pytest.raises(ValidationError):
        ReviewResult.model_validate({**VALID_REVIEW, "confidence": 0.9})


def test_review_to_json_schema_lists_all_fields():
    schema = ReviewResult.to_json_schema()
    assert schema["title"] == "ReviewResult"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"matches", "issues", "summary"}
    assert schema["properties"]["matches"]["type"] == "boolean"
    assert schema["properties"]["issues"]["type"] == "array"
    assert schema["properties"]["issues"]["maxItems"] == 20
    assert schema["properties"]["summary"]["maxLength"] == 500
    assert schema["required"] == ["matches"]


def test_review_model_json_schema_matches_helper():
    assert ReviewResult.model_json_schema() == ReviewResult.to_json_schema()


# ---------------------------------------------------------------------------
# 2D extrude primitive (docs/skills.md 5).
# ---------------------------------------------------------------------------

VALID_PROFILE: list[dict[str, float]] = [
    {"x": 0.0, "y": 0.0},
    {"x": 40.0, "y": 0.0},
    {"x": 40.0, "y": 40.0},
    {"x": 0.0, "y": 40.0},
]

VALID_EXTRUDE: dict[str, Any] = {
    "type": "extrude",
    "role": "add",
    "position": {"x": 0.0, "y": 0.0, "z": 0.0},
    "height": 25.0,
    "profile": VALID_PROFILE,
    "wall_thickness": 1.2,
    "round_radius": 1.0,
    "label": "cookie cutter wall",
}


def test_valid_extrude_primitive_validates():
    model = ModelSpecification.model_validate(spec(primitives=[VALID_EXTRUDE]))
    primitive = model.primitives[0]
    assert primitive.type == "extrude"
    assert primitive.height == pytest.approx(25.0)
    assert len(primitive.profile) == 4
    assert primitive.profile[1].x == pytest.approx(40.0)
    assert primitive.profile[1].y == pytest.approx(0.0)
    assert primitive.wall_thickness == pytest.approx(1.2)
    assert primitive.round_radius == pytest.approx(1.0)
    # A non-empty primitive list is serialised for the CAD backend.
    dumped = model.model_dump()
    assert dumped["primitives"][0]["profile"][0] == {"x": 0.0, "y": 0.0}


def test_extrude_defaults_have_empty_profile_and_optional_walls():
    primitive = Primitive.model_validate({**VALID_EXTRUDE})
    assert primitive.wall_thickness == pytest.approx(1.2)
    sphere = Primitive.model_validate({"type": "sphere", "diameter": 10.0})
    assert sphere.profile == []
    assert sphere.wall_thickness is None
    assert sphere.round_radius is None


@pytest.mark.parametrize(
    "profile",
    [
        [],
        [{"x": 0.0, "y": 0.0}],
        [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}],
    ],
)
def test_extrude_requires_at_least_three_profile_points(profile):
    with pytest.raises(ValidationError, match="at least 3 profile points"):
        Primitive.model_validate({**VALID_EXTRUDE, "profile": profile})
    with pytest.raises(ValidationError, match="at least 3 profile points"):
        ModelSpecification.model_validate(spec(primitives=[{**VALID_EXTRUDE, "profile": profile}]))


def test_extrude_with_exactly_three_points_is_accepted():
    primitive = Primitive.model_validate(
        {
            **VALID_EXTRUDE,
            "profile": [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 0.0, "y": 1.0}],
        }
    )
    assert len(primitive.profile) == 3


def test_non_extrude_types_are_not_profile_constrained():
    """Only ``extrude`` needs a profile; other types keep their loose shape."""
    for shape in (
        {"type": "box", "width": 1.0, "depth": 1.0, "height": 1.0},
        {"type": "cylinder", "diameter": 1.0, "height": 1.0},
        {"type": "sphere", "diameter": 1.0},
        {"type": "cone", "diameter": 1.0, "height": 1.0},
    ):
        assert Primitive.model_validate(shape).profile == []


def test_extrude_profile_too_many_points_is_rejected():
    profile = [{"x": float(i), "y": 0.0} for i in range(257)]
    with pytest.raises(ValidationError):
        Primitive.model_validate({**VALID_EXTRUDE, "profile": profile})


def test_extrude_profile_point_boundary_is_inclusive():
    profile = [{"x": float(i), "y": 0.0} for i in range(256)]
    assert len(Primitive.model_validate({**VALID_EXTRUDE, "profile": profile}).profile) == 256


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("value", [-1000.1, 1000.1, 10_000.0])
def test_extrude_profile_coordinates_are_bounded(axis, value):
    profile = [*VALID_PROFILE, {**VALID_PROFILE[0], axis: value}]
    with pytest.raises(ValidationError):
        Primitive.model_validate({**VALID_EXTRUDE, "profile": profile})


def test_extrude_profile_coordinate_bounds_are_inclusive():
    profile = [{"x": -1000.0, "y": 1000.0}, *VALID_PROFILE]
    primitive = Primitive.model_validate({**VALID_EXTRUDE, "profile": profile})
    assert primitive.profile[0].x == pytest.approx(-1000.0)
    assert primitive.profile[0].y == pytest.approx(1000.0)


@pytest.mark.parametrize("value", [0.0, 0.3, 100.1, 10_000.0])
def test_extrude_wall_thickness_out_of_bounds_is_rejected(value):
    with pytest.raises(ValidationError):
        Primitive.model_validate({**VALID_EXTRUDE, "wall_thickness": value})


def test_extrude_wall_thickness_bounds_are_inclusive():
    assert Primitive.model_validate(
        {**VALID_EXTRUDE, "wall_thickness": 0.4}
    ).wall_thickness == pytest.approx(0.4)
    assert Primitive.model_validate(
        {**VALID_EXTRUDE, "wall_thickness": 100.0}
    ).wall_thickness == pytest.approx(100.0)


@pytest.mark.parametrize("value", [-0.1, 100.1, 10_000.0])
def test_extrude_round_radius_out_of_bounds_is_rejected(value):
    with pytest.raises(ValidationError):
        Primitive.model_validate({**VALID_EXTRUDE, "round_radius": value})


def test_extrude_round_radius_zero_is_allowed():
    assert Primitive.model_validate({**VALID_EXTRUDE, "round_radius": 0.0}).round_radius == 0.0


def test_vec2_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Vec2.model_validate({"x": 0.0, "y": 0.0, "z": 0.0})


def test_extrude_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Primitive.model_validate({**VALID_EXTRUDE, "radius": 2.0})


def test_json_schema_exposes_extrude_fields():
    schema = ModelSpecification.to_json_schema()
    primitive_schema = schema["$defs"]["Primitive"]
    assert primitive_schema["properties"]["type"]["enum"] == [
        "box",
        "cylinder",
        "sphere",
        "cone",
        "extrude",
    ]
    profile_schema = primitive_schema["properties"]["profile"]
    assert profile_schema["maxItems"] == 256
    assert profile_schema["items"]["$ref"].endswith("/Vec2")
    assert "Vec2" in schema["$defs"]
    wall_schema = primitive_schema["properties"]["wall_thickness"]["anyOf"][0]
    assert wall_schema["minimum"] == 0.4
    assert wall_schema["maximum"] == 100.0
    round_schema = primitive_schema["properties"]["round_radius"]["anyOf"][0]
    assert round_schema["minimum"] == 0.0
    assert round_schema["maximum"] == 100.0


def test_example_helper_is_unchanged_by_extrude_fields():
    assert ModelSpecification.example() == VALID


# ---------------------------------------------------------------------------
# Planner clarification contract (docs/planner-clarification.md 1).
# ---------------------------------------------------------------------------

VALID_CLARIFICATION: dict[str, Any] = {
    "question": "How thick should the wall be?",
    "answer": "1.2",
    "kind": "assumed",
    "field": "wall_thickness",
}


def test_valid_clarification_round_trips():
    clarification = Clarification.model_validate(VALID_CLARIFICATION)
    assert clarification.question == "How thick should the wall be?"
    assert clarification.answer == "1.2"
    assert clarification.kind == "assumed"
    assert clarification.field == "wall_thickness"
    assert clarification.model_dump() == VALID_CLARIFICATION


def test_clarification_defaults_are_empty_assumed():
    clarification = Clarification.model_validate({"question": "Which material?"})
    assert clarification.answer == ""
    assert clarification.kind == "assumed"
    assert clarification.field == ""


def test_clarification_needs_user_input_kind_is_accepted():
    clarification = Clarification.model_validate({"question": "Width?", "kind": "needs_user_input"})
    assert clarification.kind == "needs_user_input"
    assert clarification.answer == ""


def test_clarification_strips_whitespace():
    clarification = Clarification.model_validate({"question": "  q  ", "answer": "  a  "})
    assert clarification.question == "q"
    assert clarification.answer == "a"


def test_clarification_bad_kind_is_rejected():
    with pytest.raises(ValidationError):
        Clarification.model_validate({**VALID_CLARIFICATION, "kind": "maybe"})


def test_clarification_question_max_length_is_enforced():
    with pytest.raises(ValidationError):
        Clarification.model_validate({"question": "x" * 301})


def test_clarification_answer_max_length_is_enforced():
    with pytest.raises(ValidationError):
        Clarification.model_validate({"question": "q", "answer": "x" * 601})


def test_clarification_field_max_length_is_enforced():
    with pytest.raises(ValidationError):
        Clarification.model_validate({"question": "q", "field": "x" * 121})


def test_clarification_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Clarification.model_validate({**VALID_CLARIFICATION, "confidence": 0.9})


def test_clarification_question_is_required():
    with pytest.raises(ValidationError):
        Clarification.model_validate({"answer": "1.2"})


def test_clarification_is_not_part_of_the_cad_contract():
    """Clarifications belong to the Planner, never to ModelSpecification."""
    assert "clarifications" not in ModelSpecification.to_json_schema()["properties"]
    with pytest.raises(ValidationError):
        ModelSpecification.model_validate(spec(clarifications=[VALID_CLARIFICATION]))
