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

__all__ = [
    "PLANNER_SYSTEM_PROMPT",
    "PlannerPlan",
    "coerce_plan",
    "make_planner_node",
]

PLANNER_SYSTEM_PROMPT = (
    "You are the Planner agent of an OpenSCAD 3D-printing workflow. "
    "Read the user's request and return ONE JSON object with two jobs: "
    "(1) 'specification' - the structured model specification (object, "
    "dimensions in millimetres, angle, wall_thickness, mounting, material); "
    "(2) 'needs_research' and 'research_query' - whether real-world product or "
    "standard-part data (e.g. exact phone dimensions, ISO screw sizes) must be "
    "looked up first, and a concise lookup query if so. "
    "Use sensible printable defaults when a value is missing. "
    "Never emit OpenSCAD code, G-code or STL data - only the structured plan."
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
        prompt = state.get("prompt", "")
        try:
            raw = provider.structured(
                prompt,
                PlannerPlan,
                system=PLANNER_SYSTEM_PROMPT,
            )
            plan = coerce_plan(raw)
        except (LLMError, ValidationError) as exc:
            return failure_state(
                state,
                stage="planner",
                error_type=type(exc).__name__,
                message=str(exc),
            )

        return {
            "specification": plan.specification.model_dump(),
            "needs_research": bool(plan.needs_research),
            "research_query": plan.research_query,
            "research_sources": [],
            "research_used": False,
            "attempt": 0,
            "max_attempts": int(max_attempts),
            "status": "planned",
            "error": None,
            "history": append_history(state, "planner: specification ready"),
        }

    return planner_node
