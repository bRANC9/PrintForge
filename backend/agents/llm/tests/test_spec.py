"""Validation tests for the structured specification (terv.md 8. fejezet)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from agents.spec import Dimensions, EditOperation, ModelSpecification, Mounting, Vec3

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
        "description": "Cut depth / boss height in mm.",
        "title": "Depth",
        "type": "number",
    }
    kinds = operation_schema["properties"]["kind"]["enum"]
    assert kinds == ["hole", "pocket", "boss", "slot", "cut", "add"]


def test_example_helper_is_unchanged_by_operations_field():
    assert ModelSpecification.example() == VALID
