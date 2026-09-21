"""Tests for the printer-control permission seam (terv.md 20.).

Covers all three grant paths -- staff/superuser, the explicit
``printers.operate_printer`` permission, and workspace ADMIN/OWNER -- plus a
plain MEMBER being rejected.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied

from designs.services import create_next_version
from printers.models import Printer
from printers.permissions import (
    PRINTER_OPERATOR,
    IsPrinterOperator,
    require_printer_operator,
    user_can_operate_printer,
)
from printers.services import enqueue_job
from projects.services import create_project
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user(username, **extra):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pw", **extra
    )


@pytest.fixture
def owner():
    return _make_user("owner")


@pytest.fixture
def admin():
    return _make_user("admin")


@pytest.fixture
def member():
    return _make_user("member")


@pytest.fixture
def outsider():
    return _make_user("outsider")


@pytest.fixture
def operator():
    """Staff user -- the operational override."""
    return _make_user("operator", is_staff=True)


@pytest.fixture
def workspace(owner):
    return create_workspace(name="Lab", owner=owner)


@pytest.fixture
def project(workspace, owner):
    return create_project(workspace=workspace, name="Holder", created_by=owner)


@pytest.fixture
def version(project, owner):
    return create_next_version(project=project, prompt="make it", created_by=owner)


@pytest.fixture
def printer():
    return Printer.objects.create(name="K2 Pro", backend="creality_k2")


@pytest.fixture
def job(project, version, printer, owner):
    """A print job on ``printer`` inside ``workspace`` (makes rule 3 resolvable)."""
    return enqueue_job(project=project, model_version=version, printer=printer, created_by=owner)


def _grant_operator_permission(user):
    """Create/attach the custom permission and return a cache-free user."""
    content_type = ContentType.objects.get_for_model(Printer)
    permission, _ = Permission.objects.get_or_create(
        codename="operate_printer",
        content_type=content_type,
        defaults={"name": "Can operate printers"},
    )
    user.user_permissions.add(permission)
    return User.objects.get(pk=user.pk)


# ---------------------------------------------------------------------------
# Rule 0: identity / authentication
# ---------------------------------------------------------------------------


def test_permission_codename_is_stable():
    assert PRINTER_OPERATOR == "printers.operate_printer"


def test_anonymous_and_missing_users_cannot_operate():
    assert user_can_operate_printer(None) is False
    assert user_can_operate_printer(AnonymousUser()) is False
    assert user_can_operate_printer(SimpleNamespace(is_authenticated=False)) is False


# ---------------------------------------------------------------------------
# Rule 1: staff / superuser override
# ---------------------------------------------------------------------------


def test_staff_can_operate(operator):
    assert user_can_operate_printer(operator) is True


def test_superuser_can_operate():
    superuser = User.objects.create_superuser(
        username="root", email="root@example.com", password="pw"
    )
    assert user_can_operate_printer(superuser) is True


# ---------------------------------------------------------------------------
# Rule 2: explicit printers.operate_printer permission
# ---------------------------------------------------------------------------


def test_explicit_permission_grants_control_to_non_staff(member, printer):
    granted = _grant_operator_permission(member)

    assert granted.is_staff is False
    assert user_can_operate_printer(granted, printer) is True
    assert user_can_operate_printer(granted) is True


def test_permission_is_required_for_the_codename(member, printer):
    # A user without the permission (and without a role) stays rejected.
    assert user_can_operate_printer(member, printer) is False


# ---------------------------------------------------------------------------
# Rule 3: workspace ADMIN / OWNER of a workspace using the printer
# ---------------------------------------------------------------------------


def test_workspace_owner_can_operate(owner, printer, job):
    assert user_can_operate_printer(owner, printer) is True


def test_workspace_admin_can_operate(admin, workspace, printer, job):
    add_member(workspace=workspace, user=admin, role=WorkspaceRole.ADMIN)

    assert user_can_operate_printer(admin, printer) is True


def test_workspace_member_is_rejected(member, workspace, printer, job):
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)

    assert user_can_operate_printer(member, printer) is False


def test_admin_of_unrelated_workspace_is_rejected(outsider, printer, job):
    # Owns another workspace, but it has no job on this printer.
    create_workspace(name="Other", owner=outsider)

    assert user_can_operate_printer(outsider, printer) is False


def test_accepts_a_plain_printer_pk(owner, printer, job):
    assert user_can_operate_printer(owner, printer.pk) is True


def test_unsaved_or_missing_printer_skips_workspace_rule(owner):
    assert user_can_operate_printer(owner, None) is False
    assert user_can_operate_printer(owner, Printer(name="Unsaved")) is False


# ---------------------------------------------------------------------------
# require_printer_operator
# ---------------------------------------------------------------------------


def test_require_printer_operator_raises_for_member(member, printer, job):
    with pytest.raises(PermissionDenied):
        require_printer_operator(member, printer)


def test_require_printer_operator_allows_admin(admin, workspace, printer, job):
    add_member(workspace=workspace, user=admin, role=WorkspaceRole.ADMIN)

    assert require_printer_operator(admin, printer) is None


def test_require_printer_operator_allows_operator(operator):
    assert require_printer_operator(operator) is None


# ---------------------------------------------------------------------------
# DRF IsPrinterOperator
# ---------------------------------------------------------------------------


def test_is_printer_operator_drf_seam(member, operator):
    permission = IsPrinterOperator()
    view = SimpleNamespace()

    assert permission.has_permission(SimpleNamespace(user=member), view) is False
    assert permission.has_permission(SimpleNamespace(user=operator), view) is True
    assert permission.has_permission(SimpleNamespace(user=AnonymousUser()), view) is False


def test_is_printer_operator_uses_view_printer(admin, member, workspace, printer, job):
    add_member(workspace=workspace, user=admin, role=WorkspaceRole.ADMIN)
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    permission = IsPrinterOperator()
    view = SimpleNamespace(printer=printer)

    assert permission.has_permission(SimpleNamespace(user=admin), view) is True
    assert permission.has_permission(SimpleNamespace(user=member), view) is False
