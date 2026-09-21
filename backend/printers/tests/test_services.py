"""Tests for the print-queue service (``printers.services``) -- no network.

Queue ordering and transition validation run against the ORM (sqlite-safe).
The printer adapter is faked for ``printer_status``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from designs.services import create_next_version
from printers.base import PrinterState, PrinterStatus
from printers.models import Printer, PrintJob, PrintJobStatus
from printers.services import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    QueueError,
    assign_filament,
    enqueue_job,
    next_job,
    printer_status,
    queue_for_printer,
    transition,
)
from projects.services import create_project
from slicers.models import FilamentProfile
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user():
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


@pytest.fixture
def project(user):
    workspace = create_workspace(name="Lab", owner=user)
    return create_project(workspace=workspace, name="Holder", created_by=user)


@pytest.fixture
def version(project, user):
    return create_next_version(project=project, prompt="make it", created_by=user)


@pytest.fixture
def printer():
    return Printer.objects.create(name="K2 Pro", backend="creality_k2", host="k2.local")


def _set_created_at(job: PrintJob, when) -> None:
    PrintJob.objects.filter(pk=job.pk).update(created_at=when)


# ---------------------------------------------------------------------------
# enqueue_job
# ---------------------------------------------------------------------------


def test_enqueue_job_creates_queued_job(project, version, printer, user):
    job = enqueue_job(
        project=project,
        model_version=version,
        printer=printer,
        created_by=user,
        priority=3,
    )

    assert job.pk is not None
    assert job.status == PrintJobStatus.QUEUED
    assert job.priority == 3
    assert job.created_by == user
    assert job.printer == printer


def test_enqueue_job_rejects_version_from_another_project(project, version, printer, user):
    other_project = create_project(workspace=project.workspace, name="Other", created_by=user)

    with pytest.raises(QueueError, match="does not belong"):
        enqueue_job(project=other_project, model_version=version, printer=printer)


def test_enqueue_job_rejects_negative_priority(project, version, printer):
    with pytest.raises(QueueError, match="priority"):
        enqueue_job(project=project, model_version=version, printer=printer, priority=-1)


# ---------------------------------------------------------------------------
# Ordering: higher priority first, then FIFO
# ---------------------------------------------------------------------------


def test_next_job_orders_priority_then_fifo(project, version, printer):
    base = timezone.now()
    low = enqueue_job(project=project, model_version=version, printer=printer, priority=0)
    high_first = enqueue_job(project=project, model_version=version, printer=printer, priority=5)
    high_second = enqueue_job(project=project, model_version=version, printer=printer, priority=5)
    _set_created_at(low, base)
    _set_created_at(high_first, base + timedelta(seconds=1))
    _set_created_at(high_second, base + timedelta(seconds=2))

    assert next_job(printer).pk == high_first.pk

    transition(high_first, PrintJobStatus.READY)
    assert next_job(printer).pk == high_second.pk

    transition(high_second, PrintJobStatus.READY)
    assert next_job(printer).pk == low.pk


def test_next_job_ignores_non_queued_jobs(project, version, printer):
    running = enqueue_job(project=project, model_version=version, printer=printer, priority=10)
    transition(running, PrintJobStatus.READY)
    queued = enqueue_job(project=project, model_version=version, printer=printer, priority=1)

    assert next_job(printer).pk == queued.pk


def test_next_job_returns_none_when_queue_empty(printer):
    assert next_job(printer) is None


def test_queue_for_printer_is_scoped_to_the_printer(project, version, printer):
    other_printer = Printer.objects.create(name="Other", backend="creality_k2")
    enqueue_job(project=project, model_version=version, printer=printer, priority=1)
    enqueue_job(project=project, model_version=version, printer=other_printer, priority=99)

    jobs = list(queue_for_printer(printer))

    assert len(jobs) == 1
    assert jobs[0].printer_id == printer.pk


# ---------------------------------------------------------------------------
# transition
# ---------------------------------------------------------------------------


def test_transition_walks_the_full_happy_path(project, version, printer):
    job = enqueue_job(project=project, model_version=version, printer=printer)

    for status in (
        PrintJobStatus.PREPARING,
        PrintJobStatus.SLICING,
        PrintJobStatus.READY,
        PrintJobStatus.PRINTING,
        PrintJobStatus.PAUSED,
        PrintJobStatus.PRINTING,
        PrintJobStatus.COMPLETED,
    ):
        transition(job, status)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.COMPLETED


def test_transition_accepts_plain_strings(project, version, printer):
    job = enqueue_job(project=project, model_version=version, printer=printer)

    transition(job, "PREPARING")

    job.refresh_from_db()
    assert job.status == PrintJobStatus.PREPARING


def test_transition_rejects_invalid_moves(project, version, printer):
    job = enqueue_job(project=project, model_version=version, printer=printer)

    with pytest.raises(InvalidTransitionError):
        transition(job, PrintJobStatus.COMPLETED)  # must be sliced/printed first

    done = enqueue_job(project=project, model_version=version, printer=printer, priority=1)
    for status in (
        PrintJobStatus.PREPARING,
        PrintJobStatus.READY,
        PrintJobStatus.PRINTING,
        PrintJobStatus.COMPLETED,
    ):
        transition(done, status)

    with pytest.raises(InvalidTransitionError):
        transition(done, PrintJobStatus.PRINTING)  # COMPLETED is terminal


def test_transition_same_status_is_idempotent_noop(project, version, printer):
    job = enqueue_job(project=project, model_version=version, printer=printer)

    assert transition(job, PrintJobStatus.QUEUED) is job
    job.refresh_from_db()
    assert job.status == PrintJobStatus.QUEUED


def test_transition_rejects_unknown_status(project, version, printer):
    job = enqueue_job(project=project, model_version=version, printer=printer)

    with pytest.raises(InvalidTransitionError, match="Unknown PrintJob status"):
        transition(job, "TELEPORTED")


def test_failed_and_cancelled_can_be_requeued(project, version, printer):
    failed = enqueue_job(project=project, model_version=version, printer=printer)
    transition(failed, PrintJobStatus.FAILED)
    transition(failed, PrintJobStatus.QUEUED)
    failed.refresh_from_db()
    assert failed.status == PrintJobStatus.QUEUED

    cancelled = enqueue_job(project=project, model_version=version, printer=printer, priority=1)
    transition(cancelled, PrintJobStatus.CANCELLED)
    transition(cancelled, PrintJobStatus.QUEUED)
    cancelled.refresh_from_db()
    assert cancelled.status == PrintJobStatus.QUEUED


def test_every_status_has_a_transition_entry():
    assert set(ALLOWED_TRANSITIONS) == set(PrintJobStatus.values)


# ---------------------------------------------------------------------------
# assign_filament
# ---------------------------------------------------------------------------


def test_assign_filament_sets_and_clears(project, version, printer):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    filament = FilamentProfile.objects.create(name="Hyper PLA Black", material="PLA")

    assign_filament(job=job, filament=filament)
    job.refresh_from_db()
    assert job.filament == filament

    assign_filament(job=job, filament=None)
    job.refresh_from_db()
    assert job.filament is None


# ---------------------------------------------------------------------------
# printer_status
# ---------------------------------------------------------------------------


def test_printer_status_delegates_to_backend(printer, monkeypatch):
    expected = PrinterStatus(online=True, state=PrinterState.IDLE, message="ready")

    class _FakeBackend:
        def status(self):
            return expected

    monkeypatch.setattr("printers.services.get_printer_backend", lambda p: _FakeBackend())

    assert printer_status(printer) is expected
