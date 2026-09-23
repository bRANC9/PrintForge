"""Prompt-content tests for the Planner and Editor system prompts.

The planner/editor LLMs must describe real geometry through the
``ModelSpecification.primitives`` list (docs/cad-primitives.md 1. and 3.
fejezet), so these tests pin the instructions that make that happen. They only
inspect the prompt strings -- no LLM, no database, no OpenSCAD.
"""

from __future__ import annotations

import pytest

from agents.graph.editor import EDITOR_SYSTEM_PROMPT
from agents.graph.planner import CLARIFICATION_PROMPT, PLANNER_SYSTEM_PROMPT
from agents.graph.reviser import REVISER_SYSTEM_PROMPT

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


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_teaches_the_primitives_field(name: str, prompt: str) -> None:
    """Both prompts must point the LLM at the ``primitives`` field."""
    assert "primitives" in prompt


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_explains_the_centered_position_convention(name: str, prompt: str) -> None:
    """``position`` is the primitive centre, in millimetres."""
    assert "position" in prompt
    assert "centre" in prompt
    assert "mm" in prompt
    assert "millimetre" in prompt


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_lists_all_primitive_types(name: str, prompt: str) -> None:
    for primitive in PRIMITIVE_TYPES:
        assert f"'{primitive}'" in prompt


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_mentions_add_subtract_roles_and_build_plate(name: str, prompt: str) -> None:
    assert "'add'" in prompt
    assert "'subtract'" in prompt
    assert "min Z = 0" in prompt
    assert "printable" in prompt


@pytest.mark.parametrize("name, prompt", PROMPTS.items())
def test_prompt_forbids_code_and_mesh_output(name: str, prompt: str) -> None:
    assert "Never emit OpenSCAD code, G-code or STL data" in prompt


@pytest.mark.parametrize("name, prompt", DIMENSION_PROMPTS.items())
def test_prompt_teaches_required_operation_dimensions(name: str, prompt: str) -> None:
    """Pin the per-kind required fields that the CAD backend enforces.

    A ``slot`` without a ``diameter`` (or any other missing required dimension)
    is the fixable specification error the reviser retry loop exists for, so
    every prompt that emits operations must spell the contract out.
    """
    assert "'depth', 'origin' and 'normal'" in prompt
    assert "'hole' -> 'diameter'" in prompt
    assert "'boss' -> 'diameter'" in prompt
    assert "'slot' -> 'diameter' and 'length'" in prompt
    assert "'pocket', 'cut' and 'add' -> 'width' and 'height'" in prompt


@pytest.mark.parametrize("name, prompt", DIMENSION_PROMPTS.items())
def test_prompt_teaches_required_primitive_dimensions(name: str, prompt: str) -> None:
    """Pin the per-type required primitive sizes the CAD backend enforces.

    A live small model emitted a ``box`` without ``depth``/``height``; this
    contract (shared by Planner, Editor and Reviser) is what prevents that.
    """
    assert "'box' requires 'width', 'depth' and 'height'" in prompt
    assert "'cylinder' and 'cone' require 'diameter' and 'height'" in prompt
    assert "'sphere' requires 'diameter'" in prompt
    assert "concrete number" in prompt
    assert "never leave a required size missing or null" in prompt


@pytest.mark.parametrize("name, prompt", DIMENSION_PROMPTS.items())
def test_prompt_explains_primitive_position_and_build_plate(name: str, prompt: str) -> None:
    """``position`` is the primitive centre and the part rests on the plate."""
    assert "primitive centre" in prompt
    assert "min Z = 0" in prompt


def test_reviser_is_told_to_repair_the_missing_numeric_fields() -> None:
    prompt = REVISER_SYSTEM_PROMPT
    assert "fill or repair exactly that field" in prompt
    assert "numeric" in prompt


def test_reviser_is_told_to_repair_missing_primitive_sizes() -> None:
    """The reviser must refill missing operation dimensions AND primitive sizes."""
    prompt = REVISER_SYSTEM_PROMPT
    assert "missing primitive sizes" in prompt
    # The shared primitive-dimension contract is appended verbatim.
    assert "'box' requires 'width', 'depth' and 'height'" in prompt


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
    # The bound and both kinds stay stated.
    assert "Return at most 8 'clarifications' entries" in prompt
    assert "'assumed'" in prompt
    assert "'needs_user_input'" in prompt
    # It stays Planner-only data and never leaks into the specification.
    assert "Planner-only data" in prompt
    assert "never put it inside the specification" in prompt
    assert "never send it to the CAD backend" in prompt
