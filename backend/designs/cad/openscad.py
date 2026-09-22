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

Runtime settings
----------------
Mode, timeout and the docker limits are resolved through
``configuration.services.get_setting`` on **every access** (DB override ->
Django settings -> env -> default), so the NAS operator can change them from
the UI and the next job picks the new value up without a worker restart:

* ``openscad_mode``         -- ``local`` or ``docker`` (default ``local``)
* ``openscad_timeout_sec``  -- hard timeout per export (default ``60``)
* ``openscad_memory_limit`` -- docker ``--memory`` (default ``1g``)
* ``openscad_cpu_limit``    -- docker ``--cpus`` (default ``1.0``)

Env-only overrides (not exposed in the configuration UI):

* ``OPENSCAD_BINARY``        -- local binary (default ``openscad``)
* ``OPENSCAD_IMAGE``         -- docker image
  (default ``ghcr.io/branc9/printforge-openscad:latest``)
* ``OPENSCAD_DOCKER_BINARY`` -- container runtime (default ``docker``)
"""

from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, fields, replace
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

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_IMAGE",
    "MAX_OPERATIONS",
    "MAX_PRIMITIVES",
    "PRIMITIVE_ROLES",
    "PRIMITIVE_TYPES",
    "SANDBOX_FLAGS",
    "HolderParameters",
    "OpenSCADBackend",
    "OpenSCADConfig",
    "OpenSCADError",
    "OpenSCADTimeout",
    "OpenSCADValidationError",
    "RenderOperation",
    "RenderPrimitive",
    "build_parameters",
    "parse_operations",
    "parse_primitives",
    "render_primitives",
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
# Annotation-driven operations (docs/visual-editing.md 3.1 / 3.3)
# ---------------------------------------------------------------------------

# Mirrors ``agents.spec`` bounds (which the CAD package must not import, to keep
# it decoupled from ``llm-provider``). Every operation value is re-coerced and
# re-bounded here because the LLM payload is untrusted.
MIN_OPERATION_MM = 0.4
MAX_OPERATION_DEPTH_MM = 200.0
MAX_OPERATION_SIZE_MM = 500.0
MAX_OPERATION_DIAMETER_MM = 200.0
MAX_OPERATION_LABEL = 120
#: Upper bound for operation coordinates/normal components (mm), catching unit
#: mistakes and absurd values that could produce degenerate geometry.
MAX_OPERATION_COORD_MM = 10_000.0
MAX_OPERATION_NORMAL_COMPONENT = 1_000.0
#: Hard cap on the number of rendered features: unbounded lists are a DoS vector.
MAX_OPERATIONS = 256

OPERATION_KINDS: tuple[str, ...] = ("hole", "pocket", "boss", "slot", "cut", "add")
_ADDITIVE_KINDS = frozenset({"boss", "add"})
_SUBTRACTIVE_KINDS = frozenset({"hole", "pocket", "cut", "slot"})
_RECTANGULAR_KINDS = frozenset({"pocket", "cut", "add"})

# Labels are only ever emitted inside a ``//`` comment, but sanitise anyway so a
# hostile label cannot close the comment or smuggle a forbidden construct.
_RE_LABEL = re.compile(r"[^\w\- .,:/]+")


@dataclass(frozen=True)
class RenderOperation:
    """A validated, bounded parametric feature ready for rendering.

    ``origin``/``normal`` are plain float tuples (not ``agents.spec.Vec3``) so
    the CAD package stays independent of the LLM layer.
    """

    kind: str
    origin: tuple[float, float, float]
    normal: tuple[float, float, float]
    depth: float
    width: float | None = None
    height: float | None = None
    diameter: float | None = None
    length: float | None = None
    label: str = ""

    @property
    def is_additive(self) -> bool:
        """True for features union-ed onto the base (``boss``/``add``)."""
        return self.kind in _ADDITIVE_KINDS


def _op_float(
    item: dict[str, Any],
    field: str,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Coerce ``item[field]`` with operation-scoped error context."""
    try:
        return _coerce_float(item, field, minimum=minimum, maximum=maximum)
    except SpecificationError as exc:
        raise SpecificationError(f"{path}.{field}: {exc}") from exc


def _op_optional_float(
    item: dict[str, Any],
    field: str,
    path: str,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    """Coerce an optional operation dimension; ``None``/missing stays ``None``."""
    value = item.get(field)
    if value is None:
        return None
    return _op_float(item, field, path, minimum=minimum, maximum=maximum)


def _coerce_vec3(
    item: dict[str, Any],
    field: str,
    path: str,
    *,
    limit: float,
    require_nonzero: bool = False,
) -> tuple[float, float, float]:
    """Parse ``item[field]`` as a bounded ``{x, y, z}`` vector."""
    value = item.get(field)
    if value is None:
        raise SpecificationError(f"{path}.{field}: missing required vector")
    if not isinstance(value, dict):
        raise SpecificationError(f"{path}.{field}: must be an object with x, y, z")
    components = tuple(
        _op_float(value, axis, f"{path}.{field}", minimum=-limit, maximum=limit)
        for axis in ("x", "y", "z")
    )
    if require_nonzero and all(component == 0.0 for component in components):
        raise SpecificationError(f"{path}.{field}: must not be a zero-length vector")
    return components  # type: ignore[return-value]


def _sanitize_label(value: Any) -> str:
    text = str(value) if value is not None else ""
    text = text.replace("\r", " ").replace("\n", " ")
    return _RE_LABEL.sub("", text).strip()[:MAX_OPERATION_LABEL]


def _parse_operation(item: Any, index: int) -> RenderOperation:
    path = f"operations[{index}]"
    if not isinstance(item, dict):
        raise SpecificationError(f"{path} must be an object")

    kind_value = item.get("kind")
    if kind_value is None:
        raise SpecificationError(f"Missing required field '{path}.kind'")
    kind = str(kind_value).strip().lower()
    if kind not in OPERATION_KINDS:
        raise SpecificationError(
            f"{path}.kind: must be one of {list(OPERATION_KINDS)} (got {kind_value!r})"
        )

    origin = _coerce_vec3(item, "origin", path, limit=MAX_OPERATION_COORD_MM)
    normal = _coerce_vec3(
        item, "normal", path, limit=MAX_OPERATION_NORMAL_COMPONENT, require_nonzero=True
    )
    depth = _op_float(item, "depth", path, minimum=MIN_OPERATION_MM, maximum=MAX_OPERATION_DEPTH_MM)
    width = _op_optional_float(
        item, "width", path, minimum=MIN_OPERATION_MM, maximum=MAX_OPERATION_SIZE_MM
    )
    height = _op_optional_float(
        item, "height", path, minimum=MIN_OPERATION_MM, maximum=MAX_OPERATION_SIZE_MM
    )
    diameter = _op_optional_float(
        item, "diameter", path, minimum=MIN_OPERATION_MM, maximum=MAX_OPERATION_DIAMETER_MM
    )
    length = _op_optional_float(
        item, "length", path, minimum=MIN_OPERATION_MM, maximum=MAX_OPERATION_SIZE_MM
    )

    if kind in ("hole", "boss") and diameter is None:
        raise SpecificationError(f"{path}.diameter is required for a '{kind}' operation")
    if kind in _RECTANGULAR_KINDS:
        if width is None:
            raise SpecificationError(f"{path}.width is required for a '{kind}' operation")
        if height is None:
            raise SpecificationError(f"{path}.height is required for a '{kind}' operation")
    if kind == "slot":
        if diameter is None:
            raise SpecificationError(f"{path}.diameter is required for a 'slot' operation")
        if length is None:
            raise SpecificationError(f"{path}.length is required for a 'slot' operation")

    return RenderOperation(
        kind=kind,
        origin=origin,
        normal=normal,
        depth=depth,
        width=width,
        height=height,
        diameter=diameter,
        length=length,
        label=_sanitize_label(item.get("label", "")),
    )


def parse_operations(specification: dict[str, Any] | None) -> list[RenderOperation]:
    """Validate ``specification['operations']`` and return bounded features.

    Missing/``None``/empty ``operations`` yields ``[]`` (the base part alone).
    Malformed entries, a zero-length ``normal`` and out-of-bounds numbers all
    raise :class:`SpecificationError`, which ``validate()`` surfaces as blocking
    problems. The LLM never supplies OpenSCAD code: only these coerced numbers
    reach the template.
    """
    spec: dict[str, Any] = specification if isinstance(specification, dict) else {}
    raw = spec.get("operations")
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise SpecificationError("Field 'operations' must be a list")
    if len(raw) > MAX_OPERATIONS:
        raise SpecificationError(
            f"Field 'operations' must have at most {MAX_OPERATIONS} entries (got {len(raw)})"
        )
    return [_parse_operation(item, index) for index, item in enumerate(raw)]


# ---------------------------------------------------------------------------
# Prompt-driven CSG primitives (docs/cad-primitives.md 1 / 2)
# ---------------------------------------------------------------------------

# Bounds mirror ``agents.spec`` (which the CAD package must not import, to keep
# it decoupled from ``llm-provider``). Every primitive value is re-coerced and
# re-bounded here because the LLM payload is untrusted.
#: Smallest printable primitive edge/diameter (below a 0.4 mm nozzle this is not printable).
MIN_PRIMITIVE_MM = 0.4
#: Upper bound for a primitive size to catch unit mistakes (cm vs mm).
MAX_PRIMITIVE_MM = 1000.0
#: Bounds for a primitive centre in model coordinates (mm).
MAX_PRIMITIVE_POSITION_MM = 1000.0
#: Bounds for a primitive rotation in degrees (XYZ order).
MAX_PRIMITIVE_ROTATION_DEG = 360.0
#: Hard cap on the number of rendered primitives: unbounded lists are a DoS vector.
MAX_PRIMITIVES = 64

PRIMITIVE_TYPES: tuple[str, ...] = ("box", "cylinder", "sphere", "cone")
PRIMITIVE_ROLES: tuple[str, ...] = ("add", "subtract")
_ADDITIVE_ROLES = frozenset({"add"})
#: Required size fields per primitive type; missing ones are a specification error.
_REQUIRED_SIZES: dict[str, tuple[str, ...]] = {
    "box": ("width", "depth", "height"),
    "cylinder": ("diameter", "height"),
    "sphere": ("diameter",),
    "cone": ("diameter", "height"),
}


@dataclass(frozen=True)
class RenderPrimitive:
    """A validated, bounded CSG primitive ready for rendering.

    ``position`` is the centre of the shape and ``rotation`` is in degrees
    (XYZ order). Unused size fields stay ``None`` so the renderer can rely on
    the per-type required fields (mirrors ``agents.spec.Primitive``).
    """

    type: str
    role: str
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    width: float | None = None
    depth: float | None = None
    height: float | None = None
    diameter: float | None = None
    label: str = ""

    @property
    def is_additive(self) -> bool:
        """True for ``role="add"`` (union-ed onto the base)."""
        return self.role in _ADDITIVE_ROLES


def _primitive_vec3(
    item: dict[str, Any],
    field: str,
    path: str,
    *,
    limit: float,
) -> tuple[float, float, float]:
    """Parse an optional ``{x, y, z}`` primitive vector, defaulting to origin."""
    value = item.get(field)
    if value is None:
        return (0.0, 0.0, 0.0)
    if not isinstance(value, dict):
        raise SpecificationError(f"{path}.{field}: must be an object with x, y, z")
    components = tuple(
        _op_float(value, axis, f"{path}.{field}", minimum=-limit, maximum=limit)
        for axis in ("x", "y", "z")
    )
    return components  # type: ignore[return-value]


def _parse_primitive(item: Any, index: int) -> RenderPrimitive:
    path = f"primitives[{index}]"
    if not isinstance(item, dict):
        raise SpecificationError(f"{path} must be an object")

    type_value = item.get("type")
    if type_value is None:
        raise SpecificationError(f"Missing required field '{path}.type'")
    kind = str(type_value).strip().lower()
    if kind not in PRIMITIVE_TYPES:
        raise SpecificationError(
            f"{path}.type: must be one of {list(PRIMITIVE_TYPES)} (got {type_value!r})"
        )

    role_value = item.get("role", "add")
    role = "add" if role_value is None else str(role_value).strip().lower()
    if role not in PRIMITIVE_ROLES:
        raise SpecificationError(
            f"{path}.role: must be one of {list(PRIMITIVE_ROLES)} (got {role_value!r})"
        )

    position = _primitive_vec3(item, "position", path, limit=MAX_PRIMITIVE_POSITION_MM)
    rotation = _primitive_vec3(item, "rotation", path, limit=MAX_PRIMITIVE_ROTATION_DEG)

    width = _op_optional_float(
        item, "width", path, minimum=MIN_PRIMITIVE_MM, maximum=MAX_PRIMITIVE_MM
    )
    depth = _op_optional_float(
        item, "depth", path, minimum=MIN_PRIMITIVE_MM, maximum=MAX_PRIMITIVE_MM
    )
    height = _op_optional_float(
        item, "height", path, minimum=MIN_PRIMITIVE_MM, maximum=MAX_PRIMITIVE_MM
    )
    diameter = _op_optional_float(
        item, "diameter", path, minimum=MIN_PRIMITIVE_MM, maximum=MAX_PRIMITIVE_MM
    )

    sizes: dict[str, float | None] = {
        "width": width,
        "depth": depth,
        "height": height,
        "diameter": diameter,
    }
    missing = [name for name in _REQUIRED_SIZES[kind] if sizes[name] is None]
    if missing:
        names = ", ".join(f"'{name}'" for name in missing)
        raise SpecificationError(f"{path}: {names} required for a '{kind}' primitive")

    return RenderPrimitive(
        type=kind,
        role=role,
        position=position,
        rotation=rotation,
        width=width,
        depth=depth,
        height=height,
        diameter=diameter,
        label=_sanitize_label(item.get("label", "")),
    )


def parse_primitives(specification: dict[str, Any] | None) -> list[RenderPrimitive]:
    """Validate ``specification['primitives']`` and return bounded primitives.

    Missing/``None``/empty ``primitives`` yields ``[]`` so the historical
    holder-token render is preserved byte-for-byte. An unknown ``type``/``role``,
    a missing per-type size or an out-of-bounds number all raise
    :class:`SpecificationError`, which ``validate()`` surfaces as blocking
    problems. The LLM never supplies OpenSCAD code: only these coerced numbers
    reach the renderer.
    """
    spec: dict[str, Any] = specification if isinstance(specification, dict) else {}
    raw = spec.get("primitives")
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise SpecificationError("Field 'primitives' must be a list")
    if len(raw) > MAX_PRIMITIVES:
        raise SpecificationError(
            f"Field 'primitives' must have at most {MAX_PRIMITIVES} entries (got {len(raw)})"
        )
    return [_parse_primitive(item, index) for index, item in enumerate(raw)]


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
"""
)

#: Top-level call for the base part. Kept out of the template so the operations
#: block can wrap ``holder();`` in ``difference(){ union(){ holder(); ... } ... }``
#: while the no-operation render stays byte-for-byte identical to before.
_HOLDER_CALL = "holder();\n"


def _num(value: float) -> str:
    """Format a float for SCAD without scientific notation or ``-0.0``."""
    if value == 0:
        return "0.0"
    text = f"{value:.4f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def render_scad(
    parameters: HolderParameters,
    operations: Sequence[RenderOperation] | None = None,
    primitives: Sequence[RenderPrimitive] | None = None,
) -> str:
    """Render the self-contained OpenSCAD template for validated parameters.

    With no primitives the output is byte-for-byte the historical holder part
    (plus the ``operations`` layer when present). With primitives the base part
    is the primitive CSG; the same ``operations`` layer is then composited on
    top, so subtractive operations cut the primitive base exactly like the
    holder template.
    """
    rendered = _SCAD_TEMPLATE.substitute(
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
    features = list(operations or [])
    parts = list(primitives or [])
    if not parts:
        if not features:
            # No primitives and no operations: preserve the historical render exactly.
            return rendered + "\n" + _HOLDER_CALL
        return rendered + "\n" + render_operations(features) + "\n"

    base = render_primitives(parts)
    if not features:
        return rendered + "\n" + base + "\n"
    return rendered + "\n" + render_operations(features, base=base) + "\n"


#: Extent the local cutters overshoot their surface so booleans are robust.
_MARGIN = 2.0


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = vector
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-9:
        raise SpecificationError("Operation normal must not be a zero-length vector")
    return (x / length, y / length, z / length)


def _cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _local_frame(
    origin: tuple[float, float, float], normal: tuple[float, float, float]
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    """Return the orthonormal ``(u, v, n)`` frame for an operation.

    ``n`` is the (normalised) operation axis; ``u``/``v`` span the plane. The
    reference axis switches away from ``z`` when the normal is nearly parallel
    to it, so ``cross(ref, n)`` can never be degenerate.
    """
    n = _normalize(normal)
    reference = (0.0, 0.0, 1.0) if abs(n[2]) < 0.9 else (1.0, 0.0, 0.0)
    u = _normalize(_cross(reference, n))
    v = _cross(n, u)
    return u, v, n


def _matrix_literal(
    u: tuple[float, float, float],
    v: tuple[float, float, float],
    n: tuple[float, float, float],
    origin: tuple[float, float, float],
) -> str:
    """Format an OpenSCAD ``multmatrix`` value.

    OpenSCAD matrices are row-major with the translation in the 4th column, so
    the columns hold the local frame axes ``u``/``v``/``n`` plus ``origin``.
    """
    rows = (
        (u[0], v[0], n[0], origin[0]),
        (u[1], v[1], n[1], origin[1]),
        (u[2], v[2], n[2], origin[2]),
        (0.0, 0.0, 0.0, 1.0),
    )
    body = ", ".join("[" + ", ".join(_num(value) for value in row) + "]" for row in rows)
    return "[" + body + "]"


def _required(value: float | None, operation: RenderOperation, field: str) -> float:
    if value is None:
        raise SpecificationError(
            f"Operation '{operation.kind}' is missing required field '{field}'"
        )
    return value


def _local_geometry(operation: RenderOperation) -> str:
    """OpenSCAD statement for ``operation`` expressed in its own local frame."""
    if operation.kind == "hole":
        diameter = _required(operation.diameter, operation, "diameter")
        return (
            f"translate([0.0, 0.0, {_num(-_MARGIN)}]) "
            f"cylinder(d={_num(diameter)}, h={_num(operation.depth + 2 * _MARGIN)});"
        )
    if operation.kind == "boss":
        diameter = _required(operation.diameter, operation, "diameter")
        return f"cylinder(d={_num(diameter)}, h={_num(operation.depth)});"
    if operation.kind in ("pocket", "cut"):
        width = _required(operation.width, operation, "width")
        height = _required(operation.height, operation, "height")
        return (
            f"translate([{_num(-width / 2)}, {_num(-height / 2)}, {_num(-_MARGIN)}]) "
            f"cube([{_num(width)}, {_num(height)}, {_num(operation.depth + 2 * _MARGIN)}]);"
        )
    if operation.kind == "add":
        width = _required(operation.width, operation, "width")
        height = _required(operation.height, operation, "height")
        return (
            f"translate([{_num(-width / 2)}, {_num(-height / 2)}, 0.0]) "
            f"cube([{_num(width)}, {_num(height)}, {_num(operation.depth)}]);"
        )
    # slot: a capsule / stadium hull of two end cylinders along the local x axis.
    diameter = _required(operation.diameter, operation, "diameter")
    length = _required(operation.length, operation, "length")
    cutter_height = _num(operation.depth + 2 * _MARGIN)
    return (
        "hull() { "
        f"translate([{_num(-length / 2)}, 0.0, {_num(-_MARGIN)}]) "
        f"cylinder(d={_num(diameter)}, h={cutter_height}); "
        f"translate([{_num(length / 2)}, 0.0, {_num(-_MARGIN)}]) "
        f"cylinder(d={_num(diameter)}, h={cutter_height}); "
        "}"
    )


def _render_operation(operation: RenderOperation, indent: str) -> str:
    u, v, n = _local_frame(operation.origin, operation.normal)
    matrix = _matrix_literal(u, v, n, operation.origin)
    comment = f"{indent}// operation: {operation.kind}"
    if operation.label:
        comment += f" -- {operation.label}"
    return f"{comment}\n{indent}multmatrix({matrix}) {_local_geometry(operation)}"


def _required_primitive(value: float | None, primitive: RenderPrimitive, field: str) -> float:
    if value is None:
        raise SpecificationError(
            f"Primitive '{primitive.type}' is missing required field '{field}'"
        )
    return value


def _render_primitive(primitive: RenderPrimitive, indent: str) -> str:
    """OpenSCAD statement for a primitive placed at its centre of mass."""
    px, py, pz = primitive.position
    rx, ry, rz = primitive.rotation
    translate = f"translate([{_num(px)}, {_num(py)}, {_num(pz)}])"
    rotate = f"rotate([{_num(rx)}, {_num(ry)}, {_num(rz)}])"
    if primitive.type == "box":
        width = _required_primitive(primitive.width, primitive, "width")
        depth = _required_primitive(primitive.depth, primitive, "depth")
        height = _required_primitive(primitive.height, primitive, "height")
        geometry = (
            f"{translate} {rotate} "
            f"translate([{_num(-width / 2)}, {_num(-depth / 2)}, {_num(-height / 2)}]) "
            f"cube([{_num(width)}, {_num(depth)}, {_num(height)}]);"
        )
    elif primitive.type == "cylinder":
        diameter = _required_primitive(primitive.diameter, primitive, "diameter")
        height = _required_primitive(primitive.height, primitive, "height")
        geometry = (
            f"{translate} {rotate} cylinder(d={_num(diameter)}, h={_num(height)}, center=true);"
        )
    elif primitive.type == "sphere":
        diameter = _required_primitive(primitive.diameter, primitive, "diameter")
        geometry = f"{translate} sphere(d={_num(diameter)});"
    else:  # cone
        diameter = _required_primitive(primitive.diameter, primitive, "diameter")
        height = _required_primitive(primitive.height, primitive, "height")
        geometry = (
            f"{translate} {rotate} "
            f"cylinder(d1={_num(diameter)}, d2=0, h={_num(height)}, center=true);"
        )
    comment = f"{indent}// primitive: {primitive.type}"
    if primitive.label:
        comment += f" -- {primitive.label}"
    return f"{comment}\n{indent}{geometry}"


def render_primitives(primitives: Sequence[RenderPrimitive]) -> str:
    """Render the primitive CSG base.

    Emits ``difference() { union() { <adds> } <subtracts> }`` so ``role="add"``
    union-ed material has the ``role="subtract"`` shapes cut out of it.
    """
    adds = [primitive for primitive in primitives if primitive.is_additive]
    subs = [primitive for primitive in primitives if not primitive.is_additive]
    lines = ["difference() {", "    union() {"]
    for primitive in adds:
        lines.append(_render_primitive(primitive, "        "))
    lines.append("    }")
    for primitive in subs:
        lines.append(_render_primitive(primitive, "    "))
    lines.append("}")
    return "\n".join(lines)


def render_operations(operations: Sequence[RenderOperation], *, base: str = "holder();") -> str:
    """Render the ``difference``/``union`` block combining base + features.

    ``base`` is the top-level statement (or block) the operations are applied
    to. It defaults to the holder call, preserving the historical output. With
    primitives the caller passes their CSG block so the annotation layer is
    composited on top of it identically.
    """
    adds = [operation for operation in operations if operation.is_additive]
    subs = [operation for operation in operations if operation.kind in _SUBTRACTIVE_KINDS]
    lines = ["difference() {", "    union() {"]
    lines.extend(f"        {line}" for line in base.splitlines())
    for operation in adds:
        lines.append(_render_operation(operation, "        "))
    lines.append("    }")
    for operation in subs:
        lines.append(_render_operation(operation, "    "))
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


#: Default sandbox image published by infra-devops (``OPENSCAD_IMAGE`` overrides).
DEFAULT_IMAGE = "ghcr.io/branc9/printforge-openscad:latest"

# Runtime settings resolved through the configuration app (UI-editable).
_MODE_SETTING = "openscad_mode"
_TIMEOUT_SETTING = "openscad_timeout_sec"
_MEMORY_SETTING = "openscad_memory_limit"
_CPU_SETTING = "openscad_cpu_limit"

# Env-only fallbacks for the same knobs (local dev / when configuration is absent).
_ENV_NAMES = {
    _MODE_SETTING: "OPENSCAD_MODE",
    _TIMEOUT_SETTING: "OPENSCAD_TIMEOUT_SEC",
    _MEMORY_SETTING: "OPENSCAD_MEMORY_LIMIT",
    _CPU_SETTING: "OPENSCAD_CPU_LIMIT",
}


def _env_override(name: str, default: str) -> str:
    """Read an env-only override (e.g. ``OPENSCAD_IMAGE``); empty means unset."""
    value = os.environ.get(name)
    return value if value else default


def _legacy_setting(name: str, default: Any = None) -> Any:
    """Fallback resolution without the configuration app: env -> settings -> default."""
    env_name = _ENV_NAMES.get(name)
    if env_name:
        value = os.environ.get(env_name)
        if value:
            return value
        value = getattr(settings, env_name, None)
        if value is not None:
            return value
    return default


def _read_runtime_setting(name: str, default: Any = None) -> Any:
    """Effective runtime setting: DB override -> settings -> env -> default.

    ``configuration.services.get_setting`` is imported lazily -- never at module
    import time -- so a UI change applies to the next job without a restart and
    tests can patch ``configuration.services.get_setting``. If the configuration
    app is missing or a lookup fails, fall back to env/settings so CAD rendering
    keeps working.
    """
    try:
        from configuration.services import get_setting
    except Exception:  # noqa: BLE001 - configuration app may land in parallel
        logger.warning("configuration service unavailable; using env/settings for %r", name)
        return _legacy_setting(name, default)
    try:
        value = get_setting(name)
    except Exception:  # noqa: BLE001 - a settings lookup must not break CAD
        logger.warning("get_setting(%r) failed; using env/settings", name)
        return _legacy_setting(name, default)
    return default if value is None else value


@dataclass(frozen=True)
class OpenSCADConfig:
    """Resolved runtime configuration for the backend."""

    mode: str = "local"
    binary: str = "openscad"
    image: str = DEFAULT_IMAGE
    docker_binary: str = "docker"
    timeout_sec: float = 60.0
    memory_limit: str = "1g"
    cpu_limit: str = "1.0"


def _source_of(model: GeneratedModel | str) -> str:
    if isinstance(model, GeneratedModel):
        return model.scad_source
    if isinstance(model, str):
        return model
    raise TypeError("model must be a GeneratedModel or an OpenSCAD source string")


class OpenSCADBackend(CADBackend):
    """Render a parametric part and export it through the OpenSCAD CLI.

    Runtime knobs are re-read from ``configuration.services.get_setting`` on
    every ``config`` access, so nothing is cached at import time and a UI change
    takes effect on the next job. Explicit constructor values (or a full
    :class:`OpenSCADConfig`) win over the settings -- handy for tests.
    """

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
        if config is not None:
            self._overrides: dict[str, Any] = {
                field.name: getattr(config, field.name) for field in fields(config)
            }
        else:
            overrides = {
                "mode": mode,
                "binary": binary,
                "image": image,
                "docker_binary": docker_binary,
                "timeout_sec": timeout_sec,
                "memory_limit": memory_limit,
                "cpu_limit": cpu_limit,
            }
            self._overrides = {key: value for key, value in overrides.items() if value is not None}
        # Fail fast on an invalid mode and read the current settings once.
        self._effective_config()

    @property
    def config(self) -> OpenSCADConfig:
        """Effective config, resolved fresh on every access (never cached)."""
        return self._effective_config()

    def _effective_config(self) -> OpenSCADConfig:
        """Build the effective config from runtime settings + explicit overrides."""
        base = OpenSCADConfig(
            mode=str(_read_runtime_setting(_MODE_SETTING, "local")).strip().lower(),
            binary=_env_override("OPENSCAD_BINARY", "openscad"),
            image=_env_override("OPENSCAD_IMAGE", DEFAULT_IMAGE),
            docker_binary=_env_override("OPENSCAD_DOCKER_BINARY", "docker"),
            timeout_sec=float(_read_runtime_setting(_TIMEOUT_SETTING, 60)),
            memory_limit=str(_read_runtime_setting(_MEMORY_SETTING, "1g")),
            cpu_limit=str(_read_runtime_setting(_CPU_SETTING, "1.0")),
        )
        resolved = replace(base, **self._overrides)
        mode = resolved.mode.strip().lower()
        if mode not in ("local", "docker"):
            raise CADError(f"Unknown openscad_mode {mode!r}; expected 'local' or 'docker'")
        return replace(resolved, mode=mode)

    # -- CADBackend ---------------------------------------------------------

    def generate(self, specification: dict[str, Any]) -> str:
        """Validate ``specification`` and render self-contained OpenSCAD source."""
        parameters = build_parameters(specification)
        operations = parse_operations(specification)
        primitives = parse_primitives(specification)
        return render_scad(parameters, operations, primitives)

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
            try:
                parse_operations(model.specification)
            except SpecificationError as exc:
                problems.append(str(exc))
            try:
                parse_primitives(model.specification)
            except SpecificationError as exc:
                problems.append(str(exc))
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

        config = self.config
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
                    timeout=config.timeout_sec,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise OpenSCADTimeout(f"OpenSCAD timed out after {config.timeout_sec}s") from exc

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
        config = self.config
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
                "-o",
                "/out/model.stl",
                "/work/model.scad",
            ]
        return [
            config.binary,
            "-o",
            str(output_dir / "model.stl"),
            str(input_dir / "model.scad"),
        ]
