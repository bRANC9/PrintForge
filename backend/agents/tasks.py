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
   :func:`agents.services.fail_run` and returns the run id;
5. dispatches in-app notifications through ``notifications.services``
   (best-effort: a notification problem never changes the run outcome).

When the caller supplies ``version_id`` (terv.md 27. fejezet), the task loads
the version's optional ``reference_image`` / ``reference_note`` and seeds the
graph state with them, so the Planner/Research agents can use the photo as a
reference. The raw bytes never reach ``AgentRun.state_json``: only the
``reference_image_used`` flag, the presence flag and the optional warning are
summarised. Without ``version_id`` the task behaves exactly as before.

The workflow dependencies (LLM provider + CAD backend + RAG ``retrieve``) are
built by :func:`build_dependencies`, which tests monkeypatch to inject fakes --
no live Ollama, OpenSCAD or pgvector is ever required.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from accounts.models import User
from agents.graph import WorkflowDeps, run_workflow
from agents.llm import get_provider
from agents.services import fail_run, finish_run, start_run
from designs.cad.openscad import OpenSCADBackend
from designs.models import ModelVersion, model_artifact_path
from designs.services import create_next_version
from embeddings.services import retrieve
from files.services import get_storage
from notifications.services import NotificationKind, notify_project_members
from projects.models import Project

__all__ = ["build_dependencies", "load_reference_image", "run_agent_workflow"]

logger = logging.getLogger(__name__)

SCAD_FILENAME = "model.scad"
STL_FILENAME = "model.stl"


def build_dependencies() -> WorkflowDeps:
    """Build the production workflow dependencies.

    This is the single injection seam: tests monkeypatch ``build_dependencies``
    to return a :class:`~agents.graph.WorkflowDeps` with a fake provider and a
    fake CAD backend.

    The Research node receives the real RAG entry point
    (:func:`embeddings.services.retrieve`) explicitly. That function is guarded
    by ``settings.RAG_ENABLED`` and by ``DB_IS_POSTGRES``: when RAG is disabled
    it returns ``[]`` without touching a model or the database, and the Research
    node keeps the Planner's specification untouched.
    """
    return WorkflowDeps(
        provider=get_provider(),
        cad_backend=OpenSCADBackend(),
        retrieve_fn=retrieve,
        max_attempts=int(getattr(settings, "AGENT_MAX_ATTEMPTS", 3)),
    )


def load_reference_image(version: ModelVersion | None) -> bytes | None:
    """Return the raw bytes of *version*'s optional reference image.

    The image path lives on ``ModelVersion.reference_image`` (terv.md 27.
    fejezet); the bytes are read through the configured storage backend
    (:func:`files.services.get_storage`) rather than Django's default storage,
    matching how the CAD artifacts are written. Missing image, missing version
    or any storage error all return ``None``: a reference photo must never
    block a run.
    """
    if version is None:
        return None
    name = getattr(getattr(version, "reference_image", None), "name", "") or ""
    if not name:
        return None
    try:
        return get_storage().read_bytes(name)
    except Exception:  # noqa: BLE001 - a broken reference image must not fail the run
        logger.warning(
            "Could not read reference image %r for ModelVersion %s",
            name,
            version.pk,
            exc_info=True,
        )
        return None


def _autofill_project_metadata(project: Project) -> None:
    """Best-effort metadata autofill after a version completes (terv.md 29.).

    ``projects.services.maybe_autofill_project_metadata`` is owned by another
    module and is documented as never raising, but this task still guards both
    the import and the call so it can never change the run outcome.

    The call is **opt-in** through ``settings.AGENT_AUTOFILL_METADATA`` (default
    ``False``): the service resolves its own LLM provider, so enabling it by
    default would introduce a live LLM call into the agent tests (whose
    ``ProjectFactory`` projects have an empty description). Deployments that
    want it set the flag; the setting itself is owned by ``config/settings.py``.
    """
    if not getattr(settings, "AGENT_AUTOFILL_METADATA", False):
        return
    try:
        from projects.services import maybe_autofill_project_metadata
    except Exception:  # noqa: BLE001 - an unavailable service is not an error here
        logger.warning("projects metadata autofill unavailable", exc_info=True)
        return
    try:
        maybe_autofill_project_metadata(project)
    except Exception:  # noqa: BLE001 - autofill must never block the run
        logger.warning("Metadata autofill failed for project %s", project.pk, exc_info=True)


def _failure_reason(error: str) -> str:
    """Extract a short, human-readable reason from a structured error string."""
    try:
        payload = json.loads(error)
    except (TypeError, ValueError):
        return (error or "unknown error")[:200]
    reason = payload.get("message") if isinstance(payload, dict) else None
    return str(reason or "unknown error")[:200]


def _notify_project(
    *,
    project: Project,
    kind: Any,
    message: str,
    url: str,
    exclude: User | None,
) -> None:
    """Best-effort notification fan-out.

    ``notify_project_members`` is itself documented as best-effort, but the
    agent workflow must never depend on that: problems here are swallowed and
    only logged, so they can never change the run outcome.
    """
    try:
        notify_project_members(
            project=project,
            kind=kind,
            message=message,
            url=url,
            exclude=exclude,
        )
    except Exception:  # noqa: BLE001 - notifications must never break the workflow
        logger.warning(
            "Could not dispatch %s notification for project %s",
            kind,
            project.pk,
            exc_info=True,
        )


def _fail_and_notify(*, run: Any, project: Project, user: User | None, error: str) -> int:
    """Mark the run failed, notify the project members and return the run id."""
    fail_run(run=run, error=error)
    _notify_project(
        project=project,
        kind=NotificationKind.AGENT_FAILED,
        message=f"Model generation failed: {_failure_reason(error)}",
        url=f"/projects/{project.pk}/",
        exclude=user,
    )
    return run.pk


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
        # Reference image provenance (terv.md 27.): only presence/usage flags,
        # never the raw bytes.
        "reference_image_provided": bool(state.get("reference_image")),
        "reference_image_used": bool(state.get("reference_image_used", False)),
        "reference_image_note": str(state.get("reference_image_note") or ""),
        "reference_image_warning": state.get("reference_image_warning"),
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
    reference_image_name: str = "",
) -> Any:
    """Create the next ``ModelVersion`` and store the CAD artifacts on it.

    The artifacts come from the graph state (OpenSCAD source + exported STL);
    the STL is what the Three.js viewer loads, so this is the "preview" step of
    terv.md 6. fejezet.

    The optional reference image/note (terv.md 27.) is copied onto the new
    version for reproducibility when the run was seeded from a version.
    """
    reference_image = state.get("reference_image")
    reference_note = str(state.get("reference_image_note") or "")
    reference_file = None
    if reference_image:
        file_name = os.path.basename(reference_image_name) or "reference_image"
        reference_file = ContentFile(reference_image, name=file_name)
    version = create_next_version(
        project=project,
        prompt=prompt,
        created_by=user,
        specification=dict(state.get("specification") or {}),
        reference_note=reference_note,
        reference_image=reference_file,
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
        "reference_image_used": bool(state.get("reference_image_used", False)),
        "reference_image_provided": bool(state.get("reference_image")),
        "scad_file": scad_rel,
        "stl_file": stl_rel,
        "stl_bytes": len(stl_bytes),
        "completed_at": timezone.now().isoformat(),
    }
    version.save(update_fields=["scad_file", "stl_file", "validation_json"])
    return version


@shared_task(name="agents.run_agent_workflow")
def run_agent_workflow(
    project_id: int,
    prompt: str,
    user_id: int | None = None,
    version_id: int | None = None,
) -> int:
    """Run the full agent workflow for ``project_id`` and return the ``AgentRun`` id.

    The run row is created first (``start_run``) and always finished by either
    ``finish_run`` or ``fail_run``; a workflow-level failure never leaves the
    run stuck in ``RUNNING``.

    ``version_id`` is optional (terv.md 27. fejezet): when given, that version's
    ``reference_image``/``reference_note`` seed the graph state so the agents can
    use the photo as a reference. It must belong to ``project_id``; a missing or
    foreign version is simply ignored (text-only run).
    """
    project = Project.objects.get(pk=project_id)
    user = User.objects.filter(pk=user_id).first() if user_id else None
    run = start_run(project=project, user_prompt=prompt)

    version = (
        ModelVersion.objects.filter(pk=version_id, project=project).first() if version_id else None
    )
    reference_image = load_reference_image(version)
    reference_image_note = str(getattr(version, "reference_note", "") or "")
    reference_image_name = str(getattr(getattr(version, "reference_image", None), "name", "") or "")

    try:
        state = run_workflow(
            prompt,
            deps=build_dependencies(),
            company_id=str(project.workspace_id),
            reference_image=reference_image,
            reference_image_note=reference_image_note,
        )
    except Exception as exc:  # noqa: BLE001 - never leave a run RUNNING
        logger.exception("Agent workflow crashed for AgentRun %s", run.pk)
        return _fail_and_notify(
            run=run,
            project=project,
            user=user,
            error=_workflow_error(
                stage="workflow",
                error_type=type(exc).__name__,
                message=str(exc),
            ),
        )

    if state.get("status") == "done" and state.get("scad_source") and state.get("stl_bytes"):
        try:
            version = _persist_version(
                project=project,
                prompt=prompt,
                user=user,
                run=run,
                state=state,
                reference_image_name=reference_image_name,
            )
        except Exception as exc:  # noqa: BLE001 - storage/DB failure must fail the run
            logger.exception("Could not persist ModelVersion for AgentRun %s", run.pk)
            return _fail_and_notify(
                run=run,
                project=project,
                user=user,
                error=_workflow_error(
                    stage="persist",
                    error_type=type(exc).__name__,
                    message=str(exc),
                ),
            )

        summary = _summarise_state(state)
        summary["version_id"] = version.pk
        summary["version"] = version.version
        finish_run(run=run, state=summary)
        logger.info("AgentRun %s produced ModelVersion %s", run.pk, version.pk)
        # Best-effort, opt-in project metadata autofill (terv.md 29.); never
        # blocks the run and is disabled by default so tests stay offline.
        _autofill_project_metadata(project)
        # Only after the version + artifacts are stored and the run is DONE.
        _notify_project(
            project=project,
            kind=NotificationKind.AGENT_DONE,
            message=f'Model v{version.version} generated for "{project.name}".',
            url=f"/projects/{project_id}/",
            exclude=user,
        )
        return run.pk

    return _fail_and_notify(
        run=run,
        project=project,
        user=user,
        error=state.get("error")
        or _workflow_error(
            stage="workflow",
            error_type="WorkflowError",
            message="The workflow finished without a valid model.",
        ),
    )
