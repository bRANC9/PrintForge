"""Printer-control permission seam (terv.md 20. fejezet).

Printer control is a **separate permission**: ``PRINTER_OPERATOR``. A workspace
``MEMBER`` may generate and edit models but must not start, pause or cancel a
physical print -- that requires this permission (or ``ADMIN``, which manages
printers).

This module is the single seam every printer-control path (services, API, MCP)
must go through. The real object-level permission system is Phase 7; until then
the checker below is the authoritative placeholder, and the ``TODO`` marks where
the workspace-scoped object permission will plug in.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

__all__ = [
    "PRINTER_OPERATOR",
    "IsPrinterOperator",
    "require_printer_operator",
    "user_can_operate_printer",
]

#: Permission codename reserved for printer control. It is not a Django
#: ``auth.Permission`` row yet (Phase 7); ``has_perm`` therefore only resolves
#: for superusers until the permission is registered.
PRINTER_OPERATOR = "printers.operate_printer"


def user_can_operate_printer(user, printer=None) -> bool:
    """Return whether ``user`` may control ``printer``.

    Current stand-in: authenticated staff/superuser, or a user holding the
    ``printers.operate_printer`` permission. Workspace role is deliberately not
    consulted yet.

    TODO(Phase 7): replace with a real object-level check that combines the
    workspace role (OWNER/ADMIN) with ``PRINTER_OPERATOR`` and the printer's
    workspace scope. Keep the signature so callers do not change.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    return bool(user.has_perm(PRINTER_OPERATOR))


def require_printer_operator(user, printer=None) -> None:
    """Raise :class:`~django.core.exceptions.PermissionDenied` unless allowed."""
    if not user_can_operate_printer(user, printer):
        raise PermissionDenied("Printer control requires the PRINTER_OPERATOR permission.")


class IsPrinterOperator(BasePermission):
    """DRF seam for printer-control endpoints.

    TODO(Phase 7): resolve the printer from the view/object and delegate to the
    real object-level permission. Never log credentials or tokens here.
    """

    message = "Printer control requires the PRINTER_OPERATOR permission."

    def has_permission(self, request, view) -> bool:
        return user_can_operate_printer(
            getattr(request, "user", None),
            getattr(view, "printer", None),
        )
