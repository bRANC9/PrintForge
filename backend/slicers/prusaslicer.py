"""PrusaSlicer backend: headless CLI slicing (terv.md 12. fejezet).

Security model (terv.md 20. fejezet)
------------------------------------
Profiles originate from user-editable ``settings_json`` and are therefore
treated as **untrusted data**:

* every config key/value is validated and either rendered into a generated
  ``.ini`` file or passed as a ``--key=value`` argument -- never concatenated
  into a shell string;
* ``subprocess`` always runs with a **list** of arguments and ``shell=False``;
* a hard ``timeout`` kills a hanging slicer;
* in ``docker`` mode the container runs with the same sandbox flags as the CAD
  worker (``--network none --read-only --tmpfs /tmp`` ...) and only the job
  directory is bind-mounted.

Mode selection
--------------
``slicer_mode`` and ``slicer_timeout_sec`` are resolved **at call time** through
``configuration.services.get_setting`` (DB override -> Django settings ->
``os.environ`` -> default), so an admin change applies to the next job without
a worker restart. No value is cached at import time.

* ``local``  -- run ``PRUSASLICER_BINARY`` directly (dev machines);
* ``docker`` -- run the published image via ``SLICER_DOCKER_BINARY``
  (production / CI, see ``docker/slicer/README.md``).

Transport overrides (env-only, read at call time):

* ``PRUSASLICER_BINARY``   -- host binary (default ``prusa-slicer``);
* ``SLICER_IMAGE``         -- container image; wins over ``PRUSASLICER_IMAGE``;
* ``PRUSASLICER_IMAGE``    -- legacy alias for the image;
* ``SLICER_DOCKER_BINARY`` -- container runtime (default ``docker``);
* ``SLICER_MEMORY_LIMIT`` / ``SLICER_CPU_LIMIT`` -- container resource caps.

Default image: ``ghcr.io/branc9/printforge-prusaslicer:latest``.

Profile -> CLI mapping
-----------------------
``settings_json`` is a flat mapping of PrusaSlicer config keys. Reserved keys:

* ``config_file`` -- path (or list of paths) to an existing ``.ini``; copied
  into the job directory and passed as ``--load``;
* ``overrides``   -- explicit ``--key=value`` arguments (underscores become
  hyphens, e.g. ``layer_height`` -> ``--layer-height=0.2``).

Every other key is written to ``<profile>.ini`` and passed with ``--load``.
PrusaSlicer loads printer, then filament, then process, so later profiles win.
The ``ProcessProfile.layer_height`` field is injected as ``layer_height`` when
no explicit value is present.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from django.conf import settings

from .base import (
    FilamentSettings,
    ModelInput,
    PrinterSettings,
    ProcessSettings,
    SlicedResult,
    SlicerBackend,
    SlicerError,
    SlicerTimeout,
    resolve_model,
)

try:  # api-dev implements this contract in parallel; degrade gracefully without it
    from configuration.services import get_setting
except Exception:  # noqa: BLE001 - runtime settings are best-effort

    def get_setting(name: str, default: Any = None) -> Any:
        """Env-only fallback while ``configuration.services`` is unavailable."""
        return os.environ.get(name.upper(), default)


__all__ = [
    "DEFAULT_IMAGE",
    "DEFAULT_MODE",
    "DEFAULT_TIMEOUT_SEC",
    "SANDBOX_FLAGS",
    "PrusaSlicerBackend",
    "PrusaSlicerConfig",
    "extract_profile_config",
    "format_duration",
    "parse_duration",
    "parse_gcode_estimates",
    "prepare_profiles",
    "render_ini",
]


#: Published PrusaSlicer sandbox image (infra-devops). Override with ``SLICER_IMAGE``.
DEFAULT_IMAGE = "ghcr.io/branc9/printforge-prusaslicer:latest"

#: Fallbacks when the runtime-settings service has no value.
DEFAULT_MODE = "local"
DEFAULT_TIMEOUT_SEC = 300.0

logger = logging.getLogger(__name__)


# Container sandbox flags shared with the CAD worker (terv.md 20. fejezet).
SANDBOX_FLAGS: tuple[str, ...] = (
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
)

#: Keys with a special meaning; everything else becomes an ini setting.
_RESERVED_KEYS = frozenset({"config_file", "overrides"})

_RE_CONFIG_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_RE_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])")
_RE_EST_TIME = re.compile(r"estimated printing time \(normal mode\)\s*=\s*(.+)")
_RE_FILAMENT_MM = re.compile(r"filament used \[mm\]\s*=\s*([0-9.]+)")
_RE_FILAMENT_G = re.compile(r"filament used \[g\]\s*=\s*([0-9.]+)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stringify(value: Any) -> str:
    """Render a JSON value as a PrusaSlicer config value."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(_stringify(item) for item in value)
    return str(value)


def _validate_pair(key: str, value: str) -> None:
    if not _RE_CONFIG_KEY.match(key):
        raise SlicerError(f"Invalid slicer setting key: {key!r}")
    if any(char in value for char in "\n\r\x00"):
        raise SlicerError(f"Invalid slicer setting value for {key!r}")


def extract_profile_config(
    settings_json: Mapping[str, Any] | None,
) -> tuple[list[str], list[str], dict[str, str]]:
    """Split a ``settings_json`` dict into config files, CLI overrides, ini keys."""
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
            cli_key = str(key).strip().replace("_", "-")
            overrides.append(f"--{cli_key}={text}")

    inline: dict[str, str] = {}
    for key, value in settings.items():
        if value is None:
            continue
        text = _stringify(value)
        _validate_pair(str(key), text)
        inline[str(key)] = text

    return config_files, overrides, inline


def render_ini(label: str, settings_map: Mapping[str, str], profile_name: str) -> str:
    """Render a deterministic PrusaSlicer ``.ini`` for ``settings_map``."""
    lines = [f"# PrintForge {label} profile: {profile_name}"]
    lines.extend(f"{key} = {settings_map[key]}" for key in sorted(settings_map))
    return "\n".join(lines) + "\n"


def _copy_external_config(input_dir: Path, label: str, index: int, path: str) -> Path:
    source = Path(path).expanduser()
    if not source.is_file():
        raise SlicerError(f"Config file for the {label} profile was not found: {path}")
    target = input_dir / f"{label}-external-{index}.ini"
    target.write_bytes(source.read_bytes())
    return target


def prepare_profiles(
    input_dir: Path,
    printer: PrinterSettings,
    filament: FilamentSettings,
    process: ProcessSettings,
) -> tuple[list[Path], list[str]]:
    """Materialise the profiles into the job directory.

    Returns ``(config_files, overrides)`` where every config file lives inside
    ``input_dir`` (so docker only needs one bind mount) and ``overrides`` are
    ready-to-use ``--key=value`` arguments.
    """
    config_files: list[Path] = []
    overrides: list[str] = []
    sources = (
        ("printer", printer.name, printer.settings),
        ("filament", filament.name, filament.settings),
        ("process", process.name, process.settings),
    )
    for label, name, settings_json in sources:
        external, extra_overrides, inline = extract_profile_config(settings_json)
        if label == "process" and process.layer_height is not None and "layer_height" not in inline:
            inline["layer_height"] = _stringify(process.layer_height)
        overrides.extend(extra_overrides)
        for index, path in enumerate(external):
            config_files.append(_copy_external_config(input_dir, label, index, path))
        if inline:
            target = input_dir / f"{label}.ini"
            target.write_text(render_ini(label, inline, name), encoding="utf-8")
            config_files.append(target)
    return config_files, overrides


# ---------------------------------------------------------------------------
# G-code estimate parsing
# ---------------------------------------------------------------------------


def parse_duration(text: str | None) -> int | None:
    """Parse a PrusaSlicer duration (``1h 2m 3s``) into seconds."""
    if not text:
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total = 0.0
    for match in _RE_DURATION_PART.finditer(text):
        total += float(match.group(1)) * units[match.group(2)]
    return int(round(total)) if total else None


def format_duration(seconds: int | float | None) -> str | None:
    """Format seconds as ``1d 2h 3m 4s`` (omitting empty leading units)."""
    if seconds is None:
        return None
    total = int(round(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def parse_gcode_estimates(gcode: str) -> dict[str, Any]:
    """Extract the estimate comments PrusaSlicer writes into the G-code."""
    estimates: dict[str, Any] = {}
    match = _RE_EST_TIME.search(gcode)
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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrusaSlicerConfig:
    """Resolved runtime configuration for the backend.

    ``mode``/``timeout_sec`` here are only *fallbacks*; :meth:`PrusaSlicerBackend.resolve_config`
    overlays the effective values from the runtime-settings service on every
    call, unless they were injected explicitly (constructor kwargs / ``config``).
    """

    mode: str = DEFAULT_MODE
    binary: str = "prusa-slicer"
    image: str = DEFAULT_IMAGE
    docker_binary: str = "docker"
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    memory_limit: str = "2g"
    cpu_limit: str = "2.0"


def _resolve(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    return str(getattr(settings, name, default))


def _config_from_environment() -> PrusaSlicerConfig:
    """Read the transport-level (env-only) overrides; mode/timeout are runtime."""
    return PrusaSlicerConfig(
        mode=DEFAULT_MODE,
        binary=_resolve("PRUSASLICER_BINARY", "prusa-slicer"),
        image=(_resolve("SLICER_IMAGE", "") or _resolve("PRUSASLICER_IMAGE", "") or DEFAULT_IMAGE),
        docker_binary=_resolve("SLICER_DOCKER_BINARY", "docker"),
        timeout_sec=DEFAULT_TIMEOUT_SEC,
        memory_limit=_resolve("SLICER_MEMORY_LIMIT", "2g"),
        cpu_limit=_resolve("SLICER_CPU_LIMIT", "2.0"),
    )


def _coerce_timeout(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid slicer_timeout_sec value: %r", value)
        return None


def _model_name(source_filename: str) -> str:
    suffix = Path(source_filename).suffix.lower()
    if suffix not in (".stl", ".3mf", ".obj"):
        suffix = ".stl"
    return f"model{suffix}"


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class PrusaSlicerBackend(SlicerBackend):
    """Slice a mesh with the headless PrusaSlicer CLI (``--export-gcode``)."""

    name = "prusaslicer"
    supported_formats = ("gcode",)

    def __init__(
        self,
        config: PrusaSlicerConfig | None = None,
        *,
        mode: str | None = None,
        binary: str | None = None,
        image: str | None = None,
        docker_binary: str | None = None,
        timeout_sec: float | None = None,
        memory_limit: str | None = None,
        cpu_limit: str | None = None,
    ) -> None:
        resolved = config or _config_from_environment()
        overrides: dict[str, Any] = {
            "mode": mode,
            "binary": binary,
            "image": image,
            "docker_binary": docker_binary,
            "timeout_sec": timeout_sec,
            "memory_limit": memory_limit,
            "cpu_limit": cpu_limit,
        }
        # Explicit injections are never overridden by the runtime-settings service.
        explicit = {key for key, value in overrides.items() if value is not None}
        if config is not None:
            explicit |= {"mode", "timeout_sec"}
        resolved = replace(
            resolved, **{key: value for key, value in overrides.items() if value is not None}
        )
        resolved = replace(resolved, mode=resolved.mode.strip().lower())
        if resolved.mode not in ("local", "docker"):
            raise SlicerError(
                f"Unknown SLICER_MODE {resolved.mode!r}; expected 'local' or 'docker'"
            )
        self.config = resolved
        self._explicit = frozenset(explicit)

    def resolve_config(self) -> PrusaSlicerConfig:
        """Overlay the runtime settings on the transport config (no caching).

        ``mode``/``timeout_sec`` are re-read from ``configuration.services`` on
        every call, so an admin change applies to the next job. Explicitly
        injected values (constructor kwargs or a ``config`` object) win.
        """
        config = self.config
        if "mode" not in self._explicit:
            config = replace(
                config,
                mode=str(get_setting("slicer_mode") or DEFAULT_MODE).strip().lower(),
            )
        if "timeout_sec" not in self._explicit:
            timeout = _coerce_timeout(get_setting("slicer_timeout_sec"))
            if timeout is not None:
                config = replace(config, timeout_sec=timeout)
        if config.mode not in ("local", "docker"):
            raise SlicerError(f"Unknown slicer_mode {config.mode!r}; expected 'local' or 'docker'")
        return config

    # -- SlicerBackend ------------------------------------------------------

    def slice(
        self,
        model: ModelInput,
        printer: PrinterSettings,
        filament: FilamentSettings,
        process: ProcessSettings,
    ) -> SlicedResult:
        source = resolve_model(model)
        model_name = _model_name(source.filename)
        config = self.resolve_config()

        with tempfile.TemporaryDirectory(prefix="printforge-slice-") as tmp:
            input_dir = Path(tmp) / "in"
            output_dir = Path(tmp) / "out"
            input_dir.mkdir()
            output_dir.mkdir()
            (input_dir / model_name).write_bytes(source.data)

            config_files, overrides = prepare_profiles(input_dir, printer, filament, process)
            args = self.build_args(
                input_dir,
                output_dir,
                config_files,
                overrides,
                model_name=model_name,
                config=config,
            )
            try:
                completed = subprocess.run(  # noqa: S603 - list args, shell=False
                    args,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=config.timeout_sec,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise SlicerTimeout(f"PrusaSlicer timed out after {config.timeout_sec}s") from exc

            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise SlicerError(
                    f"PrusaSlicer exited with code {completed.returncode}: {detail[:500]}"
                )

            output_path = output_dir / "model.gcode"
            if not output_path.exists():
                raise SlicerError("PrusaSlicer reported success but produced no G-code")
            data = output_path.read_bytes()

        estimates = parse_gcode_estimates(data.decode("utf-8", errors="replace"))
        return SlicedResult(
            data=data,
            format="gcode",
            estimated_time_sec=estimates.get("estimated_time_sec"),
            estimated_filament_g=estimates.get("filament_g"),
            estimated_filament_mm=estimates.get("filament_mm"),
            metadata={"slicer": self.name, **estimates},
        )

    def estimate(self, result: SlicedResult) -> dict[str, Any]:
        metadata = dict(result.metadata or {})
        seconds = (
            result.estimated_time_sec
            if result.estimated_time_sec is not None
            else metadata.get("estimated_time_sec")
        )
        filament_g = (
            result.estimated_filament_g
            if result.estimated_filament_g is not None
            else metadata.get("filament_g")
        )
        filament_mm = (
            result.estimated_filament_mm
            if result.estimated_filament_mm is not None
            else metadata.get("filament_mm")
        )
        return {
            "estimated_time_sec": seconds,
            "estimated_time": format_duration(seconds),
            "filament_g": filament_g,
            "filament_mm": filament_mm,
            "format": result.format,
            "slicer": metadata.get("slicer", self.name),
        }

    # -- CLI ----------------------------------------------------------------

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
    ) -> list[str]:
        """Build the PrusaSlicer command as a list (never a shell string).

        When ``config`` is omitted the effective runtime settings are resolved
        on the spot, so a mode change applies without reconstructing the backend.
        """
        config = config or self.resolve_config()
        action = ["--export-gcode", "--output"]
        if config.mode == "docker":
            load_args: list[str] = []
            for path in config_files:
                load_args += ["--load", f"/work/{Path(path).name}"]
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
                *action,
                f"/out/{output_name}",
                *load_args,
                *overrides,
                f"/work/{model_name}",
            ]

        load_args = []
        for path in config_files:
            load_args += ["--load", str(path)]
        return [
            config.binary,
            *action,
            str(output_dir / output_name),
            *load_args,
            *overrides,
            str(input_dir / model_name),
        ]
