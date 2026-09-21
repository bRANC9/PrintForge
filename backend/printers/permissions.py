"""Printer-control permission seam (terv.md 20. fejezet).

Printer control is a **separate permission**: ``PRINTER_OPERATOR``. A workspace
``MEMBER`` may generate and edit models but must not start, pause or cancel a
physical print -- that requires this permission, an ADMIN/OWNER role, or the
staff override.

This module is the single seam every printer-control path (services, API, MCP)
must go through. The service layer calls :func:`require_printer_operator`
directly (see ``printers.services.transition``/``start_job``/``cancel_job``), so
it does not depend on the HTTP-layer :class:`IsPrinterOperator`; the DRF class
is a convenience for the API, not the enforcement.

Rule set
--------

:func:`user_can_operate_printer` returns ``True`` when the user is any of:

1. a superuser or staff member (operational/admin override);
2. explicitly granted the custom permission ``printers.operate_printer``
   (created by a ``core-model`` data migration; assignable to a user or group);
3. ADMIN or OWNER of a workspace that has queued a print job on the printer
   (only evaluated when ``printer`` is provided and resolvable).

A plain workspace ``MEMBER`` is **not** an operator.

Printer -> workspace resolution
-------------------------------

``printers.Printer`` has **no** workspace foreign key: it is a global registry
and the same physical printer can be shared by several workspaces. There is
therefore no single "owning" workspace. The only reliable path is through the
printer's print jobs (``Printer <- PrintJob -> Project -> Workspace``), so rule 3
grants control to an ADMIN/OWNER of *any* workspace that has used this printer.
When no printer (or an unsaved one) is given, rule 3 is skipped. The check is a
single indexed ``EXISTS`` query and performs no writes.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from workspaces.models import WorkspaceMember, WorkspaceRole

__all__ = [
    "PRINTER_OPERATOR",
    "IsPrinterOperator",
    "require_printer_operator",
    "user_can_operate_printer",
]

#: Permission codename for printer control. The ``printers.operate_printer``
#: ``auth.Permission`` row is created by a ``core-model`` data migration, so
#: ``user.has_perm`` resolves it for users and groups that were granted it.
PRINTER_OPERATOR = "printers.operate_printer"

#: Workspace roles that may control a printer used by their workspace.
_OPERATOR_ROLES = (WorkspaceRole.ADMIN, WorkspaceRole.OWNER)


def _printer_pk(printer) -> int | None:
    """Accept a ``Printer`` instance or a plain pk; ``None`` when unresolved."""
    if isinstance(printer, int):
        return printer
    return getattr(printer, "pk", None)


def _is_workspace_printer_admin(user, printer) -> bool:
    """Rule 3: ADMIN/OWNER of a workspace that has a job on ``printer``.

    One ``EXISTS`` query, no writes. Returns ``False`` when the printer is not
    resolvable (``None``/unsaved) -- see the module docstring for why there is no
    single owning workspace.
    """
    printer_pk = _printer_pk(printer)
    if not printer_pk:
        return False
    return WorkspaceMember.objects.filter(
        user=user,
        role__in=_OPERATOR_ROLES,
        workspace__projects__print_jobs__printer_id=printer_pk,
    ).exists()


def user_can_operate_printer(user, printer=None) -> bool:
    """Return whether ``user`` may control ``printer``.

    Pure and read-only (see the module docstring for the full rule set). The
    staff/superuser check short-circuits before any query; the explicit
    permission and the workspace-role check only run for non-staff users.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    if user.has_perm(PRINTER_OPERATOR):
        return True
    return _is_workspace_printer_admin(user, printer)


def require_printer_operator(user, printer=None) -> None:
    """Raise :class:`~django.core.exceptions.PermissionDenied` unless allowed."""
    if not user_can_operate_printer(user, printer):
        raise PermissionDenied("Printer control requires the PRINTER_OPERATOR permission.")


class IsPrinterOperator(BasePermission):
    """DRF convenience wrapper around the same rules as the service layer.

    The printer is taken from ``view.printer`` when a view exposes it; otherwise
    the check falls back to the staff/permission rules (rule 3 is skipped, as
    there is nothing to resolve). The service layer remains the enforcement
    point, so a view that forgets this class cannot bypass the rules. Never log
    credentials or tokens here.
    """

    message = "Printer control requires the PRINTER_OPERATOR permission."

    def has_permission(self, request, view) -> bool:
        return user_can_operate_printer(
            getattr(request, "user", None),
            getattr(view, "printer", None),
        )
