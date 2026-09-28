"""Tests for the deterministic request <-> specification checks (no LLM)."""

from __future__ import annotations

import pytest

from agents.graph.consistency import (
    _SEPARATORS,
    HOLE_PREFIX_WORDS,
    HOLE_WHOLE_WORDS,
    HOLE_WORDS,
    PREFIX_TIER,
    PRESS_PREFIX_WORDS,
    PRESS_WHOLE_WORDS,
    PRESS_WORDS,
    SILHOUETTE_PREFIX_WORDS,
    SILHOUETTE_WHOLE_WORDS,
    SILHOUETTE_WORDS,
    SMALL_ITEM_MAX_MM,
    SMALL_ITEM_PREFIX_WORDS,
    SMALL_ITEM_WHOLE_WORDS,
    SMALL_ITEM_WORDS,
    _keyword_pattern,
    _mentions,
    consistency_issue,
    consistency_issues,
)

#: The four guard keyword sets, with the name used in an assertion message.
TIERS = {
    "SILHOUETTE": SILHOUETTE_WORDS,
    "PRESS": PRESS_WORDS,
    "HOLE": HOLE_WORDS,
    "SMALL_ITEM": SMALL_ITEM_WORDS,
}

#: Every English keyword the module carries. The prefix tier must never hold
#: one: an English word inside a Hungarian compound ("press" in *kompresszor*,
#: "pin" in *pipe*) is never a shape request.
ENGLISH_KEYWORDS = frozenset(
    {
        "angel",
        "cookie",
        "cookiecutter",
        "cutter",
        "cutout",
        "cutouts",
        "dowel",
        "dowels",
        "earring",
        "earrings",
        "emboss",
        "embossed",
        "embossing",
        "flower",
        "heart",
        "hole",
        "holes",
        "hook",
        "hooks",
        "jewellery",
        "jewelry",
        "leaf",
        "letter",
        "logo",
        "matrix",
        "moon",
        "ornament",
        "pendant",
        "pin",
        "press",
        "screw",
        "screws",
        "silhouette",
        "stamp",
        "star",
        "stud",
        "tree",
    }
)

#: 48 terms of realistic Hungarian workshop vocabulary, none of which asks for a
#: shaped outline, a thin press, a hole or a wearable-scale part. A bare
#: substring match fired on 15 of these; `press` in **kom**presszor alone
#: rejected a correct `cylinder`+`cylinder` answer for a PVC tube adapter.
WORKSHOP_CORPUS = (
    "kompresszor csatlakozó",
    "kompresszor adapter",
    "kompresszorfúró",
    "pneumatikus csatlakozó",
    "impresszum",
    "expresszió",
    "depresszió",
    "kompresszió",
    "garázsajtó zsanér",
    "sajtólap",
    "vágólap",
    "falkonzol",
    "asztali állvány",
    "kábelkanchor",
    "gépzsíró cső",
    "ventilátor kerék",
    "biciklitartó",
    "kiparógép alkatrész",
    "sárvédő",
    "garázsajtó",
    "autós tartó",
    "lámpatest",
    "bútoralkatrész",
    "támogató struktúra",
    "csőfogas szorító",
    "falra szerelhető tartó",
    "gépzsíró szekrény",
    "szemüvegkeret",
    "szigetelő alátét",
    "gumi szűrő",
    "műanyag dübel",
    "szerszámtartó doboz",
    "betonzsálem",
    "napelem keret",
    "autó lámpa tartó",
    "hegesztő asztal",
    "fúróvezeték",
    "gyorscsatlakozó",
    "mérőműszertartó",
    "állítható támasz",
    "védőlemez",
    "hordozóprofil",
    "gázcső csatlakozó",
    "szerelvényház",
    # The entries that had to move out of the prefix tier, probed directly.
    "szögskála",  # szög (nail) in szögskála (angle scale)
    "csapágy",  # csap (pin) in csapágy (bearing)
    "díszlap",  # dísz in díszlap (decorative panel)
    "vágókarom",  # vágó (cutter) in vágókarom (cutting arm)
)

#: Every real detection the two-tier match must keep. `kinyomómat` and
#: `furatokat` are the reason the prefix tier exists at all.
REAL_REQUESTS = (
    "karácsonyfa",
    "karácsony fa",
    "fa alakú",
    "csillag",
    "szív",
    "levél",
    "virág",
    "holdszakért",
    "dísz",
    "betű",
    "logó",
    "matrica",
    "sziluett",
    "tree",
    "star",
    "heart",
    "leaf",
    "ornament",
    "flower",
    "cookie cutter",
    "sütiforma",
    "kinyomó",
    "sajtó",
    "matric",
    "kiszúró",
    "vágó",
    "bélyeg",
    "cutter",
    "press",
    "stamp",
    "cookie",
    "lyuk",
    "furat",
    "csavar",
    "szög",
    "akasztó",
    "hole",
    "screw",
    "pin",
    "dowel",
    "hook",
    # The prefix tier: the case/plural ending is welded onto the stem.
    "kinyomómat",
    "furatokat",
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

#: The 4-point rhombus a 14B model drew for a Christmas tree.
RHOMBUS = [
    {"x": -75.0, "y": 0.0},
    {"x": 0.0, "y": 15.0},
    {"x": 75.0, "y": 0.0},
    {"x": 0.0, "y": -15.0},
    {"x": -75.0, "y": 0.0},
]


def spec(primitives, **overrides):
    base = {
        "object": "Christmas Tree",
        "dimensions": {"width": 100.0, "height": 200.0, "thickness": 10.0},
        "wall_thickness": 3.0,
    }
    base.update(overrides)
    return {**base, "primitives": primitives}


def tripped(text):
    """Every ``TIER:keyword`` that ``text`` matches, for a readable failure.

    Calling the matcher directly (rather than only the public guard) is the
    point: a keyword that fires without a matching primitive never reaches a
    finding, so a false positive is invisible in ``consistency_issues`` for a
    correct specification -- which is exactly how this bug survived.

    The text goes through the module's own separator normalisation, so the
    second pass agrees with ``_mentions`` on ``tree_cutter`` and ``karácsony-fa``.
    """
    text = _SEPARATORS.sub(" ", text)
    found = []
    for name, words in TIERS.items():
        if not _mentions(text, words):
            continue
        for word in words:
            if _keyword_pattern(word, word in PREFIX_TIER).search(text):
                found.append(f"{name}:{word}")
                break
    return found


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


def test_a_box_instead_of_an_extrude_is_flagged():
    """The live finding: the model answered with a plain box for a tree press.

    The silhouette/press rules must also fire when there is **no** extrude at
    all -- a box or cylinder cannot express a shaped outline.
    """
    produced = spec(
        [{"type": "box", "role": "add", "width": 50.0, "depth": 30.0, "height": 10.0}],
        object="cookie_cutter",
    )

    issue = consistency_issue("karácsony fa formájú gyurma kinyomó", produced)

    assert issue is not None
    assert "extrude" in issue


def test_every_finding_is_returned_so_one_retry_can_fix_them_all():
    """A rectangle profile *and* a missing wall are both reported at once.

    Returning only the first issue would burn one bounded attempt per problem.
    """
    produced = spec(
        [{"type": "extrude", "role": "add", "height": 200.0, "profile": RECTANGLE}],
        object="karácsonyfa",
    )

    issues = consistency_issues("karácsonyfa formájú süti kinyomó", produced)

    assert len(issues) == 2
    assert any("rectangle" in item for item in issues)
    assert any("wall_thickness" in item for item in issues)


def test_a_four_point_rhombus_is_not_a_silhouette():
    """The measured live case: a 14B model drew a 4-point rhombus for a tree.

    The rectangle rule alone accepted it (a rhombus is not axis-aligned), so a
    shaped outline now also needs enough distinct points to be a real outline.
    """
    produced = spec(
        [
            {
                "type": "extrude",
                "role": "add",
                "height": 30.0,
                "wall_thickness": 2.0,
                "profile": [
                    {"x": -75.0, "y": 0.0},
                    {"x": 0.0, "y": 15.0},
                    {"x": 75.0, "y": 0.0},
                    {"x": 0.0, "y": -15.0},
                    {"x": -75.0, "y": 0.0},
                ],
            }
        ],
        object="cookie_cutter",
    )

    issues = consistency_issues("karácsonyfa formájú süti kinyomó", produced)

    assert any("distinct points" in issue for issue in issues)


def test_a_real_tree_outline_passes_the_point_count_rule():
    produced = spec(
        [
            {
                "type": "extrude",
                "role": "add",
                "height": 30.0,
                "wall_thickness": 2.0,
                "profile": [{"x": point["x"], "y": point["y"]} for point in TREE],
            }
        ],
        object="karácsonyfa",
    )

    assert consistency_issue("karácsonyfa formájú süti kinyomó", produced) is None


def test_press_without_a_wall_is_flagged():
    produced = spec(
        [{"type": "extrude", "role": "add", "height": 25.0, "profile": TREE}],
    )

    issue = consistency_issue("süti kinyomó", produced)

    assert issue is not None
    assert "wall_thickness" in issue


def test_requested_hole_without_a_subtraction_is_flagged():
    produced = spec(
        [{"type": "box", "role": "add", "width": 40.0, "depth": 30.0, "height": 5.0}],
        object="wall_bracket",
    )

    issue = consistency_issue("40x30 mm lap 4 mm lyukkal", produced)

    assert issue is not None
    assert "hole" in issue


def test_a_subtraction_satisfies_the_hole_rule():
    produced = spec(
        [
            {"type": "box", "role": "add", "width": 40.0, "depth": 30.0, "height": 5.0},
            {"type": "cylinder", "role": "subtract", "diameter": 4.0, "height": 10.0},
        ],
        object="wall_bracket",
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


# --- the two-tier keyword match ------------------------------------------------
#
# Measured bug: every keyword longer than three characters used to be matched as
# a bare substring, so `press` fired inside *kom*presszor, `sajtó` inside
# *garáz*sajtó and `vágó` inside *vágó*lap. 15 of 48 realistic workshop terms
# tripped a guard they had no business tripping -- and because the guard only
# reports when there is no `extrude`, whether it fired depended on nothing but
# whether the model happened to emit one.


@pytest.mark.parametrize("term", WORKSHOP_CORPUS)
def test_a_plain_workshop_term_trips_no_guard(term):
    assert tripped(term) == []


@pytest.mark.parametrize("term", WORKSHOP_CORPUS)
def test_a_plain_workshop_term_is_never_flagged_on_a_correct_spec(term):
    """The end-to-end half: a bracket-like spec must pass the whole guard."""
    produced = spec(
        [
            {"type": "box", "role": "add", "width": 40.0, "depth": 30.0, "height": 5.0},
            {"type": "cylinder", "role": "subtract", "diameter": 4.0, "height": 10.0},
        ],
        object=term,
        dimensions={"width": 40.0, "height": 30.0, "thickness": 5.0},
    )

    assert consistency_issues(term, produced) == []


@pytest.mark.parametrize("term", REAL_REQUESTS)
def test_a_real_request_still_trips_its_guard(term):
    assert tripped(term) != [], f"the whole-word tier lost a real detection: {term!r}"


def test_the_measured_pvc_adapter_is_accepted():
    """The bug report, end to end.

    `phi4:14b` answered this prompt with `cylinder` + `cylinder` -- the most
    correct answer obtainable for a tube adapter -- and the guard rejected it
    for a reason about an `extrude` the request never asked for, because
    `press` is a substring of *kom*presszor.
    """
    produced = spec(
        [
            {"type": "cylinder", "role": "add", "diameter": 50.0, "height": 40.0},
            {"type": "cylinder", "role": "subtract", "diameter": 46.0, "height": 42.0},
        ],
        object="pvc_adapter",
        dimensions={"width": 50.0, "height": 50.0, "thickness": 40.0},
    )
    prompt = (
        "50 mm-es PVC csőre menő adapter, másik oldalán 1/4 collos kompresszor "
        "csatlakozó. Belső átmérő 50 mm, falvastagság 2 mm, hossz 40 mm."
    )

    assert consistency_issues(prompt, produced) == []


def test_a_hyphenated_or_slugged_keyword_still_matches():
    """A `-` or `_` separator is a boundary, not a word character.

    A model that names the part `cookie_cutter` or answers `heart-shaped` must
    not lose the detection to regex word-character semantics.
    """
    assert tripped("tree_cutter") == ["SILHOUETTE:tree", "PRESS:cutter"]
    assert tripped("heart-shaped ornament") == ["SILHOUETTE:heart"]
    assert tripped("karácsony-fa") == ["SILHOUETTE:karácsony fa"]


def test_the_short_word_boundary_still_holds_for_fal():
    """The original invariant: `fa` is "tree", but not the *fal* of a bracket.

    The old rule special-cased keywords of three characters or fewer. The
    whole-word tier now covers those too, so this must not regress.
    """
    assert tripped("falkonzol 40x30 mm, 4 mm lyuk") == ["HOLE:lyuk"]
    assert tripped("falra szerelhető tartó") == []
    assert tripped("fa") == ["SILHOUETTE:fa"]


def test_the_prefix_tier_catches_the_welded_case_ending():
    """`kinyomó` and `furat` are the reason the prefix tier exists.

    A trailing boundary would drop "kinyomómat" and "furatokat", which are the
    two most common phrasings of the requests the press and hole rules exist
    for -- so they are the two a careless "just add \\b everywhere" fix breaks.
    """
    assert "kinyomó" in PREFIX_TIER
    assert "furat" in PREFIX_TIER
    assert tripped("Karácsonyfa alakú kinyomómat szeretnék") == ["PRESS:kinyomó"]
    assert tripped("lap 4 db M6 furatokkal") == ["HOLE:furat"]


@pytest.mark.parametrize(
    "term",
    ["gyűrűs", "gyűrűtartó", "nyaklánctartó", "karkötőtartó", "ékszeres", "ékszerdoboz", "betűs"],
)
def test_the_small_item_branch_keeps_its_own_compounds_quiet(term):
    """The same bare-substring class as `press` in *kom*presszor, one guard down.

    A jewellery display stand, a ring holder or a lettered label is a
    legitimately large part; `gyűrűtartó` is over 250 mm on its own.
    """
    assert tripped(term) == []


def test_the_small_item_branch_still_catches_the_real_phrasings():
    """The collisions are quieted, not the guard: "fülbevalóhoz" is the request
    the existing size test uses, and the case ending is welded onto the stem."""
    assert tripped("fülbevalóhoz") == ["SMALL_ITEM:fülbevaló"]
    assert tripped("fülcse") == ["SMALL_ITEM:fülcse"]
    assert tripped("earrings") == ["SMALL_ITEM:earrings"]


@pytest.mark.parametrize(
    ("whole", "prefix"),
    [
        (SILHOUETTE_WHOLE_WORDS, SILHOUETTE_PREFIX_WORDS),
        (PRESS_WHOLE_WORDS, PRESS_PREFIX_WORDS),
        (HOLE_WHOLE_WORDS, HOLE_PREFIX_WORDS),
        (SMALL_ITEM_WHOLE_WORDS, SMALL_ITEM_PREFIX_WORDS),
    ],
)
def test_no_english_keyword_may_enter_the_prefix_tier(whole, prefix):
    """`press` in *kompresszor* is the measured false positive.

    Keeping the tier Hungarian-only makes that class structurally impossible
    rather than a thing to remember each time a keyword is added, and the
    English entries are recoverable in the whole-word tier where they belong.
    """
    assert not set(prefix) & ENGLISH_KEYWORDS
    # Cross-check: the list above is the module's English vocabulary, not a
    # subset chosen to make the assertion pass.
    whole_word_english = ENGLISH_KEYWORDS & (
        set(SILHOUETTE_WHOLE_WORDS)
        | set(PRESS_WHOLE_WORDS)
        | set(HOLE_WHOLE_WORDS)
        | set(SMALL_ITEM_WHOLE_WORDS)
    )
    assert len(whole_word_english) >= 30


@pytest.mark.parametrize(
    ("whole", "prefix"),
    [
        (SILHOUETTE_WHOLE_WORDS, SILHOUETTE_PREFIX_WORDS),
        (PRESS_WHOLE_WORDS, PRESS_PREFIX_WORDS),
        (HOLE_WHOLE_WORDS, HOLE_PREFIX_WORDS),
        (SMALL_ITEM_WHOLE_WORDS, SMALL_ITEM_PREFIX_WORDS),
    ],
)
def test_the_two_tiers_are_disjoint_and_free_of_duplicates(whole, prefix):
    assert not set(whole) & set(prefix)
    assert len(whole) == len(set(whole))
    assert len(prefix) == len(set(prefix))


def test_prefix_tier_is_exactly_the_declared_prefix_keywords():
    """The matcher reads one derived registry, so a new tier entry is declared once."""
    assert PREFIX_TIER == frozenset(
        SILHOUETTE_PREFIX_WORDS + PRESS_PREFIX_WORDS + HOLE_PREFIX_WORDS + SMALL_ITEM_PREFIX_WORDS
    )


@pytest.mark.parametrize("word", sorted(PREFIX_TIER))
def test_a_prefix_keyword_is_never_a_bare_substring(word):
    """The prefix tier drops the *trailing* boundary, never the leading one.

    A prefix keyword exists so a welded Hungarian ending still matches, which is
    a left-boundary-only rule -- and only a left boundary. If the leading one
    went as well the tier would be the bare substring form this whole rewrite
    exists to remove, only for the Hungarian stems this time. Nothing else
    catches that: the 48-term corpus and the *PREFIX_WORDS probes all start a
    term with the keyword or spell it as a separate word, and no realistic
    workshop term happens to carry a prefix keyword in a mid-word position, so
    the invariant is probed synthetically here.

    The second assertion keeps the test honest in the other direction: the
    welded ending (``furat`` -> "furatval") is exactly what the tier is for.
    """
    assert not _mentions(f"prefixed{word}", [word])
    assert _mentions(f"{word}val", [word])


def test_the_welded_press_ending_reaches_the_guard_end_to_end():
    """``kinyomómat`` has to produce a *finding*, not just a matcher hit.

    A missed detection is invisible at the matcher level: every press rule fires
    only when the specification has no ``extrude``, so a matcher that quietly
    stopped matching the welded form would leave the guard silent and the
    keyword-level assertions would still pass. Here the only keyword anywhere in
    the request is a welded prefix form -- the object name is deliberately
    neutral -- so the finding can only come from the prefix tier.
    """
    produced = spec(
        [{"type": "box", "role": "add", "width": 40.0, "depth": 30.0, "height": 5.0}],
        object="form",
    )

    issues = consistency_issues("Süti kinyomómat szeretnék", produced)

    assert len(issues) == 1
    assert "extrude" in issues[0]


@pytest.mark.parametrize("name", sorted(TIERS))
def test_the_old_names_stay_the_union_of_the_two_tiers(name):
    """`SILHOUETTE_WORDS` and friends are part of the module's public contract.

    They keep working as the **union**, so a caller or test that iterates the
    list still sees every keyword exactly as before -- the tier split is a
    matching detail, not a vocabulary change.
    """
    tiers = {
        "SILHOUETTE": (SILHOUETTE_WHOLE_WORDS, SILHOUETTE_PREFIX_WORDS),
        "PRESS": (PRESS_WHOLE_WORDS, PRESS_PREFIX_WORDS),
        "HOLE": (HOLE_WHOLE_WORDS, HOLE_PREFIX_WORDS),
        "SMALL_ITEM": (SMALL_ITEM_WHOLE_WORDS, SMALL_ITEM_PREFIX_WORDS),
    }
    whole, prefix = tiers[name]

    assert TIERS[name] == whole + prefix
    assert PREFIX_TIER & set(whole) == frozenset()


def test_no_keyword_was_removed_from_the_vocabulary():
    """The fix is a change of *matching*, not of vocabulary.

    Every keyword the module shipped before the two-tier split must still be
    reachable through the old names, so a detection can only ever get quieter
    (tier choice) and never disappear (a deleted entry). Several are multi-word,
    hence the comma separation.
    """
    before = {
        "SILHOUETTE": (
            "karácsonyfa, karácsony fa, fa alakú, faforma, fa alak, csillag, szív, levél,"
            " dísz, virág, holdszakért, betű, logó, kirakó, matrica, sziluett, fa, tree,"
            " star, heart, leaf, ornament, angel, flower, moon, letter, logo, silhouette"
        ),
        "PRESS": (
            "kinyomó, kinyomo, sajtó, matric, sütiforma, kiszúró, vágó, bélyeg, cutter,"
            " press, stamp, cookie, matrix, emboss"
        ),
        "HOLE": "lyuk, furat, lyukas, csavar, csap, szög, tüske, akasztó, hole, cutout, screw, pin, dowel, hook",
        "SMALL_ITEM": (
            "fülbevaló, fülbevalók, ékszer, gyűrű, nyaklánc, karkötő, fülcse, earring,"
            " jewellery, jewelry, pendant, stud"
        ),
    }

    for name, keywords in before.items():
        missing = {word.strip() for word in keywords.split(",")} - set(TIERS[name])

        assert not missing, f"{name} lost a keyword: {sorted(missing)}"


@pytest.mark.parametrize(
    ("term", "why"),
    [
        ("karácsonyfák", "the plural of a silhouette keyword"),
        ("gyűrűt", "the accusative of a wearable keyword"),
        ("sajtóval", "the instrumental of a press keyword"),
        ("betűs", "a compound that ends with a whole-word keyword"),
    ],
)
def test_the_accepted_misses_are_pinned(term, why):
    """The price of precision, written down so it cannot be forgotten.

    A whole-word keyword stops matching the moment Hungarian welds an ending
    onto it. Each of these lost a match in exchange for killing a false
    positive on a real workshop word -- the asymmetry is deliberate: a missed
    keyword leaves the guard quiet, a wrong one rejects a correct part and
    burns a retry. Restoring any of them needs the compound collision it was
    traded against to be re-checked first.

    The forms that turned out to be ordinary workshop language are *not* here:
    they were added to the whole-word list instead ("szögek", "csappal",
    "tüskék", "lyukak", "sajtómatrica", "cookiecutter", ...), since a miss on
    "4 db szögekkel" costs nothing that a miss on an exotic form would not.
    """
    assert tripped(term) == [], f"{term!r} ({why}) is an accepted miss, not a new detection"


@pytest.mark.parametrize(
    ("term", "tier"),
    [
        ("4 db szögekkel", "HOLE"),
        ("3 db csappal", "HOLE"),
        ("tüskék a peremen", "HOLE"),
        ("4 lyukak", "HOLE"),
        ("sütiforma kinyomáshoz", "PRESS"),
        ("sajtómatrica", "PRESS"),
        ("cookiecutter", "PRESS"),
    ],
)
def test_the_common_inflected_forms_were_added_back_as_whole_words(term, tier):
    """The misses that turned out to be normal phrasing were worth keeping.

    Each of these is a plural, an instrumental or a compound that a Hungarian
    workshop actually writes. The boundary match drops them, so they are listed
    explicitly -- and listed as whole words, because none of them is a prefix of
    anything, which is what makes the explicit form safe where the stem was not.
    """
    found = tripped(term)
    assert any(f.startswith(f"{tier}:") for f in found), (
        f"{term!r} no longer trips {tier}; it reports {found or 'nothing'}"
    )
