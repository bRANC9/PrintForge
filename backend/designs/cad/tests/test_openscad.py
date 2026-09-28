"""Unit tests for the OpenSCAD backend (CLI is mocked, no real binary needed)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from designs.cad.base import (
    CADBackend,
    CADError,
    GeneratedModel,
    SpecificationError,
    UnsupportedFormatError,
)
from designs.cad.openscad import (
    OpenSCADBackend,
    OpenSCADError,
    OpenSCADTimeout,
    OpenSCADValidationError,
    build_parameters,
    validate_scad_source,
)

SPEC = {
    "object": "phone_holder",
    "dimensions": {"width": 70.6, "height": 147, "thickness": 7.6},
    "angle": 15,
    "wall_thickness": 4,
    "mounting": {"type": "M5", "count": 2},
}


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


def test_cad_backend_is_abstract():
    with pytest.raises(TypeError):
        CADBackend()


# ---------------------------------------------------------------------------
# generate / template
# ---------------------------------------------------------------------------


def test_generate_interpolates_parameters():
    source = OpenSCADBackend().generate(SPEC)
    assert "70.6" in source
    assert "147.0" in source
    assert "phone_holder" in source
    assert "cube(" in source
    assert "cylinder(" in source
    assert source.rstrip().endswith("holder();")


def test_generated_source_is_self_contained():
    source = OpenSCADBackend().generate(SPEC)
    assert validate_scad_source(source) == []
    assert "import(" not in source
    assert "surface(" not in source


def test_render_scad_is_deterministic():
    backend = OpenSCADBackend()
    assert backend.generate(SPEC) == backend.generate(dict(SPEC))


def test_mounting_hole_diameter_includes_clearance():
    params = build_parameters(SPEC)
    assert params.hole_diameter == pytest.approx(5.3)
    assert params.hole_count == 2


def test_mounting_type_can_be_numeric():
    spec = {**SPEC, "mounting": {"type": "3.5", "count": 1}}
    assert build_parameters(spec).hole_diameter == pytest.approx(3.8)


@pytest.mark.parametrize(
    ("mount_type", "expected"),
    [
        ("M3", 3.3),
        ("M5", 5.3),
        ("standard_6mm", 6.3),
        ("m3 screw", 3.3),
        ("M3x10", 3.3),
        ("banana", 5.3),  # no number -> M5 fallback
        ("", 5.3),
    ],
)
def test_hole_diameter_is_tolerant(mount_type, expected):
    """Unrecognised mounting labels resolve deterministically instead of raising."""
    spec = {**SPEC, "mounting": {"type": mount_type}}
    assert build_parameters(spec).hole_diameter == pytest.approx(expected)


@pytest.mark.parametrize(
    "spec",
    [
        {"dimensions": {"width": -1}},
        {"dimensions": {"width": 1e12}},
        {"dimensions": {"height": "not-a-number"}},
        {"angle": 500},
        {"wall_thickness": 0.1},
        {"mounting": {"count": 99}},
        {"mounting": "M5"},
        {"angle": True},
    ],
)
def test_invalid_specification_raises(spec):
    with pytest.raises(SpecificationError):
        build_parameters(spec)


def test_specification_cannot_inject_code():
    injected = {**SPEC, "dimensions": {"width": '70; import("/etc/passwd")'}}
    with pytest.raises(SpecificationError):
        OpenSCADBackend().generate(injected)


def test_object_name_is_sanitised():
    spec = {**SPEC, "object": 'bad"); import("/etc/passwd");//'}
    name = build_parameters(spec).object_name
    assert "/" not in name
    assert '"' not in name
    assert ";" not in name


# ---------------------------------------------------------------------------
# build_model: the generate step's own warnings
# ---------------------------------------------------------------------------
#
# ``render_scad`` records what the weak-model tolerance had to skip or
# synthesise, but until ``generate_reported`` existed those notes only ever
# existed as ``// warning:`` comments inside the SCAD text -- which the UI never
# sees. A version that quietly fell back to a synthesised box therefore looked
# finished. The hook returns them alongside the source so
# ``designs.cad.pipeline.render_version`` can put them in
# ``validation_json["warnings"]``.
#
# A check that fires on good output is worse than no check, so the third case
# (a valid specification) is pinned as carefully as the two that produce notes.

#: A non-holder object with a declared envelope, so the box fallback is legal.
WIDGET = {
    "object": "widget",
    "dimensions": {"width": 40.0, "height": 60.0, "thickness": 10.0},
}

#: The exact note for an empty ``primitives`` list on a non-holder object.
SYNTHESIS_WARNING = "no usable primitives; synthesized a box from 'dimensions'"

#: The exact note for one unusable primitive, as the parse error renders it.
SKIPPED_WARNING = (
    "skipped primitives[0]: primitives[0].type: must be one of "
    "['box', 'cylinder', 'sphere', 'cone', 'extrude'] (got 'torus')"
)


def test_build_model_reports_the_synthesised_box_note():
    """``primitives: []`` -> a box from ``dimensions``, and it says so."""
    model = OpenSCADBackend().build_model({**WIDGET, "primitives": []})

    assert list(model.warnings) == [SYNTHESIS_WARNING]
    # The fallback is real, not just reported: the box is in the source.
    assert "primitive" in model.scad_source
    # The same note is still in the SCAD text, so the artifact stays auditable.
    assert SYNTHESIS_WARNING in model.scad_source


def test_build_model_reports_the_skipped_primitive_and_the_fallback():
    """An unknown ``type`` is skipped, then the empty result is synthesised.

    Both notes travel, in the order the parse produced them: the skip first
    (the real problem), the fallback second (its consequence). Only reporting the
    fallback would hide *why* the part is not what was asked for.
    """
    model = OpenSCADBackend().build_model(
        {**WIDGET, "primitives": [{"type": "torus", "role": "add", "major_radius": 20.0}]}
    )

    assert list(model.warnings) == [SKIPPED_WARNING, SYNTHESIS_WARNING]
    # The unusable primitive is gone from the geometry (it survives only inside
    # the ``// warning:`` comment) and the synthesised box took its place.
    assert "torus(" not in model.scad_source
    assert "cube(" in model.scad_source


def test_build_model_is_silent_for_a_valid_specification():
    """Good output must stay silent: a warning that always fires is noise."""
    valid = {
        **WIDGET,
        "primitives": [
            {
                "type": "box",
                "role": "add",
                "width": 40.0,
                "depth": 60.0,
                "height": 10.0,
                "position": {"x": 0.0, "y": 0.0, "z": 5.0},
            }
        ],
    }

    model = OpenSCADBackend().build_model(valid)

    assert model.warnings == ()
    assert "warning:" not in model.scad_source


def test_build_model_renders_exactly_what_generate_renders():
    """The source it persists is the one it reports against.

    ``render_version`` takes the source and the warnings from the *same*
    ``build_model`` call. If that call ever started rendering differently from
    ``generate``, a part would ship that contradicts its own reported warnings.
    """
    backend = OpenSCADBackend()
    for spec in (SPEC, {**WIDGET, "primitives": []}, {**WIDGET, "primitives": []}):
        assert backend.build_model(spec).scad_source == backend.generate(spec)


def test_build_model_runs_the_generate_step_exactly_once():
    """One call, because the generate step is not free.

    A mesh backend reads its source from storage, transforms, repairs and
    decimates it. A pipeline that asked the backend twice -- once for the source
    and once for the warnings -- would do that work twice and throw the first
    result away, roughly doubling the time of a large mesh import. The notes
    therefore ride on the model rather than on a second call.
    """
    calls = []

    class Counting(CADBackend):
        name = "counting"

        def generate(self, specification):
            calls.append(1)
            return "// source"

        def validate(self, model):
            return []

        def export(self, model, format):
            return b""

    Counting().build_model({"anything": True})

    assert len(calls) == 1


def test_the_default_build_model_keeps_the_plain_generate_contract():
    """A backend that has nothing to report is unchanged by the warnings field.

    ``GeneratedModel.warnings`` defaults to empty and the base ``build_model`` is
    concrete, not abstract, so an existing or duck-typed backend keeps working
    and simply reports nothing.
    """

    class Minimal(CADBackend):
        name = "minimal"

        def generate(self, specification):
            return "// minimal source"

        def validate(self, model):
            return []

        def export(self, model, format):
            return b""

    model = Minimal().build_model({"anything": True})

    assert model.scad_source == "// minimal source"
    assert model.warnings == ()


# ---------------------------------------------------------------------------
# validate / safety scan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        'import("/etc/passwd");',
        'surface(file = "/etc/passwd");',
        'import_stl("evil.stl");',
        "include <BOSL2/std.scad>",
        "use <../secret.scad>",
    ],
)
def test_forbidden_constructs_are_detected(source):
    problems = validate_scad_source(source)
    assert problems
    assert any("Forbidden" in problem for problem in problems)


def test_comments_do_not_trip_the_scan():
    source = "// import() and surface() are banned\ncube([1,1,1]);\n/* use <x> */\n"
    assert validate_scad_source(source) == []


def test_empty_source_is_a_problem():
    assert validate_scad_source("   ") == ["OpenSCAD source is empty"]


def test_validate_reports_specification_problems():
    model = GeneratedModel(
        specification={"dimensions": {"width": -5}}, scad_source="cube([1,1,1]);"
    )
    problems = OpenSCADBackend().validate(model)
    assert any("dimensions.width" in problem for problem in problems)


# ---------------------------------------------------------------------------
# export (CLI mocked)
# ---------------------------------------------------------------------------


def _model(source: str | None = None) -> GeneratedModel:
    source = source or OpenSCADBackend().generate(SPEC)
    return GeneratedModel(specification=SPEC, scad_source=source)


def test_export_local_uses_list_args_and_timeout(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        with open(args[2], "wb") as handle:
            handle.write(b"solid fake\nendsolid fake\n")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    backend = OpenSCADBackend(mode="local", binary="openscad")
    data = backend.export(_model(), "stl")

    assert data.startswith(b"solid fake")
    args = captured["args"]
    assert isinstance(args, list)
    assert args[0] == "openscad"
    assert args[1] == "-o"
    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == backend.config.timeout_sec


def test_docker_args_include_exact_sandbox_flags(tmp_path):
    backend = OpenSCADBackend(mode="docker", image="printforge-openscad")
    args = backend.build_args(tmp_path / "in", tmp_path / "out")
    joined = " ".join(args)

    for flag in (
        "--network none",
        "--read-only",
        "--tmpfs /tmp",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 256",
        "--memory 1g",
        "--cpus 1.0",
    ):
        assert flag in joined, flag

    assert "/out/model.stl" in args
    assert "/work/model.scad" in args
    assert args[0] == "docker"


def test_export_rejects_unsupported_format():
    with pytest.raises(UnsupportedFormatError):
        OpenSCADBackend().export(_model(), "step")


def test_export_refuses_forbidden_source():
    model = GeneratedModel(specification=SPEC, scad_source='import("/etc/passwd");')
    with pytest.raises(OpenSCADValidationError):
        OpenSCADBackend().export(model, "stl")


def test_export_raises_on_nonzero_exit(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "syntax error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(OpenSCADError):
        OpenSCADBackend().export(_model(), "stl")


def test_export_raises_on_missing_output(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(OpenSCADError):
        OpenSCADBackend().export(_model(), "stl")


def test_export_times_out(monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(OpenSCADTimeout):
        OpenSCADBackend(timeout_sec=3).export(_model(), "stl")


def test_unknown_mode_is_rejected():
    with pytest.raises(CADError):
        OpenSCADBackend(mode="nope")


# ---------------------------------------------------------------------------
# Runtime settings (configuration.services.get_setting)
# ---------------------------------------------------------------------------


def _patch_runtime_settings(monkeypatch, values):
    """Patch the lazy settings reader with a dict-backed fake."""
    monkeypatch.setattr(
        "designs.cad.openscad._read_runtime_setting",
        lambda name, default=None: values.get(name, default),
    )


def test_runtime_settings_resolved_per_access(monkeypatch):
    values = {
        "openscad_mode": "local",
        "openscad_timeout_sec": 60,
        "openscad_memory_limit": "1g",
        "openscad_cpu_limit": "1.0",
    }
    _patch_runtime_settings(monkeypatch, values)
    backend = OpenSCADBackend()
    assert backend.config.mode == "local"
    assert backend.config.timeout_sec == 60.0

    # A UI change must apply on the next access, without a restart / new backend.
    values.update(
        {
            "openscad_mode": "docker",
            "openscad_timeout_sec": 7,
            "openscad_memory_limit": "512m",
            "openscad_cpu_limit": "0.5",
        }
    )
    config = backend.config
    assert config.mode == "docker"
    assert config.timeout_sec == 7.0
    assert config.memory_limit == "512m"
    assert config.cpu_limit == "0.5"


def test_docker_args_use_runtime_limits(monkeypatch, tmp_path):
    _patch_runtime_settings(
        monkeypatch,
        {
            "openscad_mode": "docker",
            "openscad_timeout_sec": 30,
            "openscad_memory_limit": "2g",
            "openscad_cpu_limit": "1.5",
        },
    )
    args = OpenSCADBackend().build_args(tmp_path / "in", tmp_path / "out")
    joined = " ".join(args)
    assert "--memory 2g" in joined
    assert "--cpus 1.5" in joined
    assert "--network none" in joined


def test_export_timeout_comes_from_runtime_settings(monkeypatch):
    _patch_runtime_settings(monkeypatch, {"openscad_timeout_sec": 12})
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        with open(args[2], "wb") as handle:
            handle.write(b"solid fake\n")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    OpenSCADBackend().export(_model(), "stl")
    assert captured["timeout"] == 12.0


def test_default_image_is_the_ghcr_image(monkeypatch):
    monkeypatch.delenv("OPENSCAD_IMAGE", raising=False)
    _patch_runtime_settings(monkeypatch, {"openscad_mode": "docker"})
    backend = OpenSCADBackend()
    assert backend.config.image == "ghcr.io/branc9/printforge-openscad:latest"
    args = backend.build_args(Path("/in"), Path("/out"))
    assert "ghcr.io/branc9/printforge-openscad:latest" in args


def test_openscad_image_env_override(monkeypatch):
    monkeypatch.setenv("OPENSCAD_IMAGE", "registry.example/openscad:dev")
    _patch_runtime_settings(monkeypatch, {"openscad_mode": "docker"})
    assert OpenSCADBackend().config.image == "registry.example/openscad:dev"


def test_reads_through_configuration_service(monkeypatch):
    try:
        import configuration.services as configuration_services
    except Exception:  # noqa: BLE001 - configuration app may not be ready yet
        pytest.skip("configuration.services not available yet")

    calls: list[str] = []
    values = {
        "openscad_mode": "docker",
        "openscad_timeout_sec": 9,
        "openscad_memory_limit": "3g",
        "openscad_cpu_limit": "2.0",
    }

    def fake_get_setting(name):
        calls.append(name)
        return values.get(name)

    monkeypatch.setattr(configuration_services, "get_setting", fake_get_setting, raising=False)

    config = OpenSCADBackend().config
    assert config.mode == "docker"
    assert config.timeout_sec == 9.0
    assert config.memory_limit == "3g"
    assert config.cpu_limit == "2.0"
    assert "openscad_mode" in calls
