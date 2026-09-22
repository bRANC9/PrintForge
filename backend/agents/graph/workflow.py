"""LangGraph state machine for the agent workflow (terv.md 6. fejezet).

Graph shape::

    START
      |
      v
    planner --(needs_research)--> research
      |                               |
      | (no research)                 |
      +-------------------------------+
      |
      v
     cad <-----------------+
      |                    |
      v                    | invalid (attempt < max)
   validate ---------------+
      |
      +-- valid --> END
      +-- invalid after max attempts --> END (status=failed)

Every node is built by a factory that receives its dependencies (``LLMProvider``,
``CADBackend``, ``retrieve``) so the whole graph is mockable: tests inject fakes
and never touch Ollama, OpenSCAD or pgvector.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from langgraph.graph import END, START, StateGraph

from agents.llm import LLMProvider
from agents.rag import retrieve
from designs.cad.base import CADBackend

from .cad import SpecReviser, make_cad_node
from .planner import make_planner_node
from .research import make_research_node
from .state import WorkflowState
from .validator import make_validate_node

__all__ = ["WorkflowDeps", "build_workflow", "run_workflow"]


def _default_max_attempts() -> int:
    """Read the retry bound lazily so Django is configured when it is used."""
    return int(getattr(settings, "AGENT_MAX_ATTEMPTS", 3))


@dataclass(frozen=True)
class WorkflowDeps:
    """Injected dependencies of the workflow (the test seam).

    Production builds this in :func:`agents.tasks.build_dependencies` with the
    real Ollama provider and ``OpenSCADBackend``; tests pass fakes.
    """

    provider: LLMProvider
    cad_backend: CADBackend
    #: RAG entry point; ``retrieve(query, limit, company_id) -> list[dict]``.
    retrieve_fn: Callable[..., list[dict[str, Any]]] = retrieve
    #: Bounded retry policy (terv.md 6. fejezet).
    max_attempts: int = field(default_factory=_default_max_attempts)
    #: Maximum number of retrieved chunks handed to the Research agent.
    research_limit: int = 5
    #: Optional CAD retry hook (see :data:`agents.graph.cad.SpecReviser`).
    reviser: SpecReviser | None = None


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------


def route_after_planner(state: WorkflowState) -> str:
    """``research`` only when the Planner asked for it, otherwise straight to ``cad``."""
    if state.get("status") == "failed":
        return END
    return "research" if state.get("needs_research") else "cad"


def route_after_research(state: WorkflowState) -> str:
    """Research always hands over to the CAD agent (enrichment is best-effort)."""
    return "cad"


def route_after_cad(state: WorkflowState) -> str:
    """A CAD generation failure is terminal; a generated source goes to the Validator."""
    return END if state.get("status") == "failed" else "validate"


def route_after_validate(state: WorkflowState) -> str:
    """Bounded retry: back to ``cad`` only while attempts remain.

    ``validate`` sets ``status == "retry"`` only when the model is invalid *and*
    ``attempt < max_attempts``; once the bound is reached it sets
    ``status == "failed"`` and the workflow ends. This is what makes the loop
    bounded -- there is no unconditional back-edge.
    """
    return "cad" if state.get("status") == "retry" else END


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_workflow(deps: WorkflowDeps):
    """Compile the agent workflow with *deps* injected into every node."""
    graph = StateGraph(WorkflowState)

    graph.add_node(
        "planner",
        make_planner_node(provider=deps.provider, max_attempts=deps.max_attempts),
    )
    graph.add_node(
        "research",
        make_research_node(
            provider=deps.provider,
            retrieve_fn=deps.retrieve_fn,
            limit=deps.research_limit,
        ),
    )
    graph.add_node(
        "cad",
        make_cad_node(cad_backend=deps.cad_backend, reviser=deps.reviser),
    )
    graph.add_node(
        "validate",
        make_validate_node(cad_backend=deps.cad_backend),
    )

    graph.add_edge(START, "planner")
    graph.add_conditional_edges(
        "planner",
        route_after_planner,
        {"research": "research", "cad": "cad", END: END},
    )
    graph.add_conditional_edges(
        "research",
        route_after_research,
        {"cad": "cad"},
    )
    graph.add_conditional_edges(
        "cad",
        route_after_cad,
        {"validate": "validate", END: END},
    )
    graph.add_conditional_edges(
        "validate",
        route_after_validate,
        {"cad": "cad", END: END},
    )

    return graph.compile()


def run_workflow(
    prompt: str,
    *,
    deps: WorkflowDeps,
    company_id: str | None = None,
    reference_image: bytes | None = None,
    reference_image_note: str = "",
) -> WorkflowState:
    """Run the prompt -> specification -> SCAD -> STL workflow once.

    Args:
        prompt: Raw user request.
        deps: Injected dependencies (provider/CAD backend/RAG).
        company_id: Optional workspace scope for RAG retrieval.
        reference_image: Optional reference photo bytes (terv.md 27. fejezet).
            Kept in-process only and offered to the Planner/Research LLM calls
            when the provider supports vision; absent -> unchanged behaviour.
        reference_image_note: Optional free-text note for the reference photo.

    Returns the final state. A failed run has ``status == "failed"`` and a
    structured ``error`` JSON string; it never raises for a workflow-level
    failure (only programming/DB errors propagate).
    """
    compiled = build_workflow(deps)
    initial: WorkflowState = {
        "prompt": prompt,
        "company_id": company_id,
        "reference_image": reference_image,
        "reference_image_note": reference_image_note or "",
        "reference_image_used": False,
        "reference_image_warning": None,
        "attempt": 0,
        "max_attempts": deps.max_attempts,
        "research_sources": [],
        "research_used": False,
        "status": "pending",
        "error": None,
        "history": [],
    }
    return compiled.invoke(initial)
