"""Print queue service over ``PrintJob`` (terv.md 14., 20. fejezet).

Business logic for enqueueing, dispatching and tracking print jobs. Views, DRF
serializers and MCP tools call these functions; nothing here may depend on DRF
or an HTTP request.

Ordering
--------

Higher ``priority`` first, then FIFO by ``created_at`` (the ``PrintJob.Meta``
ordering, mirrored here explicitly so the contract is obvious):

    PrintJob.objects.filter(printer=..., status=QUEUED).order_by("-priority", "created_at")

State machine
-------------

Valid transitions are declared in :data:`ALLOWED_TRANSITIONS`. Re-transitioning
to the *same* status is an idempotent no-op. ``COMPLETED`` is terminal;
``FAILED`` and ``CANCELLED`` may be requeued explicitly (``-> QUEUED``).

Permission (terv.md 20.)
-----------------------

Printer control is a **separate** permission (``PRINTER_OPERATOR``), not MEMBER.
The service layer enforces it itself -- it never relies on the HTTP-layer
``IsPrinterOperator`` -- so an API/MCP caller cannot bypass it. Every entry point
that can start, pause, cancel or complete a print takes an optional ``user``:

* when ``user`` is provided and the target is a *control* status
  (:data:`CONTROL_STATUSES`), :func:`~printers.permissions.require_printer_operator`
  is called and a non-operator gets :class:`~django.core.exceptions.PermissionDenied`;
* when ``user`` is ``None`` the check is skipped, which is what internal workers
  (slicer, dispatcher) do.

Scope
-----

:func:`jobs_for_user` is the read entry point for the API: it returns only the
jobs whose project lives in a workspace the caller is a member of.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.db import transaction
from django.db.models import QuerySet

from files.services import get_storage
from notifications.services import NotificationKind, notify_job_owner
from workspaces.models import WorkspaceMember

from .factory import get_printer_backend
from .models import Printer, PrintJob, PrintJobStatus
from .permissions import require_printer_operator

logger = logging.getLogger(__name__)

__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "CONTROL_STATUSES",
    "QUEUEABLE_STATUSES",
    "STATUS_NOTIFICATION_KINDS",
    "InvalidTransitionError",
    "QueueError",
    "assign_filament",
    "cancel_job",
    "enqueue_job",
    "job_status",
    "jobs_for_user",
    "next_job",
    "printer_status",
    "queue_for_printer",
    "set_slicing_metadata",
    "start_job",
    "transition",
]


class QueueError(ValueError):
    """Base class for invalid print-queue operations."""


class InvalidTransitionError(QueueError):
    """The requested ``PrintJob`` status change is not allowed."""


#: Statuses that are waiting to be dispatched (eligible for ``next_job``).
QUEUEABLE_STATUSES = (PrintJobStatus.QUEUED,)

#: Statuses where a physical print is running and the adapter is involved.
ACTIVE_STATUSES = frozenset({PrintJobStatus.PRINTING, PrintJobStatus.PAUSED})

#: Target statuses that require ``PRINTER_OPERATOR`` when a user is supplied
#: (terv.md 20.). ``FAILED`` is intentionally absent: workers may fail a job.
CONTROL_STATUSES = frozenset(
    {
        PrintJobStatus.PRINTING,
        PrintJobStatus.PAUSED,
        PrintJobStatus.CANCELLED,
        PrintJobStatus.COMPLETED,
    }
)

#: The print-job state machine. Keys/values are ``PrintJobStatus`` members.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PrintJobStatus.QUEUED: frozenset(
        {
            PrintJobStatus.PREPARING,
            PrintJobStatus.SLICING,
            PrintJobStatus.READY,
            PrintJobStatus.CANCELLED,
            PrintJobStatus.FAILED,
        }
    ),
    PrintJobStatus.PREPARING: frozenset(
        {
            PrintJobStatus.SLICING,
            PrintJobStatus.READY,
            PrintJobStatus.CANCELLED,
            PrintJobStatus.FAILED,
        }
    ),
    PrintJobStatus.SLICING: frozenset(
        {
            PrintJobStatus.READY,
            PrintJobStatus.CANCELLED,
            PrintJobStatus.FAILED,
        }
    ),
    PrintJobStatus.READY: frozenset(
        {
            PrintJobStatus.PRINTING,
            PrintJobStatus.CANCELLED,
            PrintJobStatus.FAILED,
        }
    ),
    PrintJobStatus.PRINTING: frozenset(
        {
            PrintJobStatus.PAUSED,
            PrintJobStatus.COMPLETED,
            PrintJobStatus.CANCELLED,
            PrintJobStatus.FAILED,
        }
    ),
    PrintJobStatus.PAUSED: frozenset(
        {
            PrintJobStatus.PRINTING,
            PrintJobStatus.CANCELLED,
            PrintJobStatus.FAILED,
        }
    ),
    # COMPLETED is terminal. FAILED/CANCELLED may be explicitly requeued.
    PrintJobStatus.COMPLETED: frozenset(),
    PrintJobStatus.FAILED: frozenset({PrintJobStatus.QUEUED}),
    PrintJobStatus.CANCELLED: frozenset({PrintJobStatus.QUEUED}),
}


# ---------------------------------------------------------------------------
# Owner notifications (best-effort; never change the transition result)
# ---------------------------------------------------------------------------

#: Target status -> notification kind (terv.md 21. fejezet, Phase 7).
#: Silent statuses (QUEUED, PREPARING, SLICING, READY, PAUSED) are absent on
#: purpose. ``CANCELLED`` reuses ``PRINT_FAILED`` because there is no dedicated
#: "cancelled" kind; the message distinguishes them.
STATUS_NOTIFICATION_KINDS: dict[str, str] = {
    PrintJobStatus.PRINTING: NotificationKind.PRINT_STARTED,
    PrintJobStatus.COMPLETED: NotificationKind.PRINT_DONE,
    PrintJobStatus.FAILED: NotificationKind.PRINT_FAILED,
    PrintJobStatus.CANCELLED: NotificationKind.PRINT_FAILED,
}

_STATUS_NOTIFICATION_MESSAGES: dict[str, str] = {
    PrintJobStatus.PRINTING: "A(z) #{id} feladat nyomtatása elindult.",
    PrintJobStatus.COMPLETED: "A(z) #{id} feladat elkészült.",
    PrintJobStatus.FAILED: "A(z) #{id} feladat sikertelen lett.",
    PrintJobStatus.CANCELLED: "A(z) #{id} feladat le lett mondva.",
}


def _notify_job_owner(job: PrintJob, message: str, kind: str) -> None:
    """Best-effort owner notification: never raises into the caller.

    ``notify_job_owner`` already swallows its own errors; the guard here keeps
    that guarantee even if an alternate implementation raises.
    """
    try:
        notify_job_owner(job=job, message=message, kind=kind)
    except Exception:  # noqa: BLE001 - notifications must not break the queue
        logger.exception("Failed to notify the owner of PrintJob %s", getattr(job, "pk", job))


def _notify_status_change(job: PrintJob, status: str) -> None:
    """Emit the notification for a persisted ``status`` change, if any.

    Called only after the new status has been saved and only on the success path
    of :func:`transition`, so rejected transitions never notify. ``user`` is
    irrelevant here: the owner is notified even for internal worker calls.
    """
    kind = STATUS_NOTIFICATION_KINDS.get(status)
    if kind is None:
        return
    _notify_job_owner(job, _STATUS_NOTIFICATION_MESSAGES[status].format(id=job.pk), kind)


def enqueue_job(
    *,
    project,
    model_version,
    printer: Printer,
    created_by=None,
    priority: int = 0,
    filament=None,
    slicer_profile=None,
    printer_profile=None,
) -> PrintJob:
    """Create a ``QUEUED`` print job.

    ``model_version`` must belong to ``project``; ``priority`` must be >= 0.
    ``filament``/``slicer_profile``/``printer_profile`` are optional slicer
    configuration FKs and are stored as given. The printer is *not* required to
    be active -- a queue may legitimately hold work for a printer that is
    temporarily offline.

    After the job is persisted the owner gets a ``PRINT_QUEUED`` notification
    (best-effort: a notification failure never fails the enqueue).
    """
    if model_version.project_id != project.pk:
        raise QueueError("model_version does not belong to the given project")
    if priority < 0:
        raise QueueError("priority must be >= 0")
    job = PrintJob.objects.create(
        project=project,
        model_version=model_version,
        printer=printer,
        filament=filament,
        slicer_profile=slicer_profile,
        printer_profile=printer_profile,
        created_by=created_by,
        priority=priority,
        status=PrintJobStatus.QUEUED,
    )
    _notify_job_owner(
        job,
        f"A(z) #{job.pk} feladat sorba állítva.",
        NotificationKind.PRINT_QUEUED,
    )
    return job


# ---------------------------------------------------------------------------
# Scoped reads (Phase 7 -- shared printer queue)
# ---------------------------------------------------------------------------


def jobs_for_user(user) -> QuerySet[PrintJob]:
    """Jobs in every workspace ``user`` is a member of.

    Membership is resolved through :class:`~workspaces.models.WorkspaceMember`
    via a subquery (no join duplication, so no ``distinct()`` and no
    PostgreSQL ``DISTINCT``/``ORDER BY`` pitfalls). A workspace OWNER is an
    explicit member, so owners are included; anonymous users get an empty
    queryset. Ordered ``-priority, created_at`` and eager-loads the relations
    the API serializes.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return PrintJob.objects.none()
    member_workspaces = WorkspaceMember.objects.filter(user=user).values("workspace_id")
    return (
        PrintJob.objects.filter(project__workspace_id__in=member_workspaces)
        .select_related(
            "project",
            "project__workspace",
            "printer",
            "filament",
            "slicer_profile",
            "printer_profile",
            "model_version",
            "created_by",
        )
        .order_by("-priority", "created_at")
    )


def job_status(job: PrintJob) -> dict[str, Any]:
    """Small read model for polling a single job (no extra queries)."""
    return {
        "job_id": job.pk,
        "status": job.status,
        "priority": job.priority,
        "printer_id": job.printer_id,
        "gcode": getattr(job.gcode, "name", "") or "",
        "slicing": dict(job.slicing_json or {}),
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def queue_for_printer(printer: Printer):
    """Queued jobs for ``printer``: higher priority first, then FIFO."""
    return PrintJob.objects.filter(
        printer=printer,
        status__in=QUEUEABLE_STATUSES,
    ).order_by("-priority", "created_at")


def next_job(printer: Printer) -> PrintJob | None:
    """Return the next job to dispatch for ``printer``, or ``None``."""
    return queue_for_printer(printer).first()


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def _coerce_status(status) -> str:
    if isinstance(status, PrintJobStatus):
        return status
    try:
        return PrintJobStatus(status)
    except ValueError as exc:
        raise InvalidTransitionError(f"Unknown PrintJob status: {status!r}") from exc


@transaction.atomic
def transition(job: PrintJob, status, *, user=None) -> PrintJob:
    """Move ``job`` to ``status`` after validating the transition.

    Returns the (possibly unchanged) job. Raises :class:`InvalidTransitionError`
    for an unknown status or a transition not declared in
    :data:`ALLOWED_TRANSITIONS`. Same-status calls are idempotent no-ops.

    When ``user`` is provided and the target is a control status
    (:data:`CONTROL_STATUSES`), ``PRINTER_OPERATOR`` is required
    (terv.md 20.). The state machine is validated *before* the permission check,
    so an invalid move never leaks whether the caller could operate.

    On success the job owner is notified according to
    :data:`STATUS_NOTIFICATION_KINDS` (best-effort, after the new status is
    persisted). Rejected transitions notify nobody, and a notification failure
    never changes the transition result.
    """
    new_status = _coerce_status(status)
    current = job.status
    if new_status == current:
        return job

    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if new_status not in allowed:
        raise InvalidTransitionError(
            f"Cannot transition PrintJob #{job.pk} from {current} to {new_status}."
        )

    if user is not None and new_status in CONTROL_STATUSES:
        require_printer_operator(user, job.printer)

    job.status = new_status
    job.save(update_fields=["status", "updated_at"])
    _notify_status_change(job, new_status)
    return job


# ---------------------------------------------------------------------------
# Slicing metadata (replaces the sidecar estimate file)
# ---------------------------------------------------------------------------


@transaction.atomic
def set_slicing_metadata(job: PrintJob, **data: Any) -> PrintJob:
    """Merge ``data`` into ``PrintJob.slicing_json``.

    ``slicer-worker`` calls this after slicing (``format``, ``gcode_bytes``,
    ``estimate``, ``computed_at``, ...) instead of writing a sidecar estimate
    file. Existing keys are preserved; values must be JSON-serializable.
    """
    merged = dict(job.slicing_json or {})
    merged.update(data)
    try:
        json.dumps(merged)
    except (TypeError, ValueError) as exc:
        raise QueueError(f"slicing metadata is not JSON serializable: {exc}") from exc
    job.slicing_json = merged
    job.save(update_fields=["slicing_json", "updated_at"])
    return job


# ---------------------------------------------------------------------------
# Start / cancel paths (always guarded by PRINTER_OPERATOR)
# ---------------------------------------------------------------------------


def start_job(job: PrintJob, *, user=None, backend=None, storage=None) -> PrintJob:
    """Upload and start a ``READY`` job on its printer, then mark it ``PRINTING``.

    Permission: ``PRINTER_OPERATOR`` is required for ``user`` (when given).
    ``backend``/``storage`` are injectable for tests; in production they come
    from :func:`~printers.factory.get_printer_backend` and
    :func:`files.services.get_storage`.

    The adapter is never faked: an unconfigured K2 transport raises
    :class:`~printers.base.PrinterProtocolNotImplementedError` and the job stays
    ``READY``. Resuming a ``PAUSED`` job is a plain
    ``transition(job, PRINTING, user=...)`` (the pause/resume command is not part
    of the ``PrinterBackend`` contract yet).
    """
    if user is not None:
        require_printer_operator(user, job.printer)
    if job.status != PrintJobStatus.READY:
        raise InvalidTransitionError(
            f"Only a READY job can be started (PrintJob #{job.pk} is {job.status}); "
            "resume a PAUSED job with transition(job, PRINTING, user=...)."
        )

    gcode_name = getattr(job.gcode, "name", "") or ""
    if not gcode_name:
        raise QueueError(f"PrintJob #{job.pk} has no G-code to upload")

    adapter = backend if backend is not None else get_printer_backend(job.printer)
    store = storage if storage is not None else get_storage()
    remote = adapter.upload(store.read_bytes(gcode_name), gcode_name.rsplit("/", 1)[-1])
    adapter.start(remote)
    return transition(job, PrintJobStatus.PRINTING, user=user)


def cancel_job(job: PrintJob, *, user=None, backend=None) -> PrintJob:
    """Cancel ``job``, telling the printer only when a print is active.

    Permission: ``PRINTER_OPERATOR`` is required for ``user`` (when given).
    ``backend`` is injectable for tests. ``QUEUED``/``READY`` jobs are cancelled
    without touching the printer; ``PRINTING``/``PAUSED`` jobs call
    ``backend.cancel()`` first.
    """
    if user is not None:
        require_printer_operator(user, job.printer)
    if job.status == PrintJobStatus.CANCELLED:
        return job
    if PrintJobStatus.CANCELLED not in ALLOWED_TRANSITIONS.get(job.status, frozenset()):
        raise InvalidTransitionError(f"Cannot cancel PrintJob #{job.pk} from {job.status}.")

    if job.status in ACTIVE_STATUSES:
        adapter = backend if backend is not None else get_printer_backend(job.printer)
        adapter.cancel()
    return transition(job, PrintJobStatus.CANCELLED, user=user)


# ---------------------------------------------------------------------------
# Printer status / filament assignment
# ---------------------------------------------------------------------------


def printer_status(printer: Printer):
    """Return the live :class:`~printers.base.PrinterStatus` via the adapter."""
    return get_printer_backend(printer).status()


@transaction.atomic
def assign_filament(*, job: PrintJob, filament) -> PrintJob:
    """Assign a ``slicers.FilamentProfile`` to ``job`` (or ``None`` to clear)."""
    job.filament = filament
    job.save(update_fields=["filament", "updated_at"])
    return job
