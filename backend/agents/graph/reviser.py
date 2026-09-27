"""LLM-backed specification reviser for CAD retries (docs/vision-self-check.md 4.).

The CAD node accepts an optional :data:`~agents.graph.cad.SpecReviser` hook that
turns the current specification plus the validator/reviewer errors into a
corrected specification. :func:`make_llm_reviser` is the production
implementation: it asks the injected provider for a fresh, validated
:class:`~agents.spec.ModelSpecification`.

The reviser is deliberately **best-effort**: any LLM or schema failure returns
``None`` so the CAD node retries the unchanged specification instead of crashing
the run. Like every other agent, it produces structured data only -- never
OpenSCAD code, G-code or STL data.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from agents.llm import LLMError, LLMProvider
from agents.spec import ModelSpecification

from .cad import SpecReviser
from .planner import (
    OPERATION_DIMENSIONS_PROMPT,
    PRIMITIVE_DIMENSIONS_PROMPT,
    coerce_plan,
)

__all__ = ["LlmReviser", "REVISER_SYSTEM_PROMPT", "make_llm_reviser"]

logger = logging.getLogger(__name__)

REVISER_SYSTEM_PROMPT = (
    "You are the CAD reviser of an OpenSCAD 3D-printing workflow. "
    "You receive the current validated ModelSpecification and the list of "
    "validation and/or vision-review errors it produced. Return ONE JSON "
    "object: the corrected ModelSpecification matching the schema. "
    "Change only what the errors describe and keep every other field as-is. "
    + OPERATION_DIMENSIONS_PROMPT
    + PRIMITIVE_DIMENSIONS_PROMPT
    + "If an error says a required numeric field is missing or out of range, "
    "fill or repair exactly that field (and any sibling dimension the same "
    "operation kind or primitive type requires - both operation dimensions and "
    "missing primitive sizes) using the surrounding geometry and the numeric "
    "error, instead of re-emitting the same invalid value. "
    "If an error says the specification has no 'primitives' (or describes no "
    "geometry), add the 'primitives' that build the requested object instead of "
    "leaving the list empty; for a flat 2D shape use a single 'extrude' with a "
    "polygon 'profile'. "
    "NEVER shrink the part to satisfy a bound. If a value is below a minimum, "
    "RAISE that value (and its printable sibling) and leave every other "
    "dimension at the size the user asked for: a part made several times "
    "smaller is not a fix, and the requested size is what the user wants. If the "
    "errors are about shape, fix the shape - not the size. "
    "Keep dimensions, primitives and operations printable, keep the part "
    "resting on the build plate (min Z = 0) and never emit OpenSCAD code, "
    "G-code or STL data - only the structured specification."
)

#: A revised dimension below this fraction of the original counts as a collapse.
_REVISION_SHRINK_RATIO = 0.5

#: The declared bounding-box fields a revision must not shrink on its own.
_SIZE_FIELDS = ("width", "height", "thickness")


def revision_regressions(
    previous: dict[str, Any],
    revised: dict[str, Any],
    errors: Sequence[str],
) -> list[str]:
    """Why ``revised`` is a regression instead of a fix of ``previous``.

    A reviser that makes the part several times smaller to clear a minimum
    bound produces a technically valid, practically useless blob: the measured
    case was a 150x30x5 mm part turned into 10x5x0.5 mm. These checks are
    deterministic (no model involved), so a small local model cannot slip a
    collapse past them.

    Returns a list of human-readable reasons; empty means the revision is
    acceptable.
    """
    reasons: list[str] = []
    complaint = " ".join(str(error) for error in errors).lower()

    before = previous.get("dimensions") if isinstance(previous.get("dimensions"), dict) else {}
    after = revised.get("dimensions") if isinstance(revised.get("dimensions"), dict) else {}
    for field in _SIZE_FIELDS:
        old = before.get(field)
        new = after.get(field)
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            continue
        if old <= 0 or new >= old * _REVISION_SHRINK_RATIO:
            continue
        # A shrink is only legitimate for the field the complaint names (e.g.
        # "dimensions.height must be >= 5"): a *growth* is always fine.
        if field in complaint or f"dimensions.{field}" in complaint:
            continue
        reasons.append(
            f"dimensions.{field} shrank from {old:g} mm to {new:g} mm "
            f"(no error asked for a smaller part)"
        )

    before_primitives = [
        item for item in (previous.get("primitives") or []) if isinstance(item, dict)
    ]
    after_primitives = [
        item for item in (revised.get("primitives") or []) if isinstance(item, dict)
    ]
    if before_primitives and not after_primitives:
        reasons.append("the revision removed every primitive, leaving no geometry")

    def _extrudes(items: list[dict[str, Any]]) -> int:
        return sum(1 for item in items if str(item.get("type") or "") == "extrude")

    if _extrudes(before_primitives) > _extrudes(after_primitives):
        reasons.append("the revision dropped the 'extrude' outline that carried the shape")

    return reasons


def _reviser_prompt(
    specification: dict[str, Any],
    errors: list[str],
    attempt: int,
) -> str:
    """Render the current spec and the errors as a structured correction request."""
    bullet_list = "\n".join(f"- {error}" for error in errors) or "- (none)"
    return (
        f"Attempt {attempt}. Current specification (JSON):\n"
        f"{json.dumps(specification, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Errors to fix:\n{bullet_list}\n\n"
        "Return the corrected specification."
    )


class LlmReviser:
    """Callable :data:`~agents.graph.cad.SpecReviser` that records its exchange.

    Beyond returning the corrected specification it keeps the last LLM call in
    :attr:`last_exchange` (``{agent, attempt, system, prompt, response}``) so the
    CAD node can append it to the workflow's ``llm_trace`` -- that is what makes
    the reviser's input/output visible in the UI (docs/vision-self-check.md 5.).
    """

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        #: The most recent exchange, or ``None`` when the last call failed.
        self.last_exchange: dict[str, Any] | None = None

    def __call__(
        self,
        specification: dict[str, Any],
        errors: list[str],
        attempt: int,
    ) -> dict[str, Any] | None:
        """Return a corrected specification, or ``None`` on any failure.

        A revision that *regresses* (shrinks the part several times to clear a
        bound, drops every primitive or loses the ``extrude`` outline) is refused:
        the caller retries the unchanged specification, which is a smaller,
        honest part rather than a valid-looking blob.
        """
        self.last_exchange = None
        prompt = _reviser_prompt(specification, errors, attempt)
        try:
            payload = self._provider.structured(
                prompt,
                ModelSpecification,
                system=REVISER_SYSTEM_PROMPT,
            )
            plan = coerce_plan(payload)
        except (LLMError, ValidationError) as exc:
            logger.warning("LLM reviser failed, retrying the unchanged spec: %s", exc)
            return None
        corrected = plan.specification.model_dump()
        regressions = revision_regressions(specification, corrected, errors)
        self.last_exchange = {
            "agent": "reviser",
            "attempt": int(attempt),
            "system": REVISER_SYSTEM_PROMPT,
            "prompt": prompt,
            "response": corrected,
        }
        if regressions:
            logger.warning(
                "LLM reviser regressed the specification (attempt %d), keeping the original: %s",
                attempt,
                "; ".join(regressions),
            )
            self.last_exchange["rejected"] = regressions
            return None
        return corrected


def make_llm_reviser(provider: LLMProvider) -> SpecReviser:
    """Return an :class:`LlmReviser` bound to *provider*.

    The returned object has the ``(specification, errors, attempt) -> dict |
    None`` signature the CAD node expects. It returns ``None`` on any
    :class:`~agents.llm.LLMError` or validation failure, so a broken reviser can
    never kill the run.
    """
    return LlmReviser(provider)
