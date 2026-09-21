"""OpenSCAD backend: parametric template + sandboxed CLI export.

Security model (terv.md 20. fejezet)
------------------------------------
The generated ``.scad`` is treated as **untrusted**:

* the template is self-contained and only ever interpolates *coerced* numbers
  and a sanitised object name, so specification values cannot inject code;
* before execution every source is scanned for ``import()``/``surface()`` and
  ``include <..>``/``use <..>`` and rejected -- those would let a model read
  arbitrary files or DoS the worker;
* the CLI is invoked with a **list** of arguments and ``shell=False``;
* a hard ``timeout=settings.OPENSCAD_TIMEOUT_SEC`` kills infinite loops;
* in ``docker`` mode the container runs with the sandbox flags from
  terv.md 20. fejezet (``--network none --read-only --tmpfs /tmp`` ...).

Mode selection
--------------
``OPENSCAD_MODE`` (env, also honoured from Django settings) picks:

* ``local``  -- run the ``OPENSCAD_BINARY`` directly (dev machines);
* ``docker`` -- run ``OPENSCAD_IMAGE`` via ``OPENSCAD_DOCKER_BINARY`` with the
  sandbox flags (production / CI, see ``docker/openscad/README.md``).

Other knobs: ``OPENSCAD_TIMEOUT_SEC``, ``OPENSCAD_MEMORY_LIMIT``,
``OPENSCAD_CPU_LIMIT``.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from string import Template
from typing import Any

from django.conf import settings

from .base import (
    CADBackend,
    CADError,
    GeneratedModel,
    SpecificationError,
    UnsupportedFormatError,
)

__all__ = [
    "SANDBOX_FLAGS",
    "HolderParameters",
    "OpenSCADBackend",
    "OpenSCADConfig",
    "OpenSCADError",
    "OpenSCADTimeout",
    "OpenSCADValidationError",
    "build_parameters",
    "render_scad",
    "scan_for_forbidden",
    "strip_scad_comments",
    "validate_scad_source",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OpenSCADError(CADError):
    """The OpenSCAD CLI failed or produced no output."""


class OpenSCADTimeout(OpenSCADError):
    """The OpenSCAD CLI exceeded the hard timeout."""


class OpenSCADValidationError(OpenSCADError, ValueError):
    """The generated source failed the safety scan."""


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

# ``import()``/``surface()`` and friends read files from disk; ``include``/``use``
# pull in arbitrary libraries. The template never needs any of them.
_RE_FILE_READ = re.compile(
    r"\b(import|surface|import_stl|import_dxf|dxf_linear_extrude|dxf_rotate_extrude)\s*\(",
    re.IGNORECASE,
)
_RE_LIBRARY = re.compile(r"\b(include|use)\s*<", re.IGNORECASE)

# Exact runtime flags from terv.md 20. fejezet (memory/cpus appended separately
# because they are configurable).
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


def strip_scad_comments(source: str) -> str:
    """Remove ``//`` line comments and ``/* ... */`` block comments.

    Comments must be ignored by the safety scan so a harmless comment such as
    ``// no import() here`` does not trip the whitelist.
    """
    without_block = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", without_block)


def scan_for_forbidden(source: str) -> list[str]:
    """Return the sorted set of forbidden constructs found in ``source``."""
    stripped = strip_scad_comments(source)
    found = {match.group(1).lower() for match in _RE_FILE_READ.finditer(stripped)}
    found |= {match.group(1).lower() for match in _RE_LIBRARY.finditer(stripped)}
    return sorted(found)


def validate_scad_source(source: str) -> list[str]:
    """Return blocking safety problems for a block of OpenSCAD source."""
    if not isinstance(source, str) or not source.strip():
        return ["OpenSCAD source is empty"]

    problems: list[str] = []
    for token in scan_for_forbidden(source):
        problems.append(
            f"Forbidden OpenSCAD construct '{token}': file access is not allowed in the sandbox"
        )
    return problems


# ---------------------------------------------------------------------------
# Specification parsing (untrusted dict -> typed, bounded parameters)
# ---------------------------------------------------------------------------

_MISSING = object()

# ISO metric coarse screw nominal diameters (mm).
_SCREW_NOMINAL: dict[str, float] = {
    "M2": 2.0,
    "M2.5": 2.5,
    "M3": 3.0,
    "M4": 4.0,
    "M5": 5.0,
    "M6": 6.0,
    "M8": 8.0,
}
_RE_SCREW = re.compile(r"^M?(\d+(?:\.\d+)?)$", re.IGNORECASE)
_RE_SAFE_NAME = re.compile(r"[^A-Za-z0-9_\- ]+")


def _lookup(specification: dict[str, Any], path: str) -> Any:
    node: Any = specification
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _coerce_float(
    specification: dict[str, Any],
    path: str,
    *,
    default: float | object = _MISSING,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = _lookup(specification, path)
    if value is _MISSING:
        if default is _MISSING:
            raise SpecificationError(f"Missing required numeric field '{path}'")
        return float(default)  # type: ignore[arg-type]
    if isinstance(value, bool):
        raise SpecificationError(f"Field '{path}' must be a number, not a boolean")
    if not isinstance(value, (int, float, str)):
        raise SpecificationError(f"Field '{path}' must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SpecificationError(f"Field '{path}' is not a valid number: {value!r}") from exc
    if not math.isfinite(number):
        raise SpecificationError(f"Field '{path}' must be finite")
    if minimum is not None and number < minimum:
        raise SpecificationError(f"Field '{path}' must be >= {minimum} (got {number})")
    if maximum is not None and number > maximum:
        raise SpecificationError(f"Field '{path}' must be <= {maximum} (got {number})")
    return number


def _coerce_int(
    specification: dict[str, Any],
    path: str,
    *,
    default: int | object = _MISSING,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    number = _coerce_float(
        specification,
        path,
        default=default if default is _MISSING else float(default),  # type: ignore[arg-type]
        minimum=minimum,
        maximum=maximum,
    )
    if number != int(number):
        raise SpecificationError(f"Field '{path}' must be an integer")
    return int(number)


def _sanitize_object_name(value: Any) -> str:
    text = str(value) if value is not None else "custom_part"
    text = _RE_SAFE_NAME.sub("", text).strip()[:64]
    return text or "custom_part"


def _hole_diameter(mount_type: str, clearance: float) -> float:
    key = str(mount_type).strip().upper()
    nominal = _SCREW_NOMINAL.get(key)
    if nominal is None:
        match = _RE_SCREW.match(key)
        if match is None:
            raise SpecificationError(
                f"Unsupported mounting type {mount_type!r}; use an ISO metric screw e.g. 'M3', 'M5'"
            )
        nominal = float(match.group(1))
    return nominal + clearance


@dataclass(frozen=True)
class HolderParameters:
    """Validated, bounded parameters used to render the OpenSCAD template."""

    object_name: str
    width: float
    height: float
    thickness: float
    depth: float
    angle: float
    wall: float
    hole_diameter: float
    hole_count: int
    clearance: float


def build_parameters(specification: dict[str, Any] | None) -> HolderParameters:
    """Validate an untrusted specification dict and return bounded parameters.

    Every value is coerced to a finite number and range-checked, so a malicious
    specification cannot smuggle OpenSCAD code into the generated source.
    """
    spec: dict[str, Any] = specification if isinstance(specification, dict) else {}

    width = _coerce_float(spec, "dimensions.width", default=70.0, minimum=5.0, maximum=2000.0)
    height = _coerce_float(spec, "dimensions.height", default=140.0, minimum=5.0, maximum=2000.0)
    thickness = _coerce_float(spec, "dimensions.thickness", default=8.0, minimum=0.4, maximum=200.0)
    depth = _coerce_float(spec, "dimensions.depth", default=60.0, minimum=5.0, maximum=2000.0)
    angle = _coerce_float(spec, "angle", default=15.0, minimum=-80.0, maximum=80.0)
    wall = _coerce_float(spec, "wall_thickness", default=4.0, minimum=0.8, maximum=20.0)

    mounting = spec.get("mounting")
    if mounting is not None and not isinstance(mounting, dict):
        raise SpecificationError("Field 'mounting' must be an object")
    mounting = mounting or {}
    mount_type = mounting.get("type", "M5")
    clearance = _coerce_float(mounting, "clearance", default=0.3, minimum=0.0, maximum=2.0)
    hole_count = _coerce_int(mounting, "count", default=2, minimum=1, maximum=8)

    return HolderParameters(
        object_name=_sanitize_object_name(spec.get("object")),
        width=width,
        height=height,
        thickness=thickness,
        depth=depth,
        angle=angle,
        wall=wall,
        hole_diameter=_hole_diameter(mount_type, clearance),
        hole_count=hole_count,
        clearance=clearance,
    )


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

# ``$$fn`` escapes the ``$fn`` special variable for string.Template.
_SCAD_TEMPLATE = Template(
    """// PrintForge generated OpenSCAD source -- do not edit by hand.
// object: $object_name
// Self-contained by design: file-reading and library-include constructs are
// rejected by the backend, so this file can never read arbitrary paths.
$$fn = 48;

device_w = $width;
device_h = $height;
device_t = $thickness;
base_d   = $depth;
tilt     = $angle;
wall     = $wall;
hole_d   = $hole_d;
hole_n   = $hole_count;

// Base plate with screw holes along the back edge.
module base_plate() {
    difference() {
        cube([device_w + 2 * wall, base_d, wall]);
        for (i = [0 : hole_n - 1]) {
            x = (device_w + 2 * wall) * (i + 1) / (hole_n + 1);
            translate([x, base_d * 0.5, -1])
                cylinder(d = hole_d, h = wall + 2);
        }
    }
}

// Tilted back wall the device rests against.
module back_wall() {
    translate([0, base_d - wall, wall])
        rotate([tilt, 0, 0])
            cube([device_w + 2 * wall, wall, device_h * 0.75]);
}

// Low front lip keeps the device from sliding off.
module front_lip() {
    translate([0, 0, wall])
        cube([device_w + 2 * wall, wall, min(20, max(4, device_h * 0.12))]);
}

// Slot carved out for the device.
module device_slot() {
    translate([wall, -1, wall])
        cube([device_w, base_d + 2, device_t]);
}

module holder() {
    difference() {
        union() {
            base_plate();
            back_wall();
            front_lip();
        }
        device_slot();
    }
}

holder();
"""
)


def _num(value: float) -> str:
    """Format a float for SCAD without scientific notation or ``-0.0``."""
    if value == 0:
        return "0.0"
    text = f"{value:.4f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def render_scad(parameters: HolderParameters) -> str:
    """Render the self-contained OpenSCAD template for validated parameters."""
    return _SCAD_TEMPLATE.substitute(
        object_name=parameters.object_name,
        width=_num(parameters.width),
        height=_num(parameters.height),
        thickness=_num(parameters.thickness),
        depth=_num(parameters.depth),
        angle=_num(parameters.angle),
        wall=_num(parameters.wall),
        hole_d=_num(parameters.hole_diameter),
        hole_count=str(parameters.hole_count),
    )


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenSCADConfig:
    """Resolved runtime configuration for the backend."""

    mode: str = "local"
    binary: str = "openscad"
    image: str = "printforge-openscad"
    docker_binary: str = "docker"
    timeout_sec: float = 60.0
    memory_limit: str = "1g"
    cpu_limit: str = "1.0"


def _resolve(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    return str(getattr(settings, name, default))


def _config_from_environment() -> OpenSCADConfig:
    return OpenSCADConfig(
        mode=_resolve("OPENSCAD_MODE", "local").strip().lower(),
        binary=_resolve("OPENSCAD_BINARY", "openscad"),
        image=_resolve("OPENSCAD_IMAGE", "printforge-openscad"),
        docker_binary=_resolve("OPENSCAD_DOCKER_BINARY", "docker"),
        timeout_sec=float(_resolve("OPENSCAD_TIMEOUT_SEC", "60")),
        memory_limit=_resolve("OPENSCAD_MEMORY_LIMIT", "1g"),
        cpu_limit=_resolve("OPENSCAD_CPU_LIMIT", "1.0"),
    )


def _source_of(model: GeneratedModel | str) -> str:
    if isinstance(model, GeneratedModel):
        return model.scad_source
    if isinstance(model, str):
        return model
    raise TypeError("model must be a GeneratedModel or an OpenSCAD source string")


class OpenSCADBackend(CADBackend):
    """Render a parametric part and export it through the OpenSCAD CLI."""

    name = "openscad"
    supported_formats = ("stl",)

    def __init__(
        self,
        config: OpenSCADConfig | None = None,
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
        overrides = {
            "mode": mode,
            "binary": binary,
            "image": image,
            "docker_binary": docker_binary,
            "timeout_sec": timeout_sec,
            "memory_limit": memory_limit,
            "cpu_limit": cpu_limit,
        }
        resolved = replace(
            resolved, **{key: value for key, value in overrides.items() if value is not None}
        )
        resolved = replace(resolved, mode=resolved.mode.strip().lower())
        if resolved.mode not in ("local", "docker"):
            raise CADError(f"Unknown OPENSCAD_MODE {resolved.mode!r}; expected 'local' or 'docker'")
        self.config = resolved

    # -- CADBackend ---------------------------------------------------------

    def generate(self, specification: dict[str, Any]) -> str:
        """Validate ``specification`` and render self-contained OpenSCAD source."""
        return render_scad(build_parameters(specification))

    def validate(self, model: GeneratedModel | str) -> list[str]:
        """Return blocking safety/parameter problems (empty list == valid)."""
        problems = validate_scad_source(_source_of(model))
        if isinstance(model, GeneratedModel):
            try:
                parameters = build_parameters(model.specification)
            except SpecificationError as exc:
                problems.append(str(exc))
            else:
                if parameters.wall < 0.8:
                    problems.append(
                        f"wall_thickness {parameters.wall}mm is below the printable minimum (0.8mm)"
                    )
                if parameters.hole_diameter < 1.0:
                    problems.append(
                        f"mounting hole diameter {parameters.hole_diameter}mm is too small"
                    )
        return problems

    def export(self, model: GeneratedModel | str, format: str) -> bytes:
        """Export ``model`` to ``format`` (only ``stl``) and return raw bytes."""
        normalized = (format or "").strip().lower()
        if normalized not in self.supported_formats:
            raise UnsupportedFormatError(
                f"OpenSCAD backend supports {self.supported_formats}, got {format!r}"
            )
        source = _source_of(model)
        problems = validate_scad_source(source)
        if problems:
            raise OpenSCADValidationError("; ".join(problems))

        with tempfile.TemporaryDirectory(prefix="printforge-cad-") as tmp:
            input_dir = Path(tmp) / "in"
            output_dir = Path(tmp) / "out"
            input_dir.mkdir()
            output_dir.mkdir()
            scad_path = input_dir / "model.scad"
            stl_path = output_dir / "model.stl"
            scad_path.write_text(source, encoding="utf-8")

            args = self.build_args(input_dir, output_dir)
            try:
                completed = subprocess.run(  # noqa: S603 - list args, shell=False
                    args,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=self.config.timeout_sec,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise OpenSCADTimeout(
                    f"OpenSCAD timed out after {self.config.timeout_sec}s"
                ) from exc

            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise OpenSCADError(
                    f"OpenSCAD exited with code {completed.returncode}: {detail[:500]}"
                )
            if not stl_path.exists():
                raise OpenSCADError("OpenSCAD reported success but produced no output file")
            return stl_path.read_bytes()

    # -- CLI -----------------------------------------------------------------

    def build_args(self, input_dir: Path, output_dir: Path) -> list[str]:
        """Build the OpenSCAD command as a list (never a shell string)."""
        if self.config.mode == "docker":
            return [
                self.config.docker_binary,
                "run",
                "--rm",
                *SANDBOX_FLAGS,
                "--memory",
                self.config.memory_limit,
                "--cpus",
                self.config.cpu_limit,
                "-v",
                f"{input_dir}:/work:ro",
                "-v",
                f"{output_dir}:/out:rw",
                self.config.image,
                "-o",
                "/out/model.stl",
                "/work/model.scad",
            ]
        return [
            self.config.binary,
            "-o",
            str(output_dir / "model.stl"),
            str(input_dir / "model.scad"),
        ]
