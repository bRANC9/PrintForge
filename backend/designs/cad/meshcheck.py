"""Deterministic, dependency-light mesh printability report.

:func:`check_mesh` answers one question -- "can this mesh go straight to a
slicer?" -- without running any geometry kernel. It parses the bytes with
*trimesh* and reports watertightness, winding, volume, body count, bounds and a
few cheap quality signals. Everything runs in-process: no shell, no network, no
GPU and no randomness, so the report is deterministic and safe to call from a
worker (never from the Django request cycle).

Units
-----
PrintForge is **millimetres everywhere** (``agents/spec.py`` and
``slicers/threemf.py`` write ``unit="millimeter"``), but a mesh produced by an
image-to-3D model arrives in an *arbitrary* scale -- a diffusion model has no
millimetre contract at all. This module therefore does **not** rescale: it
reports the *raw* extents in whatever unit the source used and leaves the
decision to the caller (:class:`designs.cad.mesh.MeshCADBackend` applies
``transform.scale_mm``). The envelope warnings below are only a hint in
millimetres and must be re-evaluated after scaling.

The heavy libraries (``trimesh`` / ``numpy``) are imported lazily *inside* the
functions so importing :mod:`designs.cad` stays cheap and a missing dependency
surfaces as :class:`MeshCheckError`, never an ``ImportError``.

``scipy`` / ``networkx`` are *not* required: :func:`_connected_bodies` is a
pure-numpy union-find and the optional ``trimesh`` helpers that do need them
(:func:`trimesh.repair.fill_holes`, ``trimesh.repair.broken_faces`) are tried
and skipped, so a missing graph engine degrades the report instead of breaking
it.

The upload size bound
---------------------
The one hard limit this module enforces is the maximum accepted source size, and
it has **exactly one source of truth**: the runtime-editable
``mesh_max_source_bytes`` setting, read through
:func:`configuration.services.get_setting` (``DB override ->
django.conf.settings -> os.environ -> default``). See
:func:`_max_source_bytes`.

Do **not** read ``django.conf.settings.MESH_MAX_SOURCE_BYTES`` here, and do not
add a second reader. :func:`designs.services._max_source_bytes` gates the upload
*before* calling in, so a private reader here meant two gates resolving the same
policy through two different chains: the effective limit silently became
``min(DB override, Django setting)``. Tightening still worked, but *raising* the
limit in the Settings UI did not -- the 64 MiB default kept capping every upload
while the error message quoted a limit the operator had never set. If the import
service changes how the bound is resolved, change it here *and* tell it, or
move the resolution to a shared helper both sides import.

Callers that already know the limit may pass ``max_bytes=`` to make the
dependency explicit; ``None`` (the default) means "resolve it the one way".
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_MAX_SOURCE_BYTES",
    "MAX_SOURCE_BYTES_SETTING",
    "SUPPORTED_MESH_FORMATS",
    "MeshCheckError",
    "MeshReport",
    "check_mesh",
    "load_mesh",
]

logger = logging.getLogger(__name__)

#: Mesh container formats the checker can parse. Mirrors the file extensions
#: accepted by the reference-upload pipeline.
SUPPORTED_MESH_FORMATS: tuple[str, ...] = ("stl", "obj", "glb")

#: Name of the runtime setting that bounds an uploaded source mesh. See the
#: "upload size bound" section of the module docstring.
MAX_SOURCE_BYTES_SETTING = "mesh_max_source_bytes"

#: Last-resort fallback for :data:`MAX_SOURCE_BYTES_SETTING`. It matches the
#: default registered in ``configuration.services._SETTING_SPECS``, so the bound
#: is the same with or without the configuration app available.
DEFAULT_MAX_SOURCE_BYTES = 64 * 1024 * 1024

#: Print envelope used for the *non-blocking* dimension warnings, millimetres.
#: Anything smaller cannot be printed and anything larger is almost certainly a
#: unit mix-up in the source mesh.
MIN_PRINT_MM = 0.1
MAX_PRINT_MM = 1000.0

#: Shortest edge below which an FDM nozzle cannot lay a wall, millimetres.
MIN_FEATURE_MM = 0.4

#: Volume below which a closed mesh is considered to have no material at all.
_VOLUME_EPSILON = 1e-9
#: Generic float tolerance for degenerate geometry.
_EPSILON = 1e-12


class MeshCheckError(ValueError):
    """The source bytes are missing, oversized, unparseable or empty."""


@dataclass(frozen=True)
class MeshReport:
    """Printability verdict for one mesh.

    ``problems`` are blocking (the mesh cannot be sliced as-is), ``warnings``
    are advisory only. ``stats`` always holds at least ``format``, ``faces``,
    ``vertices``, ``watertight``, ``winding_consistent``, ``euler_number``,
    ``body_count``, ``volume_mm3``, ``extents_mm``, ``bounds_mm`` and
    ``degenerate_faces``; a value that could not be measured is ``None``.
    """

    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def _coerce_limit(value: Any) -> int | None:
    """A positive byte count from ``value``, or ``None`` when it is unusable.

    Defensive on purpose, because the bound crosses three boundaries that do not
    all guarantee an ``int``: ``configuration`` registers the setting with a
    plain ``env(...)`` that does not cast (so a value set in the environment
    arrives as a *string*), a caller-supplied ``max_bytes`` is arbitrary, and
    ``None`` means "resolve it yourself".

    ``None``/empty/non-numeric/non-positive all collapse to "unusable" so the
    caller falls back to :data:`DEFAULT_MAX_SOURCE_BYTES`. A non-positive limit
    must never be returned: ``0`` would reject *every* upload and a negative one
    would accept *everything*, which is the opposite of a size bound.
    """
    if value is None or value == "":
        return None
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return None
    return limit if limit > 0 else None


def _django_setting(name: str) -> Any:
    """``django.conf.settings.<name>``, or ``None`` when Django is not usable.

    Only reached as a *fallback* (the configuration app being unavailable, or its
    own lookup failing), so it must never be the thing that raises.
    """
    try:
        from django.conf import settings as django_settings

        return getattr(django_settings, name, None)
    except Exception:  # noqa: BLE001 - settings unconfigured or not importable
        logger.debug("django.conf.settings.%s is unavailable", name, exc_info=True)
        return None


def _max_source_bytes() -> int:
    """Largest accepted source mesh in bytes -- the single source of truth.

    Resolved through :func:`configuration.services.get_setting`, i.e. the
    runtime-editable :data:`MAX_SOURCE_BYTES_SETTING` setting, whose chain is
    ``DB override -> django.conf.settings -> os.environ -> default``. Reading it
    on every call is deliberate: nothing is cached at import time, so an
    operator editing the limit in the Settings UI sees it take effect on the
    next upload or render without a redeploy.

    ``configuration.services`` is imported **lazily** (the pattern
    :func:`designs.cad.openscad._read_runtime_setting` uses) because it imports
    Django models while this module is imported by ``designs.services``; a
    module-level import would let app-loading order decide whether the import
    graph resolves. The lazy import is also what makes the ``except`` below
    meaningful: the configuration app may simply not be installed.

    Never raises. An absent configuration app, a failed lookup, or a value that
    is not a positive integer all degrade to ``django.conf.settings`` and then
    to :data:`DEFAULT_MAX_SOURCE_BYTES`, so a bad value can neither reject every
    upload nor disable the bound.
    """
    value: Any = None
    try:
        from configuration.services import get_setting
    except Exception:  # noqa: BLE001 - the configuration app may not be present
        logger.warning(
            "configuration service unavailable; using django settings for the source mesh limit"
        )
    else:
        try:
            value = get_setting(MAX_SOURCE_BYTES_SETTING)
        except Exception:  # noqa: BLE001 - a settings lookup must not break a render
            logger.warning(
                "get_setting(%r) failed; using django settings for the source mesh limit",
                MAX_SOURCE_BYTES_SETTING,
            )

    # ``get_setting`` normally hands back an int, but ``_coerce_limit`` is what
    # guarantees it (env strings, and the settings fallback below).
    limit = _coerce_limit(value)
    if limit is not None:
        return limit
    return _coerce_limit(_django_setting("MESH_MAX_SOURCE_BYTES")) or DEFAULT_MAX_SOURCE_BYTES


def _resolve_limit(max_bytes: int | None) -> int:
    """The size bound to enforce: the caller's ``max_bytes`` or the resolved one.

    ``max_bytes=None`` (the default everywhere) means "use the one runtime-aware
    reader". An explicitly passed value is authoritative -- that is the point of
    the parameter -- but a nonsensical one still degrades to the resolved limit
    rather than disabling the bound.
    """
    if max_bytes is None:
        return _max_source_bytes()
    return _coerce_limit(max_bytes) or _max_source_bytes()


def _dependencies() -> tuple[Any, Any]:
    """Import ``trimesh`` / ``numpy`` lazily, as :class:`MeshCheckError`."""
    try:
        import numpy as np
        import trimesh
    except ImportError as exc:
        raise MeshCheckError("mesh inspection requires the 'trimesh' and 'numpy' packages") from exc
    return trimesh, np


def _as_trimesh(trimesh: Any, loaded: Any) -> Any:
    """Flatten a ``trimesh.Scene`` (GLB/OBJ) into a single ``Trimesh``."""
    if not isinstance(loaded, trimesh.Scene):
        return loaded
    if not loaded.geometry:
        raise MeshCheckError("mesh contains no geometry")
    # ``Scene.to_geometry`` supersedes the deprecated ``dump(concatenate=True)``
    # but only exists in newer trimesh releases; support both.
    to_geometry = getattr(loaded, "to_geometry", None)
    geometry = to_geometry() if callable(to_geometry) else loaded.dump(concatenate=True)
    if not isinstance(geometry, trimesh.Trimesh) or not len(geometry.faces):
        raise MeshCheckError("mesh scene contains no triangles")
    return geometry


def load_mesh(
    payload: bytes,
    mesh_format: str = "stl",
    *,
    max_bytes: int | None = None,
) -> Any:
    """Parse ``payload`` into a :class:`trimesh.Trimesh`.

    The mesh is loaded with ``process=False`` so broken input (NaN/inf
    coordinates) survives parsing and can be *reported* instead of being
    silently dropped by the loader. The vertices are therefore **not** welded --
    an STL keeps 3 vertices per triangle -- so callers that measure topology
    must call :meth:`trimesh.Trimesh.merge_vertices` first (as :func:`check_mesh`
    does). ``trimesh`` and ``numpy`` are imported lazily; every failure -- missing
    bytes, unknown format, unparseable payload, no geometry -- raises
    :class:`MeshCheckError`.

    ``max_bytes`` is the size bound: ``None`` (the default) resolves it through
    :func:`_max_source_bytes`, the single runtime-aware reader, so a caller that
    has already resolved the limit can pass it in and make the dependency
    explicit. This is the *only* place the bound is enforced -- :func:`check_mesh`
    forwards its own ``max_bytes`` here rather than checking twice.
    """
    trimesh, _ = _dependencies()

    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise MeshCheckError("no mesh data provided")
    data = bytes(payload)

    limit = _resolve_limit(max_bytes)
    if len(data) > limit:
        raise MeshCheckError(f"mesh is {len(data)} bytes, above the {limit} byte limit")

    normalized = (mesh_format or "").strip().lower().lstrip(".")
    if normalized not in SUPPORTED_MESH_FORMATS:
        raise MeshCheckError(
            f"unsupported mesh format {mesh_format!r}; "
            f"expected one of: {', '.join(SUPPORTED_MESH_FORMATS)}"
        )

    try:
        loaded = trimesh.load(io.BytesIO(data), file_type=normalized, process=False)
    except MeshCheckError:
        raise
    except Exception as exc:
        raise MeshCheckError(f"could not parse {normalized} mesh: {exc}") from exc

    mesh = _as_trimesh(trimesh, loaded)
    if not isinstance(mesh, trimesh.Trimesh):
        raise MeshCheckError(f"could not parse {normalized} mesh: no triangle mesh in payload")
    return mesh


def _connected_bodies(trimesh: Any, np: Any, mesh: Any) -> int:
    """Count disconnected bodies with a pure-numpy union-find.

    ``trimesh.Trimesh.body_count`` needs ``scipy`` (or ``networkx``) which are
    not project dependencies, so walk the face-adjacency graph here: an edge
    shared by exactly two faces makes those two faces part of the same body.
    """
    faces = np.asarray(getattr(mesh, "faces", ()), dtype=np.int64)
    total = len(faces)
    if total == 0:
        return 0
    try:
        edges = trimesh.repair.faces_to_edges(faces)
    except Exception:  # noqa: BLE001 - fall back to "one body" rather than fail
        logger.debug("faces_to_edges unavailable; assuming a single body", exc_info=True)
        return 1
    keys = np.sort(np.asarray(edges, dtype=np.int64), axis=1)
    _, inverse, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    order = np.argsort(np.asarray(inverse).reshape(-1), kind="stable")
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    owner = np.repeat(np.arange(total, dtype=np.int64), 3)[order]
    shared = np.flatnonzero(np.asarray(counts) == 2)

    parent = np.arange(total, dtype=np.int64)

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = int(parent[node])
        return node

    for left, right in zip(owner[starts[shared]], owner[starts[shared] + 1], strict=True):
        first, second = find(int(left)), find(int(right))
        if first != second:
            parent[second] = first
    return len({find(index) for index in range(total)})


def _extent_warnings(extents: tuple[float, ...] | None) -> list[str]:
    """Non-blocking dimension hints (see the unit note in the module docstring)."""
    if not extents:
        return []
    warnings: list[str] = []
    for label, value in zip("XYZ", extents, strict=True):
        if value < MIN_PRINT_MM:
            warnings.append(
                f"{label} extent {value:.3f}mm is below the {MIN_PRINT_MM}mm print minimum "
                f"(the source mesh is probably not in millimetres)"
            )
        elif value > MAX_PRINT_MM:
            warnings.append(
                f"{label} extent {value:.3f}mm exceeds the {MAX_PRINT_MM}mm print envelope "
                f"(the source mesh is probably not in millimetres)"
            )
    return warnings


def check_mesh(
    mesh_bytes: bytes,
    mesh_format: str = "stl",
    *,
    max_bytes: int | None = None,
) -> MeshReport:
    """Return the printability report for ``mesh_bytes``.

    The caller keeps ownership of the scale: the report describes the mesh as it
    was handed in (see the module docstring). ``warnings`` are advisory,
    ``problems`` block slicing. Failures that make a report impossible (missing
    bytes, above the size bound, unknown format, unparseable payload or no
    geometry at all) raise :class:`MeshCheckError`; a mesh that *does* parse but
    holds no triangles is reported as a blocking problem instead.

    ``max_bytes`` is forwarded to :func:`load_mesh`, which is the single place
    the bound is enforced. Leave it ``None`` unless you resolved the limit
    yourself -- see the "upload size bound" section of the module docstring.

    Blocking problems: no triangles, non-finite coordinates, zero volume, not
    watertight, inconsistent winding. Warnings: more than one disconnected body,
    dimensions outside the print envelope, features below the minimum printable
    size.
    """
    trimesh, np = _dependencies()
    mesh = load_mesh(mesh_bytes, mesh_format, max_bytes=max_bytes)
    normalized = (mesh_format or "").strip().lower().lstrip(".")

    vertices = np.asarray(getattr(mesh, "vertices", ()), dtype=np.float64)
    faces = np.asarray(getattr(mesh, "faces", ()), dtype=np.int64)
    finite = bool(vertices.size) and bool(np.isfinite(vertices).all())

    stats: dict[str, Any] = {
        "format": normalized,
        "file_bytes": len(bytes(mesh_bytes or b"")),
        "faces": int(len(faces)),
        "vertices": int(len(vertices)),
        "watertight": None,
        "winding_consistent": None,
        "euler_number": None,
        "body_count": None,
        "volume_mm3": None,
        "extents_mm": None,
        "bounds_mm": None,
        "degenerate_faces": 0,
        "min_edge_mm": None,
        "min_face_area_mm2": None,
    }
    problems: list[str] = []
    warnings: list[str] = []

    if not finite:
        problems.append("mesh contains non-finite coordinates (NaN or infinity)")
    elif not len(faces):
        problems.append("mesh contains no triangles")

    measurable = finite and bool(len(faces))

    if measurable:
        # Weld the duplicated STL vertices so a healthy solid is not reported as
        # full of holes. ``process=False`` was used on load, so this is needed.
        try:
            mesh.merge_vertices()
        except Exception:  # noqa: BLE001 - welding is best effort, never fatal
            logger.debug("merge_vertices failed; using the raw mesh", exc_info=True)
        vertices = np.asarray(mesh.vertices, dtype=np.float64)
        stats["vertices"] = int(len(vertices))

        # -- cheap per-face measurements (pure numpy) -----------------------
        areas = np.asarray(getattr(mesh, "area_faces", ()), dtype=np.float64)
        if areas.size:
            stats["degenerate_faces"] = int((areas <= _EPSILON).sum())
            stats["min_face_area_mm2"] = float(areas.min())

        bounds = np.vstack((vertices.min(axis=0), vertices.max(axis=0)))
        extents = bounds[1] - bounds[0]
        stats["bounds_mm"] = (
            (float(bounds[0][0]), float(bounds[0][1]), float(bounds[0][2])),
            (float(bounds[1][0]), float(bounds[1][1]), float(bounds[1][2])),
        )
        stats["extents_mm"] = (float(extents[0]), float(extents[1]), float(extents[2]))
        warnings.extend(_extent_warnings(stats["extents_mm"]))
        try:
            unique_edges = np.asarray(mesh.edges_unique, dtype=np.int64)
            lengths = np.linalg.norm(
                vertices[unique_edges[:, 0]] - vertices[unique_edges[:, 1]], axis=1
            )
            stats["min_edge_mm"] = float(lengths.min())
            if float(lengths.min()) < MIN_FEATURE_MM:
                warnings.append(
                    f"shortest edge is {float(lengths.min()):.3f}mm, below the "
                    f"{MIN_FEATURE_MM}mm printable feature size"
                )
        except Exception:  # noqa: BLE001 - a missing measurement is not fatal
            logger.debug("edge length measurement failed", exc_info=True)

        # -- topology --------------------------------------------------------
        for key, attribute in (
            ("watertight", "is_watertight"),
            ("winding_consistent", "is_winding_consistent"),
            ("euler_number", "euler_number"),
        ):
            try:
                value = getattr(mesh, attribute)
            except Exception:  # noqa: BLE001 - an unmeasurable stat stays ``None``
                logger.debug("%s check failed", attribute, exc_info=True)
            else:
                stats[key] = bool(value) if isinstance(value, bool) else int(value)

        try:
            stats["body_count"] = _connected_bodies(trimesh, np, mesh)
        except Exception:  # noqa: BLE001
            logger.debug("body count failed", exc_info=True)

        try:
            volume = float(mesh.volume)
        except Exception:  # noqa: BLE001
            logger.debug("volume computation failed", exc_info=True)
        else:
            if abs(volume) > _VOLUME_EPSILON:
                stats["volume_mm3"] = volume

    # -- verdict -------------------------------------------------------------
    if stats["watertight"] is False:
        problems.append("mesh is not watertight (open edges would print as holes)")
    if stats["winding_consistent"] is False:
        problems.append("mesh has inconsistent triangle winding (normals point in/out)")
    if finite and stats["volume_mm3"] is None:
        problems.append("mesh encloses zero volume (degenerate or single-sided geometry)")

    bodies = stats["body_count"]
    if isinstance(bodies, int) and bodies > 1:
        warnings.append(f"mesh has {bodies} disconnected bodies (expect separate prints)")

    return MeshReport(
        ok=not problems,
        problems=problems,
        warnings=warnings,
        stats=stats,
    )
