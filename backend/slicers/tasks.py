"""Celery tasks for the ``slicers`` app (terv.md 12., 14. fejezet).

The Django request cycle never runs a slicer binary; it only enqueues
``slice_print_job``. The task drives ``PrintJob.status`` through
``PREPARING -> SLICING -> READY`` (or ``FAILED``) and stores the G-code via
``files.services.get_storage()``.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from printers.models import PrintJob

from .services import slice_job

__all__ = ["slice_print_job"]

logger = logging.getLogger(__name__)


@shared_task(name="slicers.slice_print_job")
def slice_print_job(job_id: int) -> dict[str, Any]:
    """Slice ``PrintJob`` ``job_id`` and persist the G-code + estimate.

    Returns a small result dict. A missing job is a no-op; any failure after
    the job is loaded is persisted as ``FAILED`` and re-raised so Celery can
    retry and record the failure.
    """
    job = PrintJob.objects.filter(pk=job_id).first()
    if job is None:
        logger.warning("slice_print_job: PrintJob %s not found", job_id)
        return {"job_id": job_id, "status": "missing"}
    return slice_job(job)
