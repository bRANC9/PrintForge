"""Unit tests for annotation-driven edit operations (docs/visual-editing.md 3.1).

The Pydantic contract lives in ``agents.spec``: the LLM only ever emits an
``EditOperation`` list (never code or mesh data) and the CAD backend re-validates
every value. These pin the two failure modes the annotation API/agent layer
relies on -- an unknown ``kind`` and an out-of-bounds ``depth`` -- plus the
round-trip of a specification carrying ``operations``.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agents.spec import EditOperation, ModelSpecification

#: The terv.md 8. base example (no operations).
BASE: dict[str, Any] = {
    "object": "phone_holder",
    "dimensions": {"width": 70.6, "height": 147, "thickness": 7.6},
    "angle": 15,
    "wall_thickness": 4,
    "mounting": {"type": "M5", "count": 2},
    "material": "PETG",
}
#: A valid annotation-derived hole operation.
OPERATION: dict[str, Any] = {
    "kind": "hole",
    "origin": {"x": 35.0, "y": 12.5, "z": 8.0},
    "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
    "depth": 4.0,
    "diameter": 4.0,
    "label": "4 mm through hole",
}


def test_edit_operation_rejects_an_unknown_kind():
    with pytest.raises(ValidationError):
        EditOperation.model_validate({**OPERATION, "kind": "engrave"})


@pytest.mark.parametrize("depth", [0.0, 0.3, 200.1, 10_000.0])
def test_edit_operation_rejects_an_out_of_bounds_depth(depth):
    with pytest.raises(ValidationError):
        EditOperation.model_validate({**OPERATION, "depth": depth})


def test_edit_operation_depth_bounds_are_inclusive():
    assert EditOperation.model_validate({**OPERATION, "depth": 0.4}).depth == 0.4
    assert EditOperation.model_validate({**OPERATION, "depth": 200.0}).depth == 200.0


def test_model_specification_round_trips_with_operations():
    payload = {**BASE, "operations": [OPERATION]}

    dumped = ModelSpecification.model_validate(payload).model_dump()

    operation = dumped["operations"][0]
    assert operation["kind"] == "hole"
    assert operation["origin"] == {"x": 35.0, "y": 12.5, "z": 8.0}
    assert operation["normal"] == {"x": 0.0, "y": 0.0, "z": 1.0}
    assert operation["depth"] == 4.0
    assert operation["diameter"] == 4.0
    # Dumping and re-validating is stable: no field drift across a round-trip.
    assert ModelSpecification.model_validate(dumped).model_dump() == dumped


def test_empty_operations_are_omitted_from_the_base_spec():
    # The base terv.md 8. example round-trips exactly: the (empty) operations
    # list is serialised only when the edit actually adds features.
    assert ModelSpecification.model_validate(BASE).model_dump() == BASE
