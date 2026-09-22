"""Planner agent (terv.md 6., 7. fejezet).

The Planner turns the raw user prompt into a validated
:class:`~agents.spec.ModelSpecification` **through
``LLMProvider.structured``**. It also decides whether the Research agent is
needed and, if so, what to look up.

Boundary (terv.md 7. fejezet): the Planner NEVER produces OpenSCAD source or
STL/mesh data -- it only produces structured data. The CAD source is generated
later by the CAD node through :class:`~designs.cad.base.CADBackend`, and the
mesh only exists after the Validator exports it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents.llm import LLMError, LLMProvider
from agents.spec import ModelSpecification

from .state import WorkflowState, append_history, failure_state
from .vision import reference_images, reference_prompt_for, structured_with_reference_image

__all__ = [
    "OPERATION_DIMENSIONS_PROMPT",
    "PLANNER_SYSTEM_PROMPT",
    "PRIMITIVE_DIMENSIONS_PROMPT",
    "PlannerPlan",
    "coerce_plan",
    "make_planner_node",
]

#: Shared instruction pinning the per-operation required fields. The Planner,
#: Editor and Reviser all emit ``ModelSpecification.operations``; a ``slot``
#: without its ``diameter`` (or any other missing required dimension) is a
#: fixable specification error the CAD backend rejects, so every prompt states
#: the contract explicitly and asks the LLM to fill the fields.
OPERATION_DIMENSIONS_PROMPT = (
    "Every 'operations' entry MUST carry 'depth', 'origin' and 'normal', plus "
    "the dimensions its kind needs: 'hole' -> 'diameter'; 'boss' -> 'diameter'; "
    "'slot' -> 'diameter' and 'length'; 'pocket', 'cut' and 'add' -> 'width' "
    "and 'height'. Always fill these numeric fields explicitly and never leave a "
    "required dimension missing or null. "
)

#: Shared instruction pinning the per-type required fields of every
#: ``ModelSpecification.primitives`` entry. A ``box`` emitted without ``depth``
#: or ``height`` (or a ``cylinder``/``cone`` without ``diameter``/``height``) is
#: a fixable specification error the CAD backend rejects, so the Planner,
#: Editor and Reviser all state the contract explicitly and ask the LLM for
#: concrete numbers instead of nulls.
PRIMITIVE_DIMENSIONS_PROMPT = (
    "Every 'primitives' entry MUST carry the sizes its type needs: 'box' "
    "requires 'width', 'depth' and 'height'; 'cylinder' and 'cone' require "
    "'diameter' and 'height'; 'sphere' requires 'diameter'. 'position' is the "
    "primitive centre in mm and the part must rest on the build plate "
    "(min Z = 0); always give every listed size as a concrete number and never "
    "leave a required size missing or null. "
)

PLANNER_SYSTEM_PROMPT = (
    "You are the Planner agent of an OpenSCAD 3D-printing workflow. "
    "Read the user's request and return ONE JSON object with two jobs: "
    "(1) 'specification' - the structured model specification (object, "
    "dimensions in millimetres, angle, wall_thickness, mounting, material); "
    "(2) 'needs_research' and 'research_query' - whether real-world product or "
    "standard-part data (e.g. exact phone dimensions, ISO screw sizes) must be "
    "looked up first, and a concise lookup query if so. "
    "Describe the real geometry with the specification's 'primitives' list: "
    "build the object from one or more primitives in millimetres, each one a "
    "'box', 'cylinder', 'sphere' or 'cone' placed by its 'position' - the "
    "primitive centre in mm - with an optional 'rotation' in degrees. "
    "Use role 'add' for material and role 'subtract' for holes and cutouts. "
    "The part must rest on the build plate (min Z = 0) and use sensible, "
    "printable sizes. Keep dimensions, angle, wall_thickness and mounting "
    "filled for compatibility, and use 'primitives': [] only when the object "
    "really is the built-in phone holder; otherwise always describe the object "
    "with primitives. Use sensible printable defaults when a value is missing. "
    + OPERATION_DIMENSIONS_PROMPT
    + PRIMITIVE_DIMENSIONS_PROMPT
    + "Never emit OpenSCAD code, G-code or STL data - only the structured plan."
)


class PlannerPlan(BaseModel):
    """Structured Planner output.

    The nested ``specification`` is a fully validated
    :class:`~agents.spec.ModelSpecification`, so the LLM and the CAD backend
    keep their strict contract (terv.md 8. fejezet); the extra flags only drive
    graph routing and never reach the CAD backend.
    """

    model_config = ConfigDict(extra="forbid")

    specification: ModelSpecification
    needs_research: bool = Field(
        default=False,
        description="True when external product/standard data must be looked up.",
    )
    research_query: str | None = Field(
        default=None,
        max_length=512,
        description="Lookup query for the Research agent (when needs_research).",
    )


def coerce_plan(raw: Any) -> PlannerPlan:
    """Normalise a provider payload into a :class:`PlannerPlan`.

    Providers that support the nested schema return ``{"specification": ...}``.
    A provider that answers with a bare specification object (the plain
    ``ModelSpecification`` shape) is also accepted, with research disabled --
    this keeps the node robust across backends and trivial to fake in tests.
    """
    payload = dict(raw) if isinstance(raw, dict) else raw
    if isinstance(payload, dict) and "specification" not in payload and "object" in payload:
        return PlannerPlan(specification=ModelSpecification.model_validate(payload))
    return PlannerPlan.model_validate(payload)


def make_planner_node(
    *,
    provider: LLMProvider,
    max_attempts: int,
) -> Callable[[WorkflowState], dict[str, Any]]:
    """Build the ``planner`` graph node bound to *provider*.

    The returned callable is a LangGraph node: it takes the running state and
    returns a partial state update. All LLM access goes through the injected
    ``LLMProvider``, so tests pass a fake and no live model is required.
    """

    def planner_node(state: WorkflowState) -> dict[str, Any]:
        # The Planner only ever asks for structured data. It must not be able
        # to produce a mesh: there is no mesh concept here at all.
        prompt = reference_prompt_for(provider, state)
        try:
            # The optional reference image (terv.md 27.) is attached here; the
            # helper falls back to a text-only call and never fails the run
            # because of vision.
            raw, image_used, image_warning = structured_with_reference_image(
                provider,
                prompt,
                PlannerPlan,
                system=PLANNER_SYSTEM_PROMPT,
                images=reference_images(state),
            )
            plan = coerce_plan(raw)
        except (LLMError, ValidationError) as exc:
            return failure_state(
                state,
                stage="planner",
                error_type=type(exc).__name__,
                message=str(exc),
            )

        history = append_history(state, "planner: specification ready")
        if image_warning:
            history = [*history, f"planner: {image_warning}"]
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

    return planner_node
