"""Prompt-content tests for the Planner, Editor and Reviser system prompts.

The planner/editor/reviser LLMs must describe real geometry through the
``ModelSpecification.primitives`` list (docs/cad-primitives.md 1. and 3.
fejezet), so these tests pin the instructions that make that happen. They only
inspect the prompt strings and the JSON Schema text -- no LLM, no database, no
OpenSCAD.

Where a rule lives
------------------
A structured call sends the system prompt **and** the JSON Schema text in the
same system message (``OllamaProvider.structured`` builds exactly that), and
the measured Planner request did not fit a 4096-token context. So a rule is
now stated once: the *behavioural* rules ("never emit mesh", "``role: add`` for
material", "``primitives: []`` only for the phone holder") in the prompt,
because a schema cannot express them, and the *field contract* (units, ranges,
which type needs which size) in the field descriptions, because a paragraph
far above the field is both more expensive and further from the decision.

:func:`_contract` returns prompt + schema, so a test can assert a rule against
what the model actually reads; it fails whichever carrier loses the rule.
"""

from __future__ import annotations

import json

import pytest

from agents.graph.editor import EDITOR_SYSTEM_PROMPT
from agents.graph.planner import CLARIFICATION_PROMPT, PLANNER_SYSTEM_PROMPT, PlannerPlan
from agents.graph.reviser import REVISER_SYSTEM_PROMPT
from agents.spec import ModelSpecification

PRIMITIVE_TYPES = ("box", "cylinder", "sphere", "cone")

PROMPTS = {
    "planner": PLANNER_SYSTEM_PROMPT,
    "editor": EDITOR_SYSTEM_PROMPT,
}

#: The reviser carries the same per-operation contract as the planner/editor.
DIMENSION_PROMPTS = {
    **PROMPTS,
    "reviser": REVISER_SYSTEM_PROMPT,
}

#: The schema each prompt's model is shown, so ``_contract`` can be exact.
PROMPT_SCHEMAS = {
    "planner": PlannerPlan,
    "editor": PlannerPlan,
    "reviser": ModelSpecification,
}


def _contract(name: str) -> str:
    """Return the system prompt plus the JSON Schema text the model reads."""
    schema = PROMPT_SCHEMAS[name]
    return (
        f"{DIMENSION_PROMPTS[name]}\n{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
    )


# ---------------------------------------------------------------------------
# The prompt budget itself (the reason the contract moved where it did).
# ---------------------------------------------------------------------------


def test_planner_system_prompt_fits_the_token_budget() -> None:
    """The Planner system prompt must stay small enough for a 4096 context.

    Measured 1006 tokens before the trim, against a 4096-token context whose
    whole request (prompt + schema) was ~4.2k. The trim removed only what the
    schema already states; the behavioural instructions stayed. Ollama's stock
    build has no tokenize endpoint, so the chars/4 estimate is calibrated
    against the one real ``n_prompt_tokens`` the host reported for this request
    shape (it runs ~12% low).
    """
    estimated = len(PLANNER_SYSTEM_PROMPT) // 4
    assert estimated <= 700, f"PLANNER_SYSTEM_PROMPT grew to ~{estimated} tokens"


#: The context window a model served with Ollama's default is loaded with. The
#: measured failure is verbatim in docs/model-matrix-test-round.md 2.:
#: ``request (4208 tokens) exceeds the available context size``.
CONTEXT_TOKENS = 4096

#: ``chars / 4`` runs *low* on this request shape. Ollama's stock build has no
#: ``/api/tokenize`` endpoint, so the budget is a chars/4 estimate inflated by
#: the measured gap between it and the one real ``n_prompt_tokens`` this host
#: reported for the Planner request (~3237 real against ~2688 estimated, i.e.
#: +20%). 1.25 leaves the calibration margin on top of that. Budgeting the raw
#: estimate instead would wave through a request a quarter larger than the
#: context -- which is exactly the failure that killed five of thirteen local
#: models.
ESTIMATE_BIAS = 1.25


def _planner_system_message(monkeypatch) -> str:
    """The exact system message the Ollama host receives for one Planner call.

    Captured from the real provider rather than reassembled here, so the framing
    Ollama adds ("Return only JSON conforming to this JSON Schema") and the
    schema serialisation are both counted -- a hand-built concatenation would
    quietly stop measuring the request the day either changed.
    """
    from agents.llm import OllamaProvider

    captured: list[dict] = []
    # A valid plan, because the provider validates the reply against the schema
    # before returning: only the *request* is under test here.
    answer = json.dumps(
        {
            "specification": ModelSpecification.example(),
            "needs_research": False,
            "research_query": None,
            "clarifications": [],
        }
    )

    def fake_post(path: str, payload: dict) -> dict:
        captured.append(payload)
        return {"message": {"content": answer}}

    provider = OllamaProvider(base_url="http://test:11434", model="test-model")
    monkeypatch.setattr(provider, "_post", fake_post)

    provider.structured("make a 40 x 60 mm bracket", PlannerPlan, system=PLANNER_SYSTEM_PROMPT)

    return captured[0]["messages"][0]["content"]


def test_the_budget_measures_the_whole_system_message(monkeypatch) -> None:
    """Guards the measurement itself, so the budget cannot pass vacuously.

    :func:`test_planner_system_prompt_fits_the_token_budget` above and
    ``test_planner_plan_schema_description_budget_is_bounded`` in ``test_spec.py``
    bound a *part* of the request each. The request budget below has to see the
    whole system message -- prompt, the framing Ollama adds and the serialised
    schema -- or it would happily pass on a request that no longer tells the
    model what to emit.
    """
    system_message = _planner_system_message(monkeypatch)

    assert PLANNER_SYSTEM_PROMPT in system_message
    assert "Return only JSON conforming to this JSON Schema" in system_message
    # The schema is really in there, description text and all.
    assert '"clarifications"' in system_message
    assert "min Z = 0" in system_message


def test_the_whole_planner_request_fits_the_context_it_is_served_with(
    monkeypatch,
) -> None:
    """The number that actually broke: prompt + schema against ``num_ctx``.

    Neither component budget covers this.
    :func:`test_planner_system_prompt_fits_the_token_budget` caps the prose and
    ``test_planner_plan_schema_description_budget_is_bounded`` caps the schema's
    ``description`` strings -- but the schema's *structure* (~6.2k of the 8.0k
    characters it serialises to: property names, enums, ``$defs``, ``required``
    lists) is bounded by neither. A new nested model, a widened enum or a
    renamed ``$def`` grows the request by hundreds of characters with both
    existing tests green, and a model served with a 4096-token context then
    refuses the Planner call outright -- five of thirteen local models died that
    way, which is the regression this whole split exists to prevent.

    The user prompt is a runtime request of arbitrary length, so the budget can
    only cover the part the product controls: the system message it always sends.
    """
    system_message = _planner_system_message(monkeypatch)
    estimated = len(system_message) // 4
    budgeted = estimated * ESTIMATE_BIAS

    assert budgeted <= CONTEXT_TOKENS, (
        f"the Planner request is {len(system_message)} characters "
        f"(~{estimated} tokens, ~{budgeted:.0f} with the {ESTIMATE_BIAS} calibration "
        f"margin) against a {CONTEXT_TOKENS}-token context"
    )


# ---------------------------------------------------------------------------
# Behavioural instructions: only the prompt can carry these.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_teaches_the_primitives_field(name: str, prompt: str) -> None:
    """Both prompts must point the LLM at the ``primitives`` field."""
    assert "primitives" in prompt


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_teaches_the_position_field(name: str, prompt: str) -> None:
    """A primitive is placed by its ``position``; the model must be told to."""
    assert "position" in prompt


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_mentions_add_subtract_roles(name: str, prompt: str) -> None:
    """``role: add`` adds material, ``role: subtract`` cuts it away."""
    assert "'add'" in prompt
    assert "'subtract'" in prompt


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_requires_printable_sizes(name: str, prompt: str) -> None:
    assert "printable" in prompt


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_forbids_code_and_mesh_output(name: str, prompt: str) -> None:
    assert "Never emit OpenSCAD code, G-code or STL data" in prompt


def test_planner_keeps_legacy_fields_and_research_behaviour() -> None:
    prompt = PLANNER_SYSTEM_PROMPT
    # The terv.md 8. compatibility fields stay filled ...
    for field in ("dimensions", "angle", "wall_thickness", "mounting"):
        assert field in prompt
    # ... the research decision is untouched ...
    assert "needs_research" in prompt
    assert "research_query" in prompt
    # ... and the built-in phone-holder fallback is explicit.
    assert "phone holder" in prompt
    assert "'primitives': []" in prompt


def test_planner_still_teaches_the_flat_extrude_rule() -> None:
    """A flat 2D shape is one ``extrude`` with a real, ordered ``profile``.

    This is a behavioural instruction (when to choose a shape), so it cannot
    live in the schema; it stayed in the prompt through the trim.
    """
    prompt = PLANNER_SYSTEM_PROMPT
    assert "2D-shaped object" in prompt
    assert "'extrude'" in prompt
    assert "'profile'" in prompt
    assert "outline points in order" in prompt


def test_editor_keeps_base_primitives_and_annotation_anchoring() -> None:
    prompt = EDITOR_SYSTEM_PROMPT
    # The base geometry survives an edit unless the instruction changes it.
    assert "Keep the base object's existing 'primitives'" in prompt
    # Annotation anchoring behaviour is untouched.
    assert "annotations" in prompt
    assert "operations" in prompt
    assert "origin" in prompt
    assert "normal" in prompt
    # Research decision is preserved on the edit path too.
    assert "needs_research" in prompt
    assert "research_query" in prompt


def test_reviser_is_told_to_repair_the_missing_numeric_fields() -> None:
    prompt = REVISER_SYSTEM_PROMPT
    assert "fill or repair exactly that field" in prompt
    assert "numeric" in prompt


def test_reviser_is_told_to_repair_missing_primitive_sizes() -> None:
    """The reviser must refill missing operation dimensions AND primitive sizes."""
    prompt = REVISER_SYSTEM_PROMPT
    assert "missing primitive sizes" in prompt
    # The shared primitive-dimension contract is appended verbatim.
    assert PRIMITIVE_DIMENSIONS_CONTRACT_HEAD in prompt


#: The shared block must still demand concrete, non-null numbers.
PRIMITIVE_DIMENSIONS_CONTRACT_HEAD = "Every 'primitives' entry MUST carry the sizes its type needs"
OPERATION_DIMENSIONS_CONTRACT_HEAD = (
    "Every 'operations' entry MUST carry 'depth', 'origin' and 'normal'"
)


@pytest.mark.parametrize("name, prompt", DIMENSION_PROMPTS.items())
def test_prompt_forbids_null_required_dimensions(name: str, prompt: str) -> None:
    """A schema can bound a value; only the prompt can forbid omitting it."""
    assert OPERATION_DIMENSIONS_CONTRACT_HEAD in prompt
    assert "concrete number, never null" in prompt
    assert PRIMITIVE_DIMENSIONS_CONTRACT_HEAD in prompt
    assert "concrete number" in prompt
    assert "never leave it missing or null" in prompt


# ---------------------------------------------------------------------------
# Field contract: asserted against prompt + schema, so either carrier is fine.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(DIMENSION_PROMPTS))
def test_contract_names_every_primitive_type(name: str) -> None:
    """The five primitive types are listed once, in the schema enum."""
    contract = _contract(name)
    for primitive in PRIMITIVE_TYPES + ("extrude",):
        assert f"'{primitive}'" in contract or f'"{primitive}"' in contract


@pytest.mark.parametrize("name", sorted(DIMENSION_PROMPTS))
def test_contract_states_the_centered_position_and_build_plate(name: str) -> None:
    """``position`` is the primitive centre and the part rests on the plate.

    Measured cause of floating parts: a model treating ``position`` as a corner.
    The rule now lives in the ``position`` field description, which is where the
    model reads it; the assertion covers both carriers.
    """
    assert "min Z = 0" in _contract("planner")
    position = ModelSpecification.to_json_schema()["$defs"]["Primitive"]["properties"]["position"]
    assert "centre" in position["description"]
    assert "min Z = 0" in position["description"]
    assert "mm" in position["description"]


@pytest.mark.parametrize("name", sorted(DIMENSION_PROMPTS))
def test_contract_documents_per_kind_operation_dimensions(name: str) -> None:
    """Each operation kind's required dimensions must be stated.

    A ``slot`` without a ``diameter`` (or any other missing required dimension)
    is the fixable specification error the reviser retry loop exists for, so
    every prompt that emits operations must carry the contract -- in the prompt
    or on the field, both of which the model reads.
    """
    properties = ModelSpecification.to_json_schema()["$defs"]["EditOperation"]["properties"]
    for kind, fields in {
        "hole": ("diameter",),
        "boss": ("diameter",),
        "slot": ("diameter", "length"),
        "pocket": ("width", "height"),
        "cut": ("width", "height"),
        "add": ("width", "height"),
    }.items():
        assert kind in properties["kind"]["enum"]
        for field in fields:
            assert kind in properties[field]["description"], f"{field} lost {kind}"
    prompt = DIMENSION_PROMPTS[name]
    assert OPERATION_DIMENSIONS_CONTRACT_HEAD in prompt
    assert "dimensions its kind requires" in prompt


@pytest.mark.parametrize("name", sorted(DIMENSION_PROMPTS))
def test_contract_documents_per_type_primitive_dimensions(name: str) -> None:
    """Each primitive type's required sizes must be stated.

    They live on the fields (cheaper, and closer to the decision than a
    paragraph), and the shared prompt block points the model at them.
    """
    properties = ModelSpecification.to_json_schema()["$defs"]["Primitive"]["properties"]
    for kind, fields in {
        "box": ("width", "depth", "height"),
        "cylinder": ("diameter", "height"),
        "cone": ("diameter", "height"),
        "sphere": ("diameter",),
        "extrude": ("profile", "height"),
    }.items():
        assert kind in properties["type"]["enum"]
        for field in fields:
            assert kind in properties[field]["description"], f"{field} lost {kind}"
    # The prompt still points at that contract, and still pins the two facts a
    # schema cannot: concrete numbers, and the extrude's profile + height.
    prompt = DIMENSION_PROMPTS[name]
    assert PRIMITIVE_DIMENSIONS_CONTRACT_HEAD in prompt
    assert "description says which" in prompt
    assert "'extrude' also needs 'profile'" in prompt
    assert "and 'height'" in prompt


# ---------------------------------------------------------------------------
# Clarification few-shot contract (docs/planner-clarification.md 2.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_embeds_the_shared_clarification_contract(name: str, prompt: str) -> None:
    """The Editor must reuse the Planner's clarification block verbatim.

    A hardcoded copy would drift; asserting the shared constant is a substring
    keeps the two prompts in lock-step for free.
    """
    assert CLARIFICATION_PROMPT in prompt


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_carries_clarification_few_shot_examples(name: str, prompt: str) -> None:
    """Weak local models need a concrete JSON example to emit clarifications.

    Without it they silently assume a generic part (e.g. a generic phone for
    "Keszits egy telefontartot.") instead of asking or recording an assumption.
    """
    # Example A: an ambiguous request with a critical missing dimension -> ask.
    assert "Example A" in prompt
    assert '"kind": "needs_user_input"' in prompt
    assert '"answer": ""' in prompt
    assert '"field": "dimensions"' in prompt
    # Example B: a safe missing value -> a concrete, printable assumption.
    assert "Example B" in prompt
    assert '"kind": "assumed"' in prompt
    assert '"answer": "3"' in prompt
    assert '"field": "wall_thickness"' in prompt


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_still_states_the_clarification_contract(name: str, prompt: str) -> None:
    """The bounded, Planner-only clarification rules survive the examples."""
    # The two kinds stay stated.
    assert "'assumed'" in prompt
    assert "'needs_user_input'" in prompt
    # It stays Planner-only data and never leaks into the specification.
    assert "Planner-only data" in prompt
    assert "never put it inside the specification" in prompt
    assert "never send it to the CAD backend" in prompt
    # The "at most 8 entries" bound is now stated by the schema's maxItems.
    assert PlannerPlan.model_json_schema()["properties"]["clarifications"]["maxItems"] == 8
