"""User notifications (terv.md 21. fejezet, Phase 7).

Plain, DB-agnostic queue of in-app notifications. Creation/read-marking logic
belongs in ``notifications/services.py``; this module only stores data.
"""

from django.conf import settings
from django.db import models


class Notification(models.Model):
    """One in-app notification for a user, optionally scoped to a workspace."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    kind = models.CharField(max_length=50)
    message = models.TextField()
    url = models.CharField(max_length=500, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "is_read"], name="notif_user_is_read_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} -> {self.user_id}"
