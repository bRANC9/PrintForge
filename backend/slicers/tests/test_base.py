"""Unit tests for the backend-agnostic slicer interface."""

from __future__ import annotations

from pathlib import Path

import pytest

from slicers.base import (
    FilamentSettings,
    PrinterSettings,
    ProcessSettings,
    SlicedResult,
    SliceModel,
    SlicerBackend,
    SlicerError,
    SlicerTimeout,
    UnsupportedModelError,
    resolve_model,
)


def test_slicer_backend_is_abstract():
    with pytest.raises(TypeError):
        SlicerBackend()


def test_error_hierarchy():
    assert issubclass(SlicerTimeout, SlicerError)
    assert issubclass(UnsupportedModelError, SlicerError)
    assert issubclass(UnsupportedModelError, ValueError)


def test_resolve_model_from_bytes():
    model = resolve_model(b"solid x\n")
    assert model.data == b"solid x\n"
    assert model.filename == "model.stl"
    assert model.format == "stl"


def test_resolve_model_from_path(tmp_path):
    path = tmp_path / "part.3mf"
    path.write_bytes(b"3mf")
    model = resolve_model(path)
    assert model.data == b"3mf"
    assert model.filename == "part.3mf"
    assert model.format == "3mf"


def test_resolve_model_accepts_str_path(tmp_path):
    path = tmp_path / "part.stl"
    path.write_bytes(b"stl")
    assert resolve_model(str(path)).filename == "part.stl"


def test_resolve_model_unknown_extension_defaults_to_stl(tmp_path):
    path = tmp_path / "part.mesh"
    path.write_bytes(b"x")
    assert resolve_model(path).filename == "part.mesh"
    assert resolve_model(path).format == "stl"


def test_resolve_model_missing_file_raises():
    with pytest.raises(UnsupportedModelError):
        resolve_model(Path("/does/not/exist.stl"))


def test_resolve_model_rejects_unknown_type():
    with pytest.raises(UnsupportedModelError):
        resolve_model(42)  # type: ignore[arg-type]


def test_slice_model_is_passed_through():
    model = SliceModel(data=b"mesh", filename="custom.obj", format="obj")
    assert resolve_model(model) is model


def test_sliced_result_defaults():
    result = SlicedResult(data=b"gcode")
    assert result.format == "gcode"
    assert result.estimated_time_sec is None
    assert dict(result.metadata) == {}


def test_profile_dataclasses_are_data_only():
    printer = PrinterSettings(name="K2", settings={"nozzle_diameter": 0.4})
    filament = FilamentSettings(name="PLA", material="PLA", settings={})
    process = ProcessSettings(name="Fine", layer_height=0.15)
    assert printer.name == "K2"
    assert printer.settings["nozzle_diameter"] == 0.4
    assert filament.material == "PLA"
    assert process.layer_height == 0.15
