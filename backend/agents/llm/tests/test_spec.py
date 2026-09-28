"""Validation tests for the structured specification (terv.md 8. fejezet)."""

from __future__ import annotations

import copy
import inspect
from typing import Any

import pytest
from pydantic import ValidationError

import agents.spec as spec_module
from agents.graph.planner import PlannerPlan
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
        "description": "Cut depth / boss height in mm; always required.",
        "title": "Depth",
        "type": "number",
    }
    kinds = operation_schema["properties"]["kind"]["enum"]
    assert kinds == ["hole", "pocket", "boss", "slot", "cut", "add"]


#: Which fields each operation kind needs, and which every kind needs.
#:
#: The model is told this through the **field descriptions**, not through a
#: paragraph in the system prompt (the Planner prompt would otherwise restate
#: the schema verbatim, and the two together blew the 4096-token context), so
#: these maps are the machine-checkable form of the same contract.
OPERATION_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "hole": ("diameter",),
    "boss": ("diameter",),
    "slot": ("diameter", "length"),
    "pocket": ("width", "height"),
    "cut": ("width", "height"),
    "add": ("width", "height"),
}
ALWAYS_REQUIRED_OPERATION_FIELDS = ("depth", "origin", "normal")

#: Which sizes each primitive type needs.
PRIMITIVE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "box": ("width", "depth", "height"),
    "cylinder": ("diameter", "height"),
    "cone": ("diameter", "height"),
    "sphere": ("diameter",),
    "extrude": ("profile", "height"),
}


def _description_nodes(node: object, path: str = "") -> list[tuple[str, str]]:
    """Every ``description`` string in a JSON Schema, as ``(path, text)``."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if key == "description" and isinstance(value, str):
                found.append((child, value))
            found.extend(_description_nodes(value, child))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_description_nodes(value, f"{path}[{index}]"))
    return found


def test_planner_plan_schema_description_budget_is_bounded():
    """No developer prose may creep back into the Planner's JSON Schema.

    The Planner request is measured against a 4096-token context and
    ``PlannerPlan.model_json_schema()`` is the largest part of it: its
    ``description`` strings were 4440 characters (41% of the schema) once, of
    which a third was Sphinx roles, ``docs/*.md`` pointers and "why this
    exists" rationale that the model cannot use. A 4.2k-token request killed
    five of thirteen local models outright. The human documentation now lives in
    ``#:`` comment blocks in :mod:`agents.spec`, so this budget is the guard
    that keeps it there.
    """
    total = sum(len(text) for _, text in _description_nodes(PlannerPlan.model_json_schema()))
    assert total <= 1800, f"PlannerPlan schema prose grew to {total} characters"


def test_schema_carries_no_developer_provenance():
    """Sphinx roles, doc pointers and rationale must not reach the model."""
    text = " ".join(t for _, t in _description_nodes(PlannerPlan.model_json_schema()))
    for pointer in ("terv.md", "docs/", ":class:", "``", "fejezet"):
        assert pointer not in text, f"{pointer!r} leaked into the model-facing schema"


#: The human documentation that was moved out of the schema, and the fragments
#: that must survive directly above the class as a ``#:`` comment block.
#: Relocating it was allowed; losing it was not.
RELOCATED_DOCS: dict[type, tuple[str, ...]] = {
    Primitive: ("docs/cad-primitives.md 1", "never OpenSCAD code", "union/difference"),
    EditOperation: ("docs/visual-editing.md 3.1", "never OpenSCAD code", "clamps"),
    Clarification: ("docs/planner-clarification.md 1", "Planner-only contract"),
    ModelSpecification: ("terv.md 8. fejezet", "never free text"),
    Vec2: ("docs/skills.md 5", "linear_extrude(height) polygon(points)"),
    Vec3: ("docs 3.1", "model coordinates"),
    Mounting: ("terv.md 8.", "the part is not"),
    ReviewResult: ("docs/vision-self-check.md", "bounded collections"),
    Dimensions: ("MAX_DIMENSION_MM", "Bounding-box dimensions"),
}


def _doc_block_above(model: type) -> str:
    """Return the ``#:`` comment block directly above *model*, de-marked.

    The ``#:`` prefixes are stripped and the text is whitespace-collapsed, so a
    fragment may be matched even when the source wraps it across two lines.
    """
    lines = inspect.getsource(spec_module).splitlines()
    index = next(
        i for i, line in enumerate(lines) if line.startswith(f"class {model.__name__}(BaseModel):")
    )
    block: list[str] = []
    for line in reversed(lines[:index]):
        if line.startswith("#:"):
            block.append(line[2:].strip())
        elif not line.strip() and block:
            break
        elif not line.strip():
            continue
        else:
            break
    return " ".join(reversed(block)).strip()


@pytest.mark.parametrize(
    "model, fragments", sorted(RELOCATED_DOCS.items(), key=lambda item: item[0].__name__)
)
def test_human_documentation_is_still_in_the_source(model, fragments) -> None:
    """The prose cut from the schema must survive as a ``#:`` block in the file.

    Pydantic has no "docstring for humans, short one for the schema" split, so
    the developer documentation was moved verbatim into the comment block above
    each class. These tests keep that move honest in both directions: the budget
    test above fails if it drifts back into ``description``, and these fail if it
    is deleted instead of relocated.
    """
    block = _doc_block_above(model)
    assert block, f"{model.__name__} lost its ``#:`` documentation block"
    for fragment in fragments:
        assert fragment in block, f"{model.__name__} dropped {fragment!r}"


def test_developer_documentation_is_gone_from_every_schema_description() -> None:
    """No ``$defs`` description in any shipped model may carry provenance."""
    for model in (ModelSpecification, ReviewResult):
        for path, text in _description_nodes(model.to_json_schema()):
            assert "docs/" not in text, f"{model.__name__} {path} points at a doc file"
            assert ":class:" not in text, f"{model.__name__} {path} carries a Sphinx role"


def test_schema_documents_the_field_requirements_per_kind():
    """Each operation kind's required dimensions stay named on the fields.

    Regression guard for the editor LLM emitting a ``slot`` operation without
    ``diameter`` (the CAD parser then failed with "operations[0].diameter is
    required for a 'slot' operation"): the requirement has to be explicit in
    the structured-output schema, not only enforced downstream. It used to be
    asserted as one exact sentence per field; it is now asserted as the
    invariant, so rewording the sentence cannot silently drop a requirement.
    """
    schema = ModelSpecification.to_json_schema()["$defs"]["EditOperation"]
    properties = schema["properties"]

    for field in ALWAYS_REQUIRED_OPERATION_FIELDS:
        assert field in properties
        assert field in schema["required"]

    for kind, fields in OPERATION_REQUIREMENTS.items():
        assert kind in properties["kind"]["enum"]
        for field in fields:
            assert kind in properties[field]["description"], (
                f"{field} no longer states that it is required for {kind!r}: "
                f"{properties[field].get('description')!r}"
            )

    # A non-required dimension says so, so the model does not fill it blindly.
    assert "ignored otherwise" in properties["diameter"]["description"]
    # Units and the always-required rule survive on the field itself.
    assert "in mm" in properties["depth"]["description"]
    assert "required" in properties["depth"]["description"]
    assert "in mm" in properties["origin"]["description"]
    assert "unit length" in properties["normal"]["description"]
    # The add/subtract meaning of a kind is stated, not just enumerated.
    assert "add" in properties["kind"]["description"]
    assert "boss/add" in properties["kind"]["description"]


def test_json_schema_documents_per_type_primitive_field_requirements():
    """The schema the LLM sees must spell out which primitive types need which sizes.

    Regression guard for a local model emitting a ``box`` primitive without
    ``depth``/``height``: the requirement has to be explicit in the
    structured-output schema, not only enforced downstream. As above, the guard
    is the invariant (which type requires which size), not one exact sentence.
    """
    properties = ModelSpecification.to_json_schema()["$defs"]["Primitive"]["properties"]

    for kind, fields in PRIMITIVE_REQUIREMENTS.items():
        assert kind in properties["type"]["enum"]
        for field in fields:
            assert kind in properties[field]["description"], (
                f"{field} no longer states that it is required for {kind!r}: "
                f"{properties[field].get('description')!r}"
            )

    # A size that is not needed by a type says so, so the model does not fill
    # it blindly: ``width``/``depth`` are a box's only sizes.
    assert properties["width"]["description"].endswith("(required for box).")
    assert properties["depth"]["description"].endswith("(required for box).")
    # Every constraint the pipeline's known failures depend on, still present:
    assert properties["position"]["description"] == (
        "Primitive centre in mm; the part rests on the plate (min Z = 0)."
    )
    assert properties["rotation"]["description"] == "XYZ rotation in degrees."
    assert properties["role"]["description"] == "add adds material, subtract cuts it away."
    # Ordering and the >= 3-point minimum of the 2D profile.
    assert "in order" in properties["profile"]["description"]
    assert "min 3" in properties["profile"]["description"]
    assert "XZ plane" in properties["profile"]["description"]
    assert "in mm" in properties["profile"]["description"]
    # The extrude-only optionals.
    assert "extrude only" in properties["wall_thickness"]["description"]
    assert "extrude only" in properties["round_radius"]["description"]


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


def test_extrude_missing_height_defaults_to_object_height():
    extrude = {**VALID_EXTRUDE, "height": None}
    model = ModelSpecification.model_validate(
        spec(dimensions={"width": 60, "height": 25, "thickness": 3}, primitives=[extrude])
    )
    # A 2D extrusion's height is the object height; the spec fills it in.
    assert model.primitives[0].height == pytest.approx(25.0)


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
