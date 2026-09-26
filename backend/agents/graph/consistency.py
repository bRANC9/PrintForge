"""Deterministic request <-> specification consistency checks.

The vision self-check compares a *rendered preview* with the request, but small
vision models (``llava:7b``) rubber-stamp whatever they are shown: a 100x200x400
solid block was accepted as "Christmas tree press". These checks need no model at
all -- they compare the **structured specification** with the request text and
flag the geometric impossibilities a weak reviewer misses.

They only ever *flag*: a flag feeds the same bounded CAD retry as a vision
mismatch, so a run still finishes ``done`` when the attempts run out. Nothing
here can hard-fail a generation.

Keywords are Hungarian **and** English: the UI and the prompts are Hungarian,
while a model may answer in either language.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "HOLE_WORDS",
    "PRESS_WORDS",
    "SILHOUETTE_WORDS",
    "SMALL_ITEM_MAX_MM",
    "SMALL_ITEM_WORDS",
    "consistency_issue",
    "consistency_issues",
]

#: A named outline (not a rectangle). Such a request needs a real polygon.
#: Minden szórokon a toldalék-utáni toldat (fakonzol/falkonzol) hamis pozitívet
#: adhat, ezért a hosszabb, egyértelműbb alakokat is felsoroljuk; a `fa` csak
#: pontosan szóként illeszkedik (lásd `_mentions`).
SILHOUETTE_WORDS = (
    "karácsonyfa",
    "karácsony fa",
    "fa alakú",
    "faforma",
    "fa alak",
    "csillag",
    "szív",
    "levél",
    "dísz",
    "virág",
    "holdszakért",
    "betű",
    "logó",
    "kirakó",
    "matrica",
    "sziluett",
    "fa",
    "tree",
    "star",
    "heart",
    "leaf",
    "ornament",
    "angel",
    "flower",
    "moon",
    "letter",
    "logo",
    "silhouette",
)

#: A press / cutter / stamp: a thin extruded wall, never a solid block.
PRESS_WORDS = (
    "kinyomó",
    "kinyomo",
    "sajtó",
    "matric",
    "sütiforma",
    "kiszúró",
    "vágó",
    "bélyeg",
    "cutter",
    "press",
    "stamp",
    "cookie",
    "matrix",
    "emboss",
)

#: Hole / fastener / hanging features that need a subtraction.
HOLE_WORDS = (
    "lyuk",
    "furat",
    "lyukas",
    "csavar",
    "csap",
    "szög",
    "tüske",
    "akasztó",
    "hole",
    "cutout",
    "screw",
    "pin",
    "dowel",
    "hook",
)

#: Small wearable items; their parts are never a few hundred millimetres.
SMALL_ITEM_WORDS = (
    "fülbevaló",
    "fülbevalók",
    "ékszer",
    "gyűrű",
    "nyaklánc",
    "karkötő",
    "fülcse",
    "earring",
    "jewellery",
    "jewelry",
    "pendant",
    "stud",
)

#: Anything bigger than this is not an earring-scale part.
SMALL_ITEM_MAX_MM = 250.0

#: Operation kinds that remove material.
_SUBTRACTIVE_KINDS = frozenset({"hole", "pocket", "cut", "slot"})


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _mentions(haystack: str, words: Sequence[str]) -> bool:
    """True when any keyword appears in ``haystack`` as a whole word.

    Word-boundary matching (not plain substring) matters for short Hungarian
    stems: ``fa`` is "tree", but it is also the first two letters of ``fal``
    (wall) and ``falkonzol``. Long keywords (kinyomó, cutter, extrude) stay
    substring-matched so inflected compounds still hit.
    """
    for word in words:
        if len(word) <= 3:
            # Short stems need a boundary on BOTH sides: \bfa\b matches "fa" but
            # not the "fal" inside "falkonzol" (wall bracket).
            if re.search(rf"\b{re.escape(word)}\b", haystack):
                return True
        elif word in haystack:
            return True
    return False


def _profile_points(primitive: Mapping[str, Any]) -> list[tuple[float, float]]:
    """Valid ``(x, y)`` points of an ``extrude`` profile, in order."""
    points: list[tuple[float, float]] = []
    for item in primitive.get("profile") or []:
        if not isinstance(item, Mapping):
            continue
        try:
            points.append((float(item["x"]), float(item["y"])))
        except (KeyError, TypeError, ValueError):
            continue
    return points


def _is_axis_aligned_rectangle(points: Sequence[tuple[float, float]]) -> bool:
    """True when the outline is just a rectangle (4 corners, no shaping).

    Duplicated closing points are ignored, and every point must sit on a corner
    of the bounding box with both axes actually used -- so a triangle, an L or a
    real silhouette returns ``False``.
    """
    unique = {(round(x, 6), round(y, 6)) for x, y in points}
    if len(unique) < 3:
        return False
    xs = {x for x, _ in unique}
    ys = {y for _, y in unique}
    if len(xs) != 2 or len(ys) != 2:
        return False
    return len(unique) == 4


def _primitives(spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [item for item in (spec.get("primitives") or []) if isinstance(item, Mapping)]


def _removes_material(spec: Mapping[str, Any]) -> bool:
    """True when the specification cuts something (a primitive role or an op)."""
    for primitive in _primitives(spec):
        if str(primitive.get("role") or "add").strip().lower() == "subtract":
            return True
    for operation in spec.get("operations") or []:
        if isinstance(operation, Mapping):
            if str(operation.get("kind") or "").strip().lower() in _SUBTRACTIVE_KINDS:
                return True
    return False


def _largest_extent_mm(spec: Mapping[str, Any]) -> float:
    """Biggest modelled extent (mm): spec dimensions plus extrude size."""
    dimensions = spec.get("dimensions")
    extents: list[float] = []
    if isinstance(dimensions, Mapping):
        for key in ("width", "height", "thickness"):
            try:
                extents.append(float(dimensions[key]))
            except (KeyError, TypeError, ValueError):
                continue
    for primitive in _primitives(spec):
        if str(primitive.get("type") or "") != "extrude":
            continue
        points = _profile_points(primitive)
        if points:
            extents.append(max(x for x, _ in points) - min(x for x, _ in points))
            extents.append(max(y for _, y in points) - min(y for _, y in points))
        try:
            extents.append(float(primitive.get("height")))
        except (TypeError, ValueError):
            pass
    return max(extents) if extents else 0.0


def _has_wall(primitive: Mapping[str, Any]) -> bool:
    try:
        return float(primitive.get("wall_thickness")) > 0
    except (TypeError, ValueError):
        return False


def consistency_issues(prompt: str, specification: Mapping[str, Any] | None) -> list[str]:
    """Return every concrete request/spec mismatch (empty list when consistent).

    The checks are deliberately few and high-precision: a false positive only
    costs one bounded retry, but a false negative ships a wrong part, so each
    rule keys off an explicit keyword in the request plus a concrete geometric
    fact in the specification. **All** findings are returned (not just the first)
    so the reviser can repair a wrong shape *and* a missing wall in one retry
    instead of one issue per attempt.
    """
    spec: Mapping[str, Any] = specification if isinstance(specification, Mapping) else {}
    request = _text(prompt)
    haystack = f"{request} {_text(spec.get('object'))}"
    extrudes = [item for item in _primitives(spec) if str(item.get("type") or "") == "extrude"]
    issues: list[str] = []

    if _mentions(haystack, SILHOUETTE_WORDS) or _mentions(haystack, PRESS_WORDS):
        if not extrudes:
            kinds = sorted(
                {
                    str(item.get("type") or "?")
                    for item in _primitives(spec)
                    if str(item.get("type") or "")
                }
            )
            issues.append(
                "the request asks for a shaped outline / a thin press, but the specification "
                f"has no 'extrude' primitive at all (only {', '.join(kinds) or 'nothing'}): a "
                "box or cylinder cannot express that shape -- use one 'extrude' whose "
                "'profile' is the real 2D outline"
            )

    if _mentions(haystack, SILHOUETTE_WORDS):
        for primitive in extrudes:
            if _is_axis_aligned_rectangle(_profile_points(primitive)):
                issues.append(
                    "the request asks for a shaped outline (tree/star/heart/... silhouette), "
                    "but the 'extrude' profile is a plain rectangle: replace it with the real "
                    "outline as a polygon of at least 6 ordered {'x','y'} points"
                )

    if _mentions(haystack, PRESS_WORDS):
        for primitive in extrudes:
            if not _has_wall(primitive):
                issues.append(
                    "a press/cutter/stamp must be a thin extruded wall, but the 'extrude' "
                    "primitive has no 'wall_thickness': a solid block is not a stamp"
                )

    if _mentions(haystack, HOLE_WORDS) and not _removes_material(spec):
        issues.append(
            "the request mentions a hole/pin/screw, but the specification removes no material: "
            "add a 'subtract' primitive (cylinder) or a hole/pocket operation"
        )

    if _mentions(haystack, SMALL_ITEM_WORDS):
        largest = _largest_extent_mm(spec)
        if largest > SMALL_ITEM_MAX_MM:
            issues.append(
                f"the request is for a small wearable item, but the specification is "
                f"{largest:.0f} mm across: the part cannot be that size"
            )

    return issues


def consistency_issue(prompt: str, specification: Mapping[str, Any] | None) -> str | None:
    """First request/spec mismatch, or ``None`` when consistent.

    Convenience wrapper over :func:`consistency_issues` for callers that only
    need one finding (the review node collects all of them).
    """
    return next(iter(consistency_issues(prompt, specification)), None)
