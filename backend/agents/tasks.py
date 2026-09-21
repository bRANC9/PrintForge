"""Celery entrypoint for the agent workflow (terv.md 6., 9. fejezet).

The Django request cycle never runs the agent workflow or a CAD binary itself;
it only enqueues :func:`run_agent_workflow`. The task:

1. opens an ``AgentRun`` through :func:`agents.services.start_run`;
2. runs the LangGraph workflow (Planner -> [Research] -> Specification ->
   CAD (OpenSCAD source) -> Validator -> STL);
3. on success stores a new ``ModelVersion`` through
   :func:`designs.services.create_next_version`, writes the CAD artifacts to
   the storage backend and marks the run done through
   :func:`agents.services.finish_run`;
4. on a bounded/terminal failure marks the run failed through
   :func:`agents.services.fail_run` and returns the run id.

The workflow dependencies (LLM provider + CAD backend) are built by
:func:`build_dependencies`, which tests monkeypatch to inject fakes -- no live
Ollama or OpenSCAD is ever required.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from accounts.models import User
from agents.graph import WorkflowDeps, run_workflow
from agents.llm import get_provider
from agents.services import fail_run, finish_run, start_run
from designs.cad.openscad import OpenSCADBackend
from designs.models import model_artifact_path
from designs.services import create_next_version
from files.services import get_storage
from projects.models import Project

__all__ = ["build_dependencies", "run_agent_workflow"]

logger = logging.getLogger(__name__)

SCAD_FILENAME = "model.scad"
STL_FILENAME = "model.stl"


def build_dependencies() -> WorkflowDeps:
    """Build the production workflow dependencies.

    This is the single injection seam: tests monkeypatch ``build_dependencies``
    to return a :class:`~agents.graph.WorkflowDeps` with a fake provider and a
    fake CAD backend.
    """
    return WorkflowDeps(
        provider=get_provider(),
        cad_backend=OpenSCADBackend(),
        max_attempts=int(getattr(settings, "AGENT_MAX_ATTEMPTS", 3)),
    )


def _workflow_error(
    *, stage: str, error_type: str, message: str, details: list[str] | None = None
) -> str:
    """Structured error string persisted on ``AgentRun.error``."""
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


def _summarise_state(state: dict[str, Any]) -> dict[str, Any]:
    """Reduce the in-memory state to a JSON-serialisable ``AgentRun`` summary.

    The raw ``stl_bytes`` are deliberately not persisted on the run: only their
    size is kept (the artifact itself is written to the storage backend).
    """
    scad_source = str(state.get("scad_source") or "")
    stl_bytes = state.get("stl_bytes") or b""
    return {
        "status": state.get("status"),
        "attempts": int(state.get("attempt", 0)),
        "max_attempts": int(state.get("max_attempts", 0)),
        "needs_research": bool(state.get("needs_research", False)),
        "research_used": bool(state.get("research_used", False)),
        "research_sources": list(state.get("research_sources") or []),
        "validation": dict(state.get("validation") or {}),
        "scad_chars": len(scad_source),
        "stl_bytes": len(stl_bytes),
        "history": list(state.get("history") or []),
    }


def _persist_version(
    *,
    project: Project,
    prompt: str,
    user: User | None,
    run: Any,
    state: dict[str, Any],
) -> Any:
    """Create the next ``ModelVersion`` and store the CAD artifacts on it.

    The artifacts come from the graph state (OpenSCAD source + exported STL);
    the STL is what the Three.js viewer loads, so this is the "preview" step of
    terv.md 6. fejezet.
    """
    version = create_next_version(
        project=project,
        prompt=prompt,
        created_by=user,
        specification=dict(state.get("specification") or {}),
    )

    storage = get_storage()
    scad_rel = model_artifact_path(version, SCAD_FILENAME)
    stl_rel = model_artifact_path(version, STL_FILENAME)
    scad_source = str(state.get("scad_source") or "")
    stl_bytes = state.get("stl_bytes") or b""
    storage.write_bytes(scad_rel, scad_source.encode("utf-8"))
    storage.write_bytes(stl_rel, stl_bytes)

    version.scad_file.name = scad_rel
    version.stl_file.name = stl_rel
    version.validation_json = {
        "status": "done",
        "stage": "done",
        "errors": [],
        "attempts": int(state.get("attempt", 0)),
        "agent_run_id": run.pk,
        "research_used": bool(state.get("research_used", False)),
        "research_sources": list(state.get("research_sources") or []),
        "scad_file": scad_rel,
        "stl_file": stl_rel,
        "stl_bytes": len(stl_bytes),
        "completed_at": timezone.now().isoformat(),
    }
    version.save(update_fields=["scad_file", "stl_file", "validation_json"])
    return version


@shared_task(name="agents.run_agent_workflow")
def run_agent_workflow(project_id: int, prompt: str, user_id: int | None = None) -> int:
    """Run the full agent workflow for ``project_id`` and return the ``AgentRun`` id.

    The run row is created first (``start_run``) and always finished by either
    ``finish_run`` or ``fail_run``; a workflow-level failure never leaves the
    run stuck in ``RUNNING``.
    """
    project = Project.objects.get(pk=project_id)
    user = User.objects.filter(pk=user_id).first() if user_id else None
    run = start_run(project=project, user_prompt=prompt)

    try:
        state = run_workflow(
            prompt,
            deps=build_dependencies(),
            company_id=str(project.workspace_id),
        )
    except Exception as exc:  # noqa: BLE001 - never leave a run RUNNING
        logger.exception("Agent workflow crashed for AgentRun %s", run.pk)
        fail_run(
            run=run,
            error=_workflow_error(
                stage="workflow",
                error_type=type(exc).__name__,
                message=str(exc),
            ),
        )
        return run.pk

    if state.get("status") == "done" and state.get("scad_source") and state.get("stl_bytes"):
        try:
            version = _persist_version(
                project=project,
                prompt=prompt,
                user=user,
                run=run,
                state=state,
            )
        except Exception as exc:  # noqa: BLE001 - storage/DB failure must fail the run
            logger.exception("Could not persist ModelVersion for AgentRun %s", run.pk)
            fail_run(
                run=run,
                error=_workflow_error(
                    stage="persist",
                    error_type=type(exc).__name__,
                    message=str(exc),
                ),
            )
            return run.pk

        summary = _summarise_state(state)
        summary["version_id"] = version.pk
        summary["version"] = version.version
        finish_run(run=run, state=summary)
        logger.info("AgentRun %s produced ModelVersion %s", run.pk, version.pk)
        return run.pk

    fail_run(
        run=run,
        error=state.get("error")
        or _workflow_error(
            stage="workflow",
            error_type="WorkflowError",
            message="The workflow finished without a valid model.",
        ),
    )
    return run.pk
