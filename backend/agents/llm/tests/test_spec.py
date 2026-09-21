"""Validation tests for the structured specification (terv.md 8. fejezet)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from agents.spec import Dimensions, ModelSpecification, Mounting

VALID: dict[str, Any] = {
    "object": "phone_holder",
    "dimensions": {"width": 70.6, "height": 147, "thickness": 7.6},
    "angle": 15,
    "wall_thickness": 4,
    "mounting": {"type": "M5", "count": 2},
    "material": "PETG",
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
    dumped = ModelSpecification.model_validate(VALID).model_dump()
    assert dumped == VALID


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
    for field in ("object", "dimensions", "angle", "wall_thickness", "mounting", "material"):
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
