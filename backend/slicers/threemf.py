"""Minimal 3MF writer for multi-object build plates (terv.md 28. fejezet).

PrusaSlicer's CLI applies its transform flags (``--rotate`` / ``--scale`` /
``--translate``) to *all* loaded models, so per-item placement cannot be
expressed that way. A 3MF file instead carries an explicit 4x3 build-item
transform per object -- exactly the plate layout we need -- and PrusaSlicer
honours it when ``--dont-arrange`` is passed.

This module builds a standards-compliant (core-spec) 3MF package in memory:

* ``[Content_Types].xml`` / ``_rels/.rels`` -- OPC plumbing;
* ``3D/3dmodel.model``                      -- meshes + build items.

Only STL meshes are understood here: the stored ``PlateItem`` artifacts are STL
(validated in :mod:`slicers.services`). The bytes are written into a temporary
job directory and handed to PrusaSlicer together with the usual ``--load``
profiles; ``slicers.prusaslicer.PrusaSlicerBackend.slice_plate`` is the caller.
"""

from __future__ import annotations

import math
import struct
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO

from .base import PlateMesh, SlicerError

__all__ = [
    "CORE_NAMESPACE",
    "Mesh",
    "build_plate_3mf",
    "item_transform",
    "parse_stl",
]

#: 3MF core specification namespace.
CORE_NAMESPACE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" '
    'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    "</Types>"
)

_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rel0" Target="/3D/3dmodel.model" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    "</Relationships>"
)


@dataclass(frozen=True)
class Mesh:
    """An indexed triangle mesh parsed from an STL artifact."""

    vertices: tuple[tuple[float, float, float], ...]
    triangles: tuple[tuple[int, int, int], ...]


# ---------------------------------------------------------------------------
# STL parsing
# ---------------------------------------------------------------------------


def parse_stl(data: bytes) -> Mesh:
    """Parse a binary or ASCII STL into an indexed :class:`Mesh`.

    Raises :class:`SlicerError` when the bytes are not a usable STL.
    """
    if _looks_like_ascii(data):
        try:
            return _parse_ascii_stl(data)
        except SlicerError:
            pass  # a binary STL whose 80-byte header happens to start with "solid"
    binary = _parse_binary_stl(data)
    if binary is not None:
        return binary
    return _parse_ascii_stl(data)


def _looks_like_ascii(data: bytes) -> bool:
    head = data[:2048]
    return head.lstrip()[:5].lower() == b"solid" and b"facet" in head


def _parse_binary_stl(data: bytes) -> Mesh | None:
    """Return the mesh for a well-formed binary STL, else ``None``."""
    if len(data) < 84:
        return None
    (count,) = struct.unpack_from("<I", data, 80)
    # A mismatching length means this is not the binary layout we expect.
    if 84 + count * 50 != len(data):
        return None

    index: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    offset = 84
    for _ in range(count):
        floats = struct.unpack_from("<12f", data, offset)
        offset += 50  # 12 floats + the 2-byte attribute count
        triangle: list[int] = []
        for corner in range(3):
            vertex = (
                floats[3 + corner * 3],
                floats[4 + corner * 3],
                floats[5 + corner * 3],
            )
            idx = index.get(vertex)
            if idx is None:
                idx = len(vertices)
                index[vertex] = idx
                vertices.append(vertex)
            triangle.append(idx)
        triangles.append((triangle[0], triangle[1], triangle[2]))
    return Mesh(vertices=tuple(vertices), triangles=tuple(triangles))


def _parse_ascii_stl(data: bytes) -> Mesh:
    text = data.decode("utf-8", errors="replace")
    index: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    pending: list[int] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if not parts or parts[0] != "vertex" or len(parts) < 4:
            continue
        try:
            vertex = (float(parts[1]), float(parts[2]), float(parts[3]))
        except ValueError:
            continue
        idx = index.get(vertex)
        if idx is None:
            idx = len(vertices)
            index[vertex] = idx
            vertices.append(vertex)
        pending.append(idx)
        if len(pending) == 3:
            triangles.append((pending[0], pending[1], pending[2]))
            pending = []
    if not triangles:
        raise SlicerError("Could not parse the model as STL (binary or ASCII)")
    return Mesh(vertices=tuple(vertices), triangles=tuple(triangles))


# ---------------------------------------------------------------------------
# 3MF construction
# ---------------------------------------------------------------------------


def _format_number(value: float) -> str:
    if not math.isfinite(value):
        raise SlicerError(f"Invalid build plate transform value: {value!r}")
    if value == 0.0:
        return "0"
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def item_transform(item: PlateMesh) -> str:
    """Render the item's placement as a 3MF 4x3 transform string.

    3MF uses a row-vector convention (``v' = v * M``): the upper-left 3x3 is the
    linear part and the last three values are the translation. A CCW rotation
    around Z (in the usual column convention) therefore transposes into the
    linear part below.
    """
    angle = math.radians(item.rotation_z)
    cos = math.cos(angle)
    sin = math.sin(angle)
    scale = item.scale
    matrix = (
        scale * cos,
        scale * sin,
        0.0,
        -scale * sin,
        scale * cos,
        0.0,
        0.0,
        0.0,
        scale,
        item.x,
        item.y,
        item.z,
    )
    return " ".join(_format_number(value) for value in matrix)


def _append_object(resources: ET.Element, object_id: int, name: str, mesh: Mesh) -> None:
    obj = ET.SubElement(resources, "object")
    obj.set("id", str(object_id))
    obj.set("type", "model")
    if name:
        obj.set("name", name)

    mesh_el = ET.SubElement(obj, "mesh")
    vertices_el = ET.SubElement(mesh_el, "vertices")
    for x, y, z in mesh.vertices:
        vertex_el = ET.SubElement(vertices_el, "vertex")
        vertex_el.set("x", _format_number(x))
        vertex_el.set("y", _format_number(y))
        vertex_el.set("z", _format_number(z))

    triangles_el = ET.SubElement(mesh_el, "triangles")
    for v1, v2, v3 in mesh.triangles:
        triangle_el = ET.SubElement(triangles_el, "triangle")
        triangle_el.set("v1", str(v1))
        triangle_el.set("v2", str(v2))
        triangle_el.set("v3", str(v3))


def build_plate_3mf(items: Sequence[PlateMesh]) -> bytes:
    """Serialise ``items`` into a 3MF package (bytes).

    Items sharing a ``key`` (e.g. the same ``ModelVersion``) reuse one mesh
    object, keeping the package small. Raises :class:`SlicerError` for an empty
    plate or a mesh that cannot be parsed as STL.
    """
    if not items:
        raise SlicerError("Cannot build a 3MF for an empty build plate")

    model = ET.Element("model", {"xmlns": CORE_NAMESPACE, "unit": "millimeter"})
    resources = ET.SubElement(model, "resources")
    build = ET.SubElement(model, "build")

    object_id_by_key: dict[str, int] = {}
    next_id = 1
    for index, item in enumerate(items):
        key = item.key or f"item-{index}"
        object_id = object_id_by_key.get(key)
        if object_id is None:
            mesh = parse_stl(item.model.data)
            if not mesh.triangles:
                raise SlicerError(f"Plate item {index + 1} has an empty mesh")
            object_id = next_id
            next_id += 1
            object_id_by_key[key] = object_id
            _append_object(resources, object_id, item.name or f"item-{index + 1}", mesh)

        item_el = ET.SubElement(build, "item")
        item_el.set("objectid", str(object_id))
        item_el.set("transform", item_transform(item))

    ET.indent(model, space="  ")
    model_xml = ET.tostring(model, encoding="utf-8", xml_declaration=True)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("3D/3dmodel.model", model_xml)
    return buffer.getvalue()
