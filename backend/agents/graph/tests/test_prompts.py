"""Prompt-content tests for the Planner and Editor system prompts.

The planner/editor LLMs must describe real geometry through the
``ModelSpecification.primitives`` list (docs/cad-primitives.md 1. and 3.
fejezet), so these tests pin the instructions that make that happen. They only
inspect the prompt strings -- no LLM, no database, no OpenSCAD.
"""

from __future__ import annotations

import pytest

from agents.graph.editor import EDITOR_SYSTEM_PROMPT
from agents.graph.planner import PLANNER_SYSTEM_PROMPT

PRIMITIVE_TYPES = ("box", "cylinder", "sphere", "cone")

PROMPTS = {
    "planner": PLANNER_SYSTEM_PROMPT,
    "editor": EDITOR_SYSTEM_PROMPT,
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
