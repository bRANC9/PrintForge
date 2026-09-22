"""Unit tests for deterministic annotation anchor enforcement.

Pure logic only: no LLM, no database, no OpenSCAD. These tests pin the
guardrail that stops the Editor LLM from silently placing a feature far away
from the annotation that requested it (docs/visual-editing.md 3.5).
"""

from __future__ import annotations

import copy
from typing import Any

from agents.graph.editor import enforce_annotation_anchors

TOLERANCE_MM = 25.0


def _annotation(**overrides: Any) -> dict[str, Any]:
    annotation: dict[str, Any] = {
        "id": "c1",
        "kind": "point",
        "point": [35.0, 12.5, 8.0],
        "normal": [0.0, 0.0, 1.0],
        "instruction": "4 mm-es átmenő lyuk",
    }
    annotation.update(overrides)
    return annotation


def _operation(**overrides: Any) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "kind": "hole",
        "origin": {"x": 35.0, "y": 12.5, "z": 8.0},
        "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
        "depth": 8.0,
        "diameter": 4.0,
        "label": "existing label",
    }
    operation.update(overrides)
    return operation


def test_operation_already_at_the_annotation_origin_is_unchanged():
    operation = _operation()
    original = copy.deepcopy(operation)

    result, warnings = enforce_annotation_anchors(
        [operation], [_annotation()], tolerance_mm=TOLERANCE_MM
    )

    assert result == [original]
    assert warnings == []
    # The input dict is never mutated.
    assert operation == original


def test_operation_three_mm_away_is_snapped_to_origin_axis_and_label():
    operation = _operation(
        origin={"x": 35.0, "y": 12.5, "z": 11.0},  # 3 mm above the annotation
        normal={"x": 0.0, "y": 0.0, "z": 0.5},  # non-unit, must be normalised
        label="",
    )

    result, warnings = enforce_annotation_anchors(
        [operation], [_annotation()], tolerance_mm=TOLERANCE_MM
    )

    assert warnings == []
    assert result[0]["origin"] == {"x": 35.0, "y": 12.5, "z": 8.0}
    assert result[0]["normal"] == {"x": 0.0, "y": 0.0, "z": 1.0}
    assert result[0]["label"] == "4 mm-es átmenő lyuk"
    # The far input is untouched.
    assert operation["origin"] == {"x": 35.0, "y": 12.5, "z": 11.0}


def test_operation_far_away_is_kept_and_produces_a_warning():
    operation = _operation(origin={"x": 535.0, "y": 12.5, "z": 8.0})  # 500 mm away

    result, warnings = enforce_annotation_anchors(
        [operation], [_annotation()], tolerance_mm=TOLERANCE_MM
    )

    assert result[0]["origin"] == {"x": 535.0, "y": 12.5, "z": 8.0}
    assert len(warnings) == 1
    assert warnings[0].startswith("operation 0 is 500.0 mm")
    assert "nearest annotation" in warnings[0]
    assert "kept unchanged" in warnings[0]


def test_region_annotation_uses_its_centroid_as_the_origin():
    annotation = _annotation(
        kind="region",
        point=[10.0, 5.0, 20.0],
        normal=[1.0, 0.0, 0.0],
        region={
            "centroid": [9.8, 5.1, 20.0],
            "normal": [1.0, 0.0, 0.0],
            "size": [12.0, 4.0, 0.0],
            "count": 5,
        },
        instruction="3 mm mély zseb",
    )
    operation = _operation(
        kind="pocket",
        origin={"x": 11.8, "y": 5.1, "z": 20.0},  # 2 mm from the centroid
        normal={"x": 1.0, "y": 0.0, "z": 0.0},
        label="",
    )

    result, warnings = enforce_annotation_anchors(
        [operation], [annotation], tolerance_mm=TOLERANCE_MM
    )

    assert warnings == []
    assert result[0]["origin"] == {"x": 9.8, "y": 5.1, "z": 20.0}
    assert result[0]["normal"] == {"x": 1.0, "y": 0.0, "z": 0.0}
    assert result[0]["label"] == "3 mm mély zseb"


def test_zero_normal_annotation_is_ignored():
    annotation = _annotation(normal=[0.0, 0.0, 0.0])
    operation = _operation()
    original = copy.deepcopy(operation)

    result, warnings = enforce_annotation_anchors(
        [operation], [annotation], tolerance_mm=TOLERANCE_MM
    )

    assert warnings == []
    assert result == [original]


def test_missing_origin_annotation_is_ignored():
    annotation = _annotation()
    annotation.pop("point")
    operation = _operation()
    original = copy.deepcopy(operation)

    result, warnings = enforce_annotation_anchors(
        [operation], [annotation], tolerance_mm=TOLERANCE_MM
    )

    assert warnings == []
    assert result == [original]


def test_no_annotations_returns_copies_without_warnings():
    operation = _operation(origin={"x": 999.0, "y": 0.0, "z": 0.0})

    result, warnings = enforce_annotation_anchors([operation], [], tolerance_mm=TOLERANCE_MM)

    assert warnings == []
    assert result == [operation]
    assert result[0] is not operation  # a copy, not the same object


def test_instruction_label_is_truncated_to_120_characters():
    annotation = _annotation(instruction="x" * 200)
    operation = _operation(label="")

    result, _ = enforce_annotation_anchors([operation], [annotation], tolerance_mm=TOLERANCE_MM)

    assert result[0]["label"] == "x" * 120
