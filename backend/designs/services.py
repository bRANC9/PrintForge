"""Business logic for design versions.

The API views and the MCP tools both go through these functions; nothing here
may depend on DRF or an HTTP request. Render jobs are *only* enqueued to Celery
(terv.md 9. fejezet) -- the web process never shells out to a CAD binary.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from files.services import get_storage
from projects.models import Project

from .models import ModelVersion
from .tasks import render_model_stl

__all__ = [
    "ARTIFACT_CONTENT_TYPES",
    "ARTIFACT_KINDS",
    "RenderEnqueueError",
    "artifact_path",
    "create_annotation_edit",
    "create_next_version",
    "latest_version",
    "read_artifact",
    "start_render",
    "version_status",
    "versions_accessible_to",
    "versions_for_project",
]

#: Artifact kinds exposed by the API and the storage layout they map to.
ARTIFACT_KINDS = ("scad", "stl")
_ARTIFACT_FIELDS = {"scad": "scad_file", "stl": "stl_file"}
ARTIFACT_CONTENT_TYPES = {
    "scad": "text/plain; charset=utf-8",
    "stl": "model/stl",
}


class RenderEnqueueError(RuntimeError):
    """The render job could not be handed to the background queue.

    Raised when the Celery broker is unreachable so the API can answer with a
    ``503 Service Unavailable`` instead of leaking a 500 (see terv.md 9.).
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
) -> ModelVersion:
    """Create the next version for a project (v1, v2, ...).

    ``reference_note``/``reference_image`` are the optional reference photo and
    note attached to the prompt (terv.md 27. fejezet); both are stored on the
    version so the generation stays reproducible.
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
    """Return the relative storage path of ``kind`` (``scad``/``stl``).

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
