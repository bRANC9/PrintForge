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

For a visual-prompt edit (docs/visual-editing.md 3.5) the caller passes
``base_version_id`` + ``annotations``. The base version is loaded only when it
belongs to the project, its ``specification_json`` seeds the Editor, and the new
``ModelVersion`` records ``parent_version`` + ``annotations_json``. Only the
annotation *count* reaches ``AgentRun.state_json`` -- never the raw payload.

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
from agents.graph.reviser import make_llm_reviser
from agents.llm import get_provider
from agents.search import WebSearchBackend
from agents.services import fail_run, finish_run, start_run
from configuration.services import get_setting
from designs.cad.openscad import OpenSCADBackend
from designs.cad.preview import render_stl_preview
from designs.models import ModelVersion, ModelVersionOrigin, model_artifact_path
from designs.services import create_next_version
from embeddings.services import retrieve
from files.services import get_storage
from notifications.services import NotificationKind, notify_project_members
from projects.models import Project

__all__ = [
    "build_dependencies",
    "load_reference_image",
    "load_reference_image_bytes",
    "run_agent_workflow",
]

logger = logging.getLogger(__name__)

SCAD_FILENAME = "model.scad"
STL_FILENAME = "model.stl"
PREVIEW_FILENAME = "preview.png"


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

    The CAD retry hook is the LLM reviser (docs/vision-self-check.md 4.): it
    turns validator/vision errors into a corrected specification, falling back
    to the unchanged one on any failure. The vision self-check gets the real
    headless preview renderer.

    The review self-check can run on a dedicated vision model: the runtime
    setting ``ollama_vision_model`` (DB override -> ``settings.OLLAMA_VISION_MODEL``
    -> env -> default) is resolved through the runtime settings service so a UI
    override applies without a restart. When it is empty the review reuses the
    main provider, preserving the previous behaviour.
    """
    provider = get_provider()
    vision_model = str(get_setting("ollama_vision_model") or "").strip()
    review_provider = get_provider(model=vision_model) if vision_model else None
    return WorkflowDeps(
        provider=provider,
        cad_backend=OpenSCADBackend(),
        retrieve_fn=retrieve,
        max_attempts=int(getattr(settings, "AGENT_MAX_ATTEMPTS", 3)),
        reviser=make_llm_reviser(provider),
        preview_renderer=render_stl_preview,
        review_provider=review_provider,
        # Web search is best-effort: a disabled/unreachable backend returns []
        # without touching the network, so wiring it always is safe.
        search_fn=WebSearchBackend().search,
    )


def load_reference_image_bytes(name: str | None) -> bytes | None:
    """Return the raw bytes stored at ``name``, or ``None`` on any error.

    A reference photo must never block a run: a missing name or any storage
    error is logged and treated as "no image".
    """
    name = (name or "").strip()
    if not name:
        return None
    try:
        return get_storage().read_bytes(name)
    except Exception:  # noqa: BLE001 - a broken reference image must not fail the run
        logger.warning("Could not read reference image %r", name, exc_info=True)
        return None


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
    return load_reference_image_bytes(name)


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


def _vision_review_summary(review: Any) -> dict[str, Any]:
    """Reduce a ``vision_review`` payload to a JSON-safe summary (no bytes).

    The review node stores either a ``ReviewResult`` dump
    (``matches``/``issues``/``summary``) or a ``{"skipped": True, "reason":
    ...}`` marker; both are already byte-free. This helper only keeps the
    documented fields, so a future reviewer cannot leak large/opaque data into
    ``AgentRun.state_json`` or ``ModelVersion.validation_json``.
    """
    if not isinstance(review, dict) or not review:
        return {}
    if review.get("skipped"):
        return {"skipped": True, "reason": review.get("reason")}
    return {
        "matches": bool(review.get("matches")),
        "issues": [str(issue) for issue in review.get("issues") or []],
        "summary": str(review.get("summary") or ""),
    }


def _clarification_summary(items: Any) -> list[dict[str, str]]:
    """Reduce blocking questions to ``{question, field}`` (no assumed answer)."""
    summary: list[dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        summary.append(
            {
                "question": str(item.get("question") or ""),
                "field": str(item.get("field") or ""),
            }
        )
    return summary


def _assumption_summary(items: Any) -> list[dict[str, str]]:
    """Reduce assumptions to ``{field, question, answer}`` for the provenance."""
    summary: list[dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        summary.append(
            {
                "field": str(item.get("field") or ""),
                "question": str(item.get("question") or ""),
                "answer": str(item.get("answer") or ""),
            }
        )
    return summary


def _skill_summary(state: dict[str, Any]) -> list[dict[str, str]]:
    """Reduce the active skills to ``{slug, name, selection}`` (docs/skills.md 7.)."""
    selection = str(state.get("skill_selection") or "")
    summary: list[dict[str, str]] = []
    for skill in state.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        summary.append(
            {
                "slug": str(skill.get("slug") or ""),
                "name": str(skill.get("name") or ""),
                "selection": selection,
            }
        )
    return summary


def _summarise_state(
    state: dict[str, Any],
    *,
    annotations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reduce the in-memory state to a JSON-serialisable ``AgentRun`` summary.

    The raw ``stl_bytes`` are deliberately not persisted on the run: only their
    size is kept (the artifact itself is written to the storage backend). The
    same holds for the rendered ``preview_image`` -- only the vision verdict and
    usage flag are summarised. The visual-prompt payload (docs/visual-editing.md)
    is likewise reduced to its count -- never the raw annotation JSON. Planner
    clarifications/assumptions and the selected skills are kept as their
    documented, byte-free summaries (docs/planner-clarification.md 4.,
    docs/skills.md 7.).
    """
    scad_source = str(state.get("scad_source") or "")
    stl_bytes = state.get("stl_bytes") or b""
    clarifications = _clarification_summary(state.get("clarifications"))
    assumptions = _assumption_summary(state.get("assumptions"))
    return {
        "status": state.get("status"),
        "attempts": int(state.get("attempt", 0)),
        "max_attempts": int(state.get("max_attempts", 0)),
        "needs_research": bool(state.get("needs_research", False)),
        "research_used": bool(state.get("research_used", False)),
        "research_web_used": bool(state.get("research_web_used", False)),
        "research_sources": list(state.get("research_sources") or []),
        # Reference image provenance (terv.md 27.): only presence/usage flags,
        # never the raw bytes.
        "reference_image_provided": bool(state.get("reference_image")),
        "reference_image_used": bool(state.get("reference_image_used", False)),
        "reference_image_note": str(state.get("reference_image_note") or ""),
        "reference_image_warning": state.get("reference_image_warning"),
        # Visual-prompt provenance (docs/visual-editing.md): count only.
        "annotation_count": len(annotations or []),
        # Planner clarification / assumption provenance (docs/planner-clarification.md 4.).
        "clarification_count": len(clarifications),
        "clarifications": clarifications,
        "assumption_count": len(assumptions),
        "assumptions": assumptions,
        # Skill provenance (docs/skills.md 7.).
        "skills": _skill_summary(state),
        "skill_selection": str(state.get("skill_selection") or ""),
        # Vision self-check provenance (docs/vision-self-check.md): flags and
        # verdict only, never the preview bytes.
        "vision_used": bool(state.get("vision_used", False)),
        "vision_review": _vision_review_summary(state.get("vision_review")),
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
    parent_version: ModelVersion | None = None,
    annotations: list[dict[str, Any]] | None = None,
    regenerate: bool = False,
) -> Any:
    """Create the next ``ModelVersion`` and store the CAD artifacts on it.

    The artifacts come from the graph state (OpenSCAD source + exported STL);
    the STL is what the Three.js viewer loads, so this is the "preview" step of
    terv.md 6. fejezet.

    The optional reference image/note (terv.md 27.) is copied onto the new
    version for reproducibility when the run was seeded from a version.

    For a visual-prompt edit (docs/visual-editing.md 3.5) the new version also
    records ``parent_version`` (the edited base) and ``annotations_json`` (the
    exact input payload), forming the reproducible edit chain.

    When the vision self-check rendered a preview (docs/vision-self-check.md 5.)
    the PNG is written through the storage backend and linked on
    ``ModelVersion.preview_image``; ``validation_json`` records its path and the
    byte-free ``vision_review`` summary. Runs without a preview are unchanged.
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
        origin=ModelVersionOrigin.REGENERATE if regenerate else ModelVersionOrigin.GENERATE,
    )

    storage = get_storage()
    scad_rel = model_artifact_path(version, SCAD_FILENAME)
    stl_rel = model_artifact_path(version, STL_FILENAME)
    preview_rel = model_artifact_path(version, PREVIEW_FILENAME)
    scad_source = str(state.get("scad_source") or "")
    stl_bytes = state.get("stl_bytes") or b""
    preview_bytes = state.get("preview_image") or b""
    storage.write_bytes(scad_rel, scad_source.encode("utf-8"))
    storage.write_bytes(stl_rel, stl_bytes)

    version.scad_file.name = scad_rel
    version.stl_file.name = stl_rel
    version.parent_version = parent_version
    version.annotations_json = list(annotations or [])
    validation_json = {
        "status": "done",
        "stage": "done",
        "errors": [],
        "attempts": int(state.get("attempt", 0)),
        "agent_run_id": run.pk,
        "research_used": bool(state.get("research_used", False)),
        "research_sources": list(state.get("research_sources") or []),
        "reference_image_used": bool(state.get("reference_image_used", False)),
        "reference_image_provided": bool(state.get("reference_image")),
        "parent_version": parent_version.pk if parent_version is not None else None,
        "annotation_count": len(annotations or []),
        "scad_file": scad_rel,
        "stl_file": stl_rel,
        "stl_bytes": len(stl_bytes),
        # Vision self-check (docs/vision-self-check.md 5.): the path of the
        # rendered preview and the byte-free review verdict.
        "vision_used": bool(state.get("vision_used", False)),
        "vision_review": _vision_review_summary(state.get("vision_review")),
        "completed_at": timezone.now().isoformat(),
    }
    # Planner assumptions (docs/planner-clarification.md 4.): a run that made
    # its own guesses is marked for review and carries them on the version.
    assumptions = _assumption_summary(state.get("assumptions"))
    if assumptions:
        validation_json["assumptions"] = assumptions
        validation_json["review_required"] = True
    # Skill provenance (docs/skills.md 7.): which recipes shaped this version.
    skills = _skill_summary(state)
    if skills:
        validation_json["skills"] = skills
        validation_json["skill_selection"] = str(state.get("skill_selection") or "")
    skill_warnings = [
        str(warning) for warning in (state.get("validation") or {}).get("warnings") or []
    ]
    if skill_warnings:
        validation_json["skill_warnings"] = skill_warnings
    update_fields = [
        "scad_file",
        "stl_file",
        "parent_version",
        "annotations_json",
        "validation_json",
    ]
    if preview_bytes:
        storage.write_bytes(preview_rel, preview_bytes)
        version.preview_image.name = preview_rel
        validation_json["preview_file"] = preview_rel
        update_fields.append("preview_image")
    version.validation_json = validation_json
    version.save(update_fields=update_fields)
    return version


@shared_task(name="agents.run_agent_workflow")
def run_agent_workflow(
    project_id: int,
    prompt: str,
    user_id: int | None = None,
    version_id: int | None = None,
    reference_image_name: str = "",
    reference_note: str = "",
    base_version_id: int | None = None,
    annotations: list[dict[str, Any]] | None = None,
    clarify_policy: str = "assume",
    regenerate: bool = False,
    skill_ids: list[int] | None = None,
    auto_skill_selection: bool = False,
) -> int:
    """Run the full agent workflow for ``project_id`` and return the ``AgentRun`` id.

    The run row is created first (``start_run``) and always finished by either
    ``finish_run`` or ``fail_run``; a workflow-level failure never leaves the
    run stuck in ``RUNNING``.

    The reference photo (terv.md 27.) can be supplied two ways:

    * ``version_id`` -- seed from an existing version's
      ``reference_image``/``reference_note`` (the original flow); a missing or
      foreign version is ignored;
    * ``reference_image_name``/``reference_note`` -- a storage path written by
      the caller (the API stores the upload before enqueueing, so no
      placeholder version is needed).

    Either way the image stays optional and never fails the run.

    For a visual-prompt edit (docs/visual-editing.md 3.5) the caller passes
    ``base_version_id`` + ``annotations``: the base version must belong to
    ``project`` (a missing/foreign id is ignored), its ``specification_json`` is
    fed to the Editor as ``base_specification``, and the persisted new version
    records ``parent_version`` + ``annotations_json``.

    ``regenerate=True`` (docs/version-history-controls.md 2.) marks the created
    version ``origin="regenerate"`` with ``parent_version=base_version``; the
    capability is declared so ``designs.services`` can stop probing for it.

    ``clarify_policy="ask"`` lets the Planner stop with
    ``status == "clarification"`` (docs/planner-clarification.md): no
    ``ModelVersion`` is created, the blocking questions are persisted on
    ``AgentRun.state_json`` and the run finishes done so the user can answer and
    start a new run.

    ``skill_ids`` / ``auto_skill_selection`` select the skills for this run
    (docs/skills.md 3.): explicit ids win and mark the selection ``"manual"``,
    otherwise auto selection runs when requested. The chosen skills guide the
    Planner/Editor prompt and are enforced by the Validator; their provenance is
    recorded on the run and the version.
    """
    project = Project.objects.get(pk=project_id)
    user = User.objects.filter(pk=user_id).first() if user_id else None
    run = start_run(project=project, user_prompt=prompt)

    version = (
        ModelVersion.objects.filter(pk=version_id, project=project).first() if version_id else None
    )
    if version is not None:
        reference_image = load_reference_image(version)
        reference_image_note = str(getattr(version, "reference_note", "") or "")
        reference_image_name = str(
            getattr(getattr(version, "reference_image", None), "name", "") or ""
        )
    else:
        reference_image = load_reference_image_bytes(reference_image_name)
        reference_image_note = str(reference_note or "")

    # Visual-prompt edit: load the base version only when it belongs to this
    # project; a missing/foreign id is ignored and the run falls back to a fresh
    # generation so a bad request can never produce an orphan edit.
    base_version = (
        ModelVersion.objects.filter(pk=base_version_id, project=project).first()
        if base_version_id
        else None
    )
    if base_version_id and base_version is None:
        logger.warning(
            "Ignoring base version %s: not found in project %s", base_version_id, project.pk
        )
    edit_annotations = list(annotations or []) if base_version is not None else []
    base_specification = dict(getattr(base_version, "specification_json", None) or {})

    try:
        state = run_workflow(
            prompt,
            deps=build_dependencies(),
            company_id=str(project.workspace_id),
            reference_image=reference_image,
            reference_image_note=reference_image_note,
            base_specification=base_specification,
            annotations=edit_annotations,
            clarify_policy=clarify_policy,
            skill_ids=skill_ids,
            auto_skill_selection=auto_skill_selection,
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

    if state.get("status") == "clarification":
        # The Planner stopped for a missing critical value: no ModelVersion is
        # created (nothing was finalised and no CAD ran). The questions are
        # persisted on the run and the run finishes done, so the UI can collect
        # the answers and start a new run (docs/planner-clarification.md 4.).
        summary = _summarise_state(state, annotations=edit_annotations)
        finish_run(run=run, state=summary)
        logger.info("AgentRun %s stopped for user clarification", run.pk)
        return run.pk

    if state.get("status") == "done" and state.get("scad_source") and state.get("stl_bytes"):
        try:
            version = _persist_version(
                project=project,
                prompt=prompt,
                user=user,
                run=run,
                state=state,
                reference_image_name=reference_image_name,
                parent_version=base_version,
                annotations=edit_annotations,
                regenerate=regenerate,
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

        summary = _summarise_state(state, annotations=edit_annotations)
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
