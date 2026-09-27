"""Backend-agnostic CAD interface.

Pipeline: ``specification`` (plain ``dict``) -> backend output -> exported
bytes (STL). Two output shapes are supported behind one contract:

- *parametric* backends (:class:`~designs.cad.openscad.OpenSCADBackend`) return
  source code in :attr:`GeneratedModel.scad_source`;
- *mesh* backends (:class:`~designs.cad.mesh.MeshCADBackend`) return a loaded
  mesh in :attr:`GeneratedModel.mesh_bytes` / :attr:`GeneratedModel.mesh_format`.

Keeping this package free of LLM/spec imports decouples it from
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
    """A generated model together with the specification it came from.

    ``generate()`` returns the backend's "source" string; ``validate()`` and
    ``export()`` take a :class:`GeneratedModel` so a backend can see both that
    string and the parameters it was built from. A parametric backend fills
    :attr:`scad_source`; a mesh backend fills :attr:`mesh_bytes` and leaves
    :attr:`scad_source` holding a short human-readable manifest.
    """

    specification: dict[str, Any]
    scad_source: str = ""
    #: Raw mesh produced by non-parametric backends, or the mesh a mesh backend
    #: loaded from the source artifact. ``None`` for parametric backends.
    mesh_bytes: bytes | None = None
    #: Lowercase extension of ``mesh_bytes`` ("stl", "obj", "glb") or "".
    mesh_format: str = ""


class CADBackend(ABC):
    """Interface every CAD backend must implement.

    ``generate`` renders the backend's source representation from a structured
    specification.
    ``validate`` returns a list of *blocking* problems (empty == valid).
    ``export`` turns a generated model into bytes for the requested format.

    The two class attributes below describe what the pipeline should persist;
    their defaults reproduce the historic OpenSCAD behaviour (a ``.scad`` plus a
    ``.stl``), so a backend only overrides them when it produces something else.
    """

    name = "base"
    #: Artifact kinds this backend writes, in order. Mirrors the storage layout
    #: in ``designs.services.ARTIFACT_KINDS``.
    produced_artifacts: tuple[str, ...] = ("scad", "stl")
    #: Whether ``generate()`` returned real source code to persist. Mesh backends
    #: return a manifest string and set this to ``False`` so no ``.scad`` is written.
    produces_source_code: bool = True

    @abstractmethod
    def generate(self, specification: dict[str, Any]) -> str:
        """Render the backend's source representation for ``specification``."""

    @abstractmethod
    def validate(self, model: GeneratedModel) -> list[str]:
        """Return blocking problems for ``model`` (empty list == valid)."""

    @abstractmethod
    def export(self, model: GeneratedModel, format: str) -> bytes:
        """Export ``model`` to ``format`` and return the raw bytes."""

    def build_model(self, specification: dict[str, Any]) -> GeneratedModel:
        """Run :meth:`generate` and wrap the result in a :class:`GeneratedModel`.

        The default is the parametric path: the returned string is stored as
        :attr:`GeneratedModel.scad_source`. A mesh backend overrides this to
        attach :attr:`GeneratedModel.mesh_bytes` while still returning its
        manifest from :meth:`generate`. This is a concrete helper, never
        abstract, so existing backends and duck-typed test doubles keep working.
        """
        return GeneratedModel(specification=specification, scad_source=self.generate(specification))
