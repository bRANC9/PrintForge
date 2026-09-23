"""Validator agent (terv.md 6., 7. fejezet).

Responsibilities:

1. ``CADBackend.validate`` -- manifold / self-intersection / closed mesh /
   minimum wall thickness / minimum feature size / holes / printability;
2. on success, export the STL via ``CADBackend.export(model, "stl")`` -- this is
   the only place a mesh is produced in the workflow;
3. on failure, return a **structured** error and set ``status = "retry"`` so the
   bounded retry edge sends control back to the CAD node.

The retry bound is enforced here: after ``max_attempts`` the status becomes
``"failed"`` and the workflow ends -- it can never loop forever (terv.md 6.).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from designs.cad.base import CADBackend, GeneratedModel

from .skills import skill_constraint_errors
from .state import WorkflowState, append_history, structured_error

__all__ = ["make_validate_node"]


def make_validate_node(
    *,
    cad_backend: CADBackend,
) -> Callable[[WorkflowState], dict[str, Any]]:
    """Build the ``validate`` graph node bound to *cad_backend*."""

    def validate_node(state: WorkflowState) -> dict[str, Any]:
        specification = dict(state.get("specification") or {})
        scad_source = str(state.get("scad_source") or "")
        attempt = int(state.get("attempt", 0))
        max_attempts = int(state.get("max_attempts", 1))

        model = GeneratedModel(specification=specification, scad_source=scad_source)

        problems: list[str] = []
        try:
            problems = [str(problem) for problem in cad_backend.validate(model)]
        except Exception as exc:  # noqa: BLE001 - surface any backend bug as a problem
            problems = [f"{type(exc).__name__}: {exc}"]

        # Skills are enforced, not merely prompted (docs/skills.md 4.): every
        # active skill's constraints_json becomes a blocking problem through the
        # same channel as the backend's own validation errors.
        constraint_problems, warnings = skill_constraint_errors(
            specification,
            state.get("skills"),
        )
        problems = [*problems, *constraint_problems]

        stl_bytes: bytes | None = None
        if not problems:
            try:
                # The one and only place a mesh is created: the CAD backend
                # exports the validated source to STL.
                stl_bytes = cad_backend.export(model, "stl")
            except Exception as exc:  # noqa: BLE001 - an export failure is a CAD problem
                problems = [f"{type(exc).__name__}: {exc}"]

        if not problems and stl_bytes is not None:
            return {
                "stl_bytes": stl_bytes,
                "validation": {
                    "status": "valid",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "errors": [],
                    "warnings": warnings,
                },
                "status": "done",
                "error": None,
                "history": append_history(state, f"validator: valid (attempt {attempt})"),
            }

        exhausted = attempt >= max_attempts
        validation = {
            "status": "failed" if exhausted else "invalid",
            "attempt": attempt,
            "max_attempts": max_attempts,
            "errors": problems,
            "warnings": warnings,
        }

        if exhausted:
            return {
                "validation": validation,
                "status": "failed",
                "error": structured_error(
                    stage="validator",
                    error_type="ValidationError",
                    message=(
                        f"Model still invalid after {attempt} attempt(s) (max {max_attempts})."
                    ),
                    details=problems,
                ),
                "history": append_history(state, f"validator: failed after {attempt} attempt(s)"),
            }

        return {
            "validation": validation,
            "status": "retry",
            "error": None,
            "history": append_history(state, f"validator: invalid, retrying (attempt {attempt})"),
        }

    return validate_node
