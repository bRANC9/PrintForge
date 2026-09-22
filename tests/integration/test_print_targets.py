"""Print-job targets: a single model version XOR a build plate (terv.md 14., 28.).

The DB check constraint only requires *at least* one target; ``enqueue_job`` and
the API serializer keep the payload unambiguous. These service-level tests
complement the plate-slicing suite (which covers the slicing path itself).
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from factories import ModelVersionFactory, PrinterFactory, ProjectFactory

from printers.models import PrintJob, PrintJobStatus
from printers.services import QueueError, enqueue_job, job_target, job_target_kind
from projects.services import create_build_plate

pytestmark = pytest.mark.django_db


@pytest.fixture
def project():
    return ProjectFactory()


@pytest.fixture
def printer():
    return PrinterFactory()


@pytest.fixture
def plate(project):
    return create_build_plate(project=project, name="Plate A", created_by=project.created_by)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_enqueue_job_requires_a_target(project, printer):
    with pytest.raises(QueueError, match="model_version or a build_plate"):
        enqueue_job(project=project, printer=printer)


def test_enqueue_job_rejects_a_foreign_model_version(project, printer):
    other_version = ModelVersionFactory(project=ProjectFactory())

    with pytest.raises(QueueError, match="does not belong"):
        enqueue_job(project=project, printer=printer, model_version=other_version)


def test_enqueue_job_rejects_a_foreign_build_plate(project, printer):
    other_plate = create_build_plate(project=ProjectFactory(), name="Theirs")

    with pytest.raises(QueueError, match="does not belong"):
        enqueue_job(project=project, printer=printer, build_plate=other_plate)


def test_enqueue_job_rejects_a_negative_priority(project, printer):
    version = ModelVersionFactory(project=project)

    with pytest.raises(QueueError, match="priority"):
        enqueue_job(project=project, printer=printer, model_version=version, priority=-1)


# ---------------------------------------------------------------------------
# Accepted targets
# ---------------------------------------------------------------------------


def test_enqueue_job_accepts_a_model_version(project, printer):
    version = ModelVersionFactory(project=project)

    job = enqueue_job(project=project, printer=printer, model_version=version)

    assert job.status == PrintJobStatus.QUEUED
    assert job_target_kind(job) == "model_version"
    assert job_target(job) == version
    assert job.build_plate_id is None


def test_enqueue_job_accepts_a_build_plate(project, printer, plate):
    job = enqueue_job(project=project, printer=printer, build_plate=plate)

    assert job.status == PrintJobStatus.QUEUED
    assert job_target_kind(job) == "build_plate"
    assert job_target(job) == plate
    assert job.model_version_id is None


def test_enqueue_job_does_not_require_an_active_printer(project):
    printer = PrinterFactory(is_active=False)
    version = ModelVersionFactory(project=project)

    job = enqueue_job(project=project, printer=printer, model_version=version)

    assert job.printer_id == printer.id


def test_job_target_prefers_the_build_plate_when_both_are_set(project, printer, plate):
    version = ModelVersionFactory(project=project)
    job = PrintJob.objects.create(
        project=project,
        printer=printer,
        model_version=version,
        build_plate=plate,
    )

    assert job_target_kind(job) == "build_plate"
    assert job_target(job) == plate


# ---------------------------------------------------------------------------
# DB check constraint
# ---------------------------------------------------------------------------


def test_check_constraint_rejects_a_job_without_a_target(project, printer):
    with pytest.raises(IntegrityError), transaction.atomic():
        PrintJob.objects.create(project=project, printer=printer)
