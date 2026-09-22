"""Notification service (terv.md 21. fejezet, Phase 7).

Creation and read-marking logic for in-app notifications. Views, DRF
serializers, MCP tools and background workers (render/slice/print/agent) call
these functions; nothing here may depend on DRF or an HTTP request.

Recipient policy
----------------

* :func:`notify` -- a single explicit ``user`` (the primitive).
* :func:`notify_project_members` -- every ``WorkspaceMember`` of the project's
  workspace.
* :func:`notify_workspace_members` -- every ``WorkspaceMember`` of the
  workspace.
* :func:`notify_job_owner` -- ``PrintJob.created_by`` (``None`` when unset).

Recipients are de-duplicated (a user can only be notified once per call) and an
optional ``exclude`` (a user, an iterable of users, or ``None``) is skipped --
useful so the actor does not notify themselves.

Best-effort guarantee
---------------------

A notification must never break a render, slice, print or agent run. The
fan-out helpers (:func:`notify_project_members`,
:func:`notify_workspace_members`, :func:`notify_job_owner`) swallow and log
every exception -- including failures while resolving recipients -- and return
whatever was created. Failures are logged with ``logger.exception`` so they are
visible in the worker logs without propagating to the caller.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from django.db import models
from django.db.models import QuerySet

from workspaces.models import WorkspaceMember

from .models import Notification

logger = logging.getLogger(__name__)

__all__ = [
    "NotificationKind",
    "mark_all_read",
    "mark_read",
    "notifications_for_user",
    "notify",
    "notify_job_owner",
    "notify_project_members",
    "notify_workspace_members",
    "unread_count",
]


class NotificationKind(models.TextChoices):
    """Well-known notification kinds (the model field stays a plain CharField)."""

    INFO = "info", "Info"
    MODEL_READY = "model_ready", "Model ready"
    MODEL_FAILED = "model_failed", "Model failed"
    SLICE_READY = "slice_ready", "Slice ready"
    SLICE_FAILED = "slice_failed", "Slice failed"
    PRINT_QUEUED = "print_queued", "Print queued"
    PRINT_STARTED = "print_started", "Print started"
    PRINT_DONE = "print_done", "Print done"
    PRINT_FAILED = "print_failed", "Print failed"
    AGENT_DONE = "agent_done", "Agent run done"
    AGENT_FAILED = "agent_failed", "Agent run failed"
    OLLAMA_PULL_DONE = "ollama_pull_done", "Ollama pull done"
    OLLAMA_PULL_FAILED = "ollama_pull_failed", "Ollama pull failed"


# ---------------------------------------------------------------------------
# Primitive
# ---------------------------------------------------------------------------


def notify(
    *,
    user,
    message: str,
    kind: str = "info",
    workspace=None,
    url: str = "",
) -> Notification:
    """Create a notification for ``user`` (optionally workspace-scoped).

    This is the primitive used by the fan-out helpers; it propagates database
    errors. Call sites that must not fail should use the ``notify_*`` helpers,
    which are best-effort.
    """
    return Notification.objects.create(
        user=user,
        kind=kind,
        message=message,
        workspace=workspace,
        url=url,
    )


def notifications_for_user(user) -> QuerySet[Notification]:
    """The caller's own notifications, newest first (``Meta.ordering``)."""
    return Notification.objects.filter(user=user)


def mark_read(user, notification_id: int) -> Notification | None:
    """Mark one of ``user``'s notifications read (idempotent).

    Returns the notification, or ``None`` when it does not exist or is not
    owned by ``user`` (callers translate that into a 404).
    """
    notification = Notification.objects.filter(pk=notification_id, user=user).first()
    if notification is None:
        return None
    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=["is_read"])
    return notification


def mark_all_read(user) -> int:
    """Mark every unread notification of ``user`` read; return the count."""
    return Notification.objects.filter(user=user, is_read=False).update(is_read=True)


def unread_count(user) -> int:
    """Number of unread notifications of ``user``."""
    return Notification.objects.filter(user=user, is_read=False).count()


# ---------------------------------------------------------------------------
# Best-effort fan-out helpers
# ---------------------------------------------------------------------------


def notify_project_members(
    *,
    project,
    message: str,
    kind: str = NotificationKind.INFO,
    url: str = "",
    exclude: Any = None,
) -> list[Notification]:
    """Notify every member of ``project.workspace`` (best-effort)."""
    try:
        workspace = project.workspace
    except Exception:  # noqa: BLE001 - never break the caller
        logger.exception(
            "notify_project_members could not resolve workspace (project=%s)",
            getattr(project, "pk", project),
        )
        return []
    return notify_workspace_members(
        workspace=workspace,
        message=message,
        kind=kind,
        url=url,
        exclude=exclude,
    )


def notify_workspace_members(
    *,
    workspace,
    message: str,
    kind: str = NotificationKind.INFO,
    url: str = "",
    exclude: Any = None,
) -> list[Notification]:
    """Notify every member of ``workspace``, de-duplicated (best-effort)."""
    created: list[Notification] = []
    try:
        excluded = _exclude_user_ids(exclude)
        seen: set[int] = set()
        for user in _iter_member_users(workspace, excluded):
            user_id = getattr(user, "pk", None)
            if user_id is not None:
                if user_id in seen:
                    continue
                seen.add(user_id)
            notification = _safe_notify(
                user=user,
                message=message,
                kind=kind,
                workspace=workspace,
                url=url,
            )
            if notification is not None:
                created.append(notification)
    except Exception:  # noqa: BLE001 - never break the caller
        logger.exception(
            "notify_workspace_members failed (workspace=%s)",
            getattr(workspace, "pk", workspace),
        )
    return created


def notify_job_owner(
    *,
    job,
    message: str,
    kind: str = NotificationKind.INFO,
    url: str = "",
) -> Notification | None:
    """Notify ``job.created_by`` (best-effort); ``None`` when there is no owner."""
    try:
        user = job.created_by
    except Exception:  # noqa: BLE001 - never break the caller
        logger.exception(
            "notify_job_owner could not resolve owner (job=%s)",
            getattr(job, "pk", job),
        )
        return None
    if user is None:
        return None

    try:
        workspace = job.project.workspace
    except Exception:  # noqa: BLE001 - the notification is still useful
        logger.warning(
            "notify_job_owner could not resolve workspace (job=%s)",
            getattr(job, "pk", job),
        )
        workspace = None

    return _safe_notify(user=user, message=message, kind=kind, workspace=workspace, url=url)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_notify(
    *,
    user,
    message: str,
    kind: str,
    workspace=None,
    url: str = "",
) -> Notification | None:
    """Create one notification, logging (not raising) on failure."""
    try:
        return notify(user=user, message=message, kind=kind, workspace=workspace, url=url)
    except Exception:  # noqa: BLE001 - best-effort by contract
        logger.exception(
            "Failed to create notification (user=%s, kind=%s)",
            getattr(user, "pk", user),
            kind,
        )
        return None


def _exclude_user_ids(exclude: Any) -> set[int]:
    """Normalise ``exclude`` (user | iterable of users | None) to a pk set."""
    if exclude is None:
        return set()
    if hasattr(exclude, "pk"):
        pk = getattr(exclude, "pk", None)
        return {pk} if pk is not None else set()
    ids: set[int] = set()
    for item in exclude:
        if item is None:
            continue
        pk = getattr(item, "pk", item)
        if pk is not None:
            ids.add(pk)
    return ids


def _iter_member_users(workspace, excluded: set[int]) -> Iterator[Any]:
    """Yield each workspace member's user, skipping ``excluded`` pks."""
    members = (
        WorkspaceMember.objects.filter(workspace=workspace)
        .select_related("user")
        .order_by("user_id")
    )
    for member in members:
        if member.user_id in excluded:
            continue
        yield member.user
