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

__all__ = ["REVISER_SYSTEM_PROMPT", "make_llm_reviser"]

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
    "Keep dimensions, primitives and operations printable, keep the part "
    "resting on the build plate (min Z = 0) and never emit OpenSCAD code, "
    "G-code or STL data - only the structured specification."
)


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


def make_llm_reviser(provider: LLMProvider) -> SpecReviser:
    """Return a :data:`~agents.graph.cad.SpecReviser` backed by *provider*.

    The returned callable has the ``(specification, errors, attempt) -> dict |
    None`` signature the CAD node expects. It returns ``None`` on any
    :class:`~agents.llm.LLMError` or validation failure, so a broken reviser can
    never kill the run.
    """

    def reviser(
        specification: dict[str, Any],
        errors: list[str],
        attempt: int,
    ) -> dict[str, Any] | None:
        prompt = _reviser_prompt(specification, errors, attempt)
        try:
            payload = provider.structured(
                prompt,
                ModelSpecification,
                system=REVISER_SYSTEM_PROMPT,
            )
            plan = coerce_plan(payload)
        except (LLMError, ValidationError) as exc:
            logger.warning("LLM reviser failed, retrying the unchanged spec: %s", exc)
            return None
        return plan.specification.model_dump()

    return reviser
