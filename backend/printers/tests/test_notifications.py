"""Tests for print-queue owner notifications (best-effort, terv.md 21.).

``notify_job_owner`` is patched everywhere so nothing here depends on the
notification fan-out implementation; the real contract is covered by
``notifications``' own tests.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied

from designs.services import create_next_version
from notifications.services import NotificationKind
from printers.models import Printer, PrintJobStatus
from printers.services import (
    STATUS_NOTIFICATION_KINDS,
    InvalidTransitionError,
    cancel_job,
    enqueue_job,
    transition,
)
from projects.services import create_project
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user():
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


@pytest.fixture
def member():
    return User.objects.create_user(username="mem", email="mem@example.com", password="pw")


@pytest.fixture
def project(user):
    workspace = create_workspace(name="Lab", owner=user)
    return create_project(workspace=workspace, name="Holder", created_by=user)


@pytest.fixture
def version(project, user):
    return create_next_version(project=project, prompt="make it", created_by=user)


@pytest.fixture
def printer():
    return Printer.objects.create(name="K2 Pro", backend="creality_k2")


class Recorder:
    """Stand-in for ``notify_job_owner`` that records every call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *, job, message: str, kind: str = "info", url: str = ""):
        self.calls.append({"job": job, "message": message, "kind": kind, "url": url})
        return None

    @property
    def kinds(self) -> list[str]:
        return [call["kind"] for call in self.calls]


@pytest.fixture
def notifier(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr("printers.services.notify_job_owner", recorder)
    return recorder


def _ready_job(project, version, printer, user=None):
    job = enqueue_job(project=project, model_version=version, printer=printer, created_by=user)
    transition(job, PrintJobStatus.READY)
    return job


# ---------------------------------------------------------------------------
# Status -> kind mapping
# ---------------------------------------------------------------------------


def test_status_kind_mapping():
    assert STATUS_NOTIFICATION_KINDS[PrintJobStatus.PRINTING] == NotificationKind.PRINT_STARTED
    assert STATUS_NOTIFICATION_KINDS[PrintJobStatus.COMPLETED] == NotificationKind.PRINT_DONE
    assert STATUS_NOTIFICATION_KINDS[PrintJobStatus.FAILED] == NotificationKind.PRINT_FAILED
    # CANCELLED reuses PRINT_FAILED (documented; no dedicated kind).
    assert STATUS_NOTIFICATION_KINDS[PrintJobStatus.CANCELLED] == NotificationKind.PRINT_FAILED
    # Silent statuses.
    for silent in (
        PrintJobStatus.QUEUED,
        PrintJobStatus.PREPARING,
        PrintJobStatus.SLICING,
        PrintJobStatus.READY,
        PrintJobStatus.PAUSED,
    ):
        assert silent not in STATUS_NOTIFICATION_KINDS


# ---------------------------------------------------------------------------
# enqueue_job
# ---------------------------------------------------------------------------


def test_enqueue_job_notifies_owner_queued(project, version, printer, user, notifier):
    job = enqueue_job(project=project, model_version=version, printer=printer, created_by=user)

    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["job"] is job
    assert call["kind"] == NotificationKind.PRINT_QUEUED
    assert str(job.pk) in call["message"]


# ---------------------------------------------------------------------------
# transition -> notification by target status
# ---------------------------------------------------------------------------


def test_transition_to_printing_notifies_started(project, version, printer, notifier):
    job = _ready_job(project, version, printer)
    notifier.calls.clear()

    transition(job, PrintJobStatus.PRINTING)  # user=None (internal worker)

    assert notifier.kinds == [NotificationKind.PRINT_STARTED]


def test_transition_to_completed_notifies_done(project, version, printer, notifier):
    job = _ready_job(project, version, printer)
    transition(job, PrintJobStatus.PRINTING)
    notifier.calls.clear()

    transition(job, PrintJobStatus.COMPLETED)

    assert notifier.kinds == [NotificationKind.PRINT_DONE]


def test_transition_to_failed_notifies_failed(project, version, printer, notifier):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    notifier.calls.clear()

    transition(job, PrintJobStatus.FAILED)

    assert notifier.kinds == [NotificationKind.PRINT_FAILED]


def test_transition_to_cancelled_notifies_failed(project, version, printer, notifier):
    job = _ready_job(project, version, printer)
    notifier.calls.clear()

    transition(job, PrintJobStatus.CANCELLED)

    assert notifier.kinds == [NotificationKind.PRINT_FAILED]


def test_cancel_job_notifies_owner(project, version, printer, notifier):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    notifier.calls.clear()

    cancel_job(job)

    assert notifier.kinds == [NotificationKind.PRINT_FAILED]


@pytest.mark.parametrize(
    "target",
    [PrintJobStatus.PREPARING, PrintJobStatus.SLICING, PrintJobStatus.READY],
)
def test_non_notifying_transitions_are_silent(project, version, printer, notifier, target):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    notifier.calls.clear()

    transition(job, target)

    assert notifier.calls == []


def test_pause_is_silent(project, version, printer, notifier):
    job = _ready_job(project, version, printer)
    transition(job, PrintJobStatus.PRINTING)
    notifier.calls.clear()

    transition(job, PrintJobStatus.PAUSED)

    assert notifier.calls == []


# ---------------------------------------------------------------------------
# No notification when nothing changed
# ---------------------------------------------------------------------------


def test_invalid_transition_does_not_notify(project, version, printer, notifier):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    notifier.calls.clear()

    with pytest.raises(InvalidTransitionError):
        transition(job, PrintJobStatus.COMPLETED)

    assert notifier.calls == []


def test_permission_rejection_does_not_notify(project, version, printer, member, notifier):
    add_member(workspace=project.workspace, user=member, role=WorkspaceRole.MEMBER)
    job = _ready_job(project, version, printer)
    notifier.calls.clear()

    with pytest.raises(PermissionDenied):
        transition(job, PrintJobStatus.PRINTING, user=member)

    assert notifier.calls == []


def test_same_status_noop_does_not_notify(project, version, printer, notifier):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    notifier.calls.clear()

    transition(job, PrintJobStatus.QUEUED)

    assert notifier.calls == []


# ---------------------------------------------------------------------------
# Best-effort: a notify failure must not break the transition
# ---------------------------------------------------------------------------


def test_notify_exception_does_not_break_transition(project, version, printer, monkeypatch):
    job = _ready_job(project, version, printer)

    def boom(**kwargs):
        raise RuntimeError("notifications are down")

    monkeypatch.setattr("printers.services.notify_job_owner", boom)

    transition(job, PrintJobStatus.PRINTING)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.PRINTING


def test_notify_exception_does_not_break_enqueue(project, version, printer, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("notifications are down")

    monkeypatch.setattr("printers.services.notify_job_owner", boom)

    job = enqueue_job(project=project, model_version=version, printer=printer)

    assert job.pk is not None
    assert job.status == PrintJobStatus.QUEUED
