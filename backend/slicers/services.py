"""Slicing orchestration and profile resolution (terv.md 12., 14. fejezet).

The API/MCP layers only enqueue the Celery task in :mod:`slicers.tasks`; the
actual CLI runs out of process. This module turns the ORM rows into the plain
data classes the backend understands, then drives ``PrintJob.status`` through
``PREPARING -> SLICING -> READY`` (or ``FAILED``).

``PrintJob`` links to a physical ``printers.Printer`` and to
``slicers.FilamentProfile`` / ``slicers.ProcessProfile``. The matching slicer
``PrinterProfile`` is resolved by name, then backend, then ``is_default``,
falling back to the physical printer's own name/backend; the resolved profile
is persisted to ``PrintJob.printer_profile`` so the match is explicit.
Estimates/metadata are stored in ``PrintJob.slicing_json``.
"""

from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

from files.services import get_storage
from printers.models import PrintJob, PrintJobStatus
from printers.services import set_slicing_metadata

from .base import (
    FilamentSettings,
    PrinterSettings,
    ProcessSettings,
    SlicerBackend,
    SlicerError,
)
from .models import FilamentProfile, PrinterProfile, ProcessProfile
from .prusaslicer import PrusaSlicerBackend

try:  # api-dev implements this contract in parallel; degrade gracefully without it
    from notifications.services import NotificationKind, notify_job_owner
except Exception:  # noqa: BLE001 - notifications are best-effort and must never block import
    NotificationKind = None  # type: ignore[assignment]
    notify_job_owner = None  # type: ignore[assignment]

__all__ = [
    "filament_settings",
    "gcode_path",
    "get_backend",
    "notify_slice_failed",
    "notify_slice_ready",
    "printer_settings",
    "process_settings",
    "resolve_printer_profile",
    "slice_job",
]

logger = logging.getLogger(__name__)


def get_backend() -> PrusaSlicerBackend:
    """Return the configured slicer backend (indirection point for tests)."""
    return PrusaSlicerBackend()


# ---------------------------------------------------------------------------
# Notifications (best-effort, never affects the slicing outcome)
# ---------------------------------------------------------------------------


def _notify_owner(job: PrintJob, *, kind: Any, message: str, url: str = "") -> None:
    """Send an owner notification without ever raising into the slicing flow."""
    if notify_job_owner is None:
        logger.warning(
            "notify_job_owner is unavailable; skipping notification for PrintJob %s", job.pk
        )
        return
    try:
        notify_job_owner(job=job, kind=kind, message=message, url=url)
    except Exception:  # noqa: BLE001 - notifications must never break slicing
        logger.exception("Failed to notify the owner of PrintJob %s", job.pk)


def notify_slice_ready(job: PrintJob) -> None:
    """Notify the job owner that slicing finished."""
    _notify_owner(
        job,
        kind=getattr(NotificationKind, "SLICE_READY", "SLICE_READY"),
        message=f"A(z) #{job.pk} nyomtatási feladat szeletelése elkészült.",
        url="/printers/history/",
    )


def notify_slice_failed(job: PrintJob, error: BaseException) -> None:
    """Notify the job owner that slicing failed, with a short reason."""
    detail = str(error).strip().splitlines()
    reason = detail[0] if detail else type(error).__name__
    _notify_owner(
        job,
        kind=getattr(NotificationKind, "SLICE_FAILED", "SLICE_FAILED"),
        message=f"A(z) #{job.pk} nyomtatási feladat szeletelése sikertelen: {reason}",
        url="/printers/history/",
    )


# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------


def gcode_path(project_id: int, job_id: int) -> str:
    """Relative storage path of a job's G-code (matches printers' upload_to)."""
    return f"print_jobs/{project_id}/{job_id}.gcode"


# ---------------------------------------------------------------------------
# ORM rows -> data-only profile settings
# ---------------------------------------------------------------------------


def resolve_printer_profile(printer: Any | None) -> PrinterProfile | None:
    """Find the slicer printer profile for a physical printer.

    Tries an exact name match, then the ``backend`` column, then the default
    profile. Returns ``None`` when the registry is empty.
    """
    if printer is None:
        return PrinterProfile.objects.filter(is_default=True).first()

    profiles = PrinterProfile.objects.all()
    profile = profiles.filter(name=printer.name).first()
    if profile is None and getattr(printer, "backend", ""):
        profile = profiles.filter(backend=printer.backend).first()
    if profile is None:
        profile = profiles.filter(printer_model=printer.name).first()
    if profile is None:
        profile = profiles.filter(is_default=True).first()
    return profile


def printer_settings(printer: Any | None, profile: PrinterProfile | None = None) -> PrinterSettings:
    """Build :class:`PrinterSettings` from a physical printer + its profile.

    Pass ``profile`` to reuse an already-resolved :class:`PrinterProfile`
    (e.g. the one stored on ``PrintJob.printer_profile``) and avoid a second
    lookup.
    """
    resolved = profile if profile is not None else resolve_printer_profile(printer)
    if resolved is not None:
        return PrinterSettings(
            name=resolved.name,
            printer_model=resolved.printer_model,
            backend=resolved.backend or (getattr(printer, "backend", "") if printer else ""),
            settings=dict(resolved.settings_json or {}),
        )
    if printer is None:
        return PrinterSettings()
    return PrinterSettings(
        name=printer.name,
        backend=getattr(printer, "backend", ""),
        settings={},
    )


def filament_settings(profile: FilamentProfile | None) -> FilamentSettings:
    """Build :class:`FilamentSettings` from a ``FilamentProfile`` row."""
    if profile is None:
        return FilamentSettings()
    return FilamentSettings(
        name=profile.name,
        material=profile.material,
        brand=profile.brand,
        color=profile.color,
        settings=dict(profile.settings_json or {}),
    )


def process_settings(profile: ProcessProfile | None) -> ProcessSettings:
    """Build :class:`ProcessSettings` from a ``ProcessProfile`` row."""
    if profile is None:
        return ProcessSettings()
    return ProcessSettings(
        name=profile.name,
        layer_height=profile.layer_height,
        settings=dict(profile.settings_json or {}),
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _set_status(job: PrintJob, status: str) -> None:
    job.status = status
    job.save(update_fields=["status"])


def _persist_slicing_metadata(job: PrintJob, data: dict[str, Any]) -> None:
    """Merge ``data`` into ``PrintJob.slicing_json`` via printers.services.

    ``printers.services.set_slicing_metadata`` merges the keys (preserving any
    existing ones), validates JSON-serializability and persists the field.
    """
    set_slicing_metadata(job, **data)


def _read_model(job: PrintJob, storage: Any) -> bytes:
    version = job.model_version
    name = getattr(getattr(version, "stl_file", None), "name", "") if version else ""
    if not name:
        raise SlicerError("The model version has no STL artifact to slice")
    if not storage.exists(name):
        raise SlicerError(f"The model artifact is missing from storage: {name}")
    return storage.read_bytes(name)


def slice_job(
    job: PrintJob,
    *,
    backend: SlicerBackend | None = None,
    storage: Any | None = None,
    model_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Slice ``job.model_version`` and persist the G-code + estimate.

    ``PrintJob.status`` goes ``PREPARING -> SLICING -> READY`` on success. On
    any failure the status is persisted as ``FAILED`` and the exception is
    re-raised so Celery records the failure and can retry. Estimates/metadata
    go into ``PrintJob.slicing_json`` and the resolved slicer printer profile is
    stored on ``PrintJob.printer_profile``.
    """
    backend = backend or get_backend()
    storage = storage or get_storage()

    # Resolve the printer profile once and persist the match explicitly.
    profile = resolve_printer_profile(job.printer)
    job.printer_profile = profile
    job.status = PrintJobStatus.PREPARING
    job.save(update_fields=["status", "printer_profile"])

    try:
        data = model_bytes if model_bytes is not None else _read_model(job, storage)

        _set_status(job, PrintJobStatus.SLICING)
        result = backend.slice(
            data,
            printer_settings(job.printer, profile),
            filament_settings(job.filament),
            process_settings(job.slicer_profile),
        )
        if result.data is None:
            raise SlicerError("The slicer backend produced no G-code")

        estimate = backend.estimate(result)
        relative = gcode_path(job.project_id, job.pk)
        storage.write_bytes(relative, result.data)
        _persist_slicing_metadata(
            job,
            {
                "status": PrintJobStatus.READY.value,
                "format": result.format,
                "gcode": relative,
                "gcode_bytes": len(result.data),
                "slicer": estimate.get("slicer"),
                "estimate": estimate,
                "computed_at": timezone.now().isoformat(),
            },
        )

        job.gcode.name = relative
        job.status = PrintJobStatus.READY
        job.save(update_fields=["gcode", "status", "slicing_json"])
    except Exception as exc:
        logger.warning("Slicing failed for PrintJob %s: %s", job.pk, exc)
        _persist_slicing_metadata(
            job,
            {
                "status": PrintJobStatus.FAILED.value,
                "errors": [f"{type(exc).__name__}: {exc}"],
                "completed_at": timezone.now().isoformat(),
            },
        )
        job.status = PrintJobStatus.FAILED
        job.save(update_fields=["status", "slicing_json"])
        raise

    return {
        "job_id": job.pk,
        "status": PrintJobStatus.READY.value,
        "gcode": relative,
        "estimate": estimate,
    }
