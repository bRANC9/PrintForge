"""Permission classes for the JSON API.

The global default stays ``IsAuthenticatedOrReadOnly`` (see ``config.settings``).
Phase 7 adds object-level workspace permissions (OWNER/ADMIN/MEMBER/VIEWER,
terv.md 5. es 20. fejezet). That logic must live in a permission class -- not in
view bodies -- so it is implemented here.
"""

from rest_framework import permissions


class WorkspaceScopePermission(permissions.BasePermission):
    """Object-level workspace scoping -- Phase 7 extension point.

    Today this is intentionally a no-op: it only documents *where* the
    workspace role / membership checks will land, so the views never have to
    change. When Phase 7 lands, ``has_object_permission`` resolves the owning
    workspace (``obj.workspace`` for projects, ``obj.project.workspace`` for
    versions/agent runs) and checks the caller's :class:`WorkspaceMember` role.
    """

    message = "You do not have access to this workspace resource."

    def has_permission(self, request, view) -> bool:
        # Authentication itself is enforced by IsAuthenticatedOrReadOnly; this
        # hook stays permissive until object-level roles exist.
        return True

    def has_object_permission(self, request, view, obj) -> bool:
        # Phase 7: resolve workspace + membership here.
        return True
