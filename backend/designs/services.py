"""Business logic for design versions.

The API views and the MCP tools both go through these functions; nothing here
may depend on DRF or an HTTP request. Render jobs are *only* enqueued to Celery
(terv.md 9. fejezet) -- the web process never shells out to a CAD binary.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from files.services import get_storage
from projects.models import Project

from .models import ModelVersion, ModelVersionOrigin
from .tasks import render_model_stl

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from agents.models import AgentRun

__all__ = [
    "ARTIFACT_CONTENT_TYPES",
    "ARTIFACT_KINDS",
    "ClarificationError",
    "RenderEnqueueError",
    "answer_clarifications",
    "artifact_path",
    "create_annotation_edit",
    "create_next_version",
    "latest_version",
    "read_artifact",
    "regenerate_version",
    "start_render",
    "version_status",
    "versions_accessible_to",
    "versions_for_project",
]

#: Artifact kinds exposed by the API and the storage layout they map to.
ARTIFACT_KINDS = ("scad", "stl", "preview")
_ARTIFACT_FIELDS = {"scad": "scad_file", "stl": "stl_file", "preview": "preview_image"}
ARTIFACT_CONTENT_TYPES = {
    "scad": "text/plain; charset=utf-8",
    "stl": "model/stl",
    "preview": "image/png",
}


class RenderEnqueueError(RuntimeError):
    """The render job could not be handed to the background queue.

    Raised when the Celery broker is unreachable so the API can answer with a
    ``503 Service Unavailable`` instead of leaking a 500 (see terv.md 9.).
    """


class ClarificationError(RuntimeError):
    """A clarification run cannot be answered.

    Raised when the target :class:`~agents.models.AgentRun` is not actually
    waiting for user input (no ``status == "clarification"`` / no persisted
    ``clarifications``) or the supplied answers cannot form a usable follow-up
    prompt. The API maps it to ``409 Conflict`` (docs/planner-clarification.md
    4-5.).
    """


@transaction.atomic
def create_next_version(
    *,
    project: Project,
    prompt: str = "",
    created_by: User | None = None,
    specification: dict | None = None,
    reference_note: str = "",
    reference_image=None,
    parent_version: ModelVersion | None = None,
    origin: str = ModelVersionOrigin.GENERATE,
) -> ModelVersion:
    """Create the next version for a project (v1, v2, ...).

    ``reference_note``/``reference_image`` are the optional reference photo and
    note attached to the prompt (terv.md 27. fejezet); both are stored on the
    version so the generation stays reproducible.

    ``parent_version``/``origin`` record the provenance of a *derived* version
    (regeneration or manual specification, docs/version-history-controls.md):
    the source version and how this one came to be. A fresh generation leaves
    ``parent_version`` empty and ``origin`` at ``"generate"``.
    """
    last = project.versions.order_by("-version").first()
    next_version = (last.version + 1) if last else 1
    return ModelVersion.objects.create(
        project=project,
        version=next_version,
        prompt=prompt,
        specification_json=specification or {},
        created_by=created_by,
        reference_note=reference_note or "",
        reference_image=reference_image,
        parent_version=parent_version,
        origin=origin,
    )


def create_annotation_edit(
    *,
    base_version: ModelVersion,
    prompt: str,
    annotations: list[dict[str, Any]],
    created_by: User | None = None,
) -> None:
    """Enqueue the visual-prompt edit workflow for ``base_version``.

    Unlike :func:`create_next_version`, this does **not** create a
    ``ModelVersion``: the agent task creates the derived version and records
    ``parent_version`` / ``annotations_json`` on it itself, avoiding a duplicate
    placeholder row (docs/visual-editing.md 3.4). The UI polls the project's
    version list and waits for the version whose ``parent_version`` is the base.

    Raises :class:`RenderEnqueueError` when the Celery broker is unavailable so
    the API can translate it into a retryable ``503``.
    """
    # Imported lazily: ``agents.tasks`` imports ``designs.services`` at module
    # load time, so a top-level import would create an import cycle.
    from agents.tasks import run_agent_workflow

    try:
        run_agent_workflow.delay(
            project_id=base_version.project_id,
            prompt=prompt,
            user_id=created_by.pk if created_by else None,
            base_version_id=base_version.pk,
            annotations=list(annotations),
        )
    except Exception as exc:  # noqa: BLE001 - the broker can fail in many ways
        message = (
            "The edit queue is unavailable, the model could not be queued. Please try again later."
        )
        raise RenderEnqueueError(message) from exc


def _agent_workflow_accepts_regenerate() -> bool:
    """Whether ``agents.tasks.run_agent_workflow`` declares a ``regenerate`` kwarg.

    The ``regenerate=True`` parameter is being added by the held
    agent-orchestrator workstream (docs/version-history-controls.md 2.). Until it
    lands, passing the kwarg would raise ``TypeError`` *inside the worker* (the
    ``.delay()`` call itself only serialises), so the capability is probed from
    the task's underlying ``run`` function instead of blindly forwarded.

    ``run_agent_workflow`` is a Celery ``Task`` instance, whose ``__call__`` is
    ``(*args, **kwargs)``; inspecting ``.run`` reaches the real signature.
    """
    from agents.tasks import run_agent_workflow

    target = getattr(run_agent_workflow, "run", run_agent_workflow)
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables only
        return False
    if "regenerate" in parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def regenerate_version(
    *,
    base_version: ModelVersion,
    prompt: str | None = None,
    specification: dict | None = None,
    created_by: User | None = None,
) -> None:
    """Regenerate (or edit) ``base_version`` into a new, derived version.

    Two paths (docs/version-history-controls.md 2.):

    * ``specification is None`` -- enqueue the agent workflow with the base
      prompt (or the caller's edited ``prompt``) and ``base_version_id``. The
      task creates the derived version itself and records
      ``parent_version=base_version`` / ``origin="regenerate"``, so no
      placeholder row is created here.
    * ``specification`` given -- render that specification directly as a
      ``manual`` derived version, reusing the manual render path
      (:func:`create_next_version` + :func:`start_render`).

    Raises :class:`RenderEnqueueError` when the Celery broker is unavailable so
    the API can translate it into a retryable ``503``.
    """
    effective_prompt = prompt or base_version.prompt

    if specification is not None:
        version = create_next_version(
            project=base_version.project,
            prompt=effective_prompt,
            specification=specification,
            created_by=created_by,
            parent_version=base_version,
            origin=ModelVersionOrigin.MANUAL,
        )
        start_render(version)
        return

    # Imported lazily: ``agents.tasks`` imports ``designs.services`` at module
    # load time, so a top-level import would create an import cycle.
    from agents.tasks import run_agent_workflow

    kwargs: dict[str, Any] = {
        "project_id": base_version.project_id,
        "prompt": effective_prompt,
        "user_id": created_by.pk if created_by else None,
        "base_version_id": base_version.pk,
    }
    if _agent_workflow_accepts_regenerate():
        kwargs["regenerate"] = True
    # TODO(agent-orchestrator): drop the capability probe once
    # ``run_agent_workflow`` declares ``regenerate``. Until then the task
    # derives the same provenance from ``base_version_id`` + empty annotations.
    try:
        run_agent_workflow.delay(**kwargs)
    except Exception as exc:  # noqa: BLE001 - the broker can fail in many ways
        message = (
            "The regeneration queue is unavailable, the model could not be queued. "
            "Please try again later."
        )
        raise RenderEnqueueError(message) from exc


#: Hard bound on how many clarification answers are folded into the follow-up
#: prompt; extra entries are dropped deterministically (docs/planner-clarification.md 4.).
_MAX_CLARIFICATION_ANSWERS = 16
#: Length caps mirroring ``agents.spec`` so the augmented prompt stays bounded.
_MAX_CLARIFICATION_ANSWER_CHARS = 600
_MAX_CLARIFICATION_FIELD_CHARS = 120
_MAX_CLARIFICATION_QUESTION_CHARS = 300
#: Cap on the original prompt copied into the follow-up run.
_MAX_CLARIFICATION_PROMPT_CHARS = 8000

#: Heading of the deterministic answer block appended to the original prompt.
_CLARIFICATION_ANSWERS_HEADING = "Felhasználói válaszok:"


def _clarification_answer_block(
    answers: list[dict[str, Any]],
    clarifications: list[dict[str, Any]],
) -> str:
    """Build the bounded, deterministic „Felhasználói válaszok” block.

    Each answer is paired with the question the Planner asked for the same
    ``field`` (when it is known), so the follow-up run sees *what* was answered,
    not just the raw value. Order follows the request payload; every string is
    length-bounded and the block is capped at
    :data:`_MAX_CLARIFICATION_ANSWERS` entries. Returns ``""`` when no usable
    answer remains (the caller turns that into :class:`ClarificationError`).
    """
    questions: dict[str, str] = {}
    for item in clarifications:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        if field:
            questions[field] = str(item.get("question") or "").strip()

    lines: list[str] = []
    for index, item in enumerate(list(answers or [])[:_MAX_CLARIFICATION_ANSWERS], start=1):
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()[:_MAX_CLARIFICATION_FIELD_CHARS]
        answer = str(item.get("answer") or "").strip()[:_MAX_CLARIFICATION_ANSWER_CHARS]
        if not answer:
            continue
        question = questions.get(field, "")[:_MAX_CLARIFICATION_QUESTION_CHARS]
        label = question or field or f"kerdes {index}"
        lines.append(f"- {label}: {answer}")
    if not lines:
        return ""
    return "\n".join([_CLARIFICATION_ANSWERS_HEADING, *lines])


def _build_clarification_prompt(
    original_prompt: str,
    answers: list[dict[str, Any]],
    clarifications: list[dict[str, Any]],
) -> str:
    """Return the original prompt with the answers block appended.

    Raises :class:`ClarificationError` when the answers contain no usable reply
    (all blank / non-dict), so the caller never enqueues an answer-less run.
    """
    base = str(original_prompt or "").strip()[:_MAX_CLARIFICATION_PROMPT_CHARS]
    block = _clarification_answer_block(answers, clarifications)
    if not block:
        raise ClarificationError("The answers did not contain any usable reply.")
    return f"{base}\n\n{block}" if base else block


def answer_clarifications(
    *,
    run: AgentRun,
    answers: list[dict[str, Any]],
    created_by: User | None = None,
) -> dict[str, Any]:
    """Start a follow-up run from the user's answers to a stopped Planner run.

    The Planner run that stopped with ``status == "clarification"`` is
    immutable (docs/planner-clarification.md 3-4.): answering it enqueues a
    **new** agent run whose prompt is the original prompt augmented with a
    deterministic, bounded „Felhasználói válaszok” block, and forces
    ``clarify_policy="assume"`` so the follow-up actually generates a model.
    The original ``clarifications``/``assumptions`` live in
    ``run.state_json`` (see ``agents.tasks._summarise_state``).

    Returns ``{"queued": True, "parent_run": run.pk}``.

    Raises :class:`ClarificationError` when the run has no pending questions (or
    the answers are unusable) and :class:`RenderEnqueueError` when the Celery
    broker is unavailable, so the API can answer ``409`` / ``503`` respectively.
    """
    state = dict(run.state_json or {})
    clarifications = list(state.get("clarifications") or [])
    if state.get("status") != "clarification" or not clarifications:
        raise ClarificationError("This run is not waiting for clarification answers.")

    # The prompt is stored on the run row; ``state_json`` may also carry it, so
    # prefer the explicit state value and fall back to the model field.
    original_prompt = str(state.get("prompt") or run.user_prompt or "")
    prompt = _build_clarification_prompt(original_prompt, list(answers or []), clarifications)

    # Imported lazily: ``agents.tasks`` imports ``designs.services`` at module
    # load time, so a top-level import would create an import cycle.
    from agents.tasks import run_agent_workflow

    try:
        run_agent_workflow.delay(
            project_id=run.project_id,
            prompt=prompt,
            user_id=created_by.pk if created_by else None,
            clarify_policy="assume",
        )
    except Exception as exc:  # noqa: BLE001 - the broker can fail in many ways
        message = (
            "The clarification queue is unavailable, the follow-up model could not be "
            "queued. Please try again later."
        )
        raise RenderEnqueueError(message) from exc
    return {"queued": True, "parent_run": run.pk}


def latest_version(project: Project) -> ModelVersion | None:
    return project.versions.order_by("-version").first()


def versions_for_project(project: Project):
    """All versions of a project, newest first."""
    return project.versions.select_related("created_by").order_by("-version")


def versions_accessible_to(user):
    """Versions in the workspaces ``user`` is a member of (Phase 7 scoping)."""
    return ModelVersion.objects.filter(project__workspace__members__user=user).distinct()


def version_status(version: ModelVersion) -> dict[str, Any]:
    """Derive the job status the UI polls from ``validation_json``."""
    data = dict(version.validation_json or {})
    return {
        "status": data.get("status") or "pending",
        "stage": data.get("stage") or "pending",
        "errors": list(data.get("errors") or []),
    }


def _persist_validation(version: ModelVersion, fields: dict[str, Any]) -> ModelVersion:
    """Merge ``fields`` into ``validation_json`` and persist it."""
    current = dict(version.validation_json or {})
    current.update(fields)
    version.validation_json = current
    version.save(update_fields=["validation_json"])
    return version


def start_render(version: ModelVersion, *, task: Any | None = None) -> ModelVersion:
    """Mark ``version`` queued and enqueue the render task.

    Returns the updated version. If the broker is unavailable the version is
    marked ``failed`` and :class:`RenderEnqueueError` is raised so the caller
    can translate it into a 503.
    """
    enqueue = (task or render_model_stl).delay
    _persist_validation(
        version,
        {
            "status": "queued",
            "stage": "queued",
            "errors": [],
            "queued_at": timezone.now().isoformat(),
        },
    )
    try:
        enqueue(version.pk)
    except Exception as exc:  # noqa: BLE001 - the broker can fail in many ways
        message = (
            "The render queue is unavailable, the model could not be queued. "
            "Please try again later."
        )
        _persist_validation(
            version,
            {
                "status": "failed",
                "stage": "enqueue",
                "errors": [f"{message} ({type(exc).__name__}: {exc})"],
                "completed_at": timezone.now().isoformat(),
            },
        )
        raise RenderEnqueueError(message) from exc
    return version


def artifact_path(version: ModelVersion, kind: str) -> str | None:
    """Return the relative storage path of ``kind`` (``scad``/``stl``/``preview``).

    Falls back to the path recorded in ``validation_json`` when the model field
    is empty. Returns ``None`` for unknown kinds or missing artifacts.
    """
    field_name = _ARTIFACT_FIELDS.get(kind)
    if field_name is None:
        return None
    name = getattr(getattr(version, field_name, None), "name", None)
    if not name:
        name = (version.validation_json or {}).get(f"{kind}_file")
    return name or None


def read_artifact(version: ModelVersion, kind: str) -> tuple[str, bytes] | None:
    """Read an artifact through the storage backend.

    Returns ``(relative_path, data)`` or ``None`` when the artifact is missing.
    """
    relative_path = artifact_path(version, kind)
    if not relative_path:
        return None
    storage = get_storage()
    if not storage.exists(relative_path):
        return None
    return relative_path, storage.read_bytes(relative_path)
