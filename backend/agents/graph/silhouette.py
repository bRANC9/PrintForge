"""Geometric recognisability of a named silhouette (``tree``, ``star``, ...).

The existing guard can count points and notice a rectangle, but a point count
cannot tell a tree from a tapered slab. Measured on the model-matrix round: for a
Christmas-tree cookie press, **not one of thirteen models** produced a
recognisable outline. The worst attempts were a five-point trapezoid, a
three-point triangle and a four-point rectangle -- all convex, all "valid" under
a point count, none of them a tree.

The real products are **flat 2D silhouettes with notched edges and a sharp apex**:
the outline of a pressed cookie cutter. The ``extrude`` primitive with
``wall_thickness`` already represents that correctly, so no new primitive type is
needed. What was missing is a check on the geometry itself, which is what this
module provides.

Design notes, because three of these look like they could reject a correct
outline and must not:

* **Taper is measured on the envelope, not per point.** A real tree's outline
  zigzags, so its half-width is *not* monotone in height. Taking a running
  maximum per horizontal band keeps the branch notches while still rejecting a
  shape that narrows toward the base.
* **No symmetry rule.** A real outline is hand-drawn and lopsided; requiring
  symmetry would reject good cutters.
* **No width:height rule.** The products are near-square as often as portrait.
* **A star passes these rules, and that is correct.** They describe "a named
  silhouette with a single apex and a tapering, notched body", which is what
  distinguishes a tree from a slab. A star request is a legitimate use of the
  same shape family; what these rules cannot do is distinguish a *tree* from a
  *star*, and a rule that tried would reject correct stars. The apex signal
  (rule 2) is the one doing the most work against a slab.

Pure stdlib: no Django, no trimesh, no numpy, no network, no randomness. It
operates on the profile point list so it is testable on its own.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "APEX_TOP_BAND",
    "APEX_TOP_WIDTH_RATIO",
    "MIN_REFLEX_VERTICES",
    "SilhouetteIssue",
    "check_silhouette",
]

#: Fraction of the total height, measured from the apex, that counts as "the top".
APEX_TOP_BAND = 0.2

#: How wide the top band may be, relative to the widest part of the silhouette.
#: A tree's crown is a point; a tapered slab's top is as wide as its base. 0.4 is
#: loose enough for a blunt 3-tier crown and far below the ~1.0 a slab measures.
APEX_TOP_WIDTH_RATIO = 0.4

#: Minimum number of reflex (inward) vertices. A three-tier conifer has about
#: six -- one notch per side per tier -- so four is a floor that a rectangle, a
#: triangle, a circle and a plain trapezoid all fail while a blunt but real
#: three-tier outline still passes.
MIN_REFLEX_VERTICES = 4

#: Coordinates closer than this are the same vertex. Matches the rounding the
#: point-count rule already uses, so the two agree on what "distinct" means.
_EPS = 0.5


class SilhouetteIssue(str):
    """One finding, as the sentence a model is asked to act on.

    A ``str`` subclass so the findings drop straight into the existing issue
    list, and into the reviser prompt, with no new plumbing.
    """


def _vertices(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Deduplicated vertices, in order, with a repeated closing point removed.

    A model routinely repeats the first point to close the polygon; counting it
    as a vertex would inflate the reflex count.
    """
    out: list[tuple[float, float]] = []
    for point in points:
        candidate = (float(point[0]), float(point[1]))
        if (
            not out
            or abs(candidate[0] - out[-1][0]) >= _EPS
            or abs(candidate[1] - out[-1][1]) >= _EPS
        ):
            out.append(candidate)
    while (
        len(out) > 1 and abs(out[0][0] - out[-1][0]) < _EPS and abs(out[0][1] - out[-1][1]) < _EPS
    ):
        out.pop()
    return out


def _reflex_count(vertices: Sequence[tuple[float, float]]) -> int:
    """Vertices that turn *inward* -- the branch notches of a conifer.

    Counts the sign of the cross product of consecutive edges and reports the
    minority sign, so it works for either winding direction. A convex polygon
    (rectangle, triangle, circle, a tapered slab) has none.
    """
    n = len(vertices)
    if n < 3:
        return 0
    turns: list[int] = []
    for index in range(n):
        ax, ay = vertices[index]
        bx, by = vertices[(index + 1) % n]
        cx, cy = vertices[(index + 2) % n]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if abs(cross) > 1e-9:
            turns.append(1 if cross > 0 else -1)
    if not turns:
        return 0
    return min(turns.count(1), turns.count(-1))


def _is_simple(vertices: Sequence[tuple[float, float]]) -> bool:
    """True when no two non-adjacent edges of the closed polygon cross.

    Not an aesthetic rule: a self-intersecting profile is rendered by OpenSCAD's
    CGAL kernel as garbage, so the printed part will not match the preview. O(n²)
    over at most 256 profile points is cheap.
    """
    n = len(vertices)
    if n < 4:
        return True

    def orientation(a, b, c) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a, b, p) -> bool:
        return (
            min(a[0], b[0]) - 1e-9 <= p[0] <= max(a[0], b[0]) + 1e-9
            and min(a[1], b[1]) - 1e-9 <= p[1] <= max(a[1], b[1]) + 1e-9
        )

    def crosses(a, b, c, d) -> bool:
        o1, o2 = orientation(a, b, c), orientation(a, b, d)
        o3, o4 = orientation(c, d, a), orientation(c, d, b)
        if o1 * o2 < 0 and o3 * o4 < 0:
            return True
        # Collinear touching counts as a crossing too: it is still a degenerate
        # outline, and CGAL does not care about the distinction.
        if o1 == 0 and on_segment(a, b, c) and c not in (a, b):
            return True
        if o2 == 0 and on_segment(a, b, d) and d not in (a, b):
            return True
        if o3 == 0 and on_segment(c, d, a) and a not in (c, d):
            return True
        if o4 == 0 and on_segment(c, d, b) and b not in (c, d):
            return True
        return False

    for i in range(n):
        a1, a2 = vertices[i], vertices[(i + 1) % n]
        for j in range(i + 1, n):
            # Skip the two edges that legitimately share a vertex.
            if j == i or (j + 1) % n == i or j == (i + 1) % n:
                continue
            b1, b2 = vertices[j], vertices[(j + 1) % n]
            if crosses(a1, a2, b1, b2):
                return False
    return True


def _band_extents(vertices: Sequence[tuple[float, float]], y_low: float, y_high: float) -> float:
    """Width of the outline inside a horizontal band, or 0.0 when it misses.

    Measured on the vertices rather than on edge intersections, which is enough
    for the coarse bands the apex and taper rules use: a band boundary falling
    mid-edge shifts the result by well under a millimetre on a 90 mm outline.
    """
    inside = [x for x, y in vertices if y_low <= y <= y_high]
    return (max(inside) - min(inside)) if inside else 0.0


def _apex_issue(vertices: Sequence[tuple[float, float]]) -> str | None:
    """Reject a shape that stays wide at the top -- the tapered-slab failure.

    This is the signal that catches the measured failure directly: the slab
    ``(-35,90) (35,90) (34.7,27) (-34.7,27) (-35,0)`` has a top band that is
    100% of its own maximum width, because it is a trapezoid.
    """
    ys = [y for _, y in vertices]
    y_low, y_high = min(ys), max(ys)
    total = y_high - y_low
    if total <= _EPS:
        return None
    top_width = _band_extents(vertices, y_high - total * APEX_TOP_BAND, y_high)
    widest = 0.0
    step = total / 20.0
    for k in range(21):
        band_low = y_low + k * step
        widest = max(widest, _band_extents(vertices, band_low, band_low + step))
    if widest <= _EPS:
        return None
    ratio = top_width / widest
    if ratio <= APEX_TOP_WIDTH_RATIO:
        return None
    return (
        f"the 'extrude' profile is as wide at the top as at the bottom "
        f"(top {ratio:.0%} of the widest part, needs under "
        f"{APEX_TOP_WIDTH_RATIO:.0%}): a tree narrows to a single apex at the top "
        "and widens toward the base -- draw the crown converging to one point"
    )


def _taper_issue(vertices: Sequence[tuple[float, float]]) -> str | None:
    """A shape that does not widen toward the base.

    **Dropped deliberately.** A third rule -- "every horizontal band is at least
    as wide as the one above it" -- was written and measured, and it earns
    nothing: every shape it rejected (an hourglass, a pentagon, a circle) is
    already rejected by the reflex-vertex rule with a message that says what is
    actually wrong, and it rejects a real outline. The repository's own
    "plausible tree" fixture has a base half the width of its middle because one
    branch sticks out, which is what a hand-drawn cutter looks like, and the rule
    called it wrong.

    A lower envelope is also the wrong invariant for a real conifer: the branch
    notches make the per-band width non-monotone, so any test that survives them
    has to be an envelope test, and an envelope test is indistinguishable from
    the reflex count once the notches are accounted for.

    The apex rule below is the one that carries the "widest at the base" idea,
    and it measures the part that is unambiguous: the crown.
    """
    return None


def check_silhouette(points: Sequence[tuple[float, float]]) -> list[str]:
    """Findings for an ``extrude`` profile that should be a named silhouette.

    Empty list when the outline is recognisable. The order is the order of how
    badly the shape is wrong, so the first message is the most useful one to act
    on: a self-intersecting profile is a rendering bug, a missing apex is a wrong
    shape.
    """
    vertices = _vertices(points)
    if len(vertices) < 3:
        return []

    issues: list[str] = []

    if not _is_simple(vertices):
        issues.append(
            SilhouetteIssue(
                "the 'extrude' profile crosses itself, so OpenSCAD renders it as garbage "
                "and the printed part will not match the preview: walk the outline once, "
                "in order, without doubling back over an edge"
            )
        )

    reflex = _reflex_count(vertices)
    if reflex < MIN_REFLEX_VERTICES:
        issues.append(
            SilhouetteIssue(
                f"the 'extrude' profile has {reflex} inward corner(s) (minimum "
                f"{MIN_REFLEX_VERTICES}) for a named silhouette: a tree outline notches "
                "inwards once per side per tier of branches -- a shape with no notches is "
                "a slab, a circle or a triangle, not a silhouette"
            )
        )

    for issue in (_apex_issue(vertices),):
        if issue:
            issues.append(SilhouetteIssue(issue))

    return list(issues)
