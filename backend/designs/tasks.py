"""Celery tasks for the ``designs`` app.

The Django request cycle never shells out to CAD binaries; it only enqueues
``render_model_stl`` (terv.md 9. fejezet). The API polls the job status by
reading ``ModelVersion.validation_json["status"]`` for the version id.

When a render finishes (or fails) the project's workspace members are notified
through ``notifications.services`` (terv.md 21. fejezet, Phase 7). Those calls
are best-effort and happen *after* the critical ``validation_json`` / file
writes, so they can never change the CAD task's outcome.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from .cad.pipeline import render_version
from .models import ModelVersion

try:  # notifications.services is owned by api-dev and lands in parallel
    from notifications.services import NotificationKind, notify_project_members
except Exception:  # noqa: BLE001 - notifications is a best-effort side channel;
    # a broken or not-yet-landed notifications module must never take down CAD
    # rendering or app startup. Log and fall back to a no-op notifier.

    logging.getLogger(__name__).warning("notifications service unavailable; falling back to no-op")

    class NotificationKind:  # type: ignore[no-redef]
        """Fallback kind values mirroring ``notifications.services``."""

        MODEL_READY = "model_ready"
        MODEL_FAILED = "model_failed"

    def notify_project_members(*, project, message, kind="info", url="", exclude=None):
        """Fallback: never raises, just logs that notifications are unavailable."""
        logging.getLogger(__name__).warning(
            "notifications service unavailable; dropping '%s' notification: %s", kind, message
        )
        return []


__all__ = ["render_model_stl"]

logger = logging.getLogger(__name__)

#: Upper bound for the error text embedded in a MODEL_FAILED notification.
_FAILURE_REASON_MAX_LEN = 200


def _notify(version: ModelVersion, *, kind: str, message: str) -> None:
    """Send one best-effort notification; never masks the CAD task outcome."""
    try:
        notify_project_members(
            project=version.project,
            kind=kind,
            message=message,
            url=f"/projects/{version.project.id}/",
        )
    except Exception:  # noqa: BLE001 - a notification failure must not affect the task
        logger.exception(
            "render_model_stl: '%s' notification failed for ModelVersion %s", kind, version.pk
        )


def _notify_ready(version: ModelVersion) -> None:
    _notify(
        version,
        kind=NotificationKind.MODEL_READY,
        message=f"A(z) „{version.project.name}” v{version.version} modell elkészült.",
    )


def _notify_failed(version: ModelVersion, error: BaseException) -> None:
    reason = str(error).strip()[:_FAILURE_REASON_MAX_LEN] or type(error).__name__
    _notify(
        version,
        kind=NotificationKind.MODEL_FAILED,
        message=(
            f"A(z) „{version.project.name}” v{version.version} modell generálása "
            f"nem sikerült: {reason}"
        ),
    )


@shared_task(name="designs.render_model_stl")
def render_model_stl(version_id: int) -> dict[str, Any]:
    """Generate OpenSCAD + STL for ``ModelVersion`` ``version_id``.

    Loads the version, renders the source from ``specification_json``, exports
    STL through the sandboxed OpenSCAD backend and stores both artifacts via
    ``files.services.get_storage()``. Progress and failures are persisted in
    ``validation_json`` (status: queued/running/done/failed) -- no new model
    fields are introduced.

    On success the project's workspace members get a ``MODEL_READY``
    notification; on failure they get ``MODEL_FAILED`` with a (truncated)
    reason before the original exception is re-raised.
    """
    version = ModelVersion.objects.filter(pk=version_id).first()
    if version is None:
        logger.warning("render_model_stl: ModelVersion %s not found", version_id)
        return {"version_id": version_id, "status": "missing"}

    try:
        result = render_version(version)
    except Exception as exc:
        # ``render_version`` already persisted the failed status. Notify, then
        # re-raise so Celery records the failure; notifications never mask it.
        _notify_failed(version, exc)
        raise

    # Success state (files + validation_json) is already committed.
    _notify_ready(version)
    return result
