"""Slicing orchestration and profile resolution (terv.md 12., 14. fejezet).

The API/MCP layers only enqueue the Celery task in :mod:`slicers.tasks`; the
actual CLI runs out of process. This module turns the ORM rows into the plain
data classes the backend understands, then drives ``PrintJob.status`` through
``PREPARING -> SLICING -> READY`` (or ``FAILED``).

``PrintJob`` links to a physical ``printers.Printer`` and to
``slicers.FilamentProfile`` / ``slicers.ProcessProfile``. It does not carry a
``PrinterProfile`` FK, so the matching slicer printer profile is resolved by
name, then backend, then ``is_default``, falling back to the physical
printer's own name/backend.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.utils import timezone

from files.services import get_storage
from printers.models import PrintJob, PrintJobStatus

from .base import (
    FilamentSettings,
    PrinterSettings,
    ProcessSettings,
    SlicerBackend,
    SlicerError,
)
from .models import FilamentProfile, PrinterProfile, ProcessProfile
from .prusaslicer import PrusaSlicerBackend

__all__ = [
    "estimate_path",
    "filament_settings",
    "gcode_path",
    "get_backend",
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
# Storage layout
# ---------------------------------------------------------------------------


def gcode_path(project_id: int, job_id: int) -> str:
    """Relative storage path of a job's G-code (matches printers' upload_to)."""
    return f"print_jobs/{project_id}/{job_id}.gcode"


def estimate_path(project_id: int, job_id: int) -> str:
    """Relative storage path of the estimate sidecar JSON for a job."""
    return f"print_jobs/{project_id}/{job_id}.estimate.json"


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


def printer_settings(printer: Any | None) -> PrinterSettings:
    """Build :class:`PrinterSettings` from a physical printer + its profile."""
    profile = resolve_printer_profile(printer)
    if profile is not None:
        return PrinterSettings(
            name=profile.name,
            printer_model=profile.printer_model,
            backend=profile.backend or (getattr(printer, "backend", "") if printer else ""),
            settings=dict(profile.settings_json or {}),
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
    re-raised so Celery records the failure and can retry.
    """
    backend = backend or get_backend()
    storage = storage or get_storage()

    _set_status(job, PrintJobStatus.PREPARING)
    try:
        data = model_bytes if model_bytes is not None else _read_model(job, storage)

        _set_status(job, PrintJobStatus.SLICING)
        result = backend.slice(
            data,
            printer_settings(job.printer),
            filament_settings(job.filament),
            process_settings(job.slicer_profile),
        )
        if result.data is None:
            raise SlicerError("The slicer backend produced no G-code")

        estimate = backend.estimate(result)
        relative = gcode_path(job.project_id, job.pk)
        storage.write_bytes(relative, result.data)
        storage.write_bytes(
            estimate_path(job.project_id, job.pk),
            json.dumps(
                {
                    "job_id": job.pk,
                    "format": result.format,
                    "gcode_bytes": len(result.data),
                    "estimate": estimate,
                    "computed_at": timezone.now().isoformat(),
                },
                indent=2,
            ).encode("utf-8"),
        )

        job.gcode.name = relative
        job.status = PrintJobStatus.READY
        job.save(update_fields=["gcode", "status"])
    except Exception as exc:
        logger.warning("Slicing failed for PrintJob %s: %s", job.pk, exc)
        _set_status(job, PrintJobStatus.FAILED)
        raise

    return {
        "job_id": job.pk,
        "status": PrintJobStatus.READY.value,
        "gcode": relative,
        "estimate": estimate,
    }
