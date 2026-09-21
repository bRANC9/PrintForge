from django.conf import settings
from django.db import models


class WorkspaceRole(models.TextChoices):
    OWNER = "OWNER", "Owner"
    ADMIN = "ADMIN", "Admin"
    MEMBER = "MEMBER", "Member"
    VIEWER = "VIEWER", "Viewer"


class Workspace(models.Model):
    name = models.CharField(max_length=200)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_workspaces",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class WorkspaceMember(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    role = models.CharField(
        max_length=20,
        choices=WorkspaceRole.choices,
        default=WorkspaceRole.MEMBER,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["workspace", "user"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "user"],
                name="uniq_workspace_member",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} @ {self.workspace} ({self.role})"
