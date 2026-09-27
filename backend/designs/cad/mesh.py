"""Mesh backend: an externally generated mesh (``.stl``/``.obj``/``.glb``) -> print-ready STL.

Where :class:`~designs.cad.openscad.OpenSCADBackend` *generates* geometry from a
specification, :class:`MeshCADBackend` *adopts* geometry that was produced
elsewhere -- typically by an image-to-3D diffusion model. The mesh source is a
file in ``files`` storage referenced by a relative path, so the raw artefact
never travels through the database row or the Celery payload.

Pipeline::

    specification -> read source bytes -> parse (trimesh) -> transform -> repair
                  -> binary STL -> storage

Everything runs in-process: no shell, no network, no GPU and no randomness.

Scale convention
----------------
``transform.scale_mm`` is the **largest bounding-box edge** of the result, in
millimetres (not the diagonal, not the X extent). Pick largest edge because it
bounds the part in all three axes: the resulting part is guaranteed to fit a
``scale_mm`` cube, which is what a slicer build volume check needs. When the key
is absent, ``settings.MESH_DEFAULT_SCALE_MM`` is used; a non-positive value
disables scaling and keeps the source units.

Orientation convention
----------------------
PrintForge is Z-up with the part resting on the build plate at **minimum Z = 0**
(``agents/spec.py`` and ``designs/cad/openscad.py``), so the transform ends with
``rest_on_plate`` (translate ``min_z`` to ``0``) and ``center_xy`` (translate the
bounding-box centre of X/Y to the origin). ``transform.rotate_deg`` is an XYZ
rotation in degrees about the bounding-box centre -- the same convention as the
OpenSCAD backend's ``rotate([rx, ry, rz])``, i.e. ``Rx @ Ry @ Rz`` -- applied
*before* the plate-resting translation, so a rotated part still lands flat.

Repair convention
-----------------
Repair runs in the order normals -> holes -> decimation and each step is
optional and independently failure-tolerant: a step that the installed
trimesh extras cannot perform is recorded as a warning instead of aborting the
job. Decimation only runs when the mesh is *over* the face budget, and it
degrades to a warning when neither ``fast_simplification`` nor ``open3d`` is
installed (trimesh requires one of them for
``simplify_quadric_decimation``).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from django.conf import settings

from .base import (
    CADBackend,
    CADError,
    GeneratedModel,
    SpecificationError,
    UnsupportedFormatError,
)
from .meshcheck import SUPPORTED_MESH_FORMATS, MeshCheckError, check_mesh, load_mesh

__all__ = [
    "DEFAULT_REPAIR_ENABLED",
    "DEFAULT_SCALE_MM",
    "DEFAULT_TARGET_FACES",
    "MeshCADBackend",
    "MeshRepair",
    "MeshTransform",
]

logger = logging.getLogger(__name__)

#: Defaults used when the matching Django setting is absent.
DEFAULT_SCALE_MM = 120.0
DEFAULT_TARGET_FACES = 50_000
DEFAULT_REPAIR_ENABLED = True

#: Guard rail for a target face budget: a decimation request below this would
#: destroy the mesh, so it is clamped instead.
MIN_TARGET_FACES = 4

#: Default file extension for the exported artifact.
_MESH_FORMAT = "stl"

#: Scale factor closer to 1 than this counts as "no scaling".
_UNIT_SCALE = 1e-12


@dataclass(frozen=True)
class MeshTransform:
    """Placement of the source mesh on the build plate."""

    scale_mm: float | None = None
    rest_on_plate: bool = True
    center_xy: bool = True
    rotate_deg: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class MeshRepair:
    """Mesh-repair budget; every step is best effort."""

    enabled: bool = DEFAULT_REPAIR_ENABLED
    target_faces: int = DEFAULT_TARGET_FACES
    fill_holes: bool = True
    fix_normals: bool = True


def _setting(name: str, default: Any) -> Any:
    """Read a Django setting with a default, so a partial config still works."""
    return getattr(settings, name, default)


def _as_float(value: Any, default: float) -> float:
    """Coerce ``value`` to float, falling back to ``default``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    """Coerce ``value`` to bool without treating ``"false"`` as truthy."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    if value is None:
        return default
    return bool(value)


def _parse_transform(raw: Any) -> MeshTransform:
    """Read the ``transform`` block, tolerating a missing or partial dict.

    A missing ``scale_mm`` falls back to ``settings.MESH_DEFAULT_SCALE_MM``; an
    explicit non-positive value means "keep the source units" (useful when the
    generator already emitted a millimetre-scale mesh).
    """
    block = raw if isinstance(raw, dict) else {}
    raw_scale = block.get("scale_mm")
    if raw_scale is None:
        scale: float | None = _as_float(
            _setting("MESH_DEFAULT_SCALE_MM", DEFAULT_SCALE_MM), DEFAULT_SCALE_MM
        )
    else:
        scale = _as_float(raw_scale, DEFAULT_SCALE_MM)
        if scale <= 0.0:
            logger.warning(
                "mesh transform scale_mm %r is not positive; keeping source units", raw_scale
            )
            scale = None
    rotation = block.get("rotate_deg") or (0.0, 0.0, 0.0)
    if isinstance(rotation, (int, float)):
        rotation = (rotation, 0.0, 0.0)
    angles = [_as_float(angle, 0.0) for angle in tuple(rotation)[:3]]
    if len(angles) != 3:
        angles = [0.0, 0.0, 0.0]
    return MeshTransform(
        scale_mm=scale,
        rest_on_plate=_as_bool(block.get("rest_on_plate"), True),
        center_xy=_as_bool(block.get("center_xy"), True),
        rotate_deg=(angles[0], angles[1], angles[2]),
    )


def _parse_repair(raw: Any) -> MeshRepair:
    """Read the ``repair`` block, defaulting every key from the settings."""
    block = raw if isinstance(raw, dict) else {}
    default_faces = int(
        _as_float(_setting("MESH_TARGET_FACES", DEFAULT_TARGET_FACES), DEFAULT_TARGET_FACES)
    )
    raw_faces = block.get("target_faces")
    target = int(_as_float(raw_faces, default_faces)) if raw_faces is not None else default_faces
    default_enabled = _as_bool(_setting("MESH_REPAIR_ENABLED", DEFAULT_REPAIR_ENABLED), True)
    return MeshRepair(
        enabled=_as_bool(block.get("enabled"), default_enabled),
        target_faces=max(MIN_TARGET_FACES, target),
        fill_holes=_as_bool(block.get("fill_holes"), True),
        fix_normals=_as_bool(block.get("fix_normals"), True),
    )


def _mesh_source(specification: dict[str, Any]) -> tuple[str, str]:
    """Return ``(relative_path, format)`` for the mesh referenced by the spec.

    Both ``specification["mesh"]["source"]`` and the flat
    ``specification["source_mesh"]`` are accepted so a caller can hand the
    ``ModelVersion.source_mesh`` name straight through.
    """
    mesh = specification.get("mesh")
    block = mesh if isinstance(mesh, dict) else {}
    raw = block.get("source") or specification.get("source_mesh") or ""
    path = str(raw).strip()
    if not path:
        raise SpecificationError("mesh specification is missing 'mesh.source'")
    fmt = str(block.get("format") or "").strip().lower().lstrip(".")
    if not fmt:
        fmt = PurePosixPath(path).suffix.lower().lstrip(".")
    if fmt not in SUPPORTED_MESH_FORMATS:
        raise SpecificationError(
            f"unsupported mesh source {path!r}; expected one of: {', '.join(SUPPORTED_MESH_FORMATS)}"
        )
    return path, fmt


class MeshCADBackend(CADBackend):
    """Adopt an externally generated mesh and export it as a print-ready STL.

    The backend is intentionally side-effect free until :meth:`generate` runs:
    settings are read per call (never cached at import time) so a configuration
    change applies to the next job, and the storage backend is resolved lazily so
    importing this module never touches the filesystem.
    """

    name = "mesh"
    supported_formats = (_MESH_FORMAT,)
    produced_artifacts = (_MESH_FORMAT,)
    produces_source_code = False

    def __init__(self, storage: Any | None = None) -> None:
        self._storage = storage

    @property
    def storage(self) -> Any:
        """Storage used to read the source mesh (injectable for tests)."""
        if self._storage is None:
            from files.services import get_storage

            self._storage = get_storage()
        return self._storage

    # -- CADBackend ---------------------------------------------------------

    def generate(self, specification: dict[str, Any]) -> str:
        """Return the manifest describing the mesh that was adopted.

        The manifest is informational only: ``produces_source_code`` is ``False``
        so the pipeline never writes it as a ``.scad``. The bytes themselves
        travel on :attr:`GeneratedModel.mesh_bytes` via :meth:`build_model`.
        """
        return self.build_model(specification).scad_source

    def build_model(self, specification: dict[str, Any]) -> GeneratedModel:
        """Read, transform and repair the source mesh, then export a binary STL."""
        trimesh, np = self._dependencies()
        path, source_format = _mesh_source(specification)
        transform = _parse_transform(specification.get("transform"))
        repair = _parse_repair(specification.get("repair"))

        mesh = load_mesh(self._read_source(path), source_format)
        before_faces = len(mesh.faces)

        factor = self._apply_transform(trimesh, np, mesh, transform)
        actions = self._apply_repair(trimesh, mesh, repair)

        payload = self._export_stl(trimesh, mesh)
        return GeneratedModel(
            specification=specification,
            scad_source=self._manifest(
                path=path,
                source_format=source_format,
                source_bytes=len(payload),
                before_faces=before_faces,
                after_faces=len(mesh.faces),
                factor=factor,
                transform=transform,
                actions=actions,
                mesh=mesh,
            ),
            mesh_bytes=payload,
            mesh_format=_MESH_FORMAT,
        )

    def validate(self, model: GeneratedModel) -> list[str]:
        """Return the blocking printability problems of ``model`` (empty == valid).

        Failures that make a report impossible (missing mesh, oversized or
        unparseable bytes) are surfaced as problems too, so the pipeline raises
        :class:`~designs.cad.pipeline.CADValidationFailed` with a readable reason
        instead of an opaque traceback.
        """
        if not getattr(model, "mesh_bytes", None):
            return ["mesh backend produced no mesh bytes"]
        try:
            report = check_mesh(model.mesh_bytes, model.mesh_format or _MESH_FORMAT)
        except MeshCheckError as exc:
            return [str(exc)]
        return list(report.problems)

    def export(self, model: GeneratedModel, format: str) -> bytes:
        """Export ``model`` to ``format`` (only ``stl``) and return raw bytes.

        :meth:`build_model` already performed the binary STL export through
        trimesh, so this hands the validated bytes to storage without a second
        parse/export round-trip.
        """
        normalized = (format or "").strip().lower().lstrip(".")
        if normalized not in self.supported_formats:
            raise UnsupportedFormatError(
                f"Mesh backend supports {self.supported_formats}, got {format!r}"
            )
        payload = getattr(model, "mesh_bytes", None)
        if not payload:
            raise CADError("mesh backend has no mesh to export; call generate() first")
        return bytes(payload)

    # -- steps --------------------------------------------------------------

    def _dependencies(self) -> tuple[Any, Any]:
        """Import ``trimesh`` / ``numpy`` lazily, as :class:`CADError`."""
        try:
            import numpy as np
            import trimesh
        except ImportError as exc:
            raise CADError("mesh backend requires the 'trimesh' and 'numpy' packages") from exc
        return trimesh, np

    def _read_source(self, path: str) -> bytes:
        """Read the source mesh bytes from storage."""
        try:
            payload = self.storage.read_bytes(path)
        except FileNotFoundError as exc:
            raise SpecificationError(f"mesh source {path!r} is missing from storage") from exc
        except Exception as exc:  # noqa: BLE001 - an unreadable source is a spec problem
            raise SpecificationError(f"could not read mesh source {path!r}: {exc}") from exc
        if not payload:
            raise SpecificationError(f"mesh source {path!r} is empty")
        return bytes(payload)

    def _apply_transform(self, trimesh: Any, np: Any, mesh: Any, transform: MeshTransform) -> float:
        """Rotate about the bounding-box centre, scale, then place on the plate.

        Returns the applied uniform scale factor (``1.0`` when scaling is off).
        """
        from trimesh.transformations import euler_matrix, scale_matrix

        center = np.asarray(mesh.bounds, dtype=np.float64).mean(axis=0)
        mesh.apply_translation(-center)

        rx, ry, rz = transform.rotate_deg
        if any(abs(angle) > 0.0 for angle in (rx, ry, rz)):
            # ``sxyz`` builds ``Rx(rx) @ Ry(ry) @ Rz(rz)``, which is exactly what
            # the OpenSCAD backend emits as ``rotate([rx, ry, rz])`` -- so a mesh
            # and a parametric part with the same angles land the same way.
            # ``euler_matrix`` wants radians, not the spec's degrees.
            mesh.apply_transform(
                euler_matrix(math.radians(rx), math.radians(ry), math.radians(rz), axes="sxyz")
            )

        factor = self._scale_factor(np, mesh, transform.scale_mm)
        if abs(factor - 1.0) > _UNIT_SCALE:
            mesh.apply_transform(scale_matrix(factor))

        if transform.center_xy:
            bounds = np.asarray(mesh.bounds, dtype=np.float64)
            offset = (bounds[0] + bounds[1]) / 2.0
            mesh.apply_translation([-float(offset[0]), -float(offset[1]), 0.0])
        if transform.rest_on_plate:
            mesh.apply_translation([0.0, 0.0, -float(np.asarray(mesh.bounds)[0][2])])
        return factor

    def _scale_factor(self, np: Any, mesh: Any, scale_mm: float | None) -> float:
        """Uniform factor that makes the largest bounding-box edge ``scale_mm``."""
        if scale_mm is None:
            return 1.0
        largest = float(np.asarray(mesh.extents, dtype=np.float64).max())
        if largest <= 0.0:
            logger.warning("mesh has a zero-size bounding box; skipping scaling")
            return 1.0
        return scale_mm / largest

    def _apply_repair(self, trimesh: Any, mesh: Any, repair: MeshRepair) -> list[str]:
        """Run normals -> holes -> decimation, returning the actions taken.

        Every step is individually guarded: a trimesh helper that needs an
        uninstalled extra (``networkx``/``scipy``/``fast_simplification``) is
        recorded as a warning rather than failing the whole render.
        """
        if not repair.enabled:
            return ["repair disabled"]

        actions: list[str] = []

        if repair.fix_normals:
            try:
                trimesh.repair.fix_normals(mesh)
            except Exception as exc:  # noqa: BLE001 - needs networkx/scipy
                logger.info("mesh fix_normals unavailable: %s", exc)
                actions.append(f"fix_normals skipped ({type(exc).__name__})")
            else:
                actions.append("fix_normals")

        if repair.fill_holes:
            try:
                filled = bool(trimesh.repair.fill_holes(mesh))
            except Exception as exc:  # noqa: BLE001 - needs networkx
                logger.info("mesh fill_holes unavailable: %s", exc)
                actions.append(f"fill_holes skipped ({type(exc).__name__})")
            else:
                actions.append("fill_holes" if filled else "fill_holes (nothing to fill)")

        faces = len(mesh.faces)
        if faces > repair.target_faces:
            actions.append(self._decimate(mesh, repair.target_faces))
        return actions

    def _decimate(self, mesh: Any, target_faces: int) -> str:
        """Reduce the face count to ``target_faces``, or explain why not."""
        try:
            simplified = mesh.simplify_quadric_decimation(face_count=int(target_faces))
        except Exception as exc:  # noqa: BLE001 - needs fast_simplification or open3d
            logger.info("mesh decimation unavailable: %s", exc)
            return (
                f"decimation skipped, {len(mesh.faces)} faces above the "
                f"{target_faces} budget ({type(exc).__name__})"
            )
        if simplified is None or not len(simplified.faces):
            return f"decimation skipped, target {target_faces} faces is not reachable"
        before = len(mesh.faces)
        mesh.vertices = simplified.vertices
        mesh.faces = simplified.faces
        return f"decimated {before} -> {len(mesh.faces)} faces"

    def _export_stl(self, trimesh: Any, mesh: Any) -> bytes:
        """Export ``mesh`` as a binary STL through trimesh."""
        try:
            payload = mesh.export(file_type="stl")
        except Exception as exc:  # noqa: BLE001 - an export failure is a CAD failure
            raise CADError(f"could not export the repaired mesh to STL: {exc}") from exc
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not payload:
            raise CADError("trimesh produced an empty STL")
        return bytes(payload)

    def _manifest(
        self,
        *,
        path: str,
        source_format: str,
        source_bytes: int,
        before_faces: int,
        after_faces: int,
        factor: float,
        transform: MeshTransform,
        actions: list[str],
        mesh: Any,
    ) -> str:
        """Build the short, deterministic manifest string for ``generate()``."""
        extents = mesh.extents
        rx, ry, rz = transform.rotate_deg
        return "\n".join(
            [
                "// PrintForge mesh backend manifest (not written as model.scad)",
                f"source: {path} ({source_format}, {source_bytes} bytes)",
                f"faces: {before_faces} -> {after_faces}",
                f"scale: {factor:.6f} (largest edge {float(extents[0]):.3f}x"
                f"{float(extents[1]):.3f}x{float(extents[2]):.3f}mm)",
                f"rotation: {rx:g} {ry:g} {rz:g} deg (XYZ about the bounding-box centre)",
                f"placement: rest_on_plate={transform.rest_on_plate} center_xy={transform.center_xy}",
                f"repair: {'; '.join(actions) if actions else 'none'}",
                f"watertight: {bool(mesh.is_watertight)}",
            ]
        )
