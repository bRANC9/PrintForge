"""Unit tests for the OrcaSlicer backend (CLI mocked, no binary/Xvfb required)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from slicers import orcaslicer, prusaslicer, services
from slicers.base import (
    FilamentSettings,
    PrinterSettings,
    ProcessSettings,
    SlicedResult,
    SlicerBackend,
    SlicerError,
    SlicerTimeout,
)
from slicers.orcaslicer import (
    DEFAULT_BINARY,
    DEFAULT_IMAGE,
    OrcaSlicerBackend,
    parse_orca_gcode_estimates,
    render_preset_json,
)
from slicers.prusaslicer import PrusaSlicerBackend

GCODE = (
    b"; estimated printing time (normal mode) = 2m 30s\n"
    b"; filament used [mm] = 500.00\n"
    b"; total filament weight [g] = 1.50\n"
    b"G1 X0 Y0 E0\n"
    b"G1 X10 Y0 E1.0\n"
)

PRINTER = PrinterSettings(
    name="K2 Pro", printer_model="K2", settings={"bed_shape": "0x0,350x0,350x350,0x350"}
)
FILAMENT = FilamentSettings(name="Hyper PLA", material="PLA", settings={"temperature": 210})
PROCESS = ProcessSettings(name="Fine", layer_height=0.15, settings={"fill_density": "20%"})


@pytest.fixture(autouse=True)
def _runtime_settings(monkeypatch):
    """Deterministic runtime settings so tests never touch the DB or real env."""
    values = {"slicer_mode": "local", "slicer_timeout_sec": 300}
    monkeypatch.setattr(prusaslicer, "get_setting", lambda name: values.get(name))
    return values


# ---------------------------------------------------------------------------
# configuration / selection
# ---------------------------------------------------------------------------


def test_default_image_name(monkeypatch):
    monkeypatch.delenv("SLICER_IMAGE", raising=False)
    monkeypatch.delenv("ORCASLICER_IMAGE", raising=False)
    assert DEFAULT_IMAGE == "ghcr.io/branc9/printforge-orcaslicer:latest"
    assert DEFAULT_BINARY == "orca-slicer"
    assert OrcaSlicerBackend().config.image == DEFAULT_IMAGE


def test_orcaslicer_image_env_overrides_default(monkeypatch):
    monkeypatch.delenv("SLICER_IMAGE", raising=False)
    monkeypatch.setenv("ORCASLICER_IMAGE", "registry.example/orca:2.4.2")
    assert OrcaSlicerBackend().config.image == "registry.example/orca:2.4.2"


def test_slicer_image_wins_over_orcaslicer_image(monkeypatch):
    monkeypatch.setenv("ORCASLICER_IMAGE", "registry.example/orca:2.4.2")
    monkeypatch.setenv("SLICER_IMAGE", "registry.example/all:1.0")
    assert OrcaSlicerBackend().config.image == "registry.example/all:1.0"


def test_orcaslicer_binary_env(monkeypatch):
    monkeypatch.setenv("ORCASLICER_BINARY", "/opt/orca/AppRun")
    assert OrcaSlicerBackend().config.binary == "/opt/orca/AppRun"


def test_backend_implements_the_contract():
    backend = OrcaSlicerBackend()
    assert isinstance(backend, SlicerBackend)
    assert isinstance(backend, PrusaSlicerBackend)
    assert backend.name == "orcaslicer"


def test_get_backend_selects_orcaslicer(monkeypatch):
    monkeypatch.setenv("SLICER_BACKEND", "orcaslicer")
    assert isinstance(services.get_backend(), OrcaSlicerBackend)


def test_get_backend_accepts_orca_alias(monkeypatch):
    monkeypatch.setenv("SLICER_BACKEND", "orca")
    assert isinstance(services.get_backend(), OrcaSlicerBackend)


def test_get_backend_defaults_to_prusaslicer(monkeypatch):
    monkeypatch.delenv("SLICER_BACKEND", raising=False)
    monkeypatch.delenv("ORCASLICER_IMAGE", raising=False)
    assert isinstance(services.get_backend(), PrusaSlicerBackend)


def test_get_backend_unknown_raises(monkeypatch):
    monkeypatch.setenv("SLICER_BACKEND", "nope")
    with pytest.raises(SlicerError):
        services.get_backend()


# ---------------------------------------------------------------------------
# profile JSON presets
# ---------------------------------------------------------------------------


def test_prepare_profiles_writes_json_presets(tmp_path):
    config_files, overrides = OrcaSlicerBackend()._prepare_profiles(
        tmp_path, PRINTER, FILAMENT, PROCESS
    )

    assert {path.name for path in config_files} == {"printer.json", "process.json", "filament.json"}
    assert overrides == []

    printer = json.loads((tmp_path / "printer.json").read_text())
    process = json.loads((tmp_path / "process.json").read_text())
    filament = json.loads((tmp_path / "filament.json").read_text())

    assert printer["type"] == "machine"
    assert printer["bed_shape"] == "0x0,350x0,350x350,0x350"
    assert process["type"] == "process"
    assert process["layer_height"] == 0.15
    assert process["fill_density"] == "20%"
    assert filament["type"] == "filament"
    # Density is defaulted from the material so the gram estimate stays non-zero.
    assert filament["filament_density"] == 1.24


def test_prepare_profiles_supports_external_config_files(tmp_path):
    external = tmp_path / "external.json"
    external.write_text('{"type": "machine", "name": "External"}')
    input_dir = tmp_path / "in"
    input_dir.mkdir()

    printer = PrinterSettings(
        name="K2",
        settings={"config_file": str(external), "overrides": {"layer_height": 0.25}},
    )
    config_files, overrides = OrcaSlicerBackend()._prepare_profiles(
        input_dir, printer, FilamentSettings(), ProcessSettings()
    )

    assert overrides == ["--layer-height=0.25"]
    assert len(config_files) == 1
    assert config_files[0].name == "printer-external-0.json"
    assert json.loads(config_files[0].read_text())["name"] == "External"


def test_prepare_profiles_missing_external_config_raises(tmp_path):
    with pytest.raises(SlicerError):
        OrcaSlicerBackend()._prepare_profiles(
            tmp_path,
            PrinterSettings(settings={"config_file": str(tmp_path / "nope.json")}),
            FilamentSettings(),
            ProcessSettings(),
        )


def test_render_preset_json_carries_the_type_discriminator():
    payload = json.loads(render_preset_json("process", {"layer_height": 0.2}, "Fine", "process"))

    assert payload["type"] == "process"
    assert payload["name"] == "Fine"
    assert payload["from"] == "User"
    assert payload["layer_height"] == 0.2


# ---------------------------------------------------------------------------
# estimates
# ---------------------------------------------------------------------------


def test_parse_orca_gcode_estimates():
    estimates = parse_orca_gcode_estimates(GCODE.decode())
    assert estimates["estimated_time_sec"] == 150
    assert estimates["filament_mm"] == pytest.approx(500.0)
    assert estimates["filament_g"] == pytest.approx(1.5)


def test_parse_orca_gcode_estimates_bambu_style():
    text = (
        "; model printing time: 1h 0m 0s; total estimated time: 1h 5m 0s\n"
        "; total filament length [mm] : 1234.56\n"
        "; total filament weight [g] : 3.70\n"
    )
    estimates = parse_orca_gcode_estimates(text)

    assert estimates["estimated_time_sec"] == 3600
    assert estimates["filament_mm"] == pytest.approx(1234.56)
    assert estimates["filament_g"] == pytest.approx(3.70)


def test_estimate_normalises_result():
    estimates = parse_orca_gcode_estimates(GCODE.decode())
    result = OrcaSlicerBackend().estimate(
        SlicedResult(
            data=GCODE,
            estimated_time_sec=estimates["estimated_time_sec"],
            estimated_filament_g=estimates["filament_g"],
            estimated_filament_mm=estimates["filament_mm"],
            metadata={"slicer": "orcaslicer", **estimates},
        )
    )

    assert result == {
        "estimated_time_sec": 150,
        "estimated_time": "2m 30s",
        "filament_g": pytest.approx(1.5),
        "filament_mm": pytest.approx(500.0),
        "format": "gcode",
        "slicer": "orcaslicer",
    }


# ---------------------------------------------------------------------------
# CLI (mocked)
# ---------------------------------------------------------------------------


def _fake_run(output: bytes | None = GCODE, filename: str = "plate_1.gcode"):
    captured: dict[str, object] = {}

    def run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        if output is not None:
            output_dir = Path(args[args.index("--outputdir") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / filename).write_bytes(output)
        return subprocess.CompletedProcess(args, 0, "", "")

    return run, captured


def test_slice_local_uses_list_args_and_timeout(monkeypatch):
    run, captured = _fake_run()
    monkeypatch.setattr(subprocess, "run", run)

    backend = OrcaSlicerBackend(mode="local", binary="orca-slicer", timeout_sec=42)
    result = backend.slice(b"solid x\n", PRINTER, FILAMENT, PROCESS)

    assert result.data == GCODE
    assert result.estimated_time_sec == 150
    assert result.estimated_filament_g == pytest.approx(1.5)

    args = captured["args"]
    assert isinstance(args, list)
    assert args[0] == "orca-slicer"
    assert args[1:3] == ["--slice", "0"]
    assert args[3] == "--outputdir"
    assert "--export-gcode" not in args  # OrcaSlicer writes G-code under --outputdir

    settings_arg = args[args.index("--load-settings") + 1]
    assert [Path(part).name for part in settings_arg.split(";")] == [
        "printer.json",
        "process.json",
    ]
    filament_arg = args[args.index("--load-filaments") + 1]
    assert Path(filament_arg).name == "filament.json"
    assert str(args[-1]).endswith("model.stl")

    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 42


def test_slice_writes_generated_json_presets(monkeypatch):
    seen: list[dict] = []

    def run(args, **kwargs):
        output_dir = Path(args[args.index("--outputdir") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "plate_1.gcode").write_bytes(GCODE)
        for flag in ("--load-settings", "--load-filaments"):
            value = args[args.index(flag) + 1]
            for part in value.split(";"):
                seen.append(json.loads(Path(part).read_text()))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    OrcaSlicerBackend(mode="local").slice(b"solid x\n", PRINTER, FILAMENT, PROCESS)

    types = {payload["type"] for payload in seen}
    assert types == {"machine", "process", "filament"}


def test_slice_passes_overrides(monkeypatch):
    run, captured = _fake_run()
    monkeypatch.setattr(subprocess, "run", run)

    process = ProcessSettings(
        name="Fine", layer_height=0.2, settings={"overrides": {"infill": "25%"}}
    )
    OrcaSlicerBackend(mode="local").slice(
        b"solid x\n", PrinterSettings(), FilamentSettings(), process
    )

    assert "--infill=25%" in captured["args"]


def test_read_gcode_picks_the_largest_file(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "result.json").write_text("{}")
    (output_dir / "tiny.gcode").write_bytes(b"G1 X0 Y0\n")
    (output_dir / "big.gcode").write_bytes(GCODE)

    assert OrcaSlicerBackend()._read_gcode(output_dir) == GCODE


def test_slice_raises_on_missing_output(monkeypatch):
    run, _ = _fake_run(output=None)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SlicerError, match="no G-code"):
        OrcaSlicerBackend(mode="local").slice(b"solid x\n", PRINTER, FILAMENT, PROCESS)


def test_slice_raises_on_nonzero_exit(monkeypatch):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 2, "", "config error")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SlicerError, match="config error"):
        OrcaSlicerBackend(mode="local").slice(b"solid x\n", PRINTER, FILAMENT, PROCESS)


def test_slice_times_out(monkeypatch):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SlicerTimeout):
        OrcaSlicerBackend(mode="local", timeout_sec=3).slice(
            b"solid x\n", PRINTER, FILAMENT, PROCESS
        )


def test_docker_args_include_exact_sandbox_flags(tmp_path):
    backend = OrcaSlicerBackend(mode="docker", image="printforge-orcaslicer")
    config = tmp_path / "printer.json"
    config.write_text('{"type": "machine"}')
    args = backend.build_args(tmp_path / "in", tmp_path / "out", config_files=[config])
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
    assert "/work/printer.json" in args
    assert "/out" in args
    assert "/work/model.stl" in args


def test_docker_run_argument_list(tmp_path):
    backend = OrcaSlicerBackend(image=DEFAULT_IMAGE)
    for name in ("printer.json", "process.json", "filament.json"):
        (tmp_path / name).write_text("{}")

    args = backend.build_args(
        tmp_path / "in",
        tmp_path / "out",
        config_files=[
            tmp_path / "printer.json",
            tmp_path / "process.json",
            tmp_path / "filament.json",
        ],
        overrides=["--infill=20%"],
        config=orcaslicer.PrusaSlicerConfig(mode="docker", image=DEFAULT_IMAGE),
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
        DEFAULT_IMAGE,
        "--slice",
        "0",
        "--outputdir",
        "/out",
        "--load-settings",
        "/work/printer.json;/work/process.json",
        "--load-filaments",
        "/work/filament.json",
        "--infill=20%",
        "/work/model.stl",
    ]


def test_dont_arrange_disables_arrangement(tmp_path):
    backend = OrcaSlicerBackend(mode="local")
    args = backend.build_args(tmp_path / "in", tmp_path / "out", dont_arrange=True)
    assert "--arrange=0" in args
