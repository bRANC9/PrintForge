"""CAD worker contract.

This package documents (and will later host) the out-of-process CAD worker.
The implementation lives in the Django app so Celery can autodiscover it:

* backend code: ``designs/cad/``
* Celery task: ``designs.tasks.render_model_stl``
* task name: ``designs.render_model_stl``

The Django web process only enqueues the task; it never runs OpenSCAD inside
the request cycle. See ``README.md`` in this directory.
"""

from __future__ import annotations

#: Fully-qualified Celery task name used to enqueue a CAD render.
CAD_TASK_NAME = "designs.render_model_stl"

__all__ = ["CAD_TASK_NAME"]
