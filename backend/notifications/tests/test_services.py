"""Tests for the notification service contract and best-effort fan-out."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from designs.services import create_next_version
from notifications.models import Notification
from notifications.services import (
    NotificationKind,
    mark_all_read,
    mark_read,
    notify,
    notify_job_owner,
    notify_project_members,
    notify_workspace_members,
    unread_count,
)
from printers.models import Printer
from printers.services import enqueue_job
from projects.services import create_project
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


def make_user(username):
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="pw",
    )


@pytest.fixture
def owner():
    return make_user("owner")


@pytest.fixture
def member():
    return make_user("member")


@pytest.fixture
def other_member():
    return make_user("other")


@pytest.fixture
def workspace(owner, member, other_member):
    workspace = create_workspace(name="Lab", owner=owner)
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    add_member(workspace=workspace, user=other_member, role=WorkspaceRole.MEMBER)
    return workspace


@pytest.fixture
def project(workspace, owner):
    return create_project(workspace=workspace, name="Holder", created_by=owner)


def make_job(project, created_by=None):
    printer = Printer.objects.create(name="K2 Pro")
    version = create_next_version(project=project, prompt="make it", created_by=created_by)
    return enqueue_job(
        project=project,
        model_version=version,
        printer=printer,
        created_by=created_by,
    )


# ---------------------------------------------------------------------------
# Contract: kinds + primitive
# ---------------------------------------------------------------------------


def test_notification_kinds_have_stable_values():
    assert NotificationKind.INFO == "info"
    assert NotificationKind.MODEL_READY == "model_ready"
    assert NotificationKind.MODEL_FAILED == "model_failed"
    assert NotificationKind.SLICE_READY == "slice_ready"
    assert NotificationKind.SLICE_FAILED == "slice_failed"
    assert NotificationKind.PRINT_QUEUED == "print_queued"
    assert NotificationKind.PRINT_STARTED == "print_started"
    assert NotificationKind.PRINT_DONE == "print_done"
    assert NotificationKind.PRINT_FAILED == "print_failed"
    assert NotificationKind.AGENT_DONE == "agent_done"
    assert NotificationKind.AGENT_FAILED == "agent_failed"


def test_notify_creates_a_single_notification(owner, workspace):
    notification = notify(user=owner, message="hello", kind=NotificationKind.INFO)

    assert notification.user == owner
    assert notification.kind == "info"
    assert notification.message == "hello"
    assert notification.workspace is None
    assert notification.is_read is False


# ---------------------------------------------------------------------------
# Recipient policy
# ---------------------------------------------------------------------------


def test_notify_project_members_reaches_every_member(workspace, project, owner, member):
    created = notify_project_members(
        project=project,
        message="model ready",
        kind=NotificationKind.MODEL_READY,
    )

    assert {n.user_id for n in created} == {
        owner.pk,
        member.pk,
        workspace.members.get(user__username="other").user_id,
    }
    assert all(n.workspace_id == workspace.pk for n in created)
    assert all(n.kind == "model_ready" for n in created)
    assert Notification.objects.count() == 3


def test_exclude_accepts_a_single_user(workspace, project, owner):
    created = notify_project_members(project=project, message="x", exclude=owner)

    assert owner.pk not in {n.user_id for n in created}
    assert len(created) == 2


def test_exclude_accepts_an_iterable(workspace, owner, member):
    created = notify_workspace_members(workspace=workspace, message="x", exclude=[owner, member])

    assert len(created) == 1
    assert created[0].user_id == workspace.members.get(user__username="other").user_id


def test_workspace_members_are_deduplicated(monkeypatch, workspace, owner, member):
    monkeypatch.setattr(
        "notifications.services._iter_member_users",
        lambda workspace, excluded: iter([owner, owner, member]),
    )

    created = notify_workspace_members(workspace=workspace, message="x")

    assert [n.user_id for n in created] == [owner.pk, member.pk]


def test_notify_job_owner_returns_none_without_owner(project):
    job = make_job(project, created_by=None)

    assert notify_job_owner(job=job, message="done") is None
    assert Notification.objects.count() == 0


def test_notify_job_owner_notifies_the_creator(project, owner):
    job = make_job(project, created_by=owner)

    notification = notify_job_owner(job=job, message="print done", kind=NotificationKind.PRINT_DONE)

    assert notification is not None
    assert notification.user == owner
    assert notification.kind == "print_done"
    assert notification.workspace_id == project.workspace_id


# ---------------------------------------------------------------------------
# Best-effort guarantee
# ---------------------------------------------------------------------------


def test_fan_out_swallows_creation_errors(monkeypatch, workspace, project, owner):
    def boom(**kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr("notifications.services.notify", boom)
    job = make_job(project, created_by=owner)

    assert notify_workspace_members(workspace=workspace, message="x") == []
    assert notify_project_members(project=project, message="x") == []
    assert notify_job_owner(job=job, message="x") is None
    assert Notification.objects.count() == 0


def test_fan_out_swallows_recipient_resolution_errors(monkeypatch, workspace, project):
    def boom_iter(workspace, excluded):
        raise RuntimeError("member query failed")

    monkeypatch.setattr("notifications.services._iter_member_users", boom_iter)

    assert notify_workspace_members(workspace=workspace, message="x") == []
    # A project without a resolvable workspace must not raise either.
    assert notify_project_members(project=object(), message="x") == []


# ---------------------------------------------------------------------------
# Read-marking contract is unchanged
# ---------------------------------------------------------------------------


def test_read_marking_contract(owner):
    notification = notify(user=owner, message="m")

    assert unread_count(owner) == 1
    assert mark_read(owner, notification.pk).is_read is True
    assert mark_read(owner, notification.pk).is_read is True  # idempotent
    assert mark_read(owner, 999_999) is None

    notify(user=owner, message="m2")
    assert mark_all_read(owner) == 1
    assert unread_count(owner) == 0
