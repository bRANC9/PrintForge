"""Tests for the geometric silhouette check.

The point of the module is that a point count cannot tell a tree from a slab.
These tests therefore assert *which* rule fired for each bad shape, not merely
that some finding appeared -- a test that passes for the wrong reason is worse
than no test, because it certifies a check that is not running.
"""

from __future__ import annotations

import math

import pytest

from agents.graph.silhouette import (
    APEX_TOP_WIDTH_RATIO,
    MIN_REFLEX_VERTICES,
    check_silhouette,
)

#: A three-tier conifer: the shape the real cookie-cutter products sell.
CONIFER = [
    (0, 90),
    (14, 66),
    (7, 64),
    (24, 44),
    (14, 42),
    (34, 22),
    (22, 20),
    (40, 0),
    (-40, 0),
    (-22, 20),
    (-34, 22),
    (-14, 42),
    (-24, 44),
    (-7, 64),
    (-14, 66),
]

#: The best a model actually produced in the 13-model round: a five-point
#: tapered slab. It is convex at the crown and full width at the top.
SLAB = [(-35, 90), (35, 90), (34.7, 27), (-34.7, 27), (-35, 0)]

#: A 14B model's answer: a three-point triangle.
TRIANGLE = [(-40, 0), (40, 0), (0, 90)]

RECTANGLE = [(-40, 90), (40, 90), (40, 0), (-40, 0)]

#: Self-intersecting: renders as garbage through CGAL.
BOWTIE = [(-40, 90), (40, 0), (40, 90), (-40, 0)]

CIRCLE = [
    (40 * math.cos(2 * math.pi * k / 16), 40 * math.sin(2 * math.pi * k / 16)) for k in range(16)
]

HOURGLASS = [
    (0, 90),
    (30, 60),
    (12, 45),
    (20, 30),
    (8, 0),
    (-8, 0),
    (-20, 30),
    (-12, 45),
    (-30, 60),
]


def _right_edge(tiers, height, half_base, crown_half):
    """Bottom to top: the base, then one out/in notch per tier, then the crown.

    Both sides are monotone in y and the left stays left of the right, so the
    resulting polygon is simple by construction -- which is what makes the
    generated profiles usable as threshold fixtures at all.
    """
    points = [(half_base, 0.0)]
    for i in range(tiers):
        y = height * (i + 1) / (tiers + 1)
        # Linear taper, which is what a conifer actually is. An exponential one
        # is *wider* at the top than a real tree, and the apex rule then -- quite
        # correctly -- refuses to call it a tree. A fixture that lies about the
        # shape under test is worse than no fixture.
        width = half_base * (1 - y / height)
        points.append((width, y - height * 0.02))  # outward to the branch tip
        points.append((width * 0.6, y))  # inward: the notch
    points.append((crown_half, height))
    return points


def _outline(tiers, height=90.0, half_base=40.0, crown_half=0.0):
    """A symmetric conifer whose notch count and crown width are known.

    Each tier contributes one inward vertex per side, so the reflex count is
    ``2 * tiers`` -- which is what lets the threshold test straddle the floor
    without hand-counting corners.
    """
    right = _right_edge(tiers, height, half_base, crown_half)
    return right + [(0.0, height)] + [(-x, y) for (x, y) in reversed(right)]


# --------------------------------------------------------------------------
# A correct outline passes. A check that rejects these is worse than nothing.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "points"),
    [
        ("the 3-tier corpus conifer", CONIFER),
        ("a 2-tier small one", _outline(2, height=45.0, half_base=22.0)),
        ("a 5-tier one", _outline(5)),
        ("a 6-tier one", _outline(6, height=120.0, half_base=50.0)),
        ("a wide, blunt crown", _outline(3, height=60.0, half_base=55.0)),
    ],
)
def test_a_real_conifer_passes(label, points):
    assert check_silhouette(points) == [], f"{label} was rejected: {check_silhouette(points)}"


def test_an_asymmetric_hand_drawn_tree_passes():
    """A real outline is lopsided; a symmetry requirement would reject it.

    This shape also has a base narrower than its middle, because one branch
    sticks out. A "every band is at least as wide as the one above" rule was
    written and dropped for exactly this reason.
    """
    lopsided = [
        (0, 90),
        (14, 66),
        (7, 64),
        (24, 44),
        (14, 42),
        (34, 22),
        (22, 20),
        (40, 0),
        (-36, 0),
        (-20, 20),
        (-32, 22),
        (-12, 42),
        (-22, 44),
        (-6, 64),
        (-13, 66),
    ]
    assert check_silhouette(lopsided) == []


def test_a_star_passes_and_that_is_correct():
    """A star is concave and tapers to a point, so it satisfies these rules.

    What distinguishes a tree from a star is the branch notches' *count* and
    arrangement, not convexity -- and a rule that separated them would reject
    every correct star. The module says so in its docstring.
    """
    star = []
    for k in range(10):
        angle = math.pi / 2 + k * math.pi / 5
        radius = 40 if k % 2 == 0 else 16
        star.append((radius * math.cos(angle), radius * math.sin(angle)))
    assert check_silhouette(star) == []


def test_a_closed_polygon_repeating_its_first_point_passes():
    """A model routinely repeats the first point to close the outline.

    Counting that as a vertex would inflate the reflex count, so the closing
    point is dropped before any measurement.
    """
    closed = [*CONIFER, CONIFER[0]]
    assert check_silhouette(closed) == []


def test_a_degenerate_input_is_silent_rather_than_crashy():
    """Too few points, or no height at all, yields no finding.

    There is no shape to misjudge, so inventing a complaint would be noise: the
    existing point-count and rectangle rules already own the "too few points"
    case in the request-aware layer.
    """
    assert check_silhouette([]) == []
    assert check_silhouette([(1.0, 1.0)]) == []
    assert check_silhouette([(1.0, 1.0), (2.0, 2.0)]) == []


# --------------------------------------------------------------------------
# Each bad shape is caught, and the *specific* rule is named.
# --------------------------------------------------------------------------


def test_the_measured_slab_is_caught_by_the_apex_rule():
    """The exact failure from the 13-model round.

    The slab clears the six-point floor and is not a rectangle, so neither
    existing rule touched it. The apex measurement is what catches it: its top
    band is 100% of its own maximum width, because it is a trapezoid.
    """
    issues = check_silhouette(SLAB)
    assert issues
    assert any("as wide at the top" in issue for issue in issues), issues
    assert any("inward corner" in issue for issue in issues), issues


def test_a_rectangle_is_caught_by_both_notches_and_apex():
    issues = check_silhouette(RECTANGLE)
    assert any("inward corner" in issue for issue in issues), issues
    assert any("as wide at the top" in issue for issue in issues), issues


def test_a_triangle_is_caught_by_the_notch_rule():
    issues = check_silhouette(TRIANGLE)
    assert any("inward corner" in issue for issue in issues), issues


def test_a_circle_is_caught_and_names_the_notch_rule_first():
    issues = check_silhouette(CIRCLE)
    assert issues
    assert "inward corner" in issues[0], issues
    assert any("as wide at the top" in issue for issue in issues), issues


def test_an_hourglass_is_caught_even_though_it_has_notches():
    """It has two reflex vertices, below the floor, so the notch rule still fires."""
    issues = check_silhouette(HOURGLASS)
    assert any("inward corner" in issue for issue in issues), issues


def test_a_self_intersecting_outline_is_caught_first():
    """It is a rendering bug before it is a wrong shape, so it is reported first."""
    issues = check_silhouette(BOWTIE)
    assert "crosses itself" in issues[0], issues


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------


def test_the_notch_floor_straddles_correctly():
    """One tier below the floor is flagged; at the floor it is accepted.

    The generator's reflex count is ``2 * tiers``, so ``tiers = 2`` sits exactly
    on the floor of 4 and ``tiers = 1`` sits one notch-pair below it. The point
    is the straddle, not a hand-counted corner total: a fixture built by hand is
    exactly how a test starts passing for the wrong reason.
    """
    assert check_silhouette(_outline(MIN_REFLEX_VERTICES // 2)) == []

    below = check_silhouette(_outline(MIN_REFLEX_VERTICES // 2 - 1))
    assert any("inward corner" in issue for issue in below), below


def test_the_apex_ratio_straddles_correctly():
    """A narrow crown passes, a slab-wide crown is flagged.

    ``_outline`` takes the crown half-width directly, so the ratio is known:
    10 of 40 is 25% (inside), 34 of 40 is 85% (outside the 40% allowance).
    """
    assert check_silhouette(_outline(2, crown_half=10.0)) == []

    flagged = check_silhouette(_outline(2, crown_half=34.0))
    assert any("as wide at the top" in issue for issue in flagged), flagged


def test_the_apex_allowance_is_where_it_is_documented():
    """The ratio the message quotes is the ratio the code applies.

    A threshold nobody checks is a threshold that drifts, so assert the number
    appears in the finding it governs.
    """
    flagged = check_silhouette(_outline(2, crown_half=34.0))
    apex = next(issue for issue in flagged if "as wide at the top" in issue)
    assert f"{APEX_TOP_WIDTH_RATIO:.0%}" in apex
    assert "85%" in apex


def test_the_purity_guarantee_holds():
    """No Django, no trimesh, no numpy, no network.

    The module has to be importable and testable on its own, the way
    ``designs/cad/dimensions.py`` is: a guard runs before Django is configured.
    """
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.split(".")[0] in {"django", "trimesh", "numpy"}:
            raise ImportError(f"{name} is not available")
        return real_import(name, *args, **kwargs)

    import importlib

    module = importlib.import_module("agents.graph.silhouette")
    importlib.reload(module)
    builtins.__import__ = blocked
    try:
        assert module.check_silhouette(SLAB) == [issue for issue in module.check_silhouette(SLAB)]
        assert module.check_silhouette(SLAB)
    finally:
        builtins.__import__ = real_import


def test_the_check_is_deterministic():
    """Same input, same output, every time -- the retry depends on it."""
    first = check_silhouette(SLAB)
    for _ in range(5):
        assert check_silhouette(SLAB) == first
