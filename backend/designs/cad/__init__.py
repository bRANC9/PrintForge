"""CAD pipeline (specification -> OpenSCAD source or adopted mesh -> STL).

Public entry points:

- :class:`~designs.cad.base.CADBackend` -- backend-agnostic interface.
- :class:`~designs.cad.openscad.OpenSCADBackend` -- OpenSCAD CLI implementation.
- :class:`~designs.cad.mesh.MeshCADBackend` -- adopt an externally generated
  mesh (``.stl``/``.obj``/``.glb``), transform it onto the build plate and export
  a print-ready STL.
- :func:`~designs.cad.meshcheck.check_mesh` -- deterministic printability report
  for a mesh.
- :func:`~designs.cad.dimensions.dimension_issues` -- compare a rendered bounding
  box with the requested ``dimensions`` (pure numbers, no mesh).
- :func:`~designs.cad.preview.render_stl_preview` -- headless STL -> PNG preview
  used by the vision review node.
- :func:`~designs.cad.pipeline.render_version` -- run the pipeline for a
  ``designs.ModelVersion`` and persist artifacts through ``files.services``.

The Celery entry point lives in :mod:`designs.tasks`
(``designs.render_model_stl``). The worker contract is documented in
``workers/cad/README.md``.

The package deliberately depends only on a plain ``dict`` specification, so it
does not import ``agents.spec`` (owned by ``llm-provider``).
"""

from .base import (
    CADBackend,
    CADError,
    GeneratedModel,
    SpecificationError,
    UnsupportedFormatError,
)
from .dimensions import DIMENSION_TOLERANCE, dimension_issues
from .mesh import MeshCADBackend, MeshRepair, MeshTransform
from .meshcheck import (
    SUPPORTED_MESH_FORMATS,
    MeshCheckError,
    MeshReport,
    check_mesh,
)
from .openscad import OpenSCADBackend
from .pipeline import (
    ARTIFACT_FILENAMES,
    ARTIFACT_TARGETS,
    CAD_BACKEND_ALIASES,
    dimension_warnings,
    get_backend,
    render_version,
    resolve_backend_name,
)
from .preview import PreviewRenderError, render_stl_preview

__all__ = [
    "ARTIFACT_FILENAMES",
    "ARTIFACT_TARGETS",
    "CAD_BACKEND_ALIASES",
    "CADBackend",
    "CADError",
    "DIMENSION_TOLERANCE",
    "GeneratedModel",
    "MeshCADBackend",
    "MeshCheckError",
    "MeshRepair",
    "MeshReport",
    "MeshTransform",
    "OpenSCADBackend",
    "PreviewRenderError",
    "SUPPORTED_MESH_FORMATS",
    "SpecificationError",
    "UnsupportedFormatError",
    "check_mesh",
    "dimension_issues",
    "dimension_warnings",
    "get_backend",
    "render_stl_preview",
    "render_version",
    "resolve_backend_name",
]
