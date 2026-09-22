"""LangGraph state machine for the agent workflow (terv.md 6. fejezet).

Graph shape::

    START --(route_entry)--> planner --(needs_research)--> research
                             |                               |
                             | (no research)                 |
                             +-------------------------------+
                             |
                             v
                            cad --(generation failed, attempt < max)--> cad
                             |
                             v
                          validate --(invalid, attempt < max)--> cad
                             |
                             +-- valid --> review --(retry, attempt < max)--> cad
                                           |
                                           +-- END (match / skip / exhausted)

    ``cad`` retries itself while ``CADBackend.generate`` fails and the attempt
    bound has not been reached, carrying the backend error to the reviser through
    ``validation.errors``; once the bound is reached the failure is terminal
    (``END`` with ``status=failed``).

    ``review`` is the optional vision self-check (docs/vision-self-check.md):
    it renders a preview and, when the provider supports vision, asks the model
    to compare it with the request. It skips (never fails) when there is no STL,
    no vision or a review error, and it only routes back to ``cad`` while
    ``attempt < max_attempts``.

    In edit mode (non-empty annotations) ``START`` routes to ``editor`` instead
    of ``planner``; the editor returns the same partial state, so the rest of
    the graph is unchanged (docs/visual-editing.md 3.5).

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
from designs.cad.preview import render_stl_preview

from .cad import SpecReviser, make_cad_node
from .editor import make_editor_node
from .planner import make_planner_node
from .research import make_research_node
from .review import make_review_node
from .state import WorkflowState
from .validator import make_validate_node

__all__ = [
    "WorkflowDeps",
    "build_workflow",
    "route_after_review",
    "route_after_validate",
    "route_entry",
    "run_workflow",
]


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
    #: Renderer used by the vision self-check: ``bytes(stl) -> PNG bytes``.
    #: Defaults to the import-light headless renderer; tests inject a fake so no
    #: real mesh/trimesh/Pillow work is needed.
    preview_renderer: Callable[..., bytes] = render_stl_preview


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------


def route_entry(state: WorkflowState) -> str:
    """Pick the first agent: ``editor`` for a visual-prompt edit, else ``planner``.

    A run enters edit mode only when the annotation payload is non-empty
    (``edit_mode``), so a fresh generation and all pre-existing callers keep the
    exact ``START -> planner`` behaviour (docs/visual-editing.md 3.5).
    """
    return "editor" if state.get("edit_mode") else "planner"


def route_after_planner(state: WorkflowState) -> str:
    """``research`` only when the Planner asked for it, otherwise straight to ``cad``."""
    if state.get("status") == "failed":
        return END
    return "research" if state.get("needs_research") else "cad"


def route_after_research(state: WorkflowState) -> str:
    """Research always hands over to the CAD agent (enrichment is best-effort)."""
    return "cad"


def route_after_cad(state: WorkflowState) -> str:
    """Route a CAD result: retry back into ``cad``, validate, or stop.

    ``cad`` now reports ``status == "retry"`` when ``CADBackend.generate`` raised
    while attempts remain (the error is stored on ``validation.errors`` so the
    next pass runs the reviser); that retry is bounded by the CAD node's own
    ``attempt < max_attempts`` guard -- an exhausted failure stays terminal. A
    generated source goes to the Validator.
    """
    status = state.get("status")
    if status == "failed":
        return END
    if status == "retry":
        return "cad"
    return "validate"


def route_after_validate(state: WorkflowState) -> str:
    """Route a validated model into the vision ``review``, or retry/finish.

    ``validate`` sets ``status == "retry"`` only when the model is invalid *and*
    ``attempt < max_attempts``; once the bound is reached it sets
    ``status == "failed"`` and the workflow ends. A valid model (``"done"``)
    goes through the optional vision self-check before finishing. This is what
    makes the loop bounded -- there is no unconditional back-edge.
    """
    status = state.get("status")
    if status == "done":
        return "review"
    if status == "retry":
        return "cad"
    return END


def route_after_review(state: WorkflowState) -> str:
    """Bounded retry: back to ``cad`` only while the review asked for one.

    The review sets ``status == "retry"`` only when the preview did not match
    *and* ``attempt < max_attempts``; otherwise it keeps ``status == "done"``
    (match, no vision, skipped or exhausted bound), so the workflow ends.
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
        "editor",
        make_editor_node(provider=deps.provider, max_attempts=deps.max_attempts),
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
    graph.add_node(
        "review",
        make_review_node(
            deps.provider,
            render_preview=deps.preview_renderer,
        ),
    )

    graph.add_conditional_edges(
        START,
        route_entry,
        {"planner": "planner", "editor": "editor"},
    )
    graph.add_conditional_edges(
        "planner",
        route_after_planner,
        {"research": "research", "cad": "cad", END: END},
    )
    # The Editor returns the same partial state as the Planner, so it reuses the
    # same routing: [research] -> cad -> validate.
    graph.add_conditional_edges(
        "editor",
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
        {"cad": "cad", "validate": "validate", END: END},
    )
    graph.add_conditional_edges(
        "validate",
        route_after_validate,
        {"review": "review", "cad": "cad", END: END},
    )
    graph.add_conditional_edges(
        "review",
        route_after_review,
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
    base_specification: dict[str, Any] | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> WorkflowState:
    """Run the prompt -> specification -> SCAD -> STL workflow once.

    Args:
        prompt: Raw user request.
        deps: Injected dependencies (provider/CAD backend/RAG).
        company_id: Optional workspace scope for RAG retrieval.
        reference_image: Optional reference photo bytes (terv.md 27. fejezet).
            Kept in-process only and offered to the Editor/Planner/Research LLM
            calls when the provider supports vision; absent -> unchanged
            behaviour.
        reference_image_note: Optional free-text note for the reference photo.
        base_specification: Specification of the version being edited
            (docs/visual-editing.md 3.5); empty/absent for a fresh generation.
        annotations: Visual-prompt annotation payload. A non-empty list switches
            the graph into edit mode (``START -> editor``); ``None``/empty keeps
            ``START -> planner``.

    Returns the final state. A failed run has ``status == "failed"`` and a
    structured ``error`` JSON string; it never raises for a workflow-level
    failure (only programming/DB errors propagate).
    """
    compiled = build_workflow(deps)
    base_specification = dict(base_specification or {})
    annotations = list(annotations or [])
    initial: WorkflowState = {
        "prompt": prompt,
        "company_id": company_id,
        "reference_image": reference_image,
        "reference_image_note": reference_image_note or "",
        "reference_image_used": False,
        "reference_image_warning": None,
        # Visual-prompt edit mode (docs/visual-editing.md 3.5): the annotations
        # decide the entry route; the base spec is what the Editor updates.
        "edit_mode": bool(annotations),
        "base_specification": base_specification,
        "annotations": annotations,
        "attempt": 0,
        "max_attempts": deps.max_attempts,
        "research_sources": [],
        "research_used": False,
        # Vision self-check (docs/vision-self-check.md): the review node fills
        # these in; the empty defaults keep the summary/persistence paths simple.
        "vision_used": False,
        "vision_review": {},
        "status": "pending",
        "error": None,
        "history": [],
    }
    return compiled.invoke(initial)
