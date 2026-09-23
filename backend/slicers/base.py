"""Backend-agnostic slicing interface (terv.md 12. fejezet).

Pipeline: a mesh (STL/3MF bytes or a path) plus three data-only profiles
(``PrinterProfile`` / ``FilamentProfile`` / ``ProcessProfile``) -> G-code.

Everything here stays free of Django imports so the slicing engine can be unit
tested without a database and later reused by an out-of-process worker. The
profiles are passed as plain dataclasses (:class:`PrinterSettings`,
:class:`FilamentSettings`, :class:`ProcessSettings`) built from the ORM rows by
``slicers.services`` -- profiles are **data**, never code.

The first concrete backend is :class:`slicers.prusaslicer.PrusaSlicerBackend`;
an OrcaSlicer backend can implement the same contract later.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_FILAMENT_DENSITY",
    "DEFAULT_FILAMENT_DIAMETER",
    "MATERIAL_DENSITIES",
    "FilamentSettings",
    "ModelInput",
    "PlateMesh",
    "PrinterSettings",
    "ProcessSettings",
    "SliceModel",
    "SlicedResult",
    "SlicerBackend",
    "SlicerError",
    "SlicerTimeout",
    "UnsupportedModelError",
    "filament_density_for",
    "filament_grams_from_length",
    "resolve_filament_density",
    "resolve_filament_diameter",
    "resolve_model",
    "with_default_filament_density",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SlicerError(Exception):
    """Base class for every slicing pipeline failure."""


class SlicerTimeout(SlicerError):
    """The slicer CLI exceeded the hard timeout."""


class UnsupportedModelError(SlicerError, ValueError):
    """The model input could not be resolved (missing file, bad type)."""


# ---------------------------------------------------------------------------
# Data-only profile inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrinterSettings:
    """Slicer-side printer profile data (mirrors ``slicers.PrinterProfile``)."""

    name: str = ""
    printer_model: str = ""
    backend: str = ""
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FilamentSettings:
    """Filament profile data (mirrors ``slicers.FilamentProfile``)."""

    name: str = ""
    material: str = ""
    brand: str = ""
    color: str = ""
    settings: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Filament density / mass helpers
# ---------------------------------------------------------------------------
#
# PrusaSlicer computes ``filament used [g]`` from the loaded ``filament_density``.
# A profile that omits it makes the slicer log ``filament_density = 0`` and emit
# ``filament used [g] = 0`` even though the extruded length (``filament used
# [mm]``) is meaningful. These helpers supply a published typical density for
# the profile's material; an explicit ``filament_density`` always wins.

#: Published typical densities (g/cm³) for common FDM materials.
MATERIAL_DENSITIES: dict[str, float] = {
    "PLA": 1.24,
    "PETG": 1.27,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "PA": 1.14,  # Nylon
    "PC": 1.20,
}

#: Density (g/cm³) assumed when the material is unknown or blank.
DEFAULT_FILAMENT_DENSITY = 1.24

#: Filament diameter (mm) assumed when a profile does not set one.
DEFAULT_FILAMENT_DIAMETER = 1.75

#: Accepted material spellings -> canonical key in :data:`MATERIAL_DENSITIES`.
_MATERIAL_ALIASES: dict[str, str] = {
    "PLA": "PLA",
    "POLYLACTICACID": "PLA",
    "POLYLACTIDE": "PLA",
    "PETG": "PETG",
    "ABS": "ABS",
    "ASA": "ASA",
    "TPU": "TPU",
    "PA": "PA",
    "NYLON": "PA",
    "PA6": "PA",
    "PA12": "PA",
    "PC": "PC",
    "POLYCARBONATE": "PC",
}

#: Longest canonical key first, so ``"PETG-CF"`` matches ``PETG`` not ``PET``.
_MATERIAL_KEY_ORDER: tuple[str, ...] = tuple(sorted(MATERIAL_DENSITIES, key=len, reverse=True))


def _normalise_material(material: str | None) -> str:
    """Upper-case ``material`` and drop separators (``"pa-cf"`` -> ``"PACF"``)."""
    return "".join(char for char in (material or "").upper() if char.isalnum())


def _positive_float(value: Any) -> float | None:
    """Coerce ``value`` to a finite, strictly positive float (else ``None``)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def filament_density_for(material: str | None) -> float:
    """Return a typical density (g/cm³) for ``material``.

    Matching is case/separator-insensitive, so ``"PLA+"``, ``"pa-cf"`` and
    ``"Nylon"`` all resolve; anything unrecognised falls back to
    :data:`DEFAULT_FILAMENT_DENSITY` (PLA's 1.24 g/cm³).
    """
    normalised = _normalise_material(material)
    if not normalised:
        return DEFAULT_FILAMENT_DENSITY
    canonical = _MATERIAL_ALIASES.get(normalised, normalised)
    if canonical in MATERIAL_DENSITIES:
        return MATERIAL_DENSITIES[canonical]
    for key in _MATERIAL_KEY_ORDER:
        if normalised.startswith(key):
            return MATERIAL_DENSITIES[key]
    return DEFAULT_FILAMENT_DENSITY


def _explicit_filament_density(settings: Mapping[str, Any] | None) -> float | None:
    """Read a valid explicit ``filament_density`` (inline key or ``overrides``)."""
    settings = settings or {}
    candidates: list[Any] = [settings.get("filament_density")]
    overrides = settings.get("overrides")
    if isinstance(overrides, Mapping):
        candidates.append(overrides.get("filament_density"))
    for value in candidates:
        number = _positive_float(value)
        if number is not None:
            return number
    return None


def resolve_filament_density(settings: Mapping[str, Any] | None, material: str | None) -> float:
    """A profile's effective density: explicit value, else the material default."""
    explicit = _explicit_filament_density(settings)
    if explicit is not None:
        return explicit
    return filament_density_for(material)


def with_default_filament_density(
    settings: Mapping[str, Any] | None, material: str | None
) -> dict[str, Any]:
    """Copy ``settings``, adding a material-derived ``filament_density`` if unset.

    An explicit, valid ``filament_density`` -- inline or under ``overrides`` --
    is never overwritten, so a profile always wins over the material default.
    """
    result = dict(settings or {})
    if _explicit_filament_density(result) is not None:
        return result
    result["filament_density"] = filament_density_for(material)
    return result


def resolve_filament_diameter(
    settings: Mapping[str, Any] | None,
    default: float = DEFAULT_FILAMENT_DIAMETER,
) -> float:
    """Read ``filament_diameter`` (inline key or ``overrides``) or return ``default``."""
    settings = settings or {}
    value = settings.get("filament_diameter")
    if value is None:
        overrides = settings.get("overrides")
        if isinstance(overrides, Mapping):
            value = overrides.get("filament_diameter")
    return _positive_float(value) or default


def filament_grams_from_length(
    length_mm: float,
    density: float,
    diameter: float = DEFAULT_FILAMENT_DIAMETER,
) -> float:
    """Estimate filament mass (g) from extruded length (mm), density, diameter.

    ``mass = length x (pi x r^2) x density / 1000`` (mm³ -> g via ``g/cm³``).
    """
    radius = diameter / 2.0
    volume_mm3 = length_mm * math.pi * radius * radius
    return volume_mm3 * density / 1000.0


@dataclass(frozen=True)
class ProcessSettings:
    """Process profile data (mirrors ``slicers.ProcessProfile``)."""

    name: str = ""
    layer_height: float | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Model input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SliceModel:
    """A mesh handed to the slicer: raw bytes plus a filename/format hint."""

    data: bytes
    filename: str = "model.stl"
    format: str = "stl"


_VALID_SUFFIXES = (".stl", ".3mf", ".obj")


def _suffix(filename: str | None) -> str:
    if filename:
        lowered = filename.lower()
        for candidate in _VALID_SUFFIXES:
            if lowered.endswith(candidate):
                return candidate
    return ".stl"


#: Anything a backend accepts as the ``model`` argument.
type ModelInput = SliceModel | bytes | bytearray | memoryview | str | Path


def resolve_model(model: ModelInput) -> SliceModel:
    """Coerce ``model`` into a :class:`SliceModel`.

    Accepts a :class:`SliceModel`, raw bytes, or a filesystem path (``str`` /
    ``Path``). Reading a path here keeps the concrete backends simple; the
    caller should pass the already-stored STL/3MF artifact.
    """
    if isinstance(model, SliceModel):
        return model
    if isinstance(model, (bytes, bytearray, memoryview)):
        return SliceModel(data=bytes(model))
    if isinstance(model, (str, Path)):
        path = Path(model)
        if not path.is_file():
            raise UnsupportedModelError(f"Model file not found: {path}")
        suffix = _suffix(path.name)
        return SliceModel(data=path.read_bytes(), filename=path.name, format=suffix.lstrip("."))
    raise UnsupportedModelError(f"Unsupported model input: {type(model).__name__}")


# ---------------------------------------------------------------------------
# Build plate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlateMesh:
    """One mesh placed on a build plate (terv.md 28. fejezet).

    ``model`` is the mesh itself; ``x``/``y``/``z`` are the plate translation in
    millimetres, ``rotation_z`` the rotation around the Z axis in degrees and
    ``scale`` a uniform scaling factor. ``name`` labels the object in the slicer
    and ``key`` lets a backend reuse a single object for identical meshes (e.g.
    the ``ModelVersion`` primary key).
    """

    model: SliceModel
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    rotation_z: float = 0.0
    scale: float = 1.0
    name: str = ""
    key: str | None = None

    @property
    def is_identity(self) -> bool:
        """True when the item sits at the origin without rotation or scaling."""
        return (
            self.x == 0.0
            and self.y == 0.0
            and self.z == 0.0
            and self.rotation_z == 0.0
            and self.scale == 1.0
        )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlicedResult:
    """The output of a slicing run.

    ``data`` carries the produced bytes (G-code / 3MF); ``path`` may carry a
    storage/relative path instead when a backend streams to disk. Estimates are
    best-effort and may be refined by :meth:`SlicerBackend.estimate`.
    """

    data: bytes | None = None
    path: str | None = None
    format: str = "gcode"
    estimated_time_sec: int | None = None
    estimated_filament_g: float | None = None
    estimated_filament_mm: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


class SlicerBackend(ABC):
    """Interface every slicer backend must implement (terv.md 12. fejezet)."""

    name = "base"
    supported_formats: tuple[str, ...] = ("gcode",)

    @abstractmethod
    def slice(
        self,
        model: ModelInput,
        printer: PrinterSettings,
        filament: FilamentSettings,
        process: ProcessSettings,
    ) -> SlicedResult:
        """Slice ``model`` with the three profiles and return the result."""

    @abstractmethod
    def estimate(self, result: SlicedResult) -> dict[str, Any]:
        """Return a normalised estimate dict for ``result``."""

    def slice_plate(
        self,
        items: Sequence[PlateMesh],
        printer: PrinterSettings,
        filament: FilamentSettings,
        process: ProcessSettings,
    ) -> SlicedResult:
        """Slice a whole build plate as one job (terv.md 28. fejezet).

        The default implementation keeps single-object backends working: a
        one-item plate falls back to :meth:`slice` (the item's transform is
        ignored) and anything larger raises :class:`NotImplementedError`.
        Backends that can carry per-item transforms -- notably
        :class:`~slicers.prusaslicer.PrusaSlicerBackend`, which writes a 3MF
        project -- override this.
        """
        if not items:
            raise SlicerError("Cannot slice an empty build plate")
        if len(items) == 1:
            return self.slice(items[0].model, printer, filament, process)
        raise NotImplementedError(
            f"{type(self).__name__} does not implement multi-object plate slicing"
        )
