"""OrcaSlicer backend: headless CLI slicing (terv.md 12. fejezet).

OrcaSlicer is a BambuStudio fork (itself a PrusaSlicer fork), so this backend
reuses :class:`~slicers.prusaslicer.PrusaSlicerBackend` and overrides only what
actually differs: the binary/image names, the profile transport and the CLI
shape. Everything else -- the sandbox flags, the timeout, the runtime
``slicer_mode``/``slicer_timeout_sec`` resolution, ``estimate()`` -- is
inherited unchanged.

Headless / Xvfb
---------------
OrcaSlicer links wxWidgets/OpenGL and opens an X display *even in CLI mode*, so
the published image (:data:`DEFAULT_IMAGE`,
``ghcr.io/branc9/printforge-orcaslicer:latest``) wraps the binary in
``xvfb-run -a`` (see ``docker/slicer-orca/entrypoint.sh``). A ``local`` run on a
machine without a display likewise needs Xvfb (or ``xvfb-run``) in front of the
binary; this backend does not add it itself.

CLI differences from PrusaSlicer
--------------------------------
* profiles are **JSON presets** (``--load-settings "printer.json;process.json"``
  and ``--load-filaments "filament.json"``), not ``--load <profile>.ini``;
* the generated JSON carries the ``"type"`` discriminator OrcaSlicer requires
  (``machine`` / ``process`` / ``filament``);
* slicing uses ``--slice 0 --outputdir <dir>`` -- OrcaSlicer writes the G-code
  into the output directory under a name it chooses, so the backend reads the
  largest ``*.gcode`` produced rather than a fixed ``model.gcode``;
* ``--key=value`` setting overrides are passed exactly like PrusaSlicer
  (OrcaSlicer documents the same ``--hyphen-key=value`` syntax).

Transport overrides (env-only, read at call time):

* ``ORCASLICER_BINARY``  -- host binary (default ``orca-slicer``);
* ``SLICER_IMAGE``       -- container image; wins over ``ORCASLICER_IMAGE``;
* ``ORCASLICER_IMAGE``   -- image alias;
* ``SLICER_DOCKER_BINARY`` / ``SLICER_MEMORY_LIMIT`` / ``SLICER_CPU_LIMIT``.

The backend is selected through :func:`slicers.services.get_backend` by setting
``SLICER_BACKEND=orcaslicer`` (env / Django setting / registered runtime
setting). No private path is hardcoded: the binary and image are overridable.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .base import (
    FilamentSettings,
    PrinterSettings,
    ProcessSettings,
    SlicerError,
    with_default_filament_density,
)
from .prusaslicer import (
    DEFAULT_MODE,
    DEFAULT_TIMEOUT_SEC,
    SANDBOX_FLAGS,
    PrusaSlicerBackend,
    PrusaSlicerConfig,
    _resolve,
    _stringify,
    _validate_pair,
    parse_duration,
)

__all__ = [
    "DEFAULT_BINARY",
    "DEFAULT_IMAGE",
    "OrcaSlicerBackend",
    "parse_orca_gcode_estimates",
    "render_preset_json",
]

#: Published OrcaSlicer sandbox image (infra-devops). Override with ``SLICER_IMAGE``.
DEFAULT_IMAGE = "ghcr.io/branc9/printforge-orcaslicer:latest"

#: Host binary used in ``local`` mode.
DEFAULT_BINARY = "orca-slicer"

logger = logging.getLogger(__name__)

# OrcaSlicer / BambuStudio write several spellings of the estimate headers;
# accept all of them so the gram/time estimate survives across versions.
_RE_EST_TIME = re.compile(
    r"estimated printing time \((?:normal|silent) mode\)\s*[:=]\s*([^;\n]+)",
    re.IGNORECASE,
)
_RE_EST_TIME_ALT = re.compile(
    r"(?:model printing time|total estimated time)\s*[:=]\s*([^;\n]+)",
    re.IGNORECASE,
)
_RE_FILAMENT_MM = re.compile(r"filament (?:used|length)\s*\[mm\]\s*[:=]\s*([0-9.]+)", re.IGNORECASE)
_RE_FILAMENT_G = re.compile(r"filament (?:used|weight)\s*\[g\]\s*[:=]\s*([0-9.]+)", re.IGNORECASE)


def parse_orca_gcode_estimates(gcode: str) -> dict[str, Any]:
    """Extract time/filament estimates from OrcaSlicer's G-code comments."""
    estimates: dict[str, Any] = {}
    match = _RE_EST_TIME.search(gcode) or _RE_EST_TIME_ALT.search(gcode)
    if match:
        seconds = parse_duration(match.group(1).strip())
        if seconds is not None:
            estimates["estimated_time_sec"] = seconds
    match = _RE_FILAMENT_MM.search(gcode)
    if match:
        estimates["filament_mm"] = float(match.group(1))
    match = _RE_FILAMENT_G.search(gcode)
    if match:
        estimates["filament_g"] = float(match.group(1))
    return estimates


def render_preset_json(
    label: str,
    settings_map: Mapping[str, Any],
    profile_name: str,
    preset_type: str,
) -> str:
    """Render a deterministic OrcaSlicer JSON preset.

    ``preset_type`` is the ``"type"`` discriminator OrcaSlicer requires when a
    preset is loaded from a standalone file (``machine`` / ``process`` /
    ``filament``); without it ``--load-settings`` fails with "unknown config
    type".
    """
    payload: dict[str, Any] = dict(settings_map)
    payload["type"] = preset_type
    payload["name"] = profile_name
    payload["from"] = "User"
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _extract_preset(
    settings_json: Mapping[str, Any] | None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Split ``settings_json`` into external files, CLI overrides, JSON keys.

    Mirrors :func:`slicers.prusaslicer.extract_profile_config` (same validation
    and reserved keys) but keeps the inline values native so they land in the
    JSON preset with the right type (number vs string).
    """
    settings = dict(settings_json or {})

    config_files: list[str] = []
    raw_files = settings.pop("config_file", None)
    if raw_files:
        values = [raw_files] if isinstance(raw_files, str) else list(raw_files)
        config_files.extend(str(value) for value in values)

    overrides: list[str] = []
    raw_overrides = settings.pop("overrides", None)
    if raw_overrides:
        if not isinstance(raw_overrides, Mapping):
            raise SlicerError("'overrides' must be an object of slicer keys")
        for key, value in raw_overrides.items():
            if value is None:
                continue
            text = _stringify(value)
            _validate_pair(str(key), text)
            overrides.append(f"--{str(key).strip().replace('_', '-')}={text}")

    inline: dict[str, Any] = {}
    for key, value in settings.items():
        if value is None:
            continue
        _validate_pair(str(key), _stringify(value))
        inline[str(key)] = value

    return config_files, overrides, inline


def _copy_external_preset(input_dir: Path, label: str, index: int, path: str) -> Path:
    source = Path(path).expanduser()
    if not source.is_file():
        raise SlicerError(f"Config file for the {label} profile was not found: {path}")
    target = input_dir / f"{label}-external-{index}.json"
    target.write_bytes(source.read_bytes())
    return target


def _mount(root: str, path: Path) -> str:
    """Return ``path`` as seen from inside the container (or the host path)."""
    return f"{root}/{Path(path).name}" if root else str(path)


class OrcaSlicerBackend(PrusaSlicerBackend):
    """Slice a mesh with the headless OrcaSlicer CLI (``--slice``/``--outputdir``)."""

    name = "orcaslicer"
    supported_formats = ("gcode",)
    _display_name = "OrcaSlicer"

    @classmethod
    def _default_config(cls) -> PrusaSlicerConfig:
        """Read the OrcaSlicer transport config from the environment.

        ``SLICER_IMAGE`` still wins over ``ORCASLICER_IMAGE`` so a deployment
        can pin one image for every backend.
        """
        return PrusaSlicerConfig(
            mode=DEFAULT_MODE,
            binary=_resolve("ORCASLICER_BINARY", DEFAULT_BINARY),
            image=(
                _resolve("SLICER_IMAGE", "") or _resolve("ORCASLICER_IMAGE", "") or DEFAULT_IMAGE
            ),
            docker_binary=_resolve("SLICER_DOCKER_BINARY", "docker"),
            timeout_sec=DEFAULT_TIMEOUT_SEC,
            memory_limit=_resolve("SLICER_MEMORY_LIMIT", "2g"),
            cpu_limit=_resolve("SLICER_CPU_LIMIT", "2.0"),
        )

    # -- Profile materialisation -------------------------------------------

    def _prepare_profiles(
        self,
        input_dir: Path,
        printer: PrinterSettings,
        filament: FilamentSettings,
        process: ProcessSettings,
    ) -> tuple[list[Path], list[str]]:
        """Write OrcaSlicer JSON presets (machine/process/filament) into ``input_dir``.

        Returns the files in ``[printer, process, filament]`` order; the
        ``build_args`` override splits them into ``--load-settings`` and
        ``--load-filaments`` by the ``filament`` filename prefix.
        """
        config_files: list[Path] = []
        overrides: list[str] = []
        sources = (
            ("printer", printer.name, printer.settings, "machine"),
            ("process", process.name, process.settings, "process"),
            ("filament", filament.name, filament.settings, "filament"),
        )
        for label, name, settings_json, preset_type in sources:
            if label == "filament" and (filament.material or settings_json):
                # Default a missing density so the gram estimate stays meaningful.
                settings_json = with_default_filament_density(settings_json, filament.material)
            external, extra_overrides, inline = _extract_preset(settings_json)
            if (
                label == "process"
                and process.layer_height is not None
                and "layer_height" not in inline
            ):
                inline["layer_height"] = process.layer_height
            overrides.extend(extra_overrides)
            for index, path in enumerate(external):
                config_files.append(_copy_external_preset(input_dir, label, index, path))
            if inline:
                target = input_dir / f"{label}.json"
                target.write_text(
                    render_preset_json(label, inline, name, preset_type), encoding="utf-8"
                )
                config_files.append(target)
        return config_files, overrides

    # -- CLI ----------------------------------------------------------------

    @staticmethod
    def _load_args(
        settings_files: Sequence[Path],
        filament_files: Sequence[Path],
        *,
        root: str,
    ) -> list[str]:
        args: list[str] = []
        if settings_files:
            args += ["--load-settings", ";".join(_mount(root, path) for path in settings_files)]
        if filament_files:
            args += ["--load-filaments", ";".join(_mount(root, path) for path in filament_files)]
        return args

    def build_args(
        self,
        input_dir: Path,
        output_dir: Path,
        config_files: Sequence[Path] = (),
        overrides: Sequence[str] = (),
        *,
        model_name: str = "model.stl",
        output_name: str = "model.gcode",
        config: PrusaSlicerConfig | None = None,
        dont_arrange: bool = False,
    ) -> list[str]:
        """Build the OrcaSlicer command as a list (never a shell string).

        ``output_name`` is accepted for signature compatibility with the
        PrusaSlicer backend but unused: OrcaSlicer names the G-code itself under
        ``--outputdir``. ``dont_arrange`` becomes ``--arrange=0`` so a 3MF build
        plate keeps its per-item coordinates.
        """
        config = config or self.resolve_config()
        settings_files = [
            path for path in config_files if not Path(path).name.startswith("filament")
        ]
        filament_files = [path for path in config_files if Path(path).name.startswith("filament")]
        arrange = ["--arrange=0"] if dont_arrange else []

        if config.mode == "docker":
            return [
                config.docker_binary,
                "run",
                "--rm",
                *SANDBOX_FLAGS,
                "--memory",
                config.memory_limit,
                "--cpus",
                config.cpu_limit,
                "-v",
                f"{input_dir}:/work:ro",
                "-v",
                f"{output_dir}:/out:rw",
                config.image,
                "--slice",
                "0",
                "--outputdir",
                "/out",
                *self._load_args(settings_files, filament_files, root="/work"),
                *arrange,
                *overrides,
                f"/work/{model_name}",
            ]

        return [
            config.binary,
            "--slice",
            "0",
            "--outputdir",
            str(output_dir),
            *self._load_args(settings_files, filament_files, root=""),
            *arrange,
            *overrides,
            str(input_dir / model_name),
        ]

    def _read_gcode(self, output_dir: Path) -> bytes:
        """Read the G-code OrcaSlicer wrote under ``--outputdir``.

        The filename is derived from OrcaSlicer's filename template, so pick the
        largest ``*.gcode`` (the sliced model; ``result.json`` is ignored) with a
        deterministic name tie-break.
        """
        candidates = [path for path in output_dir.glob("*.gcode") if path.is_file()]
        if not candidates:
            raise SlicerError(f"{self._display_name} reported success but produced no G-code")
        best = max(candidates, key=lambda path: (path.stat().st_size, path.name))
        return best.read_bytes()

    def _parse_estimates(self, text: str) -> dict[str, Any]:
        return parse_orca_gcode_estimates(text)
