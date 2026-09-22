"""Headless STL -> PNG preview renderer for the vision review node.

:func:`render_stl_preview` parses an STL with *trimesh* and rasterises a few
orthographic views with *Pillow*, using a painter's-algorithm triangle sort and
per-face flat shading. Everything runs in-process: no shell, no network, no
OpenGL/GPU and no randomness, so the output is deterministic and safe to run
from a worker (never from the Django request cycle).

The heavy libraries (``trimesh`` / ``pillow`` / ``numpy``) are imported lazily
*inside* the function so importing :mod:`designs.cad` stays cheap and a missing
optional dependency surfaces as :class:`PreviewRenderError`, never an
``ImportError``.

See ``docs/vision-self-check.md`` (section 2).
"""

from __future__ import annotations

import io
import logging
from typing import Any

__all__ = ["SUPPORTED_VIEWS", "PreviewRenderError", "render_stl_preview"]

logger = logging.getLogger(__name__)

#: View names accepted by :func:`render_stl_preview`.
SUPPORTED_VIEWS: tuple[str, ...] = ("iso", "front", "top", "side")

#: Neutral background and the blue material tone of the model.
_BACKGROUND = (240, 240, 240)
_BASE_COLOR = (64, 110, 190)

#: Flat-shading model: ambient + diffuse * max(0, normal . light).
_AMBIENT = 0.30
_DIFFUSE = 0.70

# Per view: ``forward`` is the viewing direction (from the camera into the
# scene) and ``up_hint`` the preferred screen-up axis. The screen basis is
# ``right = normalize(cross(forward, up_hint))`` with
# ``up = cross(right, forward)``; the resulting 3-tuple stays right-handed.
_VIEW_BASIS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "front": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "top": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
    "side": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "iso": ((-1.0, 1.0, -1.0), (0.0, 0.0, 1.0)),
}

_MIN_SIZE = 16
_EPSILON = 1e-9


class PreviewRenderError(ValueError):
    """The STL is missing, malformed, degenerate or not renderable."""


def _array(np: Any, values: Any) -> Any:
    return np.asarray(values, dtype=np.float64)


def _unit(np: Any, vector: Any) -> Any:
    norm = float(np.linalg.norm(vector))
    if norm <= _EPSILON:
        return np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
    return vector / norm


def render_stl_preview(
    stl_bytes: bytes,
    *,
    size: int = 512,
    views: tuple[str, ...] = ("iso", "front", "top"),
) -> bytes:
    """Render ``stl_bytes`` into a deterministic multi-view PNG.

    Each requested view is drawn into its own ``size`` x ``size`` panel and the
    panels are placed side by side in the returned PNG. The mesh is centred and
    scaled so all panels share one consistent framing. Empty, unparseable or
    degenerate meshes raise :class:`PreviewRenderError`.
    """
    if not isinstance(stl_bytes, (bytes, bytearray)) or not stl_bytes:
        raise PreviewRenderError("no STL data provided")
    payload = bytes(stl_bytes)

    if not isinstance(size, int) or isinstance(size, bool) or size < _MIN_SIZE:
        raise PreviewRenderError(f"size must be an integer >= {_MIN_SIZE}")

    if isinstance(views, str):
        raise PreviewRenderError("views must be a sequence of view names")
    requested = tuple(views)
    if not requested:
        raise PreviewRenderError("at least one view is required")
    for view in requested:
        if view not in _VIEW_BASIS:
            raise PreviewRenderError(
                f"unknown view {view!r}; supported views: {', '.join(SUPPORTED_VIEWS)}"
            )

    try:
        import numpy as np
        import trimesh
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise PreviewRenderError(
            "STL preview rendering requires the 'trimesh', 'pillow' and 'numpy' packages"
        ) from exc

    try:
        loaded = trimesh.load(io.BytesIO(payload), file_type="stl")
    except Exception as exc:
        raise PreviewRenderError(f"could not parse STL: {exc}") from exc

    if isinstance(loaded, trimesh.Scene):
        if not loaded.geometry:
            raise PreviewRenderError("STL contains no geometry")
        loaded = loaded.dump(concatenate=True)

    vertices = np.asarray(getattr(loaded, "vertices", ()), dtype=np.float64)
    faces = np.asarray(getattr(loaded, "faces", ()), dtype=np.int64)
    if vertices.ndim != 2 or vertices.shape[0] == 0:
        raise PreviewRenderError("STL contains no triangles")
    if faces.ndim != 2 or faces.shape[0] == 0:
        raise PreviewRenderError("STL contains no triangles")
    if not np.isfinite(vertices).all():
        raise PreviewRenderError("STL contains non-finite coordinates")

    # Centre on the bounding-box centre so every view shares one origin.
    bounds = np.vstack((vertices.min(axis=0), vertices.max(axis=0)))
    centred = vertices - (bounds[0] + bounds[1]) / 2.0
    if float(np.abs(centred).max()) <= _EPSILON:
        raise PreviewRenderError("STL geometry is degenerate (zero size)")

    # Project the vertices once per view; a single shared scale keeps the
    # panels visually consistent instead of re-fitting each one independently.
    bases: dict[str, tuple[Any, Any, Any, Any]] = {}
    max_projection = 0.0
    for view in requested:
        forward, up_hint = _VIEW_BASIS[view]
        fwd = _unit(np, _array(np, forward))
        right = _unit(np, np.cross(fwd, _array(np, up_hint)))
        up = np.cross(right, fwd)
        screen_x = centred @ right
        screen_y = centred @ up
        depth = centred @ (-fwd)
        bases[view] = (right, up, depth, fwd)
        max_projection = max(
            max_projection,
            float(np.abs(screen_x).max()),
            float(np.abs(screen_y).max()),
        )

    if max_projection <= _EPSILON:
        raise PreviewRenderError("STL geometry projects to zero size")

    margin = max(float(size) * 0.08, 2.0)
    scale = (size / 2.0 - margin) / max_projection

    triangles = centred[faces]
    normals = np.cross(
        triangles[:, 1] - triangles[:, 0],
        triangles[:, 2] - triangles[:, 0],
    )
    lengths = np.linalg.norm(normals, axis=1)
    drawable = lengths > _EPSILON
    normals[drawable] /= lengths[drawable, None]

    canvas = Image.new("RGB", (size * len(requested), size), _BACKGROUND)
    painter = ImageDraw.Draw(canvas)
    base_color = np.asarray(_BASE_COLOR, dtype=np.float64)

    for index, view in enumerate(requested):
        right, up, depth, fwd = bases[view]
        toward_camera = -fwd

        # Orient every face normal towards the camera, then shade it against a
        # fixed light in the camera frame (front-upper-right of the viewer).
        oriented = normals.copy()
        facing_away = (oriented @ toward_camera) < 0.0
        oriented[facing_away] *= -1.0
        light = _unit(np, toward_camera + up + right)
        lambert = np.clip(oriented @ light, 0.0, 1.0)
        intensity = np.clip(_AMBIENT + _DIFFUSE * lambert, 0.0, 1.0)
        shades = np.clip(base_color[None, :] * intensity[:, None], 0.0, 255.0).astype(np.uint8)

        # Painter's algorithm: farthest triangle first, nearest last.
        face_depth = depth[faces].mean(axis=1)
        order = np.argsort(face_depth, kind="stable")

        screen_x = centred @ right
        screen_y = centred @ up
        offset = index * size
        for face_index in order:
            if not drawable[face_index]:
                continue
            face = faces[face_index]
            points = [
                (
                    offset + size / 2.0 + float(screen_x[vertex]) * scale,
                    size / 2.0 - float(screen_y[vertex]) * scale,
                )
                for vertex in face
            ]
            color = tuple(int(channel) for channel in shades[face_index])
            painter.polygon(points, fill=color, outline=color)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()
