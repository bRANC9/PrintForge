"""Orchestration: ``ModelVersion`` -> SCAD -> STL -> storage.

The Celery task in :mod:`designs.tasks` is a thin wrapper around
:func:`render_version`. Status is persisted in ``ModelVersion.validation_json``
so the API can poll it (terv.md 9. fejezet) without any new model fields:

.. code-block:: json

    {
      "status": "queued" | "running" | "done" | "failed",
      "stage": "generate" | "export" | "done" | "failed",
      "errors": ["..."],
      "warnings": [],
      "scad_file": "projects/<id>/v<version>/model.scad",
      "stl_file": "projects/<id>/v<version>/model.stl",
      "stl_bytes": 12345,
      "started_at": "...",
      "completed_at": "..."
    }

Artifacts are written through :func:`files.services.get_storage` and the
relative paths are stored on ``scad_file`` / ``stl_file``.
"""

from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

from files.services import get_storage

from ..models import ModelVersion, model_artifact_path
from .base import CADBackend, GeneratedModel
from .openscad import OpenSCADBackend

__all__ = ["CADValidationFailed", "get_backend", "render_version"]

logger = logging.getLogger(__name__)

SCAD_FILENAME = "model.scad"
STL_FILENAME = "model.stl"


class CADValidationFailed(ValueError):
    """The backend reported blocking problems before export."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def get_backend() -> OpenSCADBackend:
    """Return the configured CAD backend (indirection point for tests)."""
    return OpenSCADBackend()


def _persist_status(
    version: ModelVersion, fields: dict[str, Any], *, update_fields: list[str] | None = None
) -> None:
    current = dict(version.validation_json or {})
    current.update(fields)
    version.validation_json = current
    version.save(update_fields=update_fields or ["validation_json"])


def render_version(
    version: ModelVersion,
    *,
    backend: CADBackend | None = None,
    storage: Any | None = None,
) -> dict[str, Any]:
    """Generate SCAD + STL for ``version`` and persist the artifacts.

    Returns a small result dict. On failure the error is persisted to
    ``validation_json`` and the exception is re-raised so Celery can mark the
    task failed / retry.
    """
    backend = backend or get_backend()
    storage = storage or get_storage()
    specification = dict(version.specification_json or {})

    _persist_status(
        version,
        {
            "status": "running",
            "stage": "generate",
            "errors": [],
            "started_at": timezone.now().isoformat(),
        },
    )

    try:
        scad_source = backend.generate(specification)
        model = GeneratedModel(specification=specification, scad_source=scad_source)

        problems = backend.validate(model)
        if problems:
            raise CADValidationFailed(problems)

        _persist_status(version, {"stage": "export"})

        stl_bytes = backend.export(model, "stl")
        scad_rel = model_artifact_path(version, SCAD_FILENAME)
        stl_rel = model_artifact_path(version, STL_FILENAME)
        storage.write_bytes(scad_rel, scad_source.encode("utf-8"))
        storage.write_bytes(stl_rel, stl_bytes)
    except Exception as exc:  # noqa: BLE001 - persist *any* failure, then re-raise
        logger.warning("CAD render failed for ModelVersion %s: %s", version.pk, exc)
        _persist_status(
            version,
            {
                "status": "failed",
                "stage": "failed",
                "errors": [f"{type(exc).__name__}: {exc}"],
                "completed_at": timezone.now().isoformat(),
            },
        )
        raise

    version.scad_file.name = scad_rel
    version.stl_file.name = stl_rel
    _persist_status(
        version,
        {
            "status": "done",
            "stage": "done",
            "errors": [],
            "scad_file": scad_rel,
            "stl_file": stl_rel,
            "stl_bytes": len(stl_bytes),
            "completed_at": timezone.now().isoformat(),
        },
        update_fields=["scad_file", "stl_file", "validation_json"],
    )

    return {
        "version_id": version.pk,
        "status": "done",
        "scad_file": scad_rel,
        "stl_file": stl_rel,
        "stl_bytes": len(stl_bytes),
    }
