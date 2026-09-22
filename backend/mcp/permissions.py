"""Workspace-role permission seam for the in-process MCP tools.

The MCP server runs in the same Django process as the web app, so it must
enforce exactly the same workspace-role rules as the DRF API (terv.md 20., 25.3).
Instead of re-implementing the role matrix, this module delegates to
:mod:`api.permissions` -- the single seam the API already uses -- and raises
:class:`~django.core.exceptions.PermissionDenied` when the caller lacks the
minimum role.

Only id resolution happens here; the business logic stays in ``services.py``.
``allow_shell`` is never consulted and stays forbidden -- no tool may opt out of
its gate (see :func:`mcp.tools.call`).
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied

from accounts.models import User
from api.permissions import user_has_workspace_role
from designs.models import ModelVersion
from projects.models import Project
from slicers.models import BuildPlate
from workspaces.models import Workspace

__all__ = [
    "require_build_plate_role",
    "require_project_role",
    "require_version_role",
    "require_workspace_role",
    "require_workspace_role_by_id",
]


def require_workspace_role(user: User, workspace: Workspace, minimum: str) -> None:
    """Raise :class:`PermissionDenied` unless ``user`` holds at least ``minimum``."""
    if not user_has_workspace_role(user, workspace, minimum):
        raise PermissionDenied(
            f"This action requires the {minimum} role in the project's workspace."
        )


def require_workspace_role_by_id(*, workspace_id: int, user_id: int, minimum: str) -> None:
    """Enforce ``minimum`` for ``user_id`` on the workspace ``workspace_id``."""
    workspace = Workspace.objects.get(pk=workspace_id)
    user = User.objects.get(pk=user_id)
    require_workspace_role(user, workspace, minimum)


def require_project_role(*, project_id: int, user_id: int, minimum: str) -> None:
    """Enforce ``minimum`` for ``user_id`` on ``project_id``'s workspace."""
    project = Project.objects.select_related("workspace").get(pk=project_id)
    user = User.objects.get(pk=user_id)
    require_workspace_role(user, project.workspace, minimum)


def require_version_role(*, version_id: int, user_id: int, minimum: str) -> None:
    """Enforce ``minimum`` for ``user_id`` on the version's project workspace."""
    version = ModelVersion.objects.select_related("project__workspace").get(pk=version_id)
    user = User.objects.get(pk=user_id)
    require_workspace_role(user, version.project.workspace, minimum)


def require_build_plate_role(*, build_plate_id: int, user_id: int, minimum: str) -> None:
    """Enforce ``minimum`` for ``user_id`` on the plate's project workspace."""
    plate = BuildPlate.objects.select_related("project__workspace").get(pk=build_plate_id)
    user = User.objects.get(pk=user_id)
    require_workspace_role(user, plate.project.workspace, minimum)
