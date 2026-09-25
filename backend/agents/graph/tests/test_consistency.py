"""Tests for the deterministic request <-> specification checks (no LLM)."""

from __future__ import annotations

from agents.graph.consistency import (
    SMALL_ITEM_MAX_MM,
    consistency_issue,
)

#: A rectangle profile: the shape the weak model actually produced.
RECTANGLE = [
    {"x": 0.0, "y": 0.0},
    {"x": 100.0, "y": 0.0},
    {"x": 100.0, "y": 200.0},
    {"x": 0.0, "y": 200.0},
    {"x": 0.0, "y": 0.0},
]

#: A plausible tree outline: a triangle-ish zigzag, definitely not a rectangle.
TREE = [
    {"x": 50.0, "y": 200.0},
    {"x": 10.0, "y": 120.0},
    {"x": 30.0, "y": 120.0},
    {"x": 0.0, "y": 40.0},
    {"x": 25.0, "y": 40.0},
    {"x": 25.0, "y": 0.0},
    {"x": 75.0, "y": 0.0},
    {"x": 75.0, "y": 40.0},
    {"x": 100.0, "y": 40.0},
    {"x": 70.0, "y": 120.0},
    {"x": 90.0, "y": 120.0},
]


def spec(primitives, **overrides):
    base = {
        "object": "Christmas Tree",
        "dimensions": {"width": 100.0, "height": 200.0, "thickness": 10.0},
        "wall_thickness": 3.0,
    }
    base.update(overrides)
    return {**base, "primitives": primitives}


def test_the_real_failure_is_caught():
    """The exact case: a tree press answered with a rectangular solid block."""
    produced = spec(
        [{"type": "extrude", "role": "add", "height": 400.0, "profile": RECTANGLE}],
    )

    issue = consistency_issue("karácsony fa formájú gyurma kinyomót szeretnék", produced)

    assert issue is not None
    assert "rectangle" in issue


def test_a_real_tree_outline_passes_the_silhouette_rule():
    produced = spec(
        [
            {
                "type": "extrude",
                "role": "add",
                "height": 20.0,
                "wall_thickness": 1.2,
                "profile": TREE,
            }
        ],
    )

    assert consistency_issue("karácsony fa formájú kinyomó", produced) is None


def test_press_without_a_wall_is_flagged():
    produced = spec(
        [{"type": "extrude", "role": "add", "height": 25.0, "profile": TREE}],
    )

    issue = consistency_issue("süti kinyomó", produced)

    assert issue is not None
    assert "wall_thickness" in issue


def test_requested_hole_without_a_subtraction_is_flagged():
    produced = spec([{"type": "box", "role": "add", "width": 40.0, "depth": 30.0, "height": 5.0}])

    issue = consistency_issue("40x30 mm lap 4 mm lyukkal", produced)

    assert issue is not None
    assert "hole" in issue


def test_a_subtraction_satisfies_the_hole_rule():
    produced = spec(
        [
            {"type": "box", "role": "add", "width": 40.0, "depth": 30.0, "height": 5.0},
            {"type": "cylinder", "role": "subtract", "diameter": 4.0, "height": 10.0},
        ]
    )

    assert consistency_issue("40x30 mm lap 4 mm lyukkal", produced) is None


def test_implausible_size_for_a_wearable_item_is_flagged():
    produced = spec(
        [
            {
                "type": "extrude",
                "role": "add",
                "height": 400.0,
                "wall_thickness": 1.2,
                "profile": TREE,
            }
        ],
    )

    issue = consistency_issue("fülbevalóhoz készült karácsonyfa kinyomó", produced)

    assert issue is not None
    assert "wearable" in issue


def test_a_wearable_sized_part_is_accepted():
    produced = spec(
        [
            {
                "type": "extrude",
                "role": "add",
                "height": 15.0,
                "wall_thickness": 1.2,
                "profile": TREE,
            }
        ],
        dimensions={"width": 60.0, "height": 90.0, "thickness": 15.0},
    )

    assert consistency_issue("fülbevaló készítéséhez fa alakú kinyomó", produced) is None
    assert SMALL_ITEM_MAX_MM > 90


def test_a_consistent_bracket_request_is_untouched():
    """A matching spec must not be flagged: the checks may not fire on everything."""
    produced = spec(
        [
            {"type": "box", "role": "add", "width": 40.0, "depth": 30.0, "height": 5.0},
            {"type": "cylinder", "role": "subtract", "diameter": 4.0, "height": 10.0},
        ],
        object="wall_bracket",
    )

    assert consistency_issue("falkonzol 40x30 mm, 4 mm lyuk", produced) is None


def test_missing_or_empty_specification_is_not_an_exception():
    assert consistency_issue("valami", None) is None
    assert consistency_issue("", {}) is None
