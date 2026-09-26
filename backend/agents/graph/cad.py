"""CAD agent (terv.md 6., 7. fejezet).

Input: the validated structured specification (plus, indirectly, whatever the
Research agent enriched). Output: **OpenSCAD source**.

Boundary (terv.md 7. fejezet): the CAD agent goes through
``CADBackend.generate(specification)`` and returns OpenSCAD source only. It
never builds a mesh/STL itself -- meshing is the OpenSCAD CLI's job and is
triggered by the Validator node through ``CADBackend.export``.

``generate`` can fail for a *fixable specification* problem (e.g. the LLM left a
``slot`` operation without its ``diameter``). While attempts remain the node does
not fail terminally: it stores the backend error on ``validation.errors`` and
sets ``status = "retry"`` so the next ``cad`` pass runs the optional
:data:`SpecReviser` and repairs the specification. Only the exhausted bound is
terminal (``failure_state``), so the loop stays bounded (terv.md 6. fejezet).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from designs.cad.base import CADBackend

from .state import WorkflowState, append_history, failure_state

__all__ = ["SpecReviser", "make_cad_node"]

#: Optional retry hook: given ``(spec, validation_errors, attempt)`` it may
#: return an adjusted specification for the next CAD attempt. ``None`` means
#: the same specification is retried unchanged.
SpecReviser = Callable[[dict[str, Any], list[str], int], dict[str, Any]]


def make_cad_node(
    *,
    cad_backend: CADBackend,
    reviser: SpecReviser | None = None,
) -> Callable[[WorkflowState], dict[str, Any]]:
    """Build the ``cad`` graph node bound to *cad_backend*.

    Args:
        cad_backend: Injected backend (``OpenSCADBackend`` in production, a
            fake in tests). ``generate`` returns OpenSCAD source.
        reviser: Optional hook used on a retry after a validation failure; it
            receives the structured validator errors so a smarter CAD agent can
            adjust the specification instead of re-emitting the same source.
    """

    def cad_node(state: WorkflowState) -> dict[str, Any]:
        attempt = int(state.get("attempt", 0)) + 1
        max_attempts = int(state.get("max_attempts", 1))
        specification = dict(state.get("specification") or {})

        validation = state.get("validation") or {}
        validation_errors = [str(error) for error in validation.get("errors") or []]
        llm_trace = list(state.get("llm_trace") or [])
        if reviser is not None and validation_errors:
            try:
                revised = reviser(specification, validation_errors, attempt)
            except Exception as exc:  # noqa: BLE001 - a broken reviser must not kill the run
                revised = None
                revision_note = f"reviser failed ({type(exc).__name__})"
            else:
                revision_note = "reviser applied" if revised else "reviser skipped"
            if isinstance(revised, dict) and revised:
                specification = revised
            # The LLM reviser records its exchange (prompt + answer) so the UI
            # can show the main generator's input/output too
            # (docs/vision-self-check.md 5.).
            exchange = getattr(reviser, "last_exchange", None)
            if isinstance(exchange, dict):
                llm_trace.append(exchange)
        else:
            revision_note = None

        try:
            # OpenSCAD source only -- never a mesh/STL.
            scad_source = cad_backend.generate(specification)
        except Exception as exc:  # noqa: BLE001 - a backend failure may be a fixable spec problem
            if attempt < max_attempts:
                # Not terminal yet: surface the backend error through the same
                # ``validation.errors`` channel the Validator uses so the next
                # ``cad`` pass runs the reviser with the concrete CAD complaint.
                # ``scad_source`` is deliberately NOT set here -- a previous
                # source (if any) is kept until a generation succeeds.
                message = f"{type(exc).__name__}: {exc}"
                return {
                    "specification": specification,
                    "llm_trace": llm_trace,
                    "validation": {
                        "status": "invalid",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "errors": [message],
                        "warnings": [],
                    },
                    "attempt": attempt,
                    "status": "retry",
                    "error": None,
                    "history": append_history(
                        state,
                        f"cad: generation failed, retrying (attempt {attempt}): {exc}",
                    ),
                }
            # Attempts exhausted: the generation failure is terminal.
            return failure_state(
                state,
                stage="cad",
                error_type=type(exc).__name__,
                message=str(exc),
            )

        entry = f"cad: generated OpenSCAD source (attempt {attempt})"
        if revision_note:
            entry = f"{entry}, {revision_note}"
        return {
            "specification": specification,
            "llm_trace": llm_trace,
            "scad_source": scad_source,
            "attempt": attempt,
            "status": "generated",
            "error": None,
            "history": append_history(state, entry),
        }

    return cad_node
