"""Celery tasks for the ``designs`` app.

The Django request cycle never shells out to CAD binaries; it only enqueues
``render_model_stl`` (terv.md 9. fejezet). The API polls the job status by
reading ``ModelVersion.validation_json["status"]`` for the version id.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from .cad.pipeline import render_version
from .models import ModelVersion

__all__ = ["render_model_stl"]

logger = logging.getLogger(__name__)


@shared_task(name="designs.render_model_stl")
def render_model_stl(version_id: int) -> dict[str, Any]:
    """Generate OpenSCAD + STL for ``ModelVersion`` ``version_id``.

    Loads the version, renders the source from ``specification_json``, exports
    STL through the sandboxed OpenSCAD backend and stores both artifacts via
    ``files.services.get_storage()``. Progress and failures are persisted in
    ``validation_json`` (status: queued/running/done/failed) -- no new model
    fields are introduced.
    """
    version = ModelVersion.objects.filter(pk=version_id).first()
    if version is None:
        logger.warning("render_model_stl: ModelVersion %s not found", version_id)
        return {"version_id": version_id, "status": "missing"}
    return render_version(version)
