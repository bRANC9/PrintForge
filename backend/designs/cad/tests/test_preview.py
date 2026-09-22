"""Tests for the headless STL -> PNG preview renderer (docs/vision-self-check.md 2).

The renderer must be deterministic and dependency-light: parsing goes through
``trimesh`` and rasterisation through Pillow, with no shell/network/GPU. These
tests only assert observable properties (PNG signature, dimensions, relative
size) so they stay robust across library versions.
"""

from __future__ import annotations

import io

import pytest

from designs.cad.preview import PreviewRenderError, render_stl_preview

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _cube_stl() -> bytes:
    import trimesh

    return trimesh.creation.box(extents=(10.0, 20.0, 30.0)).export(file_type="stl")


def _png_size(payload: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(payload)) as image:
        return image.size


def test_render_default_views_returns_png():
    png = render_stl_preview(_cube_stl(), size=256)

    assert png.startswith(PNG_MAGIC)
    assert len(png) > 1000
    assert _png_size(png) == (256 * 3, 256)


def test_render_single_view_works():
    png = render_stl_preview(_cube_stl(), size=128, views=("front",))

    assert png.startswith(PNG_MAGIC)
    assert _png_size(png) == (128, 128)


def test_all_supported_views_render():
    png = render_stl_preview(
        _cube_stl(),
        size=96,
        views=("iso", "front", "top", "side"),
    )

    assert png.startswith(PNG_MAGIC)
    assert _png_size(png) == (96 * 4, 96)


def test_three_views_are_larger_than_one_view():
    stl = _cube_stl()

    one = render_stl_preview(stl, size=192, views=("front",))
    three = render_stl_preview(stl, size=192, views=("iso", "front", "top"))

    assert len(three) > len(one)


@pytest.mark.parametrize("payload", [b"", b"not an stl at all", b"\x00\x01\x02\x03"])
def test_invalid_stl_raises_preview_error(payload):
    with pytest.raises(PreviewRenderError):
        render_stl_preview(payload)


def test_degenerate_mesh_raises_preview_error():
    import trimesh

    degenerate = trimesh.Trimesh(
        vertices=[[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
        faces=[[0, 1, 2]],
        process=False,
    )
    with pytest.raises(PreviewRenderError):
        render_stl_preview(degenerate.export(file_type="stl"))


def test_unknown_view_raises_preview_error():
    with pytest.raises(PreviewRenderError):
        render_stl_preview(_cube_stl(), views=("back",))


def test_empty_views_raises_preview_error():
    with pytest.raises(PreviewRenderError):
        render_stl_preview(_cube_stl(), views=())
