"""Permission classes for the JSON API (terv.md 5., 20., 21. fejezet).

Phase 7 turns the previous no-op into real object-level workspace scoping.

Role matrix (terv.md 20.):

====================  =========================================================
role                  allowed
====================  =========================================================
(not a member)        nothing -- detail lookups 404, writes 403
VIEWER                read only
MEMBER                create/update projects & versions, enqueue print jobs
ADMIN / OWNER         manage the workspace and its members
PRINTER_OPERATOR      start / pause / cancel / transition print jobs
====================  =========================================================

Printer control is layered *on top* of the workspace role: a ``MEMBER`` without
``printers.operate_printer`` may not transition a job (see
:class:`printers.permissions.IsPrinterOperator`).

This class is the single seam. Views only declare the minimum role per action
(``permission_read_role`` / ``permission_write_role``), open up workspace
creation (``permission_open_create``) or opt out entirely for own-only
resources such as notifications (``permission_scope_exempt``).
"""

from __future__ import annotations

from rest_framework import permissions

from projects.models import Project
from workspaces.models import Workspace, WorkspaceRole

#: Ordered role levels; a higher number implies every lower capability.
ROLE_LEVELS = {
    WorkspaceRole.VIEWER: 1,
    WorkspaceRole.MEMBER: 2,
    WorkspaceRole.ADMIN: 3,
    WorkspaceRole.OWNER: 4,
}

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def role_level(role: str | None) -> int:
    """Numeric level of a role (unknown/None -> 0, i.e. no access)."""
    return ROLE_LEVELS.get(role, 0)


def user_role(user, workspace: Workspace) -> str | None:
    """Return ``user``'s role in ``workspace``, or ``None`` if not a member."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return workspace.members.filter(user=user).values_list("role", flat=True).first()


def user_has_workspace_role(user, workspace: Workspace, minimum: str) -> bool:
    """Whether ``user`` holds at least ``minimum`` in ``workspace``."""
    return role_level(user_role(user, workspace)) >= role_level(minimum)


def workspace_of(obj) -> Workspace | None:
    """Resolve the owning workspace of a scoped object.

    Handles ``Workspace`` itself, models with a direct ``workspace`` FK
    (``Project``, ``Notification``) and models reached through ``project``
    (``ModelVersion``, ``AgentRun``, ``PrintJob``).
    """
    if isinstance(obj, Workspace):
        return obj
    workspace = getattr(obj, "workspace", None)
    if workspace is not None:
        return workspace
    project = getattr(obj, "project", None)
    if project is not None:
        return project.workspace
    return None


class WorkspaceScopePermission(permissions.BasePermission):
    """Object-level workspace role enforcement (Phase 7)."""

    message = "You do not have access to this workspace resource."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            # Every endpoint except /api/v1/health/ requires authentication.
            return False

        minimum = self._minimum_role(request, view)
        if minimum is None:
            return True

        workspace = self._workspace_from_request(request, view)
        if workspace is None:
            # List / detail / own-only: the scoped queryset and
            # has_object_permission decide (missing rows become 404).
            return True
        return user_has_workspace_role(user, workspace, minimum)

    def has_object_permission(self, request, view, obj) -> bool:
        minimum = self._minimum_role(request, view)
        if minimum is None:
            return True
        workspace = workspace_of(obj)
        if workspace is None:
            return True
        return user_has_workspace_role(request.user, workspace, minimum)

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _minimum_role(request, view) -> str | None:
        """Minimum role for the current action, or ``None`` for no check."""
        if getattr(view, "permission_scope_exempt", False):
            return None
        if getattr(view, "action", None) == "create" and getattr(
            view, "permission_open_create", False
        ):
            # Creating a workspace has no pre-existing workspace to check.
            return None
        if request.method in SAFE_METHODS:
            return getattr(view, "permission_read_role", WorkspaceRole.VIEWER)
        return getattr(view, "permission_write_role", WorkspaceRole.MEMBER)

    @staticmethod
    def _writable_payload_fields(view) -> set[str]:
        """Names of the request-serializer fields the endpoint actually accepts.

        Read-only and undeclared fields are excluded, so a caller cannot smuggle
        an authorization key (e.g. ``workspace``) that the serializer silently
        drops.
        """
        get_serializer_class = getattr(view, "get_serializer_class", None)
        if get_serializer_class is None:
            return set()
        serializer_class = get_serializer_class()
        if serializer_class is None:
            return set()
        serializer = serializer_class()
        return {
            name
            for name, field in serializer.fields.items()
            if not getattr(field, "read_only", False)
        }

    @staticmethod
    def _workspace_from_request(request, view) -> Workspace | None:
        """Resolve the workspace from the object actually being written.

        Only fields the endpoint's serializer genuinely accepts are trusted. The
        real FK wins: a print job's workspace is its ``project``'s workspace, so
        an ignored ``workspace`` payload key can never authorize a foreign
        project.
        """
        resolver = getattr(view, "get_permission_workspace", None)
        if resolver is not None:
            return resolver(request)

        data = getattr(request, "data", None) or {}
        fields = WorkspaceScopePermission._writable_payload_fields(view)

        if "project" in fields:
            # The project FK is authoritative; never fall back to ``workspace``.
            project_id = data.get("project")
            if not project_id:
                return None
            project = Project.objects.filter(pk=project_id).select_related("workspace").first()
            return project.workspace if project else None

        if "workspace" in fields:
            workspace_id = data.get("workspace")
            if workspace_id:
                return Workspace.objects.filter(pk=workspace_id).first()

        return None
