"""Vision self-check review node (docs/vision-self-check.md 4. fejezet).

After the Validator produced an STL, the Reviewer renders a PNG preview and --
when the selected provider supports vision -- asks it to compare that preview
against the original request and the specification. The review is
**best-effort**: no STL, no vision, a broken renderer or a failing LLM call all
end in a warning + skip, never in a failed run.

A mismatch is fed back into the bounded CAD retry loop: while
``attempt < max_attempts`` the review issues are injected into the validation
errors and ``status`` becomes ``"retry"``, so the CAD node (and its optional
:data:`~agents.graph.cad.SpecReviser`) can correct the specification. Once the
bound is reached the run keeps ``status == "done"`` and records the issues as a
history warning -- the self-check can flag a problem but can never block a valid
model.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from agents.llm import LLMError, LLMProvider
from agents.spec import ReviewResult
from designs.cad.preview import render_stl_preview

from .state import WorkflowState, append_history
from .vision import reference_prompt_for

__all__ = ["REVIEW_SYSTEM_PROMPT", "geometry_missing_issue", "make_review_node"]

logger = logging.getLogger(__name__)

REVIEW_SYSTEM_PROMPT = (
    "You are the Vision Reviewer of an OpenSCAD 3D-printing workflow. "
    "You are shown a rendered preview of the generated part together with the "
    "user's original request and the specification the part was built from. "
    "Judge whether the preview matches the request and the specification: the "
    "shape, the proportions, the requested features and the orientation. "
    "Pay special attention to the specification's 'object' and 'primitives': "
    "when 'primitives' is empty the CAD backend falls back to a generic holder "
    "or box, so a request for any other shape is a mismatch. "
    "Answer ONLY with one JSON object matching the ReviewResult schema: "
    "'matches' (boolean), 'issues' (a list of concrete, actionable "
    "discrepancies; empty when the preview matches) and 'summary' (one short "
    "sentence). Every issue must say what is visibly wrong and what to change. "
    "Never emit OpenSCAD code, G-code or STL data - only the structured verdict."
)

#: Request tokens that mark an intentional holder request. The deterministic
#: shape guard below uses them so a real holder request is never flagged, while
#: a specification that rendered the built-in holder (or a synthesized box) for
#: a *different* object is caught even when a weak vision model rubber-stamps it
#: as a match (docs/vision-self-check.md). A false positive only costs one
#: bounded retry, never a wrong model.
_HOLDER_REQUEST_TOKENS = (
    "holder",
    "tartó",
    "tarto",
    "stand",
    "phone",
    "telefon",
    "tablet",
)


def _looks_like_holder_request(prompt: str) -> bool:
    """True when the user prompt plausibly asks for a holder/stand part."""
    text = (prompt or "").lower()
    return any(token in text for token in _HOLDER_REQUEST_TOKENS)


def geometry_missing_issue(state: WorkflowState) -> str | None:
    """Return a mismatch issue when the spec carries no geometry, else ``None``.

    A specification without ``primitives`` can only render the built-in holder
    template or a synthesized box, never the object the user asked for. This
    deterministic guard catches that even when the vision model wrongly reports
    a match, and forces the bounded CAD retry (with the reviser) to add real
    geometry. A holder request is exempt: the built-in template is then correct.
    """
    specification = state.get("specification")
    if not isinstance(specification, dict):
        return None
    if specification.get("primitives"):
        return None
    if _looks_like_holder_request(str(state.get("prompt") or "")):
        return None
    return (
        "the specification has no 'primitives', so the CAD backend can only render "
        "the built-in holder template or a plain box instead of the requested object; "
        "add primitives that build the real shape (use a single 'extrude' with a "
        "polygon 'profile' for a flat 2D shape such as a stamp or cutter)"
    )


def _review_prompt(provider: LLMProvider, state: WorkflowState) -> str:
    """Build the reviewer user prompt from the request and the specification.

    ``reference_prompt_for`` keeps the optional reference note/photo behaviour
    identical to the Planner/Editor: the note still reaches the reviewer and the
    "photo is attached" hint is only added when the provider can actually see.
    """
    request = reference_prompt_for(provider, state)
    specification = state.get("specification") or {}
    return (
        f"User request:\n{request}\n\n"
        f"Specification (JSON):\n"
        f"{json.dumps(specification, ensure_ascii=False, sort_keys=True)}\n\n"
        "The attached image is a rendered preview of the generated part. "
        "Compare it with the request and the specification above and report "
        "every concrete mismatch you can see."
    )


def _skipped(state: WorkflowState, reason: str, history_entry: str) -> dict[str, Any]:
    """Build the partial-state update for a skipped (non-fatal) review."""
    return {
        "vision_review": {"skipped": True, "reason": reason},
        "vision_used": False,
        "history": append_history(state, history_entry),
    }


def make_review_node(
    provider: LLMProvider,
    *,
    render_preview: Callable[..., bytes] = render_stl_preview,
) -> Callable[[WorkflowState], dict[str, Any]]:
    """Build the ``review`` graph node bound to *provider*.

    Args:
        provider: The injected LLM provider (never a concrete backend). Vision
            support is queried through :meth:`LLMProvider.supports_vision`.
        render_preview: ``bytes(stl) -> PNG bytes`` renderer, defaulting to the
            headless :func:`designs.cad.preview.render_stl_preview`. Injected so
            tests never need a real mesh or the trimesh/Pillow stack.

    The returned callable is a LangGraph node: it takes the running state and
    returns a partial state update. It never raises for a review-level problem.
    """

    def review_node(state: WorkflowState) -> dict[str, Any]:
        stl_bytes = state.get("stl_bytes") or b""
        if not stl_bytes:
            # Nothing was meshed: there is nothing to look at, but the run is
            # not failed -- some callers legitimately stop before the export.
            return _skipped(state, "no_stl", "review: skipped (no STL to preview)")

        if not provider.supports_vision():
            # Skip *before* rendering: a text-only provider must not pay for a
            # preview it cannot judge. The status stays ``"done"``.
            return _skipped(
                state, "no_vision", "review: skipped (the selected model has no vision)"
            )

        try:
            preview = render_preview(stl_bytes)
        except Exception as exc:  # noqa: BLE001 - a broken renderer must not fail the run
            logger.warning("STL preview rendering failed: %s", exc)
            return _skipped(
                state,
                "render_failed",
                f"review: preview render failed ({type(exc).__name__}); skipped",
            )

        review_prompt = _review_prompt(provider, state)
        try:
            payload = provider.structured(
                review_prompt,
                ReviewResult,
                images=[preview],
                system=REVIEW_SYSTEM_PROMPT,
            )
            review = ReviewResult.model_validate(payload)
        except (LLMError, ValidationError) as exc:
            # Vision is optional: a failing call is a warning, not a failure.
            logger.warning("Vision review call failed, skipping: %s", exc)
            return _skipped(
                state,
                "llm_error",
                f"review: vision call failed ({type(exc).__name__}); skipped",
            )

        # Deterministic backstop: a weak vision model (e.g. llava:7b) frequently
        # rubber-stamps a fallback part as a match. When the specification has no
        # real geometry and the request is not a holder, trust the guard over the
        # model so the bounded retry adds the missing primitives.
        guard_issue = geometry_missing_issue(state)
        guard_override = bool(review.matches and guard_issue)
        if guard_override:
            review = ReviewResult(matches=False, issues=[guard_issue], summary=guard_issue)

        attempt = int(state.get("attempt", 0))
        max_attempts = int(state.get("max_attempts", 0))
        update: dict[str, Any] = {
            "preview_image": preview,
            "vision_used": True,
            "vision_review": review.model_dump(),
            # Raw input/output for the UI (docs/vision-self-check.md 5.): the
            # literal prompt the reviewer received and the model's raw JSON
            # answer, so "what it saw and thought" is auditable. JSON-safe only.
            "vision_trace": {
                "system": REVIEW_SYSTEM_PROMPT,
                "prompt": review_prompt,
                "response": payload if isinstance(payload, dict) else {},
                "guard_override": guard_override,
            },
        }

        if review.matches:
            update["status"] = "done"
            update["history"] = append_history(
                state, f"review: preview matches the request (attempt {attempt})"
            )
            return update

        issues = [str(issue) for issue in review.issues]
        if attempt < max_attempts:
            # Hand the concrete issues to the CAD node's reviser through the
            # same ``validation.errors`` channel the Validator uses.
            validation = dict(state.get("validation") or {})
            validation["errors"] = [*(validation.get("errors") or []), *issues]
            update["validation"] = validation
            update["status"] = "retry"
            update["history"] = append_history(
                state,
                f"review: mismatch, retrying (attempt {attempt} of {max_attempts})",
            )
            return update

        # Bounded: no attempts left, so the review can only warn. The valid
        # model stays and the run finishes ``done``.
        warning = "; ".join(issues) or review.summary or "no details"
        update["status"] = "done"
        update["history"] = append_history(
            state,
            f"review: mismatch after {attempt} attempt(s); {warning}",
        )
        return update

    return review_node
