"""Integration tests for slicing orchestration + the Celery task."""

from __future__ import annotations

import pytest
from factories import ModelVersionFactory, ProjectFactory, UserFactory, WorkspaceFactory

from files.services import LocalStorage
from printers.models import Printer, PrintJob, PrintJobStatus
from slicers import services, tasks
from slicers.base import SlicedResult, SlicerError
from slicers.models import PrinterProfile

pytestmark = pytest.mark.django_db

GCODE = b"; estimated printing time (normal mode) = 1m 0s\n; filament used [g] = 1.50\nG1 X0 Y0\n"


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.received: tuple | None = None

    def slice(self, model, printer, filament, process):  # noqa: ANN001
        self.received = (model, printer, filament, process)
        return SlicedResult(
            data=GCODE,
            format="gcode",
            estimated_time_sec=60,
            estimated_filament_g=1.5,
            metadata={"slicer": "fake"},
        )

    def estimate(self, result: SlicedResult) -> dict:
        return {
            "estimated_time_sec": result.estimated_time_sec,
            "estimated_time": "1m",
            "filament_g": result.estimated_filament_g,
            "filament_mm": None,
            "format": result.format,
            "slicer": "fake",
        }


class BoomBackend(FakeBackend):
    def slice(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise SlicerError("slicer exploded")


@pytest.fixture
def job():
    user = UserFactory()
    workspace = WorkspaceFactory(owner=user)
    project = ProjectFactory(workspace=workspace, created_by=user)
    version = ModelVersionFactory(project=project, created_by=user)
    printer = Printer.objects.create(name="K2 Pro", backend="creality")
    return PrintJob.objects.create(
        project=project,
        model_version=version,
        printer=printer,
        created_by=user,
    )


def _storage_with_model(job: PrintJob, tmp_path) -> LocalStorage:
    storage = LocalStorage(root=tmp_path)
    storage.write_bytes("models/v1.stl", b"solid x\nendsolid x\n")
    job.model_version.stl_file.name = "models/v1.stl"
    job.model_version.save(update_fields=["stl_file"])
    return storage


def test_slice_job_stores_gcode_and_estimate(job, tmp_path):
    storage = _storage_with_model(job, tmp_path)
    backend = FakeBackend()

    result = services.slice_job(job, backend=backend, storage=storage)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.READY
    assert job.gcode.name == services.gcode_path(job.project_id, job.pk)
    assert storage.read_bytes(job.gcode.name) == GCODE
    assert result["status"] == "READY"
    assert result["estimate"]["filament_g"] == 1.5
    assert backend.received[0] == b"solid x\nendsolid x\n"

    metadata = job.slicing_json
    assert metadata["status"] == "READY"
    assert metadata["format"] == "gcode"
    assert metadata["gcode"] == job.gcode.name
    assert metadata["gcode_bytes"] == len(GCODE)
    assert metadata["slicer"] == "fake"
    assert metadata["estimate"]["filament_g"] == 1.5
    assert "computed_at" in metadata


def test_slice_job_links_resolved_printer_profile(job, tmp_path):
    profile = PrinterProfile.objects.create(
        name="K2 Pro",
        printer_model="K2",
        backend="creality",
        settings_json={"nozzle_diameter": 0.4},
    )
    storage = _storage_with_model(job, tmp_path)

    services.slice_job(job, backend=FakeBackend(), storage=storage)

    job.refresh_from_db()
    assert job.printer_profile_id == profile.id


def test_slice_job_links_default_printer_profile(job, tmp_path):
    profile = PrinterProfile.objects.create(name="Generic", is_default=True)
    storage = _storage_with_model(job, tmp_path)

    services.slice_job(job, backend=FakeBackend(), storage=storage)

    job.refresh_from_db()
    assert job.printer_profile_id == profile.id


def test_status_is_slicing_while_the_backend_runs(job, tmp_path):
    storage = _storage_with_model(job, tmp_path)
    observed: dict[str, str] = {}

    class SpyBackend(FakeBackend):
        def slice(self, *args, **kwargs):  # noqa: ANN002, ANN003
            observed["status"] = PrintJob.objects.get(pk=job.pk).status
            return super().slice(*args, **kwargs)

    services.slice_job(job, backend=SpyBackend(), storage=storage)
    assert observed["status"] == PrintJobStatus.SLICING


def test_slice_job_persists_failure(job, tmp_path):
    storage = _storage_with_model(job, tmp_path)

    with pytest.raises(SlicerError):
        services.slice_job(job, backend=BoomBackend(), storage=storage)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.FAILED
    assert job.slicing_json["status"] == "FAILED"
    assert "slicer exploded" in job.slicing_json["errors"][0]
    assert not storage.exists(services.gcode_path(job.project_id, job.pk))


def test_slice_job_requires_a_model_artifact(job, tmp_path):
    storage = LocalStorage(root=tmp_path)

    with pytest.raises(SlicerError):
        services.slice_job(job, backend=FakeBackend(), storage=storage)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.FAILED


def test_printer_settings_resolve_by_name(job):
    PrinterProfile.objects.create(
        name="K2 Pro",
        printer_model="K2",
        backend="creality",
        settings_json={"nozzle_diameter": 0.4},
    )
    settings = services.printer_settings(job.printer)
    assert settings.name == "K2 Pro"
    assert settings.settings["nozzle_diameter"] == 0.4


def test_printer_settings_fall_back_to_default():
    profile = PrinterProfile.objects.create(
        name="Default", settings_json={"bed_shape": "0x0"}, is_default=True
    )
    settings = services.printer_settings(None)
    assert settings.name == profile.name
    assert settings.settings["bed_shape"] == "0x0"


def test_celery_task_slices(job, tmp_path, monkeypatch):
    storage = _storage_with_model(job, tmp_path)
    monkeypatch.setattr(services, "get_backend", lambda: FakeBackend())
    monkeypatch.setattr(services, "get_storage", lambda: storage)

    result = tasks.slice_print_job(job.pk)

    job.refresh_from_db()
    assert result["status"] == "READY"
    assert job.status == PrintJobStatus.READY
    assert storage.exists(job.gcode.name)
    assert job.slicing_json["status"] == "READY"
    assert job.slicing_json["estimate"]["filament_g"] == 1.5


def test_celery_task_handles_missing_job():
    assert tasks.slice_print_job(999_999) == {"job_id": 999_999, "status": "missing"}


def test_task_name_is_stable():
    assert tasks.slice_print_job.name == "slicers.slice_print_job"
