"""Skill injection and constraint enforcement for the agent workflow.

A *skill* (docs/skills.md) is structured data plus textual guidance, never code.
Two kinds share one schema:

* ``guidance`` -- free text + ``defaults_json`` suggestions for a Planner/Editor
  prompt, plus machine-checkable ``constraints_json``;
* ``template`` -- names a built-in CAD generator through ``template_key``.

Two responsibilities live here:

* :func:`build_skills_block` renders the "Aktív skillek" prompt block that the
  Planner and Editor append to their system prompt. The LLM still answers with
  a single ``ModelSpecification`` -- skills only *guide* it.
* :func:`skill_constraint_errors` is the **enforced** half: the Validator calls
  it so ``constraints_json`` is checked mechanically, not merely suggested.

The module is intentionally import-light (no Django models at import time) so
the graph stays unit-testable and the optional ``skills`` app can be selected
lazily. It never raises: a malformed constraint degrades to a warning.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "ACTIVE_SKILLS_HEADER",
    "PLATE_TOLERANCE_MM",
    "build_skills_block",
    "resolve_skill_selection",
    "skill_constraint_errors",
    "skill_payload",
    "template_object_kind",
    "with_skills",
]

logger = logging.getLogger(__name__)

#: Header of the prompt block injected into the Planner/Editor system prompt.
ACTIVE_SKILLS_HEADER = "Aktív skillek"

#: A part "rests on the plate" when its lowest additive geometry is within this
#: tolerance of ``Z = 0`` (mm). Small enough to catch a floating/sunken part,
#: large enough not to fight floating-point noise.
PLATE_TOLERANCE_MM = 0.5

#: Constraint keys the Validator understands. Unknown keys become warnings so a
#: newer skill cannot silently disable checking.
KNOWN_CONSTRAINTS = frozenset(
    {
        "min_wall_mm",
        "must_rest_on_plate",
        "require_primitives",
        "forbid_operations",
    }
)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def skill_payload(skill: Any) -> dict[str, Any]:
    """Normalise a ``Skill`` model instance (or a test dict) to a plain dict.

    The workflow state and the persisted provenance must stay JSON-safe, so the
    model instance is reduced to the fields the graph needs. Test doubles may
    pass a dict directly; a dict keeps its own values for the known keys.
    """
    if isinstance(skill, Mapping):
        get: Any = skill.get
        pk = skill.get("id", skill.get("pk"))
    else:
        get = lambda key, default=None: getattr(skill, key, default)  # noqa: E731
        pk = getattr(skill, "pk", None)

    return {
        "id": pk,
        "slug": _text(get("slug", "")),
        "name": _text(get("name", "")),
        "kind": _text(get("kind", "")),
        "template_key": _text(get("template_key", "")),
        "object_kind": _text(get("object_kind", "")),
        "description": _text(get("description", "")),
        "guidance": _text(get("guidance", "")),
        "defaults_json": _mapping(get("defaults_json", {})),
        "constraints_json": _mapping(get("constraints_json", {})),
    }


def _skill_label(payload: Mapping[str, Any]) -> str:
    return _text(payload.get("slug")) or _text(payload.get("name")) or "skill"


def build_skills_block(skills: Sequence[Any] | None) -> str:
    """Render the "Aktív skillek" prompt block (empty string when no skills).

    Only ``name``, ``description``, ``guidance`` and ``defaults_json`` are shown
    -- ``constraints_json`` is enforced by the Validator, not prompted
    (docs/skills.md 4.).
    """
    payloads = [skill_payload(skill) for skill in skills or []]
    if not payloads:
        return ""

    lines = [
        f"{ACTIVE_SKILLS_HEADER}:",
        "The user selected the following generation recipes. Use their guidance "
        "and suggested default values when filling the specification; they are "
        "hints, so keep every value printable and physically sensible.",
    ]
    for payload in payloads:
        parts = [f"- {payload['name'] or _skill_label(payload)}"]
        if payload["description"]:
            parts.append(f"  description: {payload['description']}")
        if payload["guidance"]:
            parts.append(f"  guidance: {payload['guidance']}")
        if payload["defaults_json"]:
            defaults = json.dumps(payload["defaults_json"], ensure_ascii=False, sort_keys=True)
            parts.append(f"  defaults: {defaults}")
        lines.append("\n".join(parts))
    return "\n".join(lines)


def with_skills(base_prompt: str, skills: Sequence[Any] | None) -> str:
    """Append the skills block to *base_prompt* (unchanged when there are none)."""
    block = build_skills_block(skills)
    return f"{base_prompt}\n\n{block}" if block else base_prompt


def template_object_kind(skills: Sequence[Any] | None) -> str:
    """Return the built-in generator key a ``template`` skill selects.

    Convention (docs/skills.md 4.): when a selected skill names a
    ``template_key`` (or, for a ``template`` skill, an ``object_kind``) the
    Planner/Editor sets ``specification["object"]`` to that key, generalising
    the historical ``primitives: []`` -> built-in phone-holder behaviour. The
    CAD backend then runs its built-in generator for that object kind.
    """
    for skill in skills or []:
        payload = skill_payload(skill)
        if payload["template_key"]:
            return payload["template_key"]
        if payload["kind"] == "template" and payload["object_kind"]:
            return payload["object_kind"]
    return ""


def _resolve_workspace(company_id: str | None) -> Any:
    """Best-effort lookup of the workspace a run belongs to (for skill scope).

    ``company_id`` is the workspace id as a string (it doubles as the RAG
    scope). Any error -- a non-numeric id, an unavailable app -- degrades to
    ``None`` (only built-in/public skills are considered).
    """
    if not company_id:
        return None
    try:
        from workspaces.models import Workspace

        return Workspace.objects.filter(pk=company_id).first()
    except Exception:  # noqa: BLE001 - scoping must never fail a run
        logger.debug("Could not resolve workspace %r for skill selection", company_id)
        return None


def resolve_skill_selection(
    select_fn: Any,
    *,
    prompt: str,
    company_id: str | None = None,
    manual_ids: Sequence[int] | None = None,
    auto: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Resolve the active skills for a run and return ``(payloads, selection)``.

    Deterministic order (docs/skills.md 3.):

    * explicit ``manual_ids`` win -> ``"manual"``;
    * otherwise, when *auto* is set, the service's object-kind/tag match (and
      embedding fallback) runs -> ``"auto"``;
    * with neither, no skill is selected -> ``""`` (no DB access at all).

    Selection is best-effort: an unavailable ``skills`` app, a broken ranker or
    any other error degrades to no skills and never fails the run.
    """
    wanted = [int(pk) for pk in (manual_ids or [])]
    if not wanted and not auto:
        return [], ""
    selection = "manual" if wanted else "auto"

    if select_fn is None:
        try:
            from skills.services import select_skills

            select_fn = select_skills
        except Exception:  # noqa: BLE001 - the skills app is optional
            logger.warning("Skill selection unavailable", exc_info=True)
            return [], ""

    workspace = _resolve_workspace(company_id)
    try:
        selected = list(
            select_fn(
                prompt=prompt,
                object_kind="",
                workspace=workspace,
                manual_ids=wanted or None,
            )
        )
    except Exception:  # noqa: BLE001 - selection must never fail a generation
        logger.warning("Skill selection failed", exc_info=True)
        return [], ""

    return [skill_payload(skill) for skill in selected], selection


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _primitive_min_z(primitive: Mapping[str, Any]) -> float | None:
    """Return the lowest Z (mm) of a primitive, or ``None`` when undecidable."""
    position = primitive.get("position")
    if not isinstance(position, Mapping):
        return None
    z = _float_or_none(position.get("z"))
    if z is None:
        return None

    kind = _text(primitive.get("type"))
    if kind in {"box", "cylinder", "cone", "extrude"}:
        height = _float_or_none(primitive.get("height"))
        if kind == "extrude":
            # The profile is extruded from its own plane: the primitive centre's
            # Z is the plate contact point.
            return z
        if height is None:
            return None
        return z - height / 2.0
    if kind == "sphere":
        diameter = _float_or_none(primitive.get("diameter"))
        if diameter is None:
            return None
        return z - diameter / 2.0
    return None


def _wall_thickness(specification: Mapping[str, Any]) -> float | None:
    return _float_or_none(specification.get("wall_thickness"))


def skill_constraint_errors(
    specification: Mapping[str, Any] | None,
    skills: Sequence[Any] | None,
) -> tuple[list[str], list[str]]:
    """Enforce every active skill's ``constraints_json``.

    Returns ``(problems, warnings)``. ``problems`` are blocking strings in the
    same shape the Validator already reports (they flow into
    ``validation.errors``/``structured_error`` details); ``warnings`` list
    unknown constraint keys so a skill author sees that a check was ignored.

    Supported constraints (docs/skills.md 4.):

    * ``min_wall_mm`` -- ``specification.wall_thickness`` must be at least it;
    * ``must_rest_on_plate`` -- every **additive** primitive must touch ``Z = 0``
      (within :data:`PLATE_TOLERANCE_MM`); template parts (no primitives) are
      exempt;
    * ``require_primitives`` -- ``True`` requires at least one primitive, a list
      of type names requires each of them to be present;
    * ``forbid_operations`` -- a list of forbidden operation kinds.
    """
    spec = specification if isinstance(specification, Mapping) else {}
    primitives = [item for item in (spec.get("primitives") or []) if isinstance(item, Mapping)]
    operations = [item for item in (spec.get("operations") or []) if isinstance(item, Mapping)]

    problems: list[str] = []
    warnings: list[str] = []

    for skill in skills or []:
        payload = skill_payload(skill)
        constraints = payload["constraints_json"]
        if not constraints:
            continue
        label = _skill_label(payload)

        for key, value in constraints.items():
            if key not in KNOWN_CONSTRAINTS:
                warnings.append(f"skill:{label}: ismeretlen constraint '{key}' (kihagyva)")
                continue

            if key == "min_wall_mm":
                minimum = _float_or_none(value)
                wall = _wall_thickness(spec)
                if minimum is None or wall is None:
                    continue
                if wall < minimum:
                    problems.append(
                        f"skill:{label}: wall_thickness {wall} mm < min_wall_mm {minimum} mm"
                    )
            elif key == "must_rest_on_plate":
                if not value or not primitives:
                    continue
                mins = [
                    min_z
                    for primitive in primitives
                    if _text(primitive.get("role") or "add") == "add"
                    for min_z in [_primitive_min_z(primitive)]
                    if min_z is not None
                ]
                if not mins:
                    continue
                lowest = min(mins)
                if abs(lowest) > PLATE_TOLERANCE_MM:
                    problems.append(
                        f"skill:{label}: the part does not rest on the plate "
                        f"(lowest min Z = {lowest:.2f} mm, expected 0 ± {PLATE_TOLERANCE_MM} mm)"
                    )
            elif key == "require_primitives":
                if value is True:
                    if not primitives:
                        problems.append(f"skill:{label}: at least one primitive is required")
                    continue
                required = _string_list(value)
                if not required:
                    continue
                if not primitives:
                    problems.append(f"skill:{label}: at least one primitive is required")
                    continue
                present = {_text(primitive.get("type")) for primitive in primitives}
                missing = [kind for kind in required if kind not in present]
                if missing:
                    problems.append(
                        f"skill:{label}: missing required primitive type(s): {', '.join(missing)}"
                    )
            elif key == "forbid_operations":
                forbidden = _string_list(value)
                used = {_text(operation.get("kind")) for operation in operations}
                violated = sorted(used & set(forbidden))
                if violated:
                    problems.append(
                        f"skill:{label}: forbidden operation kind(s) used: {', '.join(violated)}"
                    )

    return problems, warnings


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_text(value)] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [_text(item) for item in value if _text(item)]
    return []
