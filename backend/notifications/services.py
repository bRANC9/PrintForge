"""Notification service (terv.md 21. fejezet, Phase 7).

Creation and read-marking logic for in-app notifications. Views, DRF
serializers and MCP tools call these functions; nothing here may depend on DRF
or an HTTP request.
"""

from __future__ import annotations

from django.db.models import QuerySet

from .models import Notification

__all__ = [
    "mark_all_read",
    "mark_read",
    "notifications_for_user",
    "notify",
    "unread_count",
]


def notify(
    *,
    user,
    message: str,
    kind: str = "info",
    workspace=None,
    url: str = "",
) -> Notification:
    """Create a notification for ``user`` (optionally workspace-scoped)."""
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
