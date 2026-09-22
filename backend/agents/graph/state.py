"""Shared state schema and small helpers for the agent LangGraph workflow.

The graph itself lives in :mod:`agents.graph.workflow`; the nodes are split by
agent (terv.md 6-7. fejezet):

* :mod:`agents.graph.planner`
* :mod:`agents.graph.research`
* :mod:`agents.graph.cad`
* :mod:`agents.graph.validator`

The state schema lives in its own module so the node modules and the graph
builder can import it without an import cycle.

The state is intentionally a plain :class:`~typing.TypedDict`: LangGraph merges
each node's returned partial dict into the running state, so nodes never mutate
their input. ``bytes`` values (the exported STL and the optional reference
photo, terv.md 27. fejezet) stay in-process -- they are never persisted to
``AgentRun.state_json`` as-is (see :mod:`agents.tasks`).
"""

from __future__ import annotations

import json
from typing import Any, TypedDict


class WorkflowState(TypedDict, total=False):
    """Running state of the prompt -> preview workflow (terv.md 6. fejezet)."""

    #: Raw user request that starts the workflow.
    prompt: str
    #: Optional tenant/workspace scope used to filter RAG retrieval.
    company_id: str | None
    #: Optional reference photo bytes (terv.md 27. fejezet). Like ``stl_bytes``
    #: this stays in-process: it is passed to the LLM as ``images=[...]`` but is
    #: never serialised into ``AgentRun.state_json`` (only its presence is).
    reference_image: bytes | None
    #: Optional free-text note attached to the reference photo by the user.
    reference_image_note: str
    #: Whether the reference image was actually accepted by an LLM call. Stays
    #: ``False`` when there is no image or when vision was unavailable/rejected.
    reference_image_used: bool
    #: Why the reference image was *not* used (``None`` when unused/absent).
    reference_image_warning: str | None
    #: Validated ``ModelSpecification`` as a plain dict (the only LLM<->CAD
    #: contract, terv.md 8. fejezet).
    specification: dict[str, Any]
    #: True when the run is a visual-prompt edit (annotations present); START
    #: routes to the Editor instead of the Planner (docs/visual-editing.md 3.5).
    edit_mode: bool
    #: Specification of the version being edited, as a plain validated dict; the
    #: Editor keeps its non-edited values. Empty for a fresh generation.
    base_specification: dict[str, Any]
    #: Visual-prompt payload: one dict per user annotation (point/region + normal
    #: + instruction, docs/visual-editing.md 2). Only structured JSON -- never
    #: raw image bytes -- so it is safe to persist/audit.
    annotations: list[dict[str, Any]]
    #: Whether the Planner asked for the Research agent.
    needs_research: bool
    #: Lookup query the Planner produced for the Research agent.
    research_query: str | None
    #: Retrieved documents / sources kept for provenance (terv.md 7. fejezet).
    research_sources: list[dict[str, Any]]
    #: Whether RAG returned any context during this run.
    research_used: bool
    #: OpenSCAD source produced by the CAD agent (never a mesh).
    scad_source: str
    #: STL bytes produced by the Validator after a successful validation.
    stl_bytes: bytes
    #: Structured validation result (status/errors/attempt).
    validation: dict[str, Any]
    #: 1-based number of CAD generation attempts already made.
    attempt: int
    #: Hard bound on CAD attempts (``settings.AGENT_MAX_ATTEMPTS``).
    max_attempts: int
    #: ``planned`` | ``generated`` | ``retry`` | ``done`` | ``failed``.
    status: str
    #: JSON string with the structured failure (only when ``status == "failed"``).
    error: str | None
    #: Human-readable step trace, handy in ``AgentRun.state_json`` and tests.
    history: list[str]


def structured_error(
    *,
    stage: str,
    error_type: str,
    message: str,
    details: list[str] | None = None,
) -> str:
    """Return a deterministic JSON string describing a workflow failure.

    The workflow never fails with a bare string: every failure carries the
    pipeline *stage*, the exception *type*, a human *message* and optional
    per-issue *details* (terv.md 7. fejezet, Validator Agent).
    """
    return json.dumps(
        {
            "stage": stage,
            "type": error_type,
            "message": message,
            "details": list(details or []),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def append_history(state: WorkflowState, entry: str) -> list[str]:
    """Return a new history list with *entry* appended (states are immutable)."""
    return [*state.get("history", []), entry]


def failure_state(
    state: WorkflowState,
    *,
    stage: str,
    error_type: str,
    message: str,
    details: list[str] | None = None,
) -> dict[str, Any]:
    """Build the partial-state update for a terminal failure at *stage*."""
    return {
        "status": "failed",
        "error": structured_error(
            stage=stage,
            error_type=error_type,
            message=message,
            details=details,
        ),
        "history": append_history(state, f"{stage}: failed ({error_type})"),
    }
