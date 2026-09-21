"""Tests for the print-queue service (``printers.services``) -- no network.

Queue ordering and transition validation run against the ORM (sqlite-safe).
The printer adapter is faked for ``printer_status``/``start_job``/``cancel_job``;
the unconfigured K2 adapter is exercised for the "never fake success" guarantee.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from designs.services import create_next_version
from files.services import LocalStorage
from printers.base import PrinterProtocolNotImplementedError, PrinterState, PrinterStatus
from printers.models import Printer, PrintJob, PrintJobStatus
from printers.services import (
    ALLOWED_TRANSITIONS,
    CONTROL_STATUSES,
    InvalidTransitionError,
    QueueError,
    assign_filament,
    cancel_job,
    enqueue_job,
    job_status,
    jobs_for_user,
    next_job,
    printer_status,
    queue_for_printer,
    set_slicing_metadata,
    start_job,
    transition,
)
from projects.services import create_project
from slicers.models import FilamentProfile, PrinterProfile
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def user():
    """Workspace owner -- a member, but deliberately *not* a printer operator."""
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


@pytest.fixture
def member():
    return User.objects.create_user(username="mem", email="mem@example.com", password="pw")


@pytest.fixture
def stranger():
    return User.objects.create_user(username="str", email="str@example.com", password="pw")


@pytest.fixture
def operator():
    """Staff user -- the current stand-in for PRINTER_OPERATOR."""
    return User.objects.create_user(
        username="op", email="op@example.com", password="pw", is_staff=True
    )


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


@pytest.fixture
def printer_profile():
    return PrinterProfile.objects.create(name="K2 Pro", backend="creality_k2")


def _set_created_at(job: PrintJob, when) -> None:
    PrintJob.objects.filter(pk=job.pk).update(created_at=when)


def _attach_gcode(job: PrintJob, storage: LocalStorage, data: bytes = b"G28\n") -> str:
    name = f"print_jobs/{job.project_id}/{job.pk}.gcode"
    storage.write_bytes(name, data)
    job.gcode.name = name
    job.save(update_fields=["gcode"])
    return name


class FakePrinterBackend:
    """Duck-typed adapter recording the calls ``start_job``/``cancel_job`` make."""

    def __init__(self, remote_filename: str = "remote.gcode") -> None:
        self.calls: list[object] = []
        self.remote_filename = remote_filename

    def status(self) -> PrinterStatus:
        return PrinterStatus(online=True, state=PrinterState.IDLE)

    def upload(self, gcode_bytes: bytes, filename: str) -> str:
        self.calls.append(("upload", gcode_bytes, filename))
        return self.remote_filename

    def start(self, filename: str) -> None:
        self.calls.append(("start", filename))

    def cancel(self) -> None:
        self.calls.append("cancel")

    def cfs_slots(self):
        return []


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


def test_enqueue_job_persists_printer_profile(project, version, printer, printer_profile):
    job = enqueue_job(
        project=project,
        model_version=version,
        printer=printer,
        printer_profile=printer_profile,
    )

    job.refresh_from_db()
    assert job.printer_profile == printer_profile
    assert job.slicing_json == {}


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
# Scoped reads: jobs_for_user / job_status
# ---------------------------------------------------------------------------


def test_jobs_for_user_includes_owner_and_members(project, version, printer, user, member):
    add_member(workspace=project.workspace, user=member)
    job = enqueue_job(project=project, model_version=version, printer=printer)

    assert list(jobs_for_user(user).values_list("pk", flat=True)) == [job.pk]
    assert list(jobs_for_user(member).values_list("pk", flat=True)) == [job.pk]


def test_jobs_for_user_excludes_non_members(project, version, printer, stranger):
    enqueue_job(project=project, model_version=version, printer=printer)

    assert jobs_for_user(stranger).count() == 0


def test_jobs_for_user_excludes_other_workspaces(project, version, printer, user, member, stranger):
    add_member(workspace=project.workspace, user=member)
    enqueue_job(project=project, model_version=version, printer=printer)

    other_workspace = create_workspace(name="Other", owner=stranger)
    other_project = create_project(workspace=other_workspace, name="Other", created_by=stranger)
    other_version = create_next_version(project=other_project, created_by=stranger)
    enqueue_job(project=other_project, model_version=other_version, printer=printer)

    # `user` is only in the first workspace; `stranger` only in the second.
    assert jobs_for_user(user).count() == 1
    assert jobs_for_user(stranger).count() == 1
    assert set(jobs_for_user(member).values_list("pk", flat=True)) == set(
        jobs_for_user(user).values_list("pk", flat=True)
    )


def test_jobs_for_user_orders_priority_then_fifo(project, version, printer, user):
    base = timezone.now()
    low = enqueue_job(project=project, model_version=version, printer=printer, priority=0)
    high = enqueue_job(project=project, model_version=version, printer=printer, priority=5)
    _set_created_at(low, base)
    _set_created_at(high, base + timedelta(seconds=1))

    assert list(jobs_for_user(user).values_list("pk", flat=True)) == [high.pk, low.pk]


def test_jobs_for_user_eager_loads_api_relations(project, user):
    select_related = jobs_for_user(user).query.select_related

    assert "printer" in select_related
    assert "model_version" in select_related
    assert "project" in select_related
    assert "filament" in select_related
    assert "printer_profile" in select_related


def test_jobs_for_user_anonymous_is_empty():
    assert jobs_for_user(AnonymousUser()).count() == 0
    assert jobs_for_user(None).count() == 0


def test_job_status_read_model(project, version, printer):
    job = enqueue_job(project=project, model_version=version, printer=printer, priority=2)
    set_slicing_metadata(job, format="gcode", gcode_bytes=123, estimate={"minutes": 4})

    status = job_status(job)

    assert status["job_id"] == job.pk
    assert status["status"] == PrintJobStatus.QUEUED
    assert status["priority"] == 2
    assert status["printer_id"] == printer.pk
    assert status["slicing"]["gcode_bytes"] == 123
    assert status["slicing"]["estimate"] == {"minutes": 4}
    assert status["updated_at"]


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


def test_control_statuses_are_the_print_actions():
    assert CONTROL_STATUSES == {
        PrintJobStatus.PRINTING,
        PrintJobStatus.PAUSED,
        PrintJobStatus.CANCELLED,
        PrintJobStatus.COMPLETED,
    }


# ---------------------------------------------------------------------------
# Permission enforcement at the service layer (terv.md 20.)
# ---------------------------------------------------------------------------


def test_transition_control_status_rejects_non_operator(project, version, printer, member):
    add_member(workspace=project.workspace, user=member, role=WorkspaceRole.MEMBER)
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.READY)

    with pytest.raises(PermissionDenied):
        transition(job, PrintJobStatus.PRINTING, user=member)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.READY  # nothing mutated


def test_transition_control_status_allows_workspace_admin(project, version, printer, member):
    add_member(workspace=project.workspace, user=member, role=WorkspaceRole.ADMIN)
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.READY)

    transition(job, PrintJobStatus.PRINTING, user=member)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.PRINTING


def test_transition_control_status_allows_operator(project, version, printer, operator):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.READY)

    transition(job, PrintJobStatus.PRINTING, user=operator)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.PRINTING


def test_transition_without_user_is_allowed_for_workers(project, version, printer):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.READY)

    transition(job, PrintJobStatus.PRINTING)  # internal dispatcher / worker

    job.refresh_from_db()
    assert job.status == PrintJobStatus.PRINTING


def test_transition_non_control_status_needs_no_permission(project, version, printer, user):
    job = enqueue_job(project=project, model_version=version, printer=printer)

    transition(job, PrintJobStatus.PREPARING, user=user)  # slicer path

    job.refresh_from_db()
    assert job.status == PrintJobStatus.PREPARING


def test_transition_invalid_move_does_not_leak_permission_check(project, version, printer, user):
    job = enqueue_job(project=project, model_version=version, printer=printer)

    # Invalid move wins over the permission check, so a non-operator cannot
    # probe the state machine through the error type.
    with pytest.raises(InvalidTransitionError):
        transition(job, PrintJobStatus.COMPLETED, user=user)


def test_cancel_transition_rejects_non_operator(project, version, printer, member):
    add_member(workspace=project.workspace, user=member, role=WorkspaceRole.MEMBER)
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.READY)

    with pytest.raises(PermissionDenied):
        transition(job, PrintJobStatus.CANCELLED, user=member)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.READY


# ---------------------------------------------------------------------------
# set_slicing_metadata
# ---------------------------------------------------------------------------


def test_set_slicing_metadata_merges_and_persists(project, version, printer):
    job = enqueue_job(project=project, model_version=version, printer=printer)

    set_slicing_metadata(job, format="gcode", gcode_bytes=10)
    set_slicing_metadata(job, estimate={"minutes": 3})

    job.refresh_from_db()
    assert job.slicing_json == {
        "format": "gcode",
        "gcode_bytes": 10,
        "estimate": {"minutes": 3},
    }


def test_set_slicing_metadata_rejects_non_serializable(project, version, printer):
    job = enqueue_job(project=project, model_version=version, printer=printer)

    with pytest.raises(QueueError, match="not JSON serializable"):
        set_slicing_metadata(job, bad=object())

    job.refresh_from_db()
    assert job.slicing_json == {}


# ---------------------------------------------------------------------------
# start_job / cancel_job (guarded adapter paths)
# ---------------------------------------------------------------------------


def test_start_job_uploads_starts_and_marks_printing(project, version, printer, operator, tmp_path):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.READY)
    storage = LocalStorage(root=tmp_path)
    _attach_gcode(job, storage, b"G28\n")
    backend = FakePrinterBackend()

    start_job(job, user=operator, backend=backend, storage=storage)

    assert backend.calls == [
        ("upload", b"G28\n", f"{job.pk}.gcode"),
        ("start", "remote.gcode"),
    ]
    job.refresh_from_db()
    assert job.status == PrintJobStatus.PRINTING


def test_start_job_rejects_non_operator_without_touching_backend(
    project, version, printer, member, tmp_path
):
    add_member(workspace=project.workspace, user=member, role=WorkspaceRole.MEMBER)
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.READY)
    backend = FakePrinterBackend()

    with pytest.raises(PermissionDenied):
        start_job(job, user=member, backend=backend, storage=LocalStorage(root=tmp_path))

    assert backend.calls == []
    job.refresh_from_db()
    assert job.status == PrintJobStatus.READY


def test_start_job_requires_a_ready_job(project, version, printer, operator, tmp_path):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    backend = FakePrinterBackend()

    with pytest.raises(InvalidTransitionError, match="READY"):
        start_job(job, user=operator, backend=backend, storage=LocalStorage(root=tmp_path))

    assert backend.calls == []


def test_start_job_requires_gcode(project, version, printer, operator, tmp_path):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.READY)

    with pytest.raises(QueueError, match="no G-code"):
        start_job(
            job,
            user=operator,
            backend=FakePrinterBackend(),
            storage=LocalStorage(root=tmp_path),
        )


def test_start_job_does_not_fake_success_for_unconfigured_k2(
    project, version, printer, operator, tmp_path
):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.READY)
    storage = LocalStorage(root=tmp_path)
    _attach_gcode(job, storage)

    with pytest.raises(PrinterProtocolNotImplementedError):
        start_job(job, user=operator, storage=storage)  # real factory -> K2 adapter

    job.refresh_from_db()
    assert job.status == PrintJobStatus.READY  # not faked to PRINTING


def test_cancel_job_calls_backend_for_active_print(project, version, printer, operator):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.READY)
    transition(job, PrintJobStatus.PRINTING)
    backend = FakePrinterBackend()

    cancel_job(job, user=operator, backend=backend)

    assert backend.calls == ["cancel"]
    job.refresh_from_db()
    assert job.status == PrintJobStatus.CANCELLED


def test_cancel_job_skips_backend_for_queued_job(project, version, printer, operator):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    backend = FakePrinterBackend()

    cancel_job(job, user=operator, backend=backend)

    assert backend.calls == []
    job.refresh_from_db()
    assert job.status == PrintJobStatus.CANCELLED


def test_cancel_job_rejects_non_operator(project, version, printer, member):
    add_member(workspace=project.workspace, user=member, role=WorkspaceRole.MEMBER)
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.READY)
    backend = FakePrinterBackend()

    with pytest.raises(PermissionDenied):
        cancel_job(job, user=member, backend=backend)

    assert backend.calls == []
    job.refresh_from_db()
    assert job.status == PrintJobStatus.READY


def test_cancel_job_is_idempotent_for_operator(project, version, printer, operator):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.CANCELLED)
    backend = FakePrinterBackend()

    assert cancel_job(job, user=operator, backend=backend) is job
    assert backend.calls == []


def test_cancel_job_rejects_completed_job(project, version, printer, operator):
    job = enqueue_job(project=project, model_version=version, printer=printer)
    transition(job, PrintJobStatus.READY)
    transition(job, PrintJobStatus.PRINTING)
    transition(job, PrintJobStatus.COMPLETED)

    with pytest.raises(InvalidTransitionError):
        cancel_job(job, user=operator, backend=FakePrinterBackend())


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
