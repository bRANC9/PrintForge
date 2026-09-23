"""Headless G-code -> PNG toolpath preview (terv.md Phase 5).

:func:`render_gcode_preview` parses the *extruding* moves of a G-code stream and
rasterises a top-down toolpath image with Pillow. It runs entirely in-process:
no shell, no network, no GPU and no randomness, so the output is deterministic
and safe to call from the slicing worker (never from the Django request cycle).

The parser understands the subset of G-code that describes the toolpath:

* ``G0``/``G1`` moves with ``X``/``Y`` (and optional ``Z``/``E``);
* ``G90``/``G91`` (absolute/relative XYZ) and ``M82``/``M83``
  (absolute/relative extrusion);
* ``G92`` position resets.

A move counts as extruding when its ``E`` delta is positive, so travel moves
(even ones that repeat the absolute ``E`` word) are never drawn.

Work is bounded so a hostile or huge file *degrades* instead of hanging: at most
``max_segments`` extruding segments are collected, the scan is capped at
:data:`_MAX_SCAN_BYTES` and the canvas side at :data:`_MAX_SIZE`. Pillow is
imported lazily inside the function, so importing :mod:`slicers` stays cheap and
a missing optional dependency surfaces as :class:`PreviewRenderError`, never an
``ImportError`` (mirrors ``designs/cad/preview.py``).
"""

from __future__ import annotations

import io
import logging
import re
from typing import NamedTuple

__all__ = [
    "GcodeSegment",
    "PreviewRenderError",
    "parse_extrusion_segments",
    "render_gcode_preview",
]

logger = logging.getLogger(__name__)

#: Background is pure white; the toolpaths use a dark blue -> dark navy gradient
#: keyed on the layer's Z so stacked layers stay visually distinguishable.
_BACKGROUND = (255, 255, 255)
_LOW_COLOR = (24, 24, 34)
_HIGH_COLOR = (78, 120, 186)

#: Smallest accepted canvas side.
_MIN_SIZE = 16

#: Largest accepted canvas side (bounds the ``size * size`` allocation).
_MAX_SIZE = 4096

#: Hard cap on how many input bytes are scanned.
_MAX_SCAN_BYTES = 64 * 1024 * 1024

_EPSILON = 1e-9


class GcodeSegment(NamedTuple):
    """A single extruding toolpath move in millimetres."""

    x0: float
    y0: float
    x1: float
    y1: float
    z: float


class PreviewRenderError(ValueError):
    """The G-code is empty, malformed or contains no extruding moves."""


# One G-code word: a letter followed by an optional sign and a decimal number.
# Matches both ``X10.5`` and the compact ``G1X10Y10E1`` spelling. Scientific
# notation is deliberately not supported: an uppercase ``E`` is the extruder
# word, so treating ``10E1`` as one number would swallow the next parameter.
_RE_WORD = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: str) -> int | None:
    number = _to_float(value)
    return None if number is None else int(number)


def _validate_canvas(size: int) -> None:
    if not isinstance(size, int) or isinstance(size, bool):
        raise PreviewRenderError("size must be an integer")
    if size < _MIN_SIZE or size > _MAX_SIZE:
        raise PreviewRenderError(f"size must be between {_MIN_SIZE} and {_MAX_SIZE}")


def _validate_max_segments(max_segments: int) -> None:
    if not isinstance(max_segments, int) or isinstance(max_segments, bool) or max_segments < 1:
        raise PreviewRenderError("max_segments must be an integer >= 1")


def parse_extrusion_segments(
    gcode_bytes: bytes,
    *,
    max_segments: int = 200_000,
) -> list[GcodeSegment]:
    """Extract the extruding moves from ``gcode_bytes`` as XY(Z) segments.

    The current position is tracked across the stream; each returned segment is
    ``(x0, y0, x1, y1, z)`` in millimetres. Parsing stops once ``max_segments``
    segments have been collected (a partial preview), and only the first
    :data:`_MAX_SCAN_BYTES` bytes are examined.
    """
    if not isinstance(gcode_bytes, (bytes, bytearray, memoryview)):
        raise PreviewRenderError("gcode must be bytes")
    payload = bytes(gcode_bytes)
    if not payload:
        raise PreviewRenderError("no G-code data provided")
    _validate_max_segments(max_segments)
    if len(payload) > _MAX_SCAN_BYTES:
        payload = payload[:_MAX_SCAN_BYTES]

    text = payload.decode("utf-8", errors="replace")
    segments: list[GcodeSegment] = []
    x = y = z = 0.0
    last_e = 0.0
    absolute_xyz = True
    relative_e = False

    for raw in text.splitlines():
        line = raw.split(";", 1)[0]
        if not line.strip():
            continue
        words = _RE_WORD.findall(line)
        if not words:
            continue

        command: tuple[str, str] | None = None
        params: list[tuple[str, str]] = []
        for index, word in enumerate(words):
            letter, value = word
            if letter.upper() in ("G", "M"):
                command = (letter.upper(), value)
                params = words[index + 1 :]
                break
        if command is None:
            continue

        letter, code = command
        code_num = _to_int(code)
        if code_num is None:
            continue

        if letter == "M":
            if code_num == 82:
                relative_e = False
            elif code_num == 83:
                relative_e = True
            continue

        if code_num == 90:
            absolute_xyz = True
            continue
        if code_num == 91:
            absolute_xyz = False
            continue
        if code_num == 92:
            for pletter, pvalue in params:
                number = _to_float(pvalue)
                if number is None:
                    continue
                key = pletter.upper()
                if key == "E":
                    last_e = 0.0 if relative_e else number
                elif key == "X":
                    x = number
                elif key == "Y":
                    y = number
                elif key == "Z":
                    z = number
            continue
        if code_num not in (0, 1):
            continue

        new_x, new_y, new_z = x, y, z
        e_value: float | None = None
        for pletter, pvalue in params:
            number = _to_float(pvalue)
            if number is None:
                continue
            key = pletter.upper()
            if key == "X":
                new_x = x + number if not absolute_xyz else number
            elif key == "Y":
                new_y = y + number if not absolute_xyz else number
            elif key == "Z":
                new_z = z + number if not absolute_xyz else number
            elif key == "E":
                e_value = number

        extruding = False
        if e_value is not None:
            if relative_e:
                delta_e = e_value
                last_e += e_value
            else:
                delta_e = e_value - last_e
                last_e = e_value
            extruding = delta_e > _EPSILON

        moved = abs(new_x - x) > _EPSILON or abs(new_y - y) > _EPSILON
        if extruding and moved:
            segments.append(GcodeSegment(x, y, new_x, new_y, new_z))
            if len(segments) >= max_segments:
                return segments

        x, y, z = new_x, new_y, new_z

    return segments


def _shade(z: float, min_z: float, span: float) -> tuple[int, int, int]:
    """Blend the dark toolpath colours by the layer's normalised Z."""
    if span <= _EPSILON:
        ratio = 0.0
    else:
        ratio = (z - min_z) / span
    ratio = min(max(ratio, 0.0), 1.0)
    return tuple(
        int(round(_LOW_COLOR[channel] + (_HIGH_COLOR[channel] - _LOW_COLOR[channel]) * ratio))
        for channel in range(3)
    )


def render_gcode_preview(
    gcode_bytes: bytes,
    *,
    size: int = 512,
    max_segments: int = 200_000,
) -> bytes:
    """Render the extruding toolpaths of ``gcode_bytes`` into a square PNG.

    The image is a top-down view (``+Y`` points up), white background, dark
    polylines shaded by layer Z. Consecutive extruding moves on the same layer
    that share an endpoint are merged into one polyline, so a typical file needs
    far fewer draw calls than it has segments.

    Raises :class:`PreviewRenderError` for unusable input (empty/malformed
    G-code, no extruding moves, or an out-of-range ``size``/``max_segments``).
    """
    _validate_canvas(size)
    segments = parse_extrusion_segments(gcode_bytes, max_segments=max_segments)
    if not segments:
        raise PreviewRenderError("G-code contains no extruding moves to preview")

    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover - Pillow is a hard dependency
        raise PreviewRenderError("G-code preview rendering requires the 'pillow' package") from exc

    min_x = min(segment.x0 for segment in segments)
    max_x = max(segment.x0 for segment in segments)
    min_y = min(segment.y0 for segment in segments)
    max_y = max(segment.y0 for segment in segments)
    min_z = min(segment.z for segment in segments)
    max_z = max(segment.z for segment in segments)
    for segment in segments:
        min_x = min(min_x, segment.x1)
        max_x = max(max_x, segment.x1)
        min_y = min(min_y, segment.y1)
        max_y = max(max_y, segment.y1)

    extent = max(max_x - min_x, max_y - min_y)
    if extent <= _EPSILON:
        raise PreviewRenderError("G-code toolpaths are degenerate (zero size)")

    margin = max(float(size) * 0.05, 1.0)
    scale = (size - 2.0 * margin) / extent
    z_span = max_z - min_z

    def project_x(value: float) -> float:
        return margin + (value - min_x) * scale

    def project_y(value: float) -> float:
        # Flip Y so the model's +Y axis points up in the image.
        return size - margin - (value - min_y) * scale

    image = Image.new("RGB", (size, size), _BACKGROUND)
    draw = ImageDraw.Draw(image)

    def draw_polyline(points: list[tuple[float, float]], z: float) -> None:
        if len(points) < 2:
            return
        pixels = [(project_x(point[0]), project_y(point[1])) for point in points]
        draw.line(pixels, fill=_shade(z, min_z, z_span), width=1)

    current: list[tuple[float, float]] = []
    current_z = 0.0
    for segment in segments:
        connected = (
            current
            and abs(current[-1][0] - segment.x0) <= _EPSILON
            and abs(current[-1][1] - segment.y0) <= _EPSILON
            and abs(current_z - segment.z) <= _EPSILON
        )
        if connected:
            current.append((segment.x1, segment.y1))
        else:
            draw_polyline(current, current_z)
            current = [(segment.x0, segment.y0), (segment.x1, segment.y1)]
            current_z = segment.z
    draw_polyline(current, current_z)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
