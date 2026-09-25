"""Text-only second opinion for the vision self-check (docs/vision-self-check.md).

The vision reviewer judges a rendered preview. Small vision models
(``llava:7b``) rubber-stamp whatever they see, so their verdict alone is not
enough. This node asks the **main** (text) model to compare the request with the
*structured specification* -- no image -- and returns the same
:class:`~agents.spec.ReviewResult` contract.

Both verdicts are required to say "matches": a part is only accepted when the
vision reviewer **and** the text reviewer agree, so either one catching a
mismatch triggers the bounded CAD retry.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from agents.llm import LLMProvider
from agents.spec import ReviewResult

logger = logging.getLogger(__name__)

__all__ = ["TEXT_REVIEW_SYSTEM_PROMPT", "run_text_review", "text_review_prompt"]

TEXT_REVIEW_SYSTEM_PROMPT = (
    "You are the Specification Reviewer of an OpenSCAD 3D-printing workflow. "
    "You get the user's original request and the structured model specification "
    "(object, dimensions, primitives, operations) that the part was built from. "
    "There is NO image: judge the GEOMETRY the specification describes. "
    "Answer 'matches: false' whenever the specification cannot produce what the "
    "request asks for. Flag at least: a plain rectangle/box profile for a shaped "
    "request (tree, star, heart, cutter, stamp, ornament); a solid block for a "
    "press/cutter (those need a thin extruded wall); a request for a hole, pin or "
    "screw with nothing subtracted; a size that does not fit the object; a missing "
    "or wrong feature; a plate standing where a shaped part was asked for. "
    "Answer ONLY with one JSON object matching the ReviewResult schema: 'matches' "
    "(boolean), 'issues' (a list of concrete, actionable discrepancies; empty when "
    "the specification matches) and 'summary' (one short sentence). Every issue "
    "must say what is wrong in the specification and what to change. "
    "Never emit OpenSCAD code, G-code or STL data - only the structured verdict."
)


def text_review_prompt(state: Mapping[str, Any]) -> str:
    """Render the request + specification for the text reviewer.

    Takes the workflow state so the optional reference note (terv.md 27.) and the
    exact specification the part was built from both reach the reviewer.
    """
    request = str(state.get("prompt") or "")
    note = str(state.get("reference_image_note") or "").strip()
    if note:
        request = f"{request}\n\nReference note: {note}"
    specification = state.get("specification") or {}
    return (
        f"User request:\n{request}\n\n"
        f"Specification the part was built from (JSON):\n"
        f"{json.dumps(specification, ensure_ascii=False, sort_keys=True)}\n\n"
        "Compare the geometry this specification describes with the request. "
        "Answer 'matches: false' with concrete issues when they disagree."
    )


def run_text_review(provider: LLMProvider, state: Mapping[str, Any]) -> ReviewResult | None:
    """Ask *provider* for a specification verdict; ``None`` when it cannot answer.

    Best-effort by contract (like the vision path): any provider failure -- an
    ``LLMError``, a malformed payload, or a provider that simply cannot serve
    this schema -- returns ``None``, so the caller keeps the vision verdict
    instead of failing the run.
    """
    from pydantic import ValidationError

    from agents.llm import LLMError

    try:
        payload = provider.structured(
            text_review_prompt(state),
            ReviewResult,
            system=TEXT_REVIEW_SYSTEM_PROMPT,
        )
        return ReviewResult.model_validate(payload)
    except (LLMError, ValidationError) as exc:
        logger.info("text specification review unavailable: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - the second opinion is optional
        logger.info("text specification review skipped (%s: %s)", type(exc).__name__, exc)
        return None
