"""CAD pipeline (specification -> OpenSCAD source -> STL).

Public entry points:

- :class:`~designs.cad.base.CADBackend` -- backend-agnostic interface.
- :class:`~designs.cad.openscad.OpenSCADBackend` -- OpenSCAD CLI implementation.
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

from .base import CADBackend, CADError, GeneratedModel, SpecificationError
from .openscad import OpenSCADBackend
from .preview import PreviewRenderError, render_stl_preview

__all__ = [
    "CADBackend",
    "CADError",
    "GeneratedModel",
    "OpenSCADBackend",
    "PreviewRenderError",
    "SpecificationError",
    "render_stl_preview",
]
