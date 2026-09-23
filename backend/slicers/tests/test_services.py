"""Integration tests for slicing orchestration + the Celery task."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from factories import ModelVersionFactory, ProjectFactory, UserFactory, WorkspaceFactory

from configuration.models import AppSettings
from configuration.services import invalidate_settings_cache
from files.services import LocalStorage
from notifications.services import NotificationKind
from printers.models import Printer, PrintJob, PrintJobStatus
from slicers import prusaslicer, services, tasks
from slicers.base import SlicedResult, SlicerError
from slicers.models import FilamentProfile, PrinterProfile

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


def test_filament_settings_defaults_density_from_material():
    profile = FilamentProfile.objects.create(name="Generic PETG", material="PETG")

    settings = services.filament_settings(profile)

    assert settings.settings["filament_density"] == 1.27


def test_filament_settings_keeps_explicit_density():
    profile = FilamentProfile.objects.create(
        name="PLA", material="PLA", settings_json={"filament_density": 1.30}
    )

    settings = services.filament_settings(profile)

    assert settings.settings["filament_density"] == 1.30


def test_slice_job_persists_nonzero_filament_g_for_pla_without_density(job, tmp_path, monkeypatch):
    """Regression: a PLA profile without ``filament_density`` must not yield 0 g."""
    job.filament = FilamentProfile.objects.create(name="Generic PLA", material="PLA")
    job.save(update_fields=["filament"])
    storage = _storage_with_model(job, tmp_path)
    loaded_ini: list[str] = []

    gcode = (
        b"; estimated printing time (normal mode) = 1m 0s\n"
        b"; filament used [mm] = 1000.00\n"
        b"; filament used [g] = 0.00\n"
        b"G1 X0 Y0 E0\n"
    )

    def run(args, **kwargs):  # noqa: ANN001, ANN003
        Path(args[args.index("--output") + 1]).write_bytes(gcode)
        for index, value in enumerate(args):
            if value == "--load":
                loaded_ini.append(Path(args[index + 1]).read_text())
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    backend = prusaslicer.PrusaSlicerBackend(mode="local")

    result = services.slice_job(job, backend=backend, storage=storage)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.READY
    assert job.slicing_json["estimate"]["filament_g"] > 0
    assert result["estimate"]["filament_g"] > 0
    assert any("filament_density = 1.24" in text for text in loaded_ini)


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


# ---------------------------------------------------------------------------
# Notifications (best-effort)
# ---------------------------------------------------------------------------


def test_task_notifies_owner_on_success(job, tmp_path, monkeypatch):
    storage = _storage_with_model(job, tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(services, "get_backend", lambda: FakeBackend())
    monkeypatch.setattr(services, "get_storage", lambda: storage)
    monkeypatch.setattr(services, "notify_job_owner", lambda **kwargs: calls.append(kwargs))

    tasks.slice_print_job(job.pk)

    assert len(calls) == 1
    assert calls[0]["kind"] == NotificationKind.SLICE_READY
    assert calls[0]["job"].pk == job.pk
    assert calls[0]["url"] == "/printers/history/"
    assert str(job.pk) in calls[0]["message"]


def test_task_notifies_owner_on_failure(job, tmp_path, monkeypatch):
    storage = _storage_with_model(job, tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(services, "get_backend", lambda: BoomBackend())
    monkeypatch.setattr(services, "get_storage", lambda: storage)
    monkeypatch.setattr(services, "notify_job_owner", lambda **kwargs: calls.append(kwargs))

    with pytest.raises(SlicerError):
        tasks.slice_print_job(job.pk)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.FAILED
    assert len(calls) == 1
    assert calls[0]["kind"] == NotificationKind.SLICE_FAILED
    assert "slicer exploded" in calls[0]["message"]


def test_notification_error_does_not_break_slicing(job, tmp_path, monkeypatch):
    storage = _storage_with_model(job, tmp_path)

    def boom(**kwargs):  # noqa: ANN003
        raise RuntimeError("notification backend down")

    monkeypatch.setattr(services, "get_backend", lambda: FakeBackend())
    monkeypatch.setattr(services, "get_storage", lambda: storage)
    monkeypatch.setattr(services, "notify_job_owner", boom)

    result = tasks.slice_print_job(job.pk)

    job.refresh_from_db()
    assert result["status"] == "READY"
    assert job.status == PrintJobStatus.READY
    assert storage.exists(job.gcode.name)


def test_notification_error_does_not_mask_slice_failure(job, tmp_path, monkeypatch):
    storage = _storage_with_model(job, tmp_path)

    def boom(**kwargs):  # noqa: ANN003
        raise RuntimeError("notification backend down")

    monkeypatch.setattr(services, "get_backend", lambda: BoomBackend())
    monkeypatch.setattr(services, "get_storage", lambda: storage)
    monkeypatch.setattr(services, "notify_job_owner", boom)

    with pytest.raises(SlicerError, match="slicer exploded"):
        tasks.slice_print_job(job.pk)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.FAILED


def test_missing_job_does_not_notify(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(services, "notify_job_owner", lambda **kwargs: calls.append(kwargs))

    assert tasks.slice_print_job(999_999) == {"job_id": 999_999, "status": "missing"}
    assert calls == []


# ---------------------------------------------------------------------------
# Runtime settings integration (real configuration.services.get_setting)
# ---------------------------------------------------------------------------


def test_resolve_config_reads_real_runtime_settings():
    AppSettings.objects.create(slicer_mode="docker", slicer_timeout_sec=42)
    invalidate_settings_cache()

    resolved = prusaslicer.PrusaSlicerBackend().resolve_config()

    assert resolved.mode == "docker"
    assert resolved.timeout_sec == 42


def test_runtime_setting_change_applies_without_new_backend():
    AppSettings.objects.create(slicer_mode="local", slicer_timeout_sec=11)
    invalidate_settings_cache()
    backend = prusaslicer.PrusaSlicerBackend()

    assert backend.resolve_config().mode == "local"

    AppSettings.objects.filter(pk=1).update(slicer_mode="docker", slicer_timeout_sec=22)
    invalidate_settings_cache()

    resolved = backend.resolve_config()
    assert resolved.mode == "docker"
    assert resolved.timeout_sec == 22
