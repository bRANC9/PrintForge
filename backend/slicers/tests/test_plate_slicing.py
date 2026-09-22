"""Build-plate / multi-object slicing tests (terv.md 28. fejezet).

The PrusaSlicer binary is mocked (no CLI required); binary/ASCII STL fixtures
are generated in-memory so the 3MF writer, the backend contract and the
orchestration can all be exercised deterministically.
"""

from __future__ import annotations

import struct
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from factories import ModelVersionFactory, ProjectFactory, UserFactory, WorkspaceFactory

from files.services import LocalStorage
from printers.models import Printer, PrintJob, PrintJobStatus
from slicers import prusaslicer, services
from slicers.base import (
    FilamentSettings,
    PlateMesh,
    PrinterSettings,
    ProcessSettings,
    SlicedResult,
    SliceModel,
    SlicerBackend,
    SlicerError,
)
from slicers.models import BuildPlate, PlateItem
from slicers.prusaslicer import PrusaSlicerBackend
from slicers.threemf import CORE_NAMESPACE, build_plate_3mf, item_transform, parse_stl

GCODE = (
    b"; estimated printing time (normal mode) = 2m 0s\n"
    b"; filament used [mm] = 2000.00\n"
    b"; filament used [g] = 3.00\n"
    b"G1 X0 Y0 E0\n"
)

PRINTER = PrinterSettings(name="K2 Pro", settings={"bed_shape": "0x0,350x0,350x350,0x350"})
FILAMENT = FilamentSettings(name="PLA", material="PLA", settings={"temperature": 210})
PROCESS = ProcessSettings(name="Fine", layer_height=0.2, settings={"fill_density": "20%"})

#: Two disjoint triangles sharing one edge.
TRIANGLES = (
    ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)),
    ((10.0, 0.0, 0.0), (10.0, 10.0, 0.0), (0.0, 10.0, 0.0)),
)

ASCII_STL = b"""solid tri
  facet normal 0 0 1
    outer loop
      vertex 0 0 0
      vertex 10 0 0
      vertex 0 10 0
    endloop
  endfacet
endsolid tri
"""


def _binary_stl(triangles=TRIANGLES) -> bytes:
    out = bytearray(b"PrintForge test".ljust(80, b"\x00"))
    out += struct.pack("<I", len(triangles))
    for triangle in triangles:
        out += struct.pack("<3f", 0.0, 0.0, 0.0)
        for vertex in triangle:
            out += struct.pack("<3f", *vertex)
        out += struct.pack("<H", 0)
    return bytes(out)


@pytest.fixture(autouse=True)
def _runtime_settings(monkeypatch):
    values = {"slicer_mode": "local", "slicer_timeout_sec": 300}
    monkeypatch.setattr(prusaslicer, "get_setting", lambda name: values.get(name))
    return values


# ---------------------------------------------------------------------------
# SlicerBackend default plate contract
# ---------------------------------------------------------------------------


class _DummyBackend(SlicerBackend):
    name = "dummy"

    def slice(self, model, printer, filament, process):  # noqa: ANN001
        return SlicedResult(data=b"single", metadata={"slicer": "dummy"})

    def estimate(self, result):  # noqa: ANN001
        return {"slicer": "dummy"}


def _single_item() -> PlateMesh:
    return PlateMesh(model=SliceModel(data=b"solid x\n"))


def test_base_slice_plate_single_item_falls_back_to_slice():
    result = _DummyBackend().slice_plate(
        [_single_item()], PrinterSettings(), FilamentSettings(), ProcessSettings()
    )
    assert result.data == b"single"


def test_base_slice_plate_multi_item_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        _DummyBackend().slice_plate(
            [_single_item(), _single_item()],
            PrinterSettings(),
            FilamentSettings(),
            ProcessSettings(),
        )


def test_base_slice_plate_empty_raises():
    with pytest.raises(SlicerError):
        _DummyBackend().slice_plate([], PrinterSettings(), FilamentSettings(), ProcessSettings())


# ---------------------------------------------------------------------------
# STL parsing + 3MF writing
# ---------------------------------------------------------------------------


def test_parse_binary_stl():
    mesh = parse_stl(_binary_stl())
    assert len(mesh.triangles) == 2
    assert len(mesh.vertices) == 4  # the shared edge is deduplicated


def test_parse_ascii_stl():
    mesh = parse_stl(ASCII_STL)
    assert mesh.triangles == ((0, 1, 2),)
    assert mesh.vertices == ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0))


def test_parse_stl_rejects_garbage():
    with pytest.raises(SlicerError):
        parse_stl(b"this is not a mesh")


def test_item_transform_identity():
    assert item_transform(_single_item()) == "1 0 0 0 1 0 0 0 1 0 0 0"


def test_item_transform_rotation_scale_translation():
    item = PlateMesh(model=SliceModel(data=b"x"), x=10.0, y=20.0, z=1.0, rotation_z=90.0, scale=2.0)
    values = [float(value) for value in item_transform(item).split()]
    assert values[0] == pytest.approx(0.0)
    assert values[1] == pytest.approx(2.0)
    assert values[3] == pytest.approx(-2.0)
    assert values[4] == pytest.approx(0.0)
    assert values[8] == pytest.approx(2.0)
    assert values[9:] == pytest.approx([10.0, 20.0, 1.0])


def _plate_mesh(name: str, key: str, x: float) -> PlateMesh:
    return PlateMesh(
        model=SliceModel(data=_binary_stl(), filename=f"{name}.stl"),
        x=x,
        name=name,
        key=key,
    )


def test_build_plate_3mf_has_objects_and_build_items():
    data = build_plate_3mf([_plate_mesh("a", "1", 10.0), _plate_mesh("b", "2", 50.0)])
    assert data[:2] == b"PK"

    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = set(archive.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"} <= names
        model_xml = archive.read("3D/3dmodel.model")

    root = ET.fromstring(model_xml)
    namespace = {"m": CORE_NAMESPACE}
    objects = root.findall("m:resources/m:object", namespace)
    items = root.findall("m:build/m:item", namespace)
    assert len(objects) == 2
    assert len(items) == 2
    assert items[0].get("transform", "").split()[-3:] == ["10", "0", "0"]
    assert items[1].get("transform", "").split()[-3:] == ["50", "0", "0"]


def test_build_plate_3mf_reuses_one_object_per_key():
    shared = SliceModel(data=_binary_stl(), filename="shared.stl")
    data = build_plate_3mf(
        [
            PlateMesh(model=shared, name="a", key="7"),
            PlateMesh(model=shared, name="b", key="7", x=30.0),
        ]
    )
    with zipfile.ZipFile(BytesIO(data)) as archive:
        model_xml = archive.read("3D/3dmodel.model")
    root = ET.fromstring(model_xml)
    namespace = {"m": CORE_NAMESPACE}
    assert len(root.findall("m:resources/m:object", namespace)) == 1
    assert len(root.findall("m:build/m:item", namespace)) == 2


def test_build_plate_3mf_rejects_empty_plate():
    with pytest.raises(SlicerError):
        build_plate_3mf([])


# ---------------------------------------------------------------------------
# PrusaSlicer slice_plate (CLI mocked)
# ---------------------------------------------------------------------------


def _capture_run(output: bytes | None = GCODE):
    captured: dict[str, object] = {}

    def run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["model"] = Path(args[-1]).read_bytes()
        if output is not None:
            Path(args[args.index("--output") + 1]).write_bytes(output)
        return subprocess.CompletedProcess(args, 0, "", "")

    return run, captured


def test_slice_plate_writes_3mf_and_passes_dont_arrange(monkeypatch):
    run, captured = _capture_run()
    monkeypatch.setattr(subprocess, "run", run)

    items = [_plate_mesh("a", "1", 10.0), _plate_mesh("b", "2", 50.0)]
    result = PrusaSlicerBackend(mode="local").slice_plate(items, PRINTER, FILAMENT, PROCESS)

    assert result.data == GCODE
    assert result.estimated_filament_g == pytest.approx(3.0)
    assert result.metadata["plate"]["item_count"] == 2

    args = captured["args"]
    assert isinstance(args, list)
    assert "--dont-arrange" in args
    assert args[-1].endswith("plate.3mf")
    assert captured["model"][:2] == b"PK"
    assert captured["kwargs"]["shell"] is False


def test_slice_plate_single_identity_item_uses_stl_path(monkeypatch):
    run, captured = _capture_run()
    monkeypatch.setattr(subprocess, "run", run)

    item = PlateMesh(model=SliceModel(data=b"solid x\n", filename="solo.stl"))
    PrusaSlicerBackend(mode="local").slice_plate([item], PRINTER, FILAMENT, PROCESS)

    assert "--dont-arrange" not in captured["args"]
    assert captured["args"][-1].endswith("model.stl")
    assert captured["model"] == b"solid x\n"


def test_slice_plate_rejects_empty_plate():
    with pytest.raises(SlicerError):
        PrusaSlicerBackend(mode="local").slice_plate([], PRINTER, FILAMENT, PROCESS)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@pytest.fixture
def plate_job():
    user = UserFactory()
    workspace = WorkspaceFactory(owner=user)
    project = ProjectFactory(workspace=workspace, created_by=user)
    printer = Printer.objects.create(name="K2 Pro", backend="creality")
    plate = BuildPlate.objects.create(project=project, name="Plate A", created_by=user)
    return user, project, printer, plate


class PlateBackend:
    name = "fake"

    def __init__(self) -> None:
        self.plate_items: list[PlateMesh] | None = None

    def slice(self, model, printer, filament, process):  # noqa: ANN001
        raise AssertionError("single-model slice must not be used for a plate job")

    def slice_plate(self, items, printer, filament, process):  # noqa: ANN001
        self.plate_items = list(items)
        return SlicedResult(
            data=GCODE,
            format="gcode",
            estimated_time_sec=120,
            estimated_filament_g=3.0,
            metadata={"slicer": "fake"},
        )

    def estimate(self, result: SlicedResult) -> dict:
        return {
            "estimated_time_sec": result.estimated_time_sec,
            "estimated_time": "2m",
            "filament_g": result.estimated_filament_g,
            "filament_mm": None,
            "format": result.format,
            "slicer": "fake",
        }


class SingleBackend(PlateBackend):
    def __init__(self) -> None:
        self.single_received = None

    def slice(self, model, printer, filament, process):  # noqa: ANN001
        self.single_received = (model, printer, filament, process)
        return SlicedResult(data=GCODE, format="gcode", metadata={"slicer": "fake"})

    def slice_plate(self, items, printer, filament, process):  # noqa: ANN001
        raise AssertionError("plate path must not be used for a single-model job")


def _add_item(plate, project, user, storage, name, **kwargs) -> PlateItem:
    version = ModelVersionFactory(project=project, created_by=user)
    storage.write_bytes(name, _binary_stl())
    version.stl_file.name = name
    version.save(update_fields=["stl_file"])
    return PlateItem.objects.create(build_plate=plate, model_version=version, **kwargs)


@pytest.mark.django_db
def test_slice_plate_job_calls_slice_plate_and_records_metadata(plate_job, tmp_path):
    user, project, printer, plate = plate_job
    storage = LocalStorage(root=tmp_path)
    _add_item(
        plate, project, user, storage, "models/a.stl", position_x=10.0, rotation_z=45.0, scale=1.5
    )
    _add_item(plate, project, user, storage, "models/b.stl", position_x=50.0)
    job = PrintJob.objects.create(
        project=project, build_plate=plate, printer=printer, created_by=user
    )

    backend = PlateBackend()
    result = services.slice_job(job, backend=backend, storage=storage)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.READY
    assert storage.read_bytes(job.gcode.name) == GCODE
    assert result["item_count"] == 2

    assert backend.plate_items is not None
    assert len(backend.plate_items) == 2
    first = backend.plate_items[0]
    assert first.x == 10.0
    assert first.rotation_z == 45.0
    assert first.scale == 1.5
    assert first.model.data == _binary_stl()
    assert first.key is not None

    metadata = job.slicing_json
    assert metadata["status"] == "READY"
    assert metadata["item_count"] == 2
    assert metadata["build_plate"] == plate.pk
    assert metadata["plate"]["item_count"] == 2
    assert metadata["plate"]["items"][0]["x"] == 10.0
    assert metadata["estimate"]["filament_g"] == 3.0


@pytest.mark.django_db
def test_single_model_job_still_uses_slice(plate_job, tmp_path):
    user, project, printer, _ = plate_job
    storage = LocalStorage(root=tmp_path)
    version = ModelVersionFactory(project=project, created_by=user)
    storage.write_bytes("models/solo.stl", _binary_stl())
    version.stl_file.name = "models/solo.stl"
    version.save(update_fields=["stl_file"])
    job = PrintJob.objects.create(
        project=project, model_version=version, printer=printer, created_by=user
    )

    backend = SingleBackend()
    services.slice_job(job, backend=backend, storage=storage)

    assert backend.single_received is not None
    assert backend.single_received[0] == _binary_stl()


@pytest.mark.django_db
def test_plate_job_fails_when_item_artifact_is_missing(plate_job, tmp_path):
    user, project, printer, plate = plate_job
    storage = LocalStorage(root=tmp_path)
    version = ModelVersionFactory(project=project, created_by=user)
    version.stl_file.name = "models/missing.stl"
    version.save(update_fields=["stl_file"])
    PlateItem.objects.create(build_plate=plate, model_version=version)
    job = PrintJob.objects.create(
        project=project, build_plate=plate, printer=printer, created_by=user
    )

    with pytest.raises(SlicerError, match="missing from storage"):
        services.slice_job(job, backend=PlateBackend(), storage=storage)

    job.refresh_from_db()
    assert job.status == PrintJobStatus.FAILED
    assert "missing from storage" in job.slicing_json["errors"][0]


@pytest.mark.django_db
def test_plate_job_rejects_non_stl_artifact(plate_job, tmp_path):
    user, project, printer, plate = plate_job
    storage = LocalStorage(root=tmp_path)
    version = ModelVersionFactory(project=project, created_by=user)
    storage.write_bytes("models/a.3mf", b"3mf-bytes")
    version.stl_file.name = "models/a.3mf"
    version.save(update_fields=["stl_file"])
    PlateItem.objects.create(build_plate=plate, model_version=version)
    job = PrintJob.objects.create(
        project=project, build_plate=plate, printer=printer, created_by=user
    )

    with pytest.raises(SlicerError, match="not an STL"):
        services.slice_job(job, backend=PlateBackend(), storage=storage)


@pytest.mark.django_db
def test_plate_job_rejects_empty_plate(plate_job, tmp_path):
    user, project, printer, plate = plate_job
    storage = LocalStorage(root=tmp_path)
    job = PrintJob.objects.create(
        project=project, build_plate=plate, printer=printer, created_by=user
    )

    with pytest.raises(SlicerError, match="no items"):
        services.slice_job(job, backend=PlateBackend(), storage=storage)


@pytest.mark.django_db
def test_read_plate_items_builds_plate_meshes(plate_job, tmp_path):
    user, project, _, plate = plate_job
    storage = LocalStorage(root=tmp_path)
    _add_item(plate, project, user, storage, "models/a.stl", position_y=5.0)

    items = services.read_plate_items(plate, storage)

    assert len(items) == 1
    assert items[0].model.format == "stl"
    assert items[0].model.filename == "a.stl"
    assert items[0].y == 5.0
