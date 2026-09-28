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

Every list is split into **two tiers that are matched differently** (see
`_mentions`), because a bare substring test is a false-positive machine. The
measured case: the *correct* answer to "50 mm-es PVC csőre menő adapter, másik
oldalán 1/4 collos kompresszor csatlakozó" is ``cylinder`` + ``cylinder``, and
``phi4:14b`` produced exactly that -- but ``press`` matched the bare substring
inside **kom**presszor, so the guard rejected a right answer and burned a retry.
The same class of bug: ``sajtó`` in **garáz**sajtó, ``vágó`` in **vágó**lap.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from .silhouette import check_silhouette

__all__ = [
    "HOLE_PREFIX_WORDS",
    "HOLE_WHOLE_WORDS",
    "HOLE_WORDS",
    "PREFIX_TIER",
    "PRESS_PREFIX_WORDS",
    "PRESS_WHOLE_WORDS",
    "PRESS_WORDS",
    "SILHOUETTE_PREFIX_WORDS",
    "SILHOUETTE_WHOLE_WORDS",
    "SILHOUETTE_WORDS",
    "SMALL_ITEM_MAX_MM",
    "SMALL_ITEM_PREFIX_WORDS",
    "SMALL_ITEM_WHOLE_WORDS",
    "SMALL_ITEM_WORDS",
    "consistency_issue",
    "consistency_issues",
]

#: The two tiers
#: ----------------
#:
#: * ``*_WHOLE_WORDS`` -- matched with a boundary on **both** sides. Every
#:   English entry is here: an English word inside a Hungarian compound
#:   (``press`` in *kompresszor*, ``pin`` in *pipe*) is never a shape request.
#: * ``*_PREFIX_WORDS`` -- matched with a leading boundary only, for the few
#:   Hungarian stems whose case/plural ending is welded onto them
#:   (``kinyomó`` -> "kinyomómat", ``furat`` -> "furatokat").
#:
#: An entry may only join the prefix tier when **nothing common starts with it
#: that is not the thing itself** -- otherwise the compound is a false positive
#: again (``vágó``lap, ``csap``ágy, ``szög``skála). No entry may join it when it
#: is a word *ending* (``sajtó`` in *garázsajtó*): a leading boundary already
#: rules those out, but a whole-word entry is the stricter, clearer rule.
#:
#: The trade-off is deliberate and asymmetric: a **missed** keyword only leaves
#: the guard quiet (a box instead of an extrude, a human notices), while a
#: **wrong** keyword rejects a correct part and burns the bounded retry budget.
#: So where a plural/case form and a compound collision compete, precision wins
#: and the inflected form is documented below as an accepted miss.

#: A named outline (not a rectangle). Such a request needs a real polygon.
#: Minden szórokon a toldalék-utáni toldat (fakonzol/falkonzol) hamis pozitívet
#: adhat, ezért a hosszabb, egyértelműbb alakokat is felsoroljuk; a `fa` csak
#: pontosan szóként illeszkedik (lásd `_mentions`).
#:
#: No silhouette keyword needs the prefix tier: each one is a closed compound
#: whose Hungarian use is `"<szó> alakú"` with a space, which the whole-word
#: boundary already accepts.
SILHOUETTE_WHOLE_WORDS = (
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
    # English: an English word inside a Hungarian compound is never a shape.
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
SILHOUETTE_PREFIX_WORDS: tuple[str, ...] = ()

#: A press / cutter / stamp: a thin extruded wall, never a solid block.
PRESS_WHOLE_WORDS = (
    # `sajtó` a toldat: "garázsajtó" (garage door) is a door, not a press.
    "sajtó",
    "matric",
    # The two stems welded into one word: without it "sajtómatrica" (the actual
    # Hungarian word for a cookie-press die) loses both whole-word entries.
    "sajtómatrica",
    "sütiforma",
    "kiszúró",
    # `vágó` toldatét kezdi: "vágólap" (cutting mat) is a mat, not a cutter.
    "vágó",
    "bélyeg",
    "cutter",
    "press",
    "stamp",
    "cookie",
    # The one-word spellings the boundary would otherwise lose. The slug
    # spelling ("cookie_cutter", see `_mentions`) needs no entry: the
    # separator is normalised to a space, so "cookie" already matches.
    "cookiecutter",
    "matrix",
    "emboss",
    "embossed",
    "embossing",
)
#: `kinyomó`/`kinyomo`: the request grammar welds the possessive or the object
#: suffix onto the stem ("kinyomómat", "kinyomot", "süti kinyomóhoz"), which is
#: the canonical phrasing -- and nothing but a press starts with the stem.
PRESS_PREFIX_WORDS = (
    "kinyomó",
    "kinyomo",
)

#: Hole / fastener / hanging features that need a subtraction.
HOLE_WHOLE_WORDS = (
    "lyukas",
    # `csap` a toldatét kezdi: "csapágy" (bearing) is a bearing, not a pin.
    "csap",
    # `szög` a toldatét kezdi: "szöglet" (angle bracket), "szögskála" (angle
    # scale), "szöghossz" (cosine) are common workshop words, not nails.
    "szög",
    # `tüske` a toldatét kezdi: "tüskés" (a spiky texture) is not a pin.
    "tüske",
    # The inflected forms the boundary match drops. Left to the whole-word tier
    # each of these is a *miss* -- the request is normal workshop language and
    # the guard simply stays quiet -- and a miss is far cheaper than the
    # collision it was bought against. Each one is also a whole word: none is a
    # prefix of anything, which is what the boundary actually buys here.
    "szögek",
    "szögekkel",
    "csapok",
    "csappal",
    "tüskék",
    "lyukak",
    "hole",
    "holes",
    "cutout",
    "cutouts",
    "screw",
    "screws",
    "pin",
    "dowel",
    "dowels",
    "hook",
    "hooks",
)
#: Stems whose ending is welded onto the word, and which no common workshop word
#: *starts* with other than the feature itself:
#:
#: * `lyuk` -> "lyukkal" (4 mm **lyukkal**), "lyukak"; the compounds that start
#:   with it -- lyukas/lyukasztó/lyukfúró -- are holes as well.
#: * `furat` -> "furatok", "furatokat" (required); furatmérő/furatlemez are a
#:   hole gauge and a hole plate.
#: * `csavar` -> "csavarok", "csavarokkal"; csavaranya (nut) is still a screw.
#: * `akasztó` -> "ruhatartó akasztó", "akasztóval", "falra akasztó"; the
#:   compounds that start with it (akasztóhorog, akasztókerék) are hooks.
HOLE_PREFIX_WORDS = (
    "lyuk",
    "furat",
    "csavar",
    "akasztó",
)

#: Small wearable items; their parts are never a few hundred millimetres.
SMALL_ITEM_WHOLE_WORDS = (
    "fülbevalók",
    "ékszer",
    # `gyűrű` a toldatét kezdi: "gyűrűs", "gyűrűtartó" (ring holder) are a holder.
    "gyűrű",
    # `nyaklánc`/`karkötő` a toldatét kezdi: "nyaklánctartó", "karkötőtartó"
    # (display stands) are routinely 250 mm and over.
    "nyaklánc",
    "karkötő",
    "fülcse",
    "earring",
    "earrings",
    "jewellery",
    "jewelry",
    "pendant",
    "stud",
)
#: `fülbevaló`: the canonical request is "fülbevalóhoz" / "fülbevalót" (the case
#: ending is welded onto the stem), and nothing but an earring starts with it.
SMALL_ITEM_PREFIX_WORDS = ("fülbevaló",)

#: The four guard keyword sets stay importable under their original names as
#: the **union** of the two tiers, so an old caller or test that iterates
#: `SILHOUETTE_WORDS` still sees every keyword exactly as before.
SILHOUETTE_WORDS = SILHOUETTE_WHOLE_WORDS + SILHOUETTE_PREFIX_WORDS
PRESS_WORDS = PRESS_WHOLE_WORDS + PRESS_PREFIX_WORDS
HOLE_WORDS = HOLE_WHOLE_WORDS + HOLE_PREFIX_WORDS
SMALL_ITEM_WORDS = SMALL_ITEM_WHOLE_WORDS + SMALL_ITEM_PREFIX_WORDS

#: Every prefix-tier keyword, which is the only thing `_mentions` needs to know
#: about the tier split. It is **derived** from the four constants above, so an
#: entry is declared exactly once and the old two-argument `_mentions(haystack,
#: words)` call site cannot silently degrade to whole-word-only matching.
PREFIX_TIER = frozenset(
    SILHOUETTE_PREFIX_WORDS + PRESS_PREFIX_WORDS + HOLE_PREFIX_WORDS + SMALL_ITEM_PREFIX_WORDS
)

#: Anything bigger than this is not an earring-scale part.
SMALL_ITEM_MAX_MM = 250.0

#: Operation kinds that remove material.
_SUBTRACTIVE_KINDS = frozenset({"hole", "pocket", "cut", "slot"})


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


#: A slug separator is a word character for `\b`, so "cookie_cutter" and
#: "heart-shaped" would miss a keyword that the same text spells with a space.
#: Normalising them to spaces makes the boundary mean what it looks like.
_SEPARATORS = re.compile(r"[-_]+")


@lru_cache(maxsize=256)
def _keyword_pattern(word: str, prefix: bool) -> re.Pattern[str]:
    """Compile one keyword matcher: ``\\bword\\b``, or ``\\bword`` for a prefix."""
    body = re.escape(word)
    return re.compile(rf"\b{body}" if prefix else rf"\b{body}\b")


def _mentions(haystack: str, words: Sequence[str]) -> bool:
    """True when any keyword of ``words`` appears in ``haystack``.

    Two tiers, because one rule cannot be right for both languages:

    * a keyword in `PREFIX_TIER` is matched with a **leading** boundary only --
      Hungarian welds the case and plural ending onto the stem ("kinyomómat",
      "furatokat", "csavarok"), so a trailing boundary would drop the most
      common phrasing of the very request the guard exists for;
    * every other keyword is matched as a **whole word**. That is what keeps
      ``fa`` out of *falkonzol*, and -- the measured bug -- ``press`` out of
      *kompresszor*, ``sajtó`` out of *garázsajtó*, ``vágó`` out of *vágólap*,
      ``szög`` out of *szögskála*, ``csap`` out of *csapágy*.

    A keyword is a whole word, never a bare substring: the substring form is
    what let ``press`` reject a correct ``cylinder``+``cylinder`` answer for a
    PVC adapter, and it is not deterministic -- whether it fires depends only
    on whether the model happened to emit an ``extrude``.
    """
    text = _SEPARATORS.sub(" ", haystack)
    return any(_keyword_pattern(word, word in PREFIX_TIER).search(text) for word in words)


#: A named silhouette needs a real outline: a rectangle, a triangle or a rhombus
#: is never a tree/star/heart. Six distinct points is the floor for a readable
#: 2D outline; a proper tree wants more.
_SILHOUETTE_MIN_POINTS = 6


def _round_point(point: tuple[float, float]) -> tuple[float, float]:
    """A profile point rounded for de-duplication (a closing point repeats)."""
    return (round(point[0], 6), round(point[1], 6))


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

    Every keyword test is a two-tier word-boundary match (see `_mentions`), so
    a Hungarian compound ("kompresszor", "garázsajtó", "vágólap", "falkonzol")
    cannot trip a guard on a part that is none of those things.
    """
    spec: Mapping[str, Any] = specification if isinstance(specification, Mapping) else {}
    request = _text(prompt)
    haystack = f"{request} {_text(spec.get('object'))}"
    extrudes = [item for item in _primitives(spec) if str(item.get("type") or "") == "extrude"]
    issues: list[str] = []

    wants_shape = _mentions(haystack, SILHOUETTE_WORDS) or _mentions(haystack, PRESS_WORDS)

    if wants_shape and not extrudes:
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
            points = _profile_points(primitive)
            if _is_axis_aligned_rectangle(points):
                issues.append(
                    "the request asks for a shaped outline (tree/star/heart/... silhouette), "
                    "but the 'extrude' profile is a plain rectangle: replace it with the real "
                    "outline as a polygon of at least 6 ordered {'x','y'} points"
                )
            elif len({_round_point(point) for point in points}) < _SILHOUETTE_MIN_POINTS:
                # A diamond/rhombus or a triangle is not a rectangle, yet it is still
                # not the requested silhouette: a tree, star or heart needs a real
                # outline. Measured: a 14B model answered a tree-cutter request
                # with a 4-point rhombus, which the rectangle rule alone accepted.
                issues.append(
                    f"the request asks for a shaped outline, but the 'extrude' profile has "
                    f"only {len({_round_point(point) for point in points})} distinct points "
                    f"(minimum {_SILHOUETTE_MIN_POINTS}): draw the actual silhouette as a "
                    "polygon (a tree outline needs at least 6 ordered points, more for branches)"
                )
            # The point count is a floor, not evidence of a shape. Measured: for a
            # tree-cookie-cutter request none of thirteen models drew a
            # recognisable outline -- the best of them a five-point trapezoid,
            # which clears six points nowhere but is certainly not a tree. The
            # geometry check is what actually tells the two apart.
            else:
                issues.extend(check_silhouette(points))

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
