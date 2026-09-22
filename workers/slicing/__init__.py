"""Slicing worker contract.

This package documents the out-of-process slicing worker. The implementation
lives in the Django app so Celery can autodiscover it:

* backend code: ``slicers/base.py``, ``slicers/prusaslicer.py``
* 3MF plate writer: ``slicers/threemf.py``
* orchestration: ``slicers/services.py``
* Celery task: ``slicers.tasks.slice_print_job``
* task name: ``slicers.slice_print_job``

A ``PrintJob`` either points at a single ``designs.ModelVersion``
(``SlicerBackend.slice``) or at a ``slicers.BuildPlate`` with several
``PlateItem`` rows (``SlicerBackend.slice_plate``); the worker picks the path
from ``PrintJob.build_plate_id``. The Django web process only enqueues the task;
it never runs a slicer inside the request cycle. See ``README.md`` here.
"""

from __future__ import annotations

#: Fully-qualified Celery task name used to enqueue a slicing job.
SLICE_TASK_NAME = "slicers.slice_print_job"

__all__ = ["SLICE_TASK_NAME"]
