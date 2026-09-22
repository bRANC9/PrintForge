"""Editor agent for annotation-driven model editing (docs/visual-editing.md 3.5).

The Editor is the visual-prompt counterpart of the Planner: instead of turning a
free-text request into a specification from scratch, it takes the *base*
specification plus the user's surface annotations and returns an **updated full
:class:`~agents.spec.ModelSpecification`** whose ``operations`` list holds the
validated features derived from the annotations.

Boundary (terv.md 7. fejezet): exactly like the Planner, the Editor NEVER
produces OpenSCAD source, G-code or STL/mesh data -- it only produces structured
data. The operations are rendered later by the CAD node through
``CADBackend.generate``; the Validator is still the only mesh producer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from agents.llm import LLMError, LLMProvider

from .planner import PlannerPlan, coerce_plan
from .state import WorkflowState, append_history, failure_state
from .vision import reference_images, reference_prompt_for, structured_with_reference_image

__all__ = ["EDITOR_SYSTEM_PROMPT", "make_editor_node"]

EDITOR_SYSTEM_PROMPT = (
    "You are the Editor agent of an OpenSCAD 3D-printing workflow. "
    "You receive the base model specification, the user's edit instruction and "
    "annotations drawn on the 3D surface (a point + normal, or a region "
    "centroid + average normal), each carrying its own 'instruction'. "
    "Return ONE JSON object with 'specification' (the FULL updated "
    "ModelSpecification) plus 'needs_research'/'research_query'. "
    "Each annotation instruction MUST become one or more entries in the "
    "specification's 'operations' list: use the annotation's point (or region "
    "centroid) as the operation 'origin' in model coordinates (mm) and the "
    "annotation normal as the operation 'normal' axis. "
    "Keep the base object kind and dimensions unless the edit explicitly "
    "requires a change. Emit 'operations': [] when the edit adds no feature. "
    "Never emit OpenSCAD code, G-code or STL data - only the structured plan."
)


def _editor_prompt(provider: LLMProvider, state: WorkflowState) -> str:
    """Build the Editor user prompt from the instruction, base spec and annotations.

    ``reference_prompt_for`` keeps the optional reference note/photo behaviour
    identical to the Planner; the base specification and the annotation payload
    are attached as JSON (the annotations only ever carry structured JSON, never
    raw image bytes).
    """
    instruction = reference_prompt_for(provider, state)
    base = state.get("base_specification") or {}
    annotations = state.get("annotations") or []
    return (
        f"User edit instruction:\n{instruction}\n\n"
        f"Base specification (JSON):\n"
        f"{json.dumps(base, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Annotations (JSON):\n"
        f"{json.dumps(annotations, ensure_ascii=False, sort_keys=True)}\n\n"
        "Annotation shape: {id, kind, point [x,y,z], normal [x,y,z], region "
        "{centroid [x,y,z], normal [x,y,z], size, count}, instruction}. "
        "For each annotation, turn its 'instruction' into one or more "
        "'operations' entries anchored at that annotation's origin (point or "
        "region centroid) with the annotation's normal as the operation axis "
        "(the 'operations' schema is: kind, origin, normal, depth, width, "
        "height, diameter, length, label). Keep the base dimensions unless the "
        "edit requires a change; emit 'operations': [] when the edit adds no "
        "feature."
    )


def make_editor_node(
    *,
    provider: LLMProvider,
    max_attempts: int,
) -> Callable[[WorkflowState], dict[str, Any]]:
    """Build the ``editor`` graph node bound to *provider*.

    The returned callable is a LangGraph node: it takes the running state and
    returns a partial state update shaped exactly like the Planner's, so the
    downstream ``route_after_planner`` edge and the CAD/Validator nodes need no
    knowledge of the edit path. All LLM access goes through the injected
    ``LLMProvider``, so tests pass a fake and no live model is required.
    """

    def editor_node(state: WorkflowState) -> dict[str, Any]:
        # The Editor, like the Planner, only ever asks for structured data: it
        # has no concept of code or meshes at all.
        prompt = _editor_prompt(provider, state)
        try:
            # The optional reference image (terv.md 27.) is attached here; the
            # helper falls back to a text-only call and never fails the run
            # because of vision.
            raw, image_used, image_warning = structured_with_reference_image(
                provider,
                prompt,
                PlannerPlan,
                system=EDITOR_SYSTEM_PROMPT,
                images=reference_images(state),
            )
            plan = coerce_plan(raw)
        except (LLMError, ValidationError) as exc:
            return failure_state(
                state,
                stage="editor",
                error_type=type(exc).__name__,
                message=str(exc),
            )

        history = append_history(state, "editor: specification updated (edit mode)")
        if image_warning:
            history = [*history, f"editor: {image_warning}"]
        return {
            "specification": plan.specification.model_dump(),
            "needs_research": bool(plan.needs_research),
            "research_query": plan.research_query,
            "research_sources": [],
            "research_used": False,
            "reference_image_used": image_used,
            "reference_image_warning": image_warning,
            "attempt": 0,
            "max_attempts": int(max_attempts),
            "status": "planned",
            "error": None,
            "history": history,
        }

    return editor_node
