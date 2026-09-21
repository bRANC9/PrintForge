"""Structured specification validation (terv.md 8. es 26.3).

``agents.spec.ModelSpecification`` is the only contract between the LLM layer
and the CAD backend. These tests pin the terv.md 8. example and its bounds, then
verify a valid spec actually drives the OpenSCAD backend while an out-of-range
one never reaches it.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from agents.spec import ModelSpecification
from designs.cad.openscad import OpenSCADBackend, build_parameters, validate_scad_source

#: The exact example from terv.md 8. fejezet.
TERV_EXAMPLE: dict[str, Any] = {
    "object": "phone_holder",
    "dimensions": {
        "width": 70.6,
        "height": 147,
        "thickness": 7.6,
    },
    "angle": 15,
    "wall_thickness": 4,
    "mounting": {
        "type": "M5",
        "count": 2,
    },
    "material": "PETG",
}


def spec(**overrides: Any) -> dict[str, Any]:
    """A deep copy of :data:`TERV_EXAMPLE` with top-level/nested overrides."""
    payload = copy.deepcopy(TERV_EXAMPLE)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            payload[key].update(value)
        else:
            payload[key] = value
    return payload


# ---------------------------------------------------------------------------
# Accepted: the terv.md 8. example
# ---------------------------------------------------------------------------


def test_terv_example_is_accepted():
    model = ModelSpecification.model_validate(TERV_EXAMPLE)

    assert model.object == "phone_holder"
    assert model.dimensions.width == pytest.approx(70.6)
    assert model.dimensions.height == pytest.approx(147)
    assert model.dimensions.thickness == pytest.approx(7.6)
    assert model.angle == pytest.approx(15)
    assert model.wall_thickness == pytest.approx(4)
    assert model.mounting.type == "M5"
    assert model.mounting.count == 2
    assert model.material == "PETG"


def test_terv_example_round_trips_through_model_dump():
    assert ModelSpecification.model_validate(TERV_EXAMPLE).model_dump() == TERV_EXAMPLE


def test_example_helper_matches_the_terv_example():
    assert ModelSpecification.example() == TERV_EXAMPLE


def test_material_is_normalised_to_uppercase():
    model = ModelSpecification.model_validate(spec(material=" petg "))

    assert model.material == "PETG"


def test_bounds_are_inclusive():
    assert ModelSpecification.model_validate(spec(angle=0)).angle == 0
    assert ModelSpecification.model_validate(spec(angle=180)).angle == 180
    assert ModelSpecification.model_validate(spec(wall_thickness=0.4)).wall_thickness == 0.4
    assert (
        ModelSpecification.model_validate(spec(dimensions={"width": 1000})).dimensions.width == 1000
    )


def test_json_schema_exposes_every_field():
    schema = ModelSpecification.to_json_schema()

    assert schema["title"] == "ModelSpecification"
    for field in ("object", "dimensions", "angle", "wall_thickness", "mounting", "material"):
        assert field in schema["properties"]


# ---------------------------------------------------------------------------
# Rejected: out-of-range / malformed values
# ---------------------------------------------------------------------------


OUT_OF_RANGE: list[dict[str, Any]] = [
    spec(object=""),
    spec(object="x" * 129),
    spec(dimensions={"width": 0}),
    spec(dimensions={"width": -5}),
    spec(dimensions={"width": 1001}),
    spec(dimensions={"height": 0}),
    spec(dimensions={"thickness": 0}),
    spec(dimensions={"thickness": 1001}),
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
    spec(mounting={"unknown_field": 1}),
    {"object": "phone_holder"},
    {},
]


@pytest.mark.parametrize("payload", OUT_OF_RANGE)
def test_out_of_range_or_malformed_specs_are_rejected(payload):
    with pytest.raises(ValidationError):
        ModelSpecification.model_validate(payload)


# ---------------------------------------------------------------------------
# Integration: a valid spec drives the CAD backend, an invalid one never does
# ---------------------------------------------------------------------------


def test_valid_spec_generates_sandbox_clean_scad():
    specification = ModelSpecification.model_validate(TERV_EXAMPLE).model_dump()

    source = OpenSCADBackend(mode="local").generate(specification)

    assert validate_scad_source(source) == []
    assert "import(" not in source
    assert "surface(" not in source
    assert "phone_holder" in source


def test_valid_spec_maps_to_cad_parameters():
    specification = ModelSpecification.model_validate(TERV_EXAMPLE).model_dump()

    parameters = build_parameters(specification)

    assert parameters.width == pytest.approx(70.6)
    assert parameters.height == pytest.approx(147)
    assert parameters.hole_count == 2
    assert parameters.hole_diameter == pytest.approx(5.3)


def test_out_of_range_spec_is_rejected_before_the_cad_backend():
    # 0.2 mm wall passes nothing: Pydantic rejects it, so the CAD backend is
    # never handed a spec it cannot print.
    with pytest.raises(ValidationError):
        ModelSpecification.model_validate(spec(wall_thickness=0.2))
