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
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from django.conf import settings
from django.utils import timezone

from files.services import get_storage
from printers.models import PrintJob, PrintJobStatus
from printers.services import set_slicing_metadata

from .base import (
    FilamentSettings,
    PlateMesh,
    PrinterSettings,
    ProcessSettings,
    SliceModel,
    SlicerBackend,
    SlicerError,
    with_default_filament_density,
)
from .models import FilamentProfile, PrinterProfile, ProcessProfile
from .orcaslicer import OrcaSlicerBackend
from .preview import render_gcode_preview
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
    "preview_path",
    "printer_settings",
    "process_settings",
    "read_plate_items",
    "resolve_backend_name",
    "resolve_printer_profile",
    "slice_job",
]

logger = logging.getLogger(__name__)

#: ``SLICER_BACKEND`` value -> backend class. Aliases are accepted so both the
#: short and the canonical spelling work in config.
_BACKEND_REGISTRY: dict[str, type[SlicerBackend]] = {
    "prusaslicer": PrusaSlicerBackend,
    "prusa": PrusaSlicerBackend,
    "orcaslicer": OrcaSlicerBackend,
    "orca": OrcaSlicerBackend,
}


def resolve_backend_name() -> str:
    """Resolve the configured backend name (runtime -> env -> Django -> default).

    ``configuration.services.get_setting`` only knows registered runtime names;
    an unknown ``slicer_backend`` (not yet registered by the config app) falls
    through to ``SLICER_BACKEND`` / the Django setting, then ``prusaslicer``.
    """
    name = ""
    try:
        from configuration.services import get_setting

        name = str(get_setting("slicer_backend") or "")
    except Exception:  # noqa: BLE001 - unregistered key / settings backend unavailable
        name = ""
    if not name:
        name = str(os.environ.get("SLICER_BACKEND") or getattr(settings, "SLICER_BACKEND", ""))
    return (name or "prusaslicer").strip().lower()


def get_backend() -> SlicerBackend:
    """Return the configured slicer backend (indirection point for tests)."""
    name = resolve_backend_name()
    backend_cls = _BACKEND_REGISTRY.get(name)
    if backend_cls is None:
        supported = ", ".join(sorted(set(_BACKEND_REGISTRY)))
        raise SlicerError(f"Unknown SLICER_BACKEND {name!r}; expected one of: {supported}")
    return backend_cls()


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


def preview_path(project_id: int, job_id: int) -> str:
    """Relative storage path of a job's rendered G-code preview (PNG)."""
    return f"print_jobs/{project_id}/{job_id}/preview.png"


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
    """Build :class:`FilamentSettings` from a ``FilamentProfile`` row.

    When ``settings_json`` omits ``filament_density``, a published typical
    density for ``profile.material`` is filled in so the slicer can compute a
    gram estimate (see :mod:`slicers.base`). An explicit density always wins.
    """
    if profile is None:
        return FilamentSettings()
    settings = with_default_filament_density(dict(profile.settings_json or {}), profile.material)
    return FilamentSettings(
        name=profile.name,
        material=profile.material,
        brand=profile.brand,
        color=profile.color,
        settings=settings,
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


def _render_preview(job: PrintJob, storage: Any, gcode: bytes) -> dict[str, Any]:
    """Render + persist the G-code preview, never failing the slice.

    Returns the ``slicing_json`` keys to merge (``preview`` + ``preview_bytes``)
    or an empty dict when rendering/storage failed -- a preview is a nice-to-have
    and must not turn a successful slice into a failed job.
    """
    try:
        png = render_gcode_preview(gcode)
    except Exception as exc:  # noqa: BLE001 - preview is best-effort
        logger.warning("G-code preview rendering failed for PrintJob %s: %s", job.pk, exc)
        return {}

    relative = preview_path(job.project_id, job.pk)
    try:
        storage.write_bytes(relative, png)
    except Exception as exc:  # noqa: BLE001 - preview is best-effort
        logger.warning("Could not persist the G-code preview for PrintJob %s: %s", job.pk, exc)
        return {}
    return {"preview": relative, "preview_bytes": len(png)}


def _read_model(job: PrintJob, storage: Any) -> bytes:
    version = job.model_version
    name = getattr(getattr(version, "stl_file", None), "name", "") if version else ""
    if not name:
        raise SlicerError("The model version has no STL artifact to slice")
    if not storage.exists(name):
        raise SlicerError(f"The model artifact is missing from storage: {name}")
    return storage.read_bytes(name)


def read_plate_items(plate: Any, storage: Any) -> list[PlateMesh]:
    """Resolve a ``BuildPlate`` into backend-ready :class:`PlateMesh` items.

    Every item's ``model_version.stl_file`` is read through ``storage``. Raises
    :class:`SlicerError` when the plate has no items, an item has no STL
    artifact, or the artifact is missing from storage -- the caller persists the
    message into ``PrintJob.slicing_json``.
    """
    if plate is None:
        raise SlicerError("The print job has no build plate to slice")

    items = list(plate.items.select_related("model_version").order_by("id"))
    if not items:
        raise SlicerError(f"Build plate {getattr(plate, 'name', plate)!r} has no items to slice")

    meshes: list[PlateMesh] = []
    for item in items:
        version = item.model_version
        version_pk = getattr(version, "pk", None)
        artifact = getattr(getattr(version, "stl_file", None), "name", "") if version else ""
        if not artifact:
            raise SlicerError(
                f"Plate item #{item.pk} (model version #{version_pk}) has no STL artifact to slice"
            )
        if Path(artifact).suffix.lower() != ".stl":
            raise SlicerError(f"Plate item #{item.pk} artifact is not an STL file: {artifact}")
        if not storage.exists(artifact):
            raise SlicerError(f"Plate item #{item.pk} artifact is missing from storage: {artifact}")
        meshes.append(
            PlateMesh(
                model=SliceModel(
                    data=storage.read_bytes(artifact),
                    filename=Path(artifact).name,
                    format="stl",
                ),
                x=item.position_x,
                y=item.position_y,
                z=item.position_z,
                rotation_z=item.rotation_z,
                scale=item.scale,
                name=Path(artifact).stem,
                key=str(version_pk) if version_pk is not None else None,
            )
        )
    return meshes


def _plate_metadata(plate: Any, items: Sequence[PlateMesh]) -> dict[str, Any]:
    """Plate-level slicing metadata (item count + per-item breakdown)."""
    return {
        "build_plate": getattr(plate, "pk", None),
        "item_count": len(items),
        "plate": {
            "id": getattr(plate, "pk", None),
            "name": getattr(plate, "name", ""),
            "item_count": len(items),
            "items": [
                {
                    "name": item.name,
                    "model_version_id": int(item.key) if item.key and item.key.isdigit() else None,
                    "x": item.x,
                    "y": item.y,
                    "z": item.z,
                    "rotation_z": item.rotation_z,
                    "scale": item.scale,
                }
                for item in items
            ],
        },
    }


def slice_job(
    job: PrintJob,
    *,
    backend: SlicerBackend | None = None,
    storage: Any | None = None,
    model_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Slice ``job`` and persist the G-code + estimate.

    A job with a ``build_plate`` gathers its ``PlateItem`` meshes and calls
    :meth:`SlicerBackend.slice_plate`; otherwise the single ``model_version``
    path is used, so existing jobs are unaffected. ``model_bytes`` only applies
    to that single-model path.

    ``PrintJob.status`` goes ``PREPARING -> SLICING -> READY`` on success. On
    any failure the status is persisted as ``FAILED`` and the exception is
    re-raised so Celery records the failure and can retry. Estimates/metadata
    go into ``PrintJob.slicing_json`` (with plate item breakdown for plates) and
    the resolved slicer printer profile is stored on ``PrintJob.printer_profile``.
    """
    backend = backend or get_backend()
    storage = storage or get_storage()

    # Resolve the printer profile once and persist the match explicitly.
    profile = resolve_printer_profile(job.printer)
    job.printer_profile = profile
    job.status = PrintJobStatus.PREPARING
    job.save(update_fields=["status", "printer_profile"])

    try:
        plate_items: list[PlateMesh] | None = None
        model_data: bytes | None = None
        if job.build_plate_id:
            plate_items = read_plate_items(job.build_plate, storage)
        else:
            model_data = model_bytes if model_bytes is not None else _read_model(job, storage)

        _set_status(job, PrintJobStatus.SLICING)
        printer = printer_settings(job.printer, profile)
        filament = filament_settings(job.filament)
        process = process_settings(job.slicer_profile)
        if plate_items is not None:
            result = backend.slice_plate(plate_items, printer, filament, process)
        else:
            result = backend.slice(model_data, printer, filament, process)
        if result.data is None:
            raise SlicerError("The slicer backend produced no G-code")

        estimate = backend.estimate(result)
        relative = gcode_path(job.project_id, job.pk)
        storage.write_bytes(relative, result.data)
        metadata: dict[str, Any] = {
            "status": PrintJobStatus.READY.value,
            "format": result.format,
            "gcode": relative,
            "gcode_bytes": len(result.data),
            "slicer": estimate.get("slicer"),
            "estimate": estimate,
            "computed_at": timezone.now().isoformat(),
        }
        if plate_items is not None:
            metadata.update(_plate_metadata(job.build_plate, plate_items))
        preview = _render_preview(job, storage, result.data)
        metadata.update(preview)
        _persist_slicing_metadata(job, metadata)

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

    response: dict[str, Any] = {
        "job_id": job.pk,
        "status": PrintJobStatus.READY.value,
        "gcode": relative,
        "estimate": estimate,
    }
    if preview:
        response["preview"] = preview["preview"]
    if plate_items is not None:
        response["item_count"] = len(plate_items)
    return response
