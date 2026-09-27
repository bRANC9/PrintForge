"""Business logic for design versions.

The API views and the MCP tools both go through these functions; nothing here
may depend on DRF or an HTTP request. Render jobs are *only* enqueued to Celery
(terv.md 9. fejezet) -- the web process never shells out to a CAD binary.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from configuration.services import get_setting
from files.services import get_storage
from projects.models import Project

from .cad import meshcheck
from .models import ModelVersion, ModelVersionOrigin, model_artifact_path
from .tasks import render_model_stl

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from agents.models import AgentRun

logger = logging.getLogger(__name__)

__all__ = [
    "ARTIFACT_CONTENT_TYPES",
    "ARTIFACT_KINDS",
    "ClarificationError",
    "MESH_SOURCE_FORMATS",
    "MeshNotPrintableError",
    "RenderEnqueueError",
    "answer_clarifications",
    "artifact_path",
    "attach_source_mesh_to_version",
    "create_annotation_edit",
    "create_next_version",
    "create_next_version_from_mesh",
    "delete_version",
    "latest_version",
    "read_artifact",
    "read_source_mesh",
    "regenerate_version",
    "source_mesh_format",
    "source_mesh_path",
    "source_mesh_warnings",
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

#: Source-mesh extensions the mesh-import CAD backend accepts. This *is* the
#: parser registry (:data:`designs.cad.meshcheck.SUPPORTED_MESH_FORMATS`), not a
#: second copy of it: the import gate below and the mesh readers must accept
#: exactly what :func:`~designs.cad.meshcheck.check_mesh` can parse, and a
#: duplicated list is the one thing that silently breaks that. Every user-facing
#: message about accepted formats is generated from this tuple. It is *not* an
#: artifact kind: the source mesh is a version's **input**, stored on
#: ``ModelVersion.source_mesh`` like ``reference_image``, never a render output.
MESH_SOURCE_FORMATS: tuple[str, ...] = meshcheck.SUPPORTED_MESH_FORMATS


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
        "regenerate": True,
    }
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


# ---------------------------------------------------------------------------
# Mesh import: adopt an externally generated .stl/.obj/.glb as a version's source
# ---------------------------------------------------------------------------
#
# The mesh backend turns geometry it did not create into a print-ready STL. The
# upload is therefore validated *here*, before any row or file exists: a mesh
# that cannot be printed is rejected at upload time (a 400) instead of failing
# in a Celery render minutes later. The non-blocking findings are stored on the
# version so the UI can show them next to the model.


class MeshNotPrintableError(ValueError):
    """The uploaded source mesh cannot be printed as-is.

    Carries the blocking ``problems`` of :func:`designs.cad.meshcheck.check_mesh`
    so the API can answer ``400`` with a readable reason. It is a plain
    :class:`ValueError`, so the generic upload error handling catches it; only
    ``problems`` block -- the report's ``warnings`` are recorded on the version
    instead (see :func:`source_mesh_warnings`).
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("The source mesh is not printable: " + "; ".join(self.problems))


def _max_source_bytes() -> int:
    """Largest accepted source mesh, from the ``mesh_max_source_bytes`` setting.

    Read through :func:`configuration.services.get_setting` (never
    ``django.conf.settings``) so a runtime override applies and the value is
    already type-coerced. A missing or nonsensical value falls back to the
    checker's own default.
    """
    try:
        limit = int(get_setting("mesh_max_source_bytes"))
    except (TypeError, ValueError):
        return meshcheck.DEFAULT_MAX_SOURCE_BYTES
    return limit if limit > 0 else meshcheck.DEFAULT_MAX_SOURCE_BYTES


def _mesh_basename(filename: str) -> str:
    """The last ``/``-separated segment of ``filename`` -- the part that matters.

    Shared by the extension reader and the error message so a rejected name is
    always reported the same way the format is derived from it.
    """
    return str(filename or "").rsplit("/", 1)[-1]


def _mesh_suffix(filename: str) -> str:
    """Lowercased, dot-less suffix of ``filename`` (``""`` when it has none).

    The single place a mesh extension is read from a name, shared by the import
    gate (:func:`_mesh_source_format`) and the read-only projection
    (:func:`source_mesh_format`), so the two can never disagree about what a
    stored ``source.<ext>`` file is.

    Only the base name (:func:`_mesh_basename`) is looked at, so a client that
    sends a full path (``dir/box.stl``, ``C:\\dir\\box.stl``) is judged on its file
    name alone and is not rejected for the separators. That is safe because the
    uploaded name is *never* used as a path: it only selects the parser, and the
    stored name is always the server-generated ``source.<ext>`` of
    :func:`model_artifact_path`.
    """
    return PurePosixPath(_mesh_basename(filename)).suffix.lower().lstrip(".")


def _mesh_format_list() -> str:
    """The accepted extensions as one readable list (``"stl, obj, glb"``).

    Every message that names the accepted formats is built from this, so the text
    a user reads can never drift from the formats :func:`_mesh_source_format`
    actually accepts.
    """
    return ", ".join(MESH_SOURCE_FORMATS)


def _mesh_source_format(filename: str) -> str:
    """Container format of ``filename``, taken from its suffix (case-insensitive).

    The suffix only picks the parser: :func:`~designs.cad.meshcheck.check_mesh`
    is the real gate, so a mislabelled file fails there instead of silently
    producing garbage geometry. A name with no extension at all has no suffix to
    pick from, so it is rejected here like any other unsupported one.
    """
    name = _mesh_basename(filename)
    suffix = _mesh_suffix(filename)
    if suffix not in MESH_SOURCE_FORMATS:
        raise ValueError(
            f"Unsupported source mesh {name or '(unnamed)'!r}; expected one of: "
            f"{_mesh_format_list()}"
        )
    return suffix


def _mesh_payload(mesh_bytes: Any) -> bytes:
    """Coerce an upload to ``bytes`` and enforce the non-empty/size bound."""
    if not isinstance(mesh_bytes, (bytes, bytearray, memoryview)):
        raise ValueError("The source mesh must be bytes.")
    payload = bytes(mesh_bytes)
    if not payload:
        raise ValueError(
            f"The source mesh is empty; expected a non-empty file in one of: {_mesh_format_list()}."
        )
    limit = _max_source_bytes()
    if len(payload) > limit:
        raise ValueError(f"The source mesh is {len(payload)} bytes, above the {limit} byte limit.")
    return payload


def _mesh_specification(
    *,
    source: str,
    scale_mm: float | None,
    rotate_deg: Sequence[float] | None,
    repair: bool | None,
) -> dict[str, Any]:
    """Build the specification :class:`~designs.cad.mesh.MeshCADBackend` reads.

    Only the keys the caller actually passed are written; everything else is
    left to the backend, whose defaults come from the ``mesh_*`` runtime
    settings. Duplicating those defaults here would only make the two drift
    apart, so a bare ``{"generator": "mesh", "mesh": {"source": ...}}`` is a
    complete and valid specification.
    """
    specification: dict[str, Any] = {"generator": "mesh", "mesh": {"source": source}}
    transform: dict[str, Any] = {}
    if scale_mm is not None:
        transform["scale_mm"] = float(scale_mm)
    if rotate_deg:
        # An empty sequence means "no rotation asked for" and is dropped so the
        # backend keeps its own zero rotation.
        transform["rotate_deg"] = [float(angle) for angle in rotate_deg]
    if transform:
        specification["transform"] = transform
    repair_block: dict[str, Any] = {}
    if repair is not None:
        repair_block["enabled"] = bool(repair)
    if repair_block:
        specification["repair"] = repair_block
    return specification


def _delete_stored_file(relative_path: str, version: ModelVersion, storage: Any) -> None:
    """Remove one stored file, best effort.

    A storage failure must never roll back the database write that produced the
    row, so the error is logged and swallowed.
    """
    if not relative_path:
        return
    try:
        storage.delete(relative_path)
    except Exception:  # noqa: BLE001 - file cleanup must never fail the write
        logger.warning("could not delete stored file %r for version %s", relative_path, version.pk)


def _store_source_mesh(
    version: ModelVersion,
    *,
    payload: bytes,
    mesh_format: str,
    report: meshcheck.MeshReport,
    scale_mm: float | None,
    rotate_deg: Sequence[float] | None,
    repair: bool | None,
    reference_note: str = "",
) -> ModelVersion:
    """Write the mesh, point ``version`` at it and record the report.

    The storage name is stable per version
    (``projects/<project_id>/v<version>/source.<ext>``), so re-importing a mesh
    overwrites the previous file instead of accumulating copies. The origin
    becomes ``manual``: a version whose geometry came from a user-supplied mesh
    is not a generated one. A non-empty ``reference_note`` is written as the
    version's note.
    """
    storage = get_storage()
    previous = version.source_mesh.name or ""
    relative = model_artifact_path(version, f"source.{mesh_format}")
    storage.write_bytes(relative, payload)
    if previous != relative:
        _delete_stored_file(previous, version, storage)

    version.source_mesh.name = relative
    version.specification_json = _mesh_specification(
        source=relative,
        scale_mm=scale_mm,
        rotate_deg=rotate_deg,
        repair=repair,
    )
    version.origin = ModelVersionOrigin.MANUAL
    update_fields = ["source_mesh", "specification_json", "origin"]
    if reference_note:
        version.reference_note = reference_note
        update_fields.append("reference_note")
    version.save(update_fields=update_fields)
    return _persist_validation(version, {"warnings": list(report.warnings)})


@transaction.atomic
def create_next_version_from_mesh(
    *,
    project: Project,
    mesh_bytes: bytes,
    filename: str,
    created_by: User | None = None,
    scale_mm: float | None = None,
    rotate_deg: Sequence[float] | None = None,
    repair: bool | None = None,
    reference_note: str = "",
) -> ModelVersion:
    """Create the next version of ``project`` from an externally generated mesh.

    This is the mesh counterpart of :func:`create_next_version`: instead of a
    prompt the source of truth is an uploaded ``.stl``/``.obj``/``.glb`` (an
    image-to-3D model, say), and the version's specification points the mesh CAD
    backend at it. The version is created through :func:`create_next_version`
    with ``origin="manual"`` -- the honest provenance for a user-supplied mesh --
    and no render is enqueued here: the caller queues it with :func:`start_render`
    so both callers (HTTP and MCP) decide when the CAD work starts.

    The whole body is one transaction and nothing is written before the upload
    passes the gate, so a rejected mesh leaves no version and no file behind.
    Raises :class:`ValueError` for an unsupported suffix, an empty/oversized
    upload or an unparseable mesh, and :class:`MeshNotPrintableError` when
    :func:`~designs.cad.meshcheck.check_mesh` reports blocking problems.
    """
    mesh_format = _mesh_source_format(filename)
    payload = _mesh_payload(mesh_bytes)
    report = meshcheck.check_mesh(payload, mesh_format)
    if not report.ok:
        raise MeshNotPrintableError(report.problems)

    version = create_next_version(
        project=project,
        created_by=created_by,
        reference_note=reference_note,
        origin=ModelVersionOrigin.MANUAL,
    )
    return _store_source_mesh(
        version,
        payload=payload,
        mesh_format=mesh_format,
        report=report,
        scale_mm=scale_mm,
        rotate_deg=rotate_deg,
        repair=repair,
    )


@transaction.atomic
def attach_source_mesh_to_version(
    *,
    version: ModelVersion,
    mesh_bytes: bytes,
    filename: str,
    scale_mm: float | None = None,
    rotate_deg: Sequence[float] | None = None,
    repair: bool | None = None,
    reference_note: str = "",
) -> ModelVersion:
    """Re-point an existing ``version`` at a new source mesh.

    The same gate and the same storage layout as
    :func:`create_next_version_from_mesh`, but on a version that already exists:
    the previous mesh file is removed, the specification is rewritten so the next
    render goes through the mesh backend, and a non-empty ``reference_note``
    replaces the version's note (the new mesh *is* the new reference). The
    version keeps its identity, so the caller re-renders it with
    :func:`start_render`; the existing artifacts are left in place and the next
    render overwrites the STL -- a mesh render produces no ``.scad``.

    Raises the same errors as :func:`create_next_version_from_mesh`; the
    transaction keeps a rejected upload from touching the version at all.
    """
    mesh_format = _mesh_source_format(filename)
    payload = _mesh_payload(mesh_bytes)
    report = meshcheck.check_mesh(payload, mesh_format)
    if not report.ok:
        raise MeshNotPrintableError(report.problems)

    stored = _store_source_mesh(
        version,
        payload=payload,
        mesh_format=mesh_format,
        report=report,
        scale_mm=scale_mm,
        rotate_deg=rotate_deg,
        repair=repair,
        reference_note=reference_note,
    )
    return stored


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


def source_mesh_path(version: ModelVersion) -> str | None:
    """Relative storage path of ``version``'s source mesh, or ``None``.

    The :func:`artifact_path` counterpart for the *input* mesh. The source mesh is
    deliberately not an artifact kind: it is what the version was made from, not
    something a render produced, so it stays out of :data:`ARTIFACT_KINDS` /
    ``_ARTIFACT_FIELDS`` (which mirror ``pipeline.ARTIFACT_TARGETS``) exactly like
    ``reference_image``.
    """
    name = getattr(getattr(version, "source_mesh", None), "name", None)
    return name or None


def source_mesh_format(version: ModelVersion) -> str:
    """Container format of ``version``'s source mesh, or ``""`` when it has none.

    Read off the stored ``source.<ext>`` name (:func:`source_mesh_path`) because
    the download route ``/versions/{id}/source-mesh/`` carries no extension of
    its own: a client has to be told whether the bytes are STL, OBJ or GLB
    instead of guessing a parser from the URL. A suffix outside
    :data:`MESH_SOURCE_FORMATS` -- only reachable by writing to storage behind the
    import gate's back -- reports ``""`` rather than raising, because this is a
    read-only projection, not a validation step.
    """
    relative_path = source_mesh_path(version)
    if not relative_path:
        return ""
    suffix = _mesh_suffix(relative_path)
    return suffix if suffix in MESH_SOURCE_FORMATS else ""


def read_source_mesh(version: ModelVersion) -> tuple[str, bytes] | None:
    """Read the source mesh through the storage backend.

    Returns ``(relative_path, data)`` or ``None`` when the version has no source
    mesh (or the stored file is gone).
    """
    relative_path = source_mesh_path(version)
    if not relative_path:
        return None
    storage = get_storage()
    if not storage.exists(relative_path):
        return None
    return relative_path, storage.read_bytes(relative_path)


def source_mesh_warnings(version: ModelVersion) -> list[str]:
    """The non-blocking mesh findings recorded for ``version``.

    Stored in ``validation_json["warnings"]`` -- the key
    :mod:`designs.cad.pipeline` documents -- so they survive re-renders, which
    only rewrite the status/stage keys. Empty for a version that has no mesh
    report (a parametric version, or one that predates the import).
    """
    return list((version.validation_json or {}).get("warnings") or [])


@transaction.atomic
def delete_version(version: ModelVersion) -> None:
    """Delete a version and its stored artifacts (manual cleanup).

    The API guards this against versions referenced by history that a cascade
    would erase (print jobs, build plates). Derived versions keep existing --
    ``parent_version`` is ``SET_NULL``, so the edit chain is simply detached.
    Artifact files are removed best-effort: a storage failure must not roll back
    the database delete. ``source_mesh`` is an input rather than an artifact but
    is stored the same way, so it is cleaned up here too.
    """
    storage = get_storage()
    for field_name in (
        "scad_file",
        "stl_file",
        "glb_file",
        "preview_image",
        "reference_image",
        "source_mesh",
    ):
        name = getattr(getattr(version, field_name, None), "name", "") or ""
        _delete_stored_file(name, version, storage)
    version.delete()
