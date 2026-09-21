"""Agent workflow package (terv.md 6. fejezet).

Public entry points::

    from agents.graph import WorkflowDeps, build_workflow, run_workflow

The nodes (Planner, Research, CAD, Validator) are built by dependency-injected
factories, so the graph is fully mockable and needs neither Ollama nor a live
OpenSCAD in tests.
"""

from __future__ import annotations

from .state import WorkflowState
from .workflow import WorkflowDeps, build_workflow, run_workflow

__all__ = [
    "WorkflowDeps",
    "WorkflowState",
    "build_workflow",
    "run_workflow",
]
