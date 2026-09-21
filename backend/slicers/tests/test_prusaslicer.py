"""Unit tests for the PrusaSlicer backend (CLI mocked, no binary required)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from slicers import prusaslicer
from slicers.base import (
    FilamentSettings,
    PrinterSettings,
    ProcessSettings,
    SlicedResult,
    SliceModel,
    SlicerError,
    SlicerTimeout,
)
from slicers.prusaslicer import (
    PrusaSlicerBackend,
    extract_profile_config,
    format_duration,
    parse_duration,
    parse_gcode_estimates,
    prepare_profiles,
    render_ini,
)

GCODE = (
    b"; estimated printing time (normal mode) = 1h 2m 3s\n"
    b"; filament used [mm] = 1234.56\n"
    b"; filament used [g] = 3.70\n"
    b"G1 X0 Y0 E0\n"
)

PRINTER = PrinterSettings(
    name="K2 Pro", printer_model="K2", settings={"bed_shape": "0x0,350x0,350x350,0x350"}
)
FILAMENT = FilamentSettings(name="Hyper PLA", material="PLA", settings={"temperature": 210})
PROCESS = ProcessSettings(name="Fine", layer_height=0.15, settings={"fill_density": "20%"})


@pytest.fixture(autouse=True)
def _runtime_settings(monkeypatch):
    """Deterministic runtime settings so tests never touch the DB or real env.

    Individual tests may re-patch ``prusaslicer.get_setting`` afterwards.
    """
    values = {"slicer_mode": "local", "slicer_timeout_sec": 300}
    monkeypatch.setattr(prusaslicer, "get_setting", lambda name: values.get(name))
    return values


# ---------------------------------------------------------------------------
# settings_json -> config
# ---------------------------------------------------------------------------


def test_extract_profile_config_splits_reserved_keys():
    config_files, overrides, inline = extract_profile_config(
        {
            "config_file": "/etc/printforge/printer.ini",
            "overrides": {"layer_height": 0.25},
            "nozzle_diameter": 0.4,
            "enabled": True,
        }
    )
    assert config_files == ["/etc/printforge/printer.ini"]
    assert overrides == ["--layer-height=0.25"]
    assert inline == {"nozzle_diameter": "0.4", "enabled": "1"}


def test_extract_profile_config_lists_become_comma_separated():
    _, _, inline = extract_profile_config({"bed_shape": ["0x0", "10x0", "10x10"]})
    assert inline["bed_shape"] == "0x0,10x0,10x10"


def test_invalid_setting_key_is_rejected():
    with pytest.raises(SlicerError):
        extract_profile_config({"bad key": "x"})


def test_setting_value_with_newline_is_rejected():
    with pytest.raises(SlicerError):
        extract_profile_config({"layer_height": "0.2\nG1"})


def test_overrides_must_be_a_mapping():
    with pytest.raises(SlicerError):
        extract_profile_config({"overrides": ["--layer-height=0.2"]})


def test_render_ini_is_deterministic():
    text = render_ini("printer", {"b": "2", "a": "1"}, "K2")
    assert text == "# PrintForge printer profile: K2\na = 1\nb = 2\n"


def test_prepare_profiles_writes_one_ini_per_profile(tmp_path):
    config_files, overrides = prepare_profiles(tmp_path, PRINTER, FILAMENT, PROCESS)
    names = {path.name for path in config_files}
    assert names == {"printer.ini", "filament.ini", "process.ini"}
    assert overrides == []

    process_ini = (tmp_path / "process.ini").read_text()
    assert "layer_height = 0.15" in process_ini
    assert "fill_density = 20%" in process_ini
    assert "nozzle_diameter" not in process_ini


def test_prepare_profiles_supports_external_config_files(tmp_path):
    external = tmp_path / "external.ini"
    external.write_text("fill_density = 15%\n")
    input_dir = tmp_path / "in"
    input_dir.mkdir()

    printer = PrinterSettings(
        name="K2",
        settings={"config_file": str(external), "overrides": {"layer_height": 0.25}},
    )
    config_files, overrides = prepare_profiles(
        input_dir, printer, FilamentSettings(), ProcessSettings()
    )

    assert overrides == ["--layer-height=0.25"]
    assert len(config_files) == 1
    assert config_files[0].read_text() == "fill_density = 15%\n"
    assert config_files[0].parent == input_dir


def test_prepare_profiles_missing_external_config_raises(tmp_path):
    with pytest.raises(SlicerError):
        prepare_profiles(
            tmp_path,
            PrinterSettings(settings={"config_file": str(tmp_path / "nope.ini")}),
            FilamentSettings(),
            ProcessSettings(),
        )


# ---------------------------------------------------------------------------
# estimates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1h 2m 3s", 3723), ("45s", 45), ("2m", 120), ("1d 1h", 90000), ("garbage", None)],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_format_duration_hides_empty_units():
    assert format_duration(3723) == "1h 2m 3s"
    assert format_duration(45) == "45s"
    assert format_duration(0) == "0s"
    assert format_duration(None) is None


def test_parse_gcode_estimates():
    estimates = parse_gcode_estimates(GCODE.decode())
    assert estimates["estimated_time_sec"] == 3723
    assert estimates["filament_mm"] == pytest.approx(1234.56)
    assert estimates["filament_g"] == pytest.approx(3.70)


def test_estimate_normalises_result():
    estimates = parse_gcode_estimates(GCODE.decode())
    result = PrusaSlicerBackend().estimate(
        SlicedResult(
            data=GCODE,
            estimated_time_sec=estimates["estimated_time_sec"],
            estimated_filament_g=estimates["filament_g"],
            estimated_filament_mm=estimates["filament_mm"],
            metadata={"slicer": "prusaslicer", **estimates},
        )
    )
    assert result == {
        "estimated_time_sec": 3723,
        "estimated_time": "1h 2m 3s",
        "filament_g": pytest.approx(3.70),
        "filament_mm": pytest.approx(1234.56),
        "format": "gcode",
        "slicer": "prusaslicer",
    }


# ---------------------------------------------------------------------------
# CLI (mocked)
# ---------------------------------------------------------------------------


def _fake_run(output: bytes | None = GCODE):
    captured: dict[str, object] = {}

    def run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        if output is not None:
            output_path = args[args.index("--output") + 1]
            Path(output_path).write_bytes(output)
        return subprocess.CompletedProcess(args, 0, "", "")

    return run, captured


def test_slice_local_uses_list_args_and_timeout(monkeypatch):
    run, captured = _fake_run()
    monkeypatch.setattr(subprocess, "run", run)

    backend = PrusaSlicerBackend(mode="local", binary="prusa-slicer", timeout_sec=42)
    result = backend.slice(b"solid x\n", PRINTER, FILAMENT, PROCESS)

    assert result.data == GCODE
    assert result.estimated_time_sec == 3723
    args = captured["args"]
    assert isinstance(args, list)
    assert args[0] == "prusa-slicer"
    assert "--export-gcode" in args
    assert args.count("--load") == 3
    assert str(args[-1]).endswith("model.stl")
    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 42


def test_slice_writes_generated_ini_files(monkeypatch, tmp_path):
    seen: list[str] = []

    def run(args, **kwargs):
        output_path = args[args.index("--output") + 1]
        Path(output_path).write_bytes(GCODE)
        for index, value in enumerate(args):
            if value == "--load":
                seen.append(Path(args[index + 1]).read_text())
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    backend = PrusaSlicerBackend(mode="local")
    backend.slice(b"solid x\n", PRINTER, FILAMENT, PROCESS)

    assert len(seen) == 3
    assert any("nozzle_diameter" not in text and "bed_shape" in text for text in seen)
    assert any("layer_height = 0.15" in text for text in seen)


def test_slice_passes_overrides(monkeypatch):
    run, captured = _fake_run()
    monkeypatch.setattr(subprocess, "run", run)

    process = ProcessSettings(
        name="Fine", layer_height=0.2, settings={"overrides": {"infill": "25%"}}
    )
    PrusaSlicerBackend(mode="local").slice(
        b"solid x\n", PrinterSettings(), FilamentSettings(), process
    )

    assert "--infill=25%" in captured["args"]


def test_docker_args_include_exact_sandbox_flags(tmp_path):
    backend = PrusaSlicerBackend(mode="docker", image="printforge-prusaslicer")
    config = tmp_path / "printer.ini"
    config.write_text("nozzle_diameter = 0.4\n")
    args = backend.build_args(
        tmp_path / "in",
        tmp_path / "out",
        config_files=[config],
        overrides=["--infill=20%"],
    )
    joined = " ".join(args)

    for flag in (
        "--network none",
        "--read-only",
        "--tmpfs /tmp",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 256",
    ):
        assert flag in joined, flag

    assert args[0] == "docker"
    assert "/work/printer.ini" in args
    assert "/out/model.gcode" in args
    assert "/work/model.stl" in args


def test_docker_run_argument_list(monkeypatch, tmp_path):
    monkeypatch.setattr(
        prusaslicer,
        "get_setting",
        lambda name: {"slicer_mode": "docker", "slicer_timeout_sec": 300}.get(name),
    )
    backend = PrusaSlicerBackend(image="ghcr.io/branc9/printforge-prusaslicer:latest")
    config = tmp_path / "printer.ini"
    config.write_text("nozzle_diameter = 0.4\n")

    args = backend.build_args(
        tmp_path / "in",
        tmp_path / "out",
        config_files=[config],
        overrides=["--infill=20%"],
    )

    assert args == [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "2g",
        "--cpus",
        "2.0",
        "-v",
        f"{tmp_path / 'in'}:/work:ro",
        "-v",
        f"{tmp_path / 'out'}:/out:rw",
        "ghcr.io/branc9/printforge-prusaslicer:latest",
        "--export-gcode",
        "--output",
        "/out/model.gcode",
        "--load",
        "/work/printer.ini",
        "--infill=20%",
        "/work/model.stl",
    ]


# ---------------------------------------------------------------------------
# Runtime settings (configuration.services.get_setting)
# ---------------------------------------------------------------------------


def test_default_image_name(monkeypatch):
    monkeypatch.delenv("SLICER_IMAGE", raising=False)
    monkeypatch.delenv("PRUSASLICER_IMAGE", raising=False)
    assert prusaslicer.DEFAULT_IMAGE == "ghcr.io/branc9/printforge-prusaslicer:latest"
    assert PrusaSlicerBackend().config.image == prusaslicer.DEFAULT_IMAGE


def test_slicer_image_env_overrides_default(monkeypatch):
    monkeypatch.setenv("SLICER_IMAGE", "registry.example/printforge-slicer:1.2")
    assert PrusaSlicerBackend().config.image == "registry.example/printforge-slicer:1.2"


def test_legacy_prusaslicer_image_env(monkeypatch):
    monkeypatch.delenv("SLICER_IMAGE", raising=False)
    monkeypatch.setenv("PRUSASLICER_IMAGE", "legacy/printforge-slicer:2")
    assert PrusaSlicerBackend().config.image == "legacy/printforge-slicer:2"


def test_mode_resolved_from_runtime_setting(monkeypatch, tmp_path):
    monkeypatch.setattr(
        prusaslicer,
        "get_setting",
        lambda name: {"slicer_mode": "docker"}.get(name),
    )
    backend = PrusaSlicerBackend()
    args = backend.build_args(tmp_path / "in", tmp_path / "out")
    assert args[0] == "docker"
    assert prusaslicer.DEFAULT_IMAGE in args


def test_timeout_resolved_from_runtime_setting(monkeypatch):
    monkeypatch.setattr(
        prusaslicer,
        "get_setting",
        lambda name: {"slicer_mode": "local", "slicer_timeout_sec": 123}.get(name),
    )
    assert PrusaSlicerBackend().resolve_config().timeout_sec == 123


def test_runtime_setting_change_applies_to_next_job(monkeypatch):
    state: dict[str, object] = {"slicer_mode": "local", "slicer_timeout_sec": 60}
    monkeypatch.setattr(prusaslicer, "get_setting", lambda name: state.get(name))
    backend = PrusaSlicerBackend()

    assert backend.resolve_config().mode == "local"
    state["slicer_mode"] = "docker"
    state["slicer_timeout_sec"] = 90

    resolved = backend.resolve_config()
    assert resolved.mode == "docker"
    assert resolved.timeout_sec == 90


def test_explicit_mode_wins_over_runtime_setting(monkeypatch):
    monkeypatch.setattr(
        prusaslicer,
        "get_setting",
        lambda name: "docker" if name == "slicer_mode" else None,
    )
    assert PrusaSlicerBackend(mode="local").resolve_config().mode == "local"


def test_invalid_runtime_mode_is_rejected(monkeypatch):
    monkeypatch.setattr(prusaslicer, "get_setting", lambda name: "nonsense")
    with pytest.raises(SlicerError):
        PrusaSlicerBackend().resolve_config()


def test_invalid_timeout_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(
        prusaslicer,
        "get_setting",
        lambda name: "not-a-number" if name == "slicer_timeout_sec" else "local",
    )
    assert PrusaSlicerBackend().resolve_config().timeout_sec == prusaslicer.DEFAULT_TIMEOUT_SEC


def test_slice_uses_runtime_timeout(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["kwargs"] = kwargs
        output_path = args[args.index("--output") + 1]
        Path(output_path).write_bytes(GCODE)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(
        prusaslicer,
        "get_setting",
        lambda name: {"slicer_mode": "local", "slicer_timeout_sec": 77}.get(name),
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    PrusaSlicerBackend().slice(b"solid x\n", PRINTER, FILAMENT, PROCESS)
    assert captured["kwargs"]["timeout"] == 77


def test_slice_raises_on_nonzero_exit(monkeypatch):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 2, "", "config error")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SlicerError, match="config error"):
        PrusaSlicerBackend(mode="local").slice(b"solid x\n", PRINTER, FILAMENT, PROCESS)


def test_slice_raises_on_missing_output(monkeypatch):
    run, _ = _fake_run(output=None)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SlicerError, match="no G-code"):
        PrusaSlicerBackend(mode="local").slice(b"solid x\n", PRINTER, FILAMENT, PROCESS)


def test_slice_times_out(monkeypatch):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SlicerTimeout):
        PrusaSlicerBackend(mode="local", timeout_sec=3).slice(
            b"solid x\n", PRINTER, FILAMENT, PROCESS
        )


def test_unknown_mode_is_rejected():
    with pytest.raises(SlicerError):
        PrusaSlicerBackend(mode="nope")


def test_model_suffix_is_preserved_for_3mf(monkeypatch, tmp_path):
    run, captured = _fake_run()
    monkeypatch.setattr(subprocess, "run", run)

    PrusaSlicerBackend(mode="local").slice(
        SliceModel(data=b"3mf", filename="part.3mf", format="3mf"),
        PRINTER,
        FILAMENT,
        PROCESS,
    )
    assert str(captured["args"][-1]).endswith("model.3mf")
