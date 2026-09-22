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

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
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
    "resolve_model",
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
