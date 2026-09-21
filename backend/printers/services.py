"""Print queue service over ``PrintJob`` (terv.md 14. fejezet).

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
"""

from __future__ import annotations

from django.db import transaction

from .factory import get_printer_backend
from .models import Printer, PrintJob, PrintJobStatus

__all__ = [
    "ALLOWED_TRANSITIONS",
    "QUEUEABLE_STATUSES",
    "InvalidTransitionError",
    "QueueError",
    "assign_filament",
    "enqueue_job",
    "next_job",
    "printer_status",
    "queue_for_printer",
    "transition",
]


class QueueError(ValueError):
    """Base class for invalid print-queue operations."""


class InvalidTransitionError(QueueError):
    """The requested ``PrintJob`` status change is not allowed."""


#: Statuses that are waiting to be dispatched (eligible for ``next_job``).
QUEUEABLE_STATUSES = (PrintJobStatus.QUEUED,)

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


def enqueue_job(
    *,
    project,
    model_version,
    printer: Printer,
    created_by=None,
    priority: int = 0,
    filament=None,
    slicer_profile=None,
) -> PrintJob:
    """Create a ``QUEUED`` print job.

    ``model_version`` must belong to ``project``; ``priority`` must be >= 0.
    The printer is *not* required to be active -- a queue may legitimately hold
    work for a printer that is temporarily offline.
    """
    if model_version.project_id != project.pk:
        raise QueueError("model_version does not belong to the given project")
    if priority < 0:
        raise QueueError("priority must be >= 0")
    return PrintJob.objects.create(
        project=project,
        model_version=model_version,
        printer=printer,
        filament=filament,
        slicer_profile=slicer_profile,
        created_by=created_by,
        priority=priority,
        status=PrintJobStatus.QUEUED,
    )


def queue_for_printer(printer: Printer):
    """Queued jobs for ``printer``: higher priority first, then FIFO."""
    return PrintJob.objects.filter(
        printer=printer,
        status__in=QUEUEABLE_STATUSES,
    ).order_by("-priority", "created_at")


def next_job(printer: Printer) -> PrintJob | None:
    """Return the next job to dispatch for ``printer``, or ``None``."""
    return queue_for_printer(printer).first()


def _coerce_status(status) -> str:
    if isinstance(status, PrintJobStatus):
        return status
    try:
        return PrintJobStatus(status)
    except ValueError as exc:
        raise InvalidTransitionError(f"Unknown PrintJob status: {status!r}") from exc


@transaction.atomic
def transition(job: PrintJob, status) -> PrintJob:
    """Move ``job`` to ``status`` after validating the transition.

    Returns the (possibly unchanged) job. Raises :class:`InvalidTransitionError`
    for an unknown status or a transition not declared in
    :data:`ALLOWED_TRANSITIONS`. Same-status calls are idempotent no-ops.
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

    job.status = new_status
    job.save(update_fields=["status", "updated_at"])
    return job


def printer_status(printer: Printer):
    """Return the live :class:`~printers.base.PrinterStatus` via the adapter."""
    return get_printer_backend(printer).status()


@transaction.atomic
def assign_filament(*, job: PrintJob, filament) -> PrintJob:
    """Assign a ``slicers.FilamentProfile`` to ``job`` (or ``None`` to clear)."""
    job.filament = filament
    job.save(update_fields=["filament", "updated_at"])
    return job
