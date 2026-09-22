"""Editor agent for annotation-driven model editing (docs/visual-editing.md 3.5).

The Editor is the visual-prompt counterpart of the Planner: instead of turning a
free-text request into a specification from scratch, it takes the *base*
specification plus the user's surface annotations and returns an **updated full
:class:`~agents.spec.ModelSpecification`** whose ``operations`` list holds the
validated features derived from the annotations.

Boundary (terv.md 7. fejezet): exactly like the Planner, the Editor NEVER
produces OpenSCAD source, G-code or STL/mesh data -- it only produces structured
data. The operations are rendered later by the CAD node through
``CADBackend.generate``; the Validator is still the only mesh producer.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable
from typing import Any, NamedTuple

from django.conf import settings
from pydantic import ValidationError

from agents.llm import LLMError, LLMProvider
from agents.spec import MAX_OPERATION_LABEL

from .planner import PlannerPlan, coerce_plan
from .state import WorkflowState, append_history, failure_state
from .vision import reference_images, reference_prompt_for, structured_with_reference_image

__all__ = [
    "DEFAULT_ANCHOR_TOLERANCE_MM",
    "EDITOR_SYSTEM_PROMPT",
    "enforce_annotation_anchors",
    "make_editor_node",
]

#: Snap tolerance (mm) used when ``settings.AGENT_ANCHOR_TOLERANCE_MM`` is absent.
DEFAULT_ANCHOR_TOLERANCE_MM = 25.0

EDITOR_SYSTEM_PROMPT = (
    "You are the Editor agent of an OpenSCAD 3D-printing workflow. "
    "You receive the base model specification, the user's edit instruction and "
    "annotations drawn on the 3D surface (a point + normal, or a region "
    "centroid + average normal), each carrying its own 'instruction'. "
    "Return ONE JSON object with 'specification' (the FULL updated "
    "ModelSpecification) plus 'needs_research'/'research_query'. "
    "The specification describes real geometry with its 'primitives' list: "
    "one or more primitives in millimetres, each one a 'box', 'cylinder', "
    "'sphere' or 'cone' placed by its 'position' - the primitive centre in mm - "
    "with an optional 'rotation' in degrees. Use role 'add' for material and "
    "role 'subtract' for holes and cutouts; the part must rest on the build "
    "plate (min Z = 0) and use sensible, printable sizes. "
    "Keep the base object's existing 'primitives' unless the edit instruction "
    "explicitly changes them, and keep the existing 'primitives': [] only when "
    "the object really is the built-in phone holder. "
    "Each annotation instruction MUST become one or more entries in the "
    "specification's 'operations' list: use the annotation's point (or region "
    "centroid) as the operation 'origin' in model coordinates (mm) and the "
    "annotation normal as the operation 'normal' axis. "
    "Keep the base object kind and dimensions unless the edit explicitly "
    "requires a change. Emit 'operations': [] when the edit adds no feature. "
    "Never emit OpenSCAD code, G-code or STL data - only the structured plan."
)


def _editor_prompt(provider: LLMProvider, state: WorkflowState) -> str:
    """Build the Editor user prompt from the instruction, base spec and annotations.

    ``reference_prompt_for`` keeps the optional reference note/photo behaviour
    identical to the Planner; the base specification and the annotation payload
    are attached as JSON (the annotations only ever carry structured JSON, never
    raw image bytes).
    """
    instruction = reference_prompt_for(provider, state)
    base = state.get("base_specification") or {}
    annotations = state.get("annotations") or []
    return (
        f"User edit instruction:\n{instruction}\n\n"
        f"Base specification (JSON):\n"
        f"{json.dumps(base, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Annotations (JSON):\n"
        f"{json.dumps(annotations, ensure_ascii=False, sort_keys=True)}\n\n"
        "Annotation shape: {id, kind, point [x,y,z], normal [x,y,z], region "
        "{centroid [x,y,z], normal [x,y,z], size, count}, instruction}. "
        "For each annotation, turn its 'instruction' into one or more "
        "'operations' entries anchored at that annotation's origin (point or "
        "region centroid) with the annotation's normal as the operation axis "
        "(the 'operations' schema is: kind, origin, normal, depth, width, "
        "height, diameter, length, label). Keep the base dimensions unless the "
        "edit requires a change; emit 'operations': [] when the edit adds no "
        "feature."
    )


class _Anchor(NamedTuple):
    """One reference frame derived from an annotation (docs/visual-editing.md 2)."""

    origin: tuple[float, float, float]
    axis: tuple[float, float, float]
    label: str


def _coerce_vec3(value: Any) -> tuple[float, float, float] | None:
    """Return a finite ``(x, y, z)`` tuple, or ``None`` for an invalid vector.

    ``annotations`` arrive from the API as JSON (``[x, y, z]``) while
    ``operations`` come from a dumped ``ModelSpecification`` (``{x, y, z}``), so
    both shapes are accepted.
    """
    if isinstance(value, dict):
        raw: tuple[Any, Any, Any] = (value.get("x"), value.get("y"), value.get("z"))
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        raw = (value[0], value[1], value[2])
    else:
        return None
    try:
        x, y, z = (float(component) for component in raw)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(component) for component in (x, y, z)):
        return None
    return (x, y, z)


def _normalise(vector: tuple[float, float, float] | None) -> tuple[float, float, float] | None:
    """Return the unit vector of *vector*, or ``None`` when it is zero/invalid."""
    if vector is None:
        return None
    x, y, z = vector
    length = math.sqrt(x * x + y * y + z * z)
    if not math.isfinite(length) or length <= 0.0:
        return None
    return (x / length, y / length, z / length)


def _vec3_payload(template: Any, values: tuple[float, float, float]) -> Any:
    """Serialise *values* in the same container shape as *template*.

    Keeping the caller's shape (list vs ``{"x": ...}``) means an operation that
    is already anchored round-trips byte-for-byte.
    """
    if isinstance(template, (list, tuple)):
        return [values[0], values[1], values[2]]
    return {"x": values[0], "y": values[1], "z": values[2]}


def _reference_frames(annotations: list[dict[str, Any]]) -> list[_Anchor]:
    """Build the usable reference frames from the annotation payload.

    The reference origin is ``region.centroid`` when present, otherwise the
    annotation ``point``; the axis is the (normalised) annotation ``normal``.
    Annotations without a valid origin or with a zero normal are skipped -- they
    cannot anchor anything deterministically.
    """
    frames: list[_Anchor] = []
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        region = annotation.get("region")
        region = region if isinstance(region, dict) else {}
        origin_raw = region.get("centroid")
        if origin_raw is None:
            origin_raw = annotation.get("point")
        origin = _coerce_vec3(origin_raw)
        if origin is None:
            continue
        normal_raw = annotation.get("normal")
        if normal_raw is None:
            normal_raw = region.get("normal")
        axis = _normalise(_coerce_vec3(normal_raw))
        if axis is None:
            continue
        frames.append(
            _Anchor(
                origin=origin,
                axis=axis,
                label=str(annotation.get("instruction") or "").strip(),
            )
        )
    return frames


def enforce_annotation_anchors(
    operations: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    *,
    tolerance_mm: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Deterministically anchor *operations* to the user's annotations.

    The LLM must not be able to silently place a feature far from the surface
    prompt that requested it. Each operation is therefore matched to the nearest
    annotation reference (``region.centroid`` else ``point``): within
    *tolerance_mm* its ``origin`` and ``normal`` are snapped to that reference
    and an empty ``label`` is filled from the annotation ``instruction``;
    otherwise it is kept and a warning is returned. The input dicts are never
    mutated -- the returned operations are deep copies.

    Returns a ``(operations, warnings)`` tuple.
    """
    frames = _reference_frames(annotations)
    if not frames:
        # No usable annotation: nothing to enforce, no warnings, no mutation.
        return [copy.deepcopy(operation) for operation in operations], []

    try:
        tolerance = float(tolerance_mm)
    except (TypeError, ValueError):
        tolerance = 0.0

    adjusted: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, operation in enumerate(operations):
        copied = copy.deepcopy(operation)
        origin = _coerce_vec3(copied.get("origin"))
        if origin is None:
            warnings.append(f"operation {index} has no valid origin (kept unchanged)")
            adjusted.append(copied)
            continue

        nearest = min(frames, key=lambda frame: math.dist(origin, frame.origin))
        distance = math.dist(origin, nearest.origin)
        if distance <= tolerance:
            copied["origin"] = _vec3_payload(copied.get("origin"), nearest.origin)
            copied["normal"] = _vec3_payload(copied.get("normal"), nearest.axis)
            if not str(copied.get("label") or "").strip():
                copied["label"] = nearest.label[:MAX_OPERATION_LABEL]
            adjusted.append(copied)
        else:
            warnings.append(
                f"operation {index} is {distance:.1f} mm from the nearest annotation "
                "(kept unchanged)"
            )
            adjusted.append(copied)

    return adjusted, warnings


def make_editor_node(
    *,
    provider: LLMProvider,
    max_attempts: int,
) -> Callable[[WorkflowState], dict[str, Any]]:
    """Build the ``editor`` graph node bound to *provider*.

    The returned callable is a LangGraph node: it takes the running state and
    returns a partial state update shaped exactly like the Planner's, so the
    downstream ``route_after_planner`` edge and the CAD/Validator nodes need no
    knowledge of the edit path. All LLM access goes through the injected
    ``LLMProvider``, so tests pass a fake and no live model is required.
    """

    def editor_node(state: WorkflowState) -> dict[str, Any]:
        # The Editor, like the Planner, only ever asks for structured data: it
        # has no concept of code or meshes at all.
        prompt = _editor_prompt(provider, state)
        try:
            # The optional reference image (terv.md 27.) is attached here; the
            # helper falls back to a text-only call and never fails the run
            # because of vision.
            raw, image_used, image_warning = structured_with_reference_image(
                provider,
                prompt,
                PlannerPlan,
                system=EDITOR_SYSTEM_PROMPT,
                images=reference_images(state),
            )
            plan = coerce_plan(raw)
        except (LLMError, ValidationError) as exc:
            return failure_state(
                state,
                stage="editor",
                error_type=type(exc).__name__,
                message=str(exc),
            )

        specification = plan.specification.model_dump()
        history = append_history(state, "editor: specification updated (edit mode)")

        # Deterministic guardrail: the LLM must not silently place a feature far
        # from the annotation that asked for it (docs/visual-editing.md 3.5).
        annotations = list(state.get("annotations") or [])
        if annotations:
            tolerance = getattr(
                settings,
                "AGENT_ANCHOR_TOLERANCE_MM",
                DEFAULT_ANCHOR_TOLERANCE_MM,
            )
            operations, anchor_warnings = enforce_annotation_anchors(
                specification.get("operations", []),
                annotations,
                tolerance_mm=tolerance,
            )
            specification["operations"] = operations
            history = [
                *history,
                *(f"editor: anchor: {warning}" for warning in anchor_warnings),
            ]

        if image_warning:
            history = [*history, f"editor: {image_warning}"]
        return {
            "specification": specification,
            "needs_research": bool(plan.needs_research),
            "research_query": plan.research_query,
            "research_sources": [],
            "research_used": False,
            "reference_image_used": image_used,
            "reference_image_warning": image_warning,
            "attempt": 0,
            "max_attempts": int(max_attempts),
            "status": "planned",
            "error": None,
            "history": history,
        }

    return editor_node
