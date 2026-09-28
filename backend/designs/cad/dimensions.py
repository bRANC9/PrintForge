"""Post-render dimension check: is the STL the size that was asked for?

:mod:`designs.cad.meshcheck` judges the *rendered geometry* (watertight, winding,
volume, bodies, thin edges) and :mod:`agents.spec` validates the *declared*
numbers. Neither compares the two, so a part that renders at 2.5x the requested
height is watertight, printable and completely wrong: on the 2026-09-27 model
matrix (docs/model-matrix-test-round.md 2.) all 8 renderable STLs passed every
check, and several of them did not match the request at all. This module is the
missing third leg -- it takes the rendered bounding box and the requested
envelope and reports the difference.

Design rules
------------
* **Pure.** No shell, no network, no GPU, no randomness, no Django and -- the
  point of the split -- **no trimesh**: :func:`dimension_issues` takes numbers,
  not a mesh. The measurement (bytes -> extents) lives in
  :func:`designs.cad.pipeline.dimension_warnings`, which is the only place that
  knows about mesh containers. That keeps this module importable and testable
  without the heavy dependencies, and lets the agent path and a future non-OpenSCAD
  backend reuse it.
* **Advisory, never blocking.** A real part legitimately overshoots its nominal
  envelope (a bracket with an arm, a holder built *for* a device of the declared
  size), and a heuristic that failed good output would be worse than no check. The
  findings therefore go to ``validation_json["warnings"]``, next to the meshcheck
  findings that are already advisory.
* **Silent about what was not asked.** A requested value is only compared when it
  is a usable number (> 0): ``None``, ``0``, ``""`` and a missing key are skipped
  rather than flagged, because plenty of legitimate requests leave ``dimensions``
  nominal or absent (a mesh-import specification has no ``dimensions`` at all).

The mapping below is the one the CAD package already uses
(:func:`designs.cad.openscad._synthesize_box` / ``_BOX_DIMENSION_FALLBACK``):
``width -> X``, ``height -> Y``, ``thickness -> Z``, millimetres everywhere.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "AXIS_DIMENSION_KEYS",
    "DIMENSION_TOLERANCE",
    "MIN_Z_EPSILON_MM",
    "dimension_issues",
]

#: Relative tolerance when comparing the rendered mesh to the requested
#: dimensions. A part is allowed to differ from its nominal bounding box: the
#: spec's ``dimensions`` describe the envelope the planner believed in, and a
#: real part legitimately overshoots or undershoots it. 25% is measured, not
#: guessed -- rendered through the real sandbox, the wrong parts of
#: docs/model-matrix-test-round.md 2. miss by 95%..1900% (a 2 mm-thick "40 mm
#: long" adapter, a 4.5 mm "90 mm tall" press, a 223.5 mm tree) while a part the
#: pipeline builds correctly from a matching primitive lands at 0.0%. The one
#: systematic false positive is the legacy phone-holder template, which builds a
#: holder *for* a device of the declared size: it is off by 59% and 1367%, so no
#: tolerance fixes it -- that is a spec-semantics question, not a threshold one.
DIMENSION_TOLERANCE = 0.25

#: Rendered axis -> the ``dimensions`` key it is compared against. Mirrors
#: ``openscad._BOX_DIMENSION_FALLBACK`` (a box's ``width`` is X, its ``depth``
#: comes from ``dimensions.height`` and its ``height`` from
#: ``dimensions.thickness``).
AXIS_DIMENSION_KEYS: tuple[tuple[str, str], ...] = (
    ("X", "width"),
    ("Y", "height"),
    ("Z", "thickness"),
)

#: Absolute slack for the build-plate check, millimetres. An STL stores float32
#: coordinates, so even a metre out the rounding is ~6e-5 mm; 0.01 mm is three
#: orders of magnitude above that noise and 40x below one 0.4 mm nozzle layer, so
#: a real offset is never mistaken for float dust.
MIN_Z_EPSILON_MM = 0.01


def _usable(value: Any) -> float | None:
    """A positive finite number from ``value``, or ``None`` when unusable.

    ``None``/``""``/non-numeric text/``bool``/``NaN``/``inf``/``<= 0`` all
    collapse to "not a usable measurement", so a caller skips the comparison
    instead of dividing by zero or flagging a number nobody asked for. A numeric
    string is accepted (``"90"`` -> ``90.0``) because that is what
    ``openscad._coerce_float`` accepts from an untrusted specification.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    return number if math.isfinite(number) and number > 0 else None


def dimension_issues(
    extents_mm: Sequence[float],
    requested: Mapping[str, Any] | None,
    *,
    min_z_mm: float = 0.0,
) -> list[str]:
    """Return the non-blocking dimension findings for one rendered part.

    ``extents_mm`` is the rendered mesh's ``(x, y, z)`` extents in millimetres and
    ``requested`` is the specification's ``dimensions`` mapping
    (``width``/``height``/``thickness``, all mm -- see ``agents.spec``'s
    ``Dimensions``). An axis is compared only when *both* numbers are usable
    (> 0): a missing key, ``None``, ``0``, ``""`` or a non-finite value is
    skipped silently, never flagged.

    An axis is reported when ``abs(actual - requested) / requested`` exceeds
    :data:`DIMENSION_TOLERANCE` (strictly greater, so a part exactly on the
    tolerance edge is clean). The message names the axis, the requested key, both
    numbers and the signed percentage.

    ``min_z_mm`` is the rendered mesh's minimum Z. Anything but ~0 is reported:
    the viewer, the slicer and ``agents/spec`` all assume the part rests on the
    build plate, and a primitive placed on its centre (a ``cylinder`` with no
    ``position``) sinks half of itself into the plate. This check runs even when
    no dimension was comparable.

    Returns a list of human-readable strings in a deterministic order (X, Y, Z,
    then the build plate). It never raises and never blocks: a caller that cannot
    measure something passes nothing comparable and gets ``[]``.
    """
    issues: list[str] = []
    wanted: Mapping[str, Any] = requested if isinstance(requested, Mapping) else {}
    # ``str``/``bytes`` are iterable but are never three measurements: without
    # this guard ``"40,60,10"`` would be read as the characters '4' and '0'.
    if isinstance(extents_mm, (str, bytes, bytearray)):
        axes: list[Any] = []
    else:
        try:
            axes = list(extents_mm or ())
        except TypeError:  # a non-iterable extent sequence is simply not comparable
            axes = []

    for index, (axis, key) in enumerate(AXIS_DIMENSION_KEYS):
        if index >= len(axes):
            break
        actual = _usable(axes[index])
        if actual is None:
            continue
        expected = _usable(wanted.get(key))
        if expected is None:
            continue
        deviation = (actual - expected) / expected
        if abs(deviation) <= DIMENSION_TOLERANCE:
            continue
        issues.append(
            f"rendered {axis} extent {actual:.1f}mm differs from the requested "
            f"{key} {expected:.1f}mm by {deviation * 100:+.0f}% "
            f"(tolerance +-{DIMENSION_TOLERANCE * 100:.0f}%)"
        )

    try:
        min_z = float(min_z_mm)
    except (TypeError, ValueError):
        min_z = 0.0
    if math.isfinite(min_z) and abs(min_z) > MIN_Z_EPSILON_MM:
        issues.append(
            f"rendered part starts at Z={min_z:.3f}mm; the rest of the app assumes "
            "the part rests on the build plate (min Z = 0)"
        )
    return issues
