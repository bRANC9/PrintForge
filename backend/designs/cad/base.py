"""Backend-agnostic CAD interface.

Pipeline: ``specification`` (plain ``dict``) -> OpenSCAD source -> exported
bytes (STL). Keeping this package free of LLM/spec imports decouples it from
``llm-provider`` (see terv.md 3., 8. es 9. fejezet).

Later backends (CadQuery, build123d, FreeCAD) implement the same
:class:`CADBackend` contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CADBackend",
    "CADError",
    "GeneratedModel",
    "SpecificationError",
    "UnsupportedFormatError",
]


class CADError(Exception):
    """Base class for every CAD pipeline failure."""


class SpecificationError(CADError, ValueError):
    """The specification dict is missing fields or holds invalid values."""


class UnsupportedFormatError(CADError, ValueError):
    """The requested export format is not supported by the backend."""


@dataclass(frozen=True)
class GeneratedModel:
    """A generated OpenSCAD model together with the specification it came from.

    ``generate()`` returns the raw SCAD source string; ``validate()`` and
    ``export()`` take a :class:`GeneratedModel` so a backend can see both the
    source and the parameters it was built from.
    """

    specification: dict[str, Any]
    scad_source: str


class CADBackend(ABC):
    """Interface every CAD backend must implement.

    ``generate`` renders OpenSCAD source from a structured specification.
    ``validate`` returns a list of *blocking* problems (empty == valid).
    ``export`` turns a generated model into bytes for the requested format.
    """

    name = "base"

    @abstractmethod
    def generate(self, specification: dict[str, Any]) -> str:
        """Render OpenSCAD source for ``specification``."""

    @abstractmethod
    def validate(self, model: GeneratedModel) -> list[str]:
        """Return blocking problems for ``model`` (empty list == valid)."""

    @abstractmethod
    def export(self, model: GeneratedModel, format: str) -> bytes:
        """Export ``model`` to ``format`` and return the raw bytes."""
