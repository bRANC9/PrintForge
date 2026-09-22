"""Unit tests for the Editor agent (docs/visual-editing.md 3.5).

No network, no database, no OpenSCAD: the LLM is a fake and the node is called
directly with a hand-built state.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from agents.graph.editor import EDITOR_SYSTEM_PROMPT, make_editor_node
from agents.graph.tests.fakes import DEFAULT_SPEC, FakeProvider
from agents.llm import LLMError

ANNOTATION: dict[str, Any] = {
    "id": "c1",
    "kind": "point",
    "point": [35.0, 12.5, 8.0],
    "normal": [0.0, 0.0, 1.0],
    "faces": [],
    "instruction": "4 mm-es átmenő lyuk",
}
EDIT_SPEC: dict[str, Any] = {
    **DEFAULT_SPEC,
    "operations": [
        {
            "kind": "hole",
            "origin": {"x": 35.0, "y": 12.5, "z": 8.0},
            "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
            "depth": 8.0,
            "diameter": 4.0,
        }
    ],
}


class RecordingProvider(FakeProvider):
    """Fake provider that also records the prompts it received."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.systems.append(str(kwargs.get("system", "")))
        return super().structured(prompt, schema, **kwargs)


def _state() -> dict[str, Any]:
    return {
        "prompt": "A kijelölt peremre tegyél egy lyukat.",
        "base_specification": DEFAULT_SPEC,
        "annotations": [ANNOTATION],
    }


def _provider(plan: dict[str, Any] | None = None) -> RecordingProvider:
    return RecordingProvider(
        plan=plan
        or {
            "specification": EDIT_SPEC,
            "needs_research": False,
            "research_query": None,
        }
    )


def test_editor_prompt_carries_the_annotation_instruction_and_shape():
    provider = _provider()
    node = make_editor_node(provider=provider, max_attempts=3)

    result = node(_state())

    assert result["status"] == "planned"
    assert result["error"] is None
    assert result["specification"]["operations"][0]["kind"] == "hole"

    prompt = provider.prompts[0]
    # The actual annotation instruction reaches the LLM ...
    assert ANNOTATION["instruction"] in prompt
    # ... together with the operation schema it must map it onto.
    assert "operations" in prompt
    assert "origin" in prompt
    # The base specification is included as JSON as well.
    assert DEFAULT_SPEC["object"] in prompt

    assert provider.systems[0] == EDITOR_SYSTEM_PROMPT
    # Boundary: the system prompt forbids code/mesh output.
    assert "Never emit OpenSCAD code, G-code or STL data" in provider.systems[0]
    # The Editor must describe geometry with the shared primitive list too.
    assert "primitives" in provider.systems[0]
    assert "position" in provider.systems[0]
    assert "min Z = 0" in provider.systems[0]


def test_editor_validation_failure_is_a_structured_editor_error():
    provider = _provider(
        plan={"specification": {"object": ""}, "needs_research": False, "research_query": None}
    )
    node = make_editor_node(provider=provider, max_attempts=3)

    result = node(_state())

    assert result["status"] == "failed"
    error = json.loads(result["error"])
    assert error["stage"] == "editor"
    assert error["type"] == "ValidationError"


def test_editor_llm_failure_is_a_structured_editor_error():
    provider = _provider()
    provider.planner_error = LLMError("editor model offline")
    node = make_editor_node(provider=provider, max_attempts=3)

    result = node(_state())

    assert result["status"] == "failed"
    error = json.loads(result["error"])
    assert error["stage"] == "editor"
    assert error["type"] == "LLMError"
    assert "editor model offline" in error["message"]


# ---------------------------------------------------------------------------
# Deterministic anchor enforcement (docs/visual-editing.md 3.5)
# ---------------------------------------------------------------------------


def test_editor_snaps_operation_and_fills_label_from_the_annotation():
    provider = _provider()
    node = make_editor_node(provider=provider, max_attempts=3)

    result = node(_state())

    operation = result["specification"]["operations"][0]
    assert operation["origin"] == {"x": 35.0, "y": 12.5, "z": 8.0}
    assert operation["normal"] == {"x": 0.0, "y": 0.0, "z": 1.0}
    assert operation["label"] == ANNOTATION["instruction"]


FAR_EDIT_SPEC: dict[str, Any] = {
    **DEFAULT_SPEC,
    "operations": [
        {
            "kind": "hole",
            "origin": {"x": 535.0, "y": 12.5, "z": 8.0},  # 500 mm from the annotation
            "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
            "depth": 8.0,
            "diameter": 4.0,
        }
    ],
}


def test_editor_records_a_history_warning_for_a_far_operation():
    provider = _provider(
        plan={
            "specification": FAR_EDIT_SPEC,
            "needs_research": False,
            "research_query": None,
        }
    )
    node = make_editor_node(provider=provider, max_attempts=3)

    result = node(_state())

    assert result["status"] == "planned"
    assert result["specification"]["operations"][0]["origin"] == {
        "x": 535.0,
        "y": 12.5,
        "z": 8.0,
    }
    anchors = [entry for entry in result["history"] if entry.startswith("editor: anchor:")]
    assert len(anchors) == 1
    assert "kept unchanged" in anchors[0]
