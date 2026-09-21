"""In-process MCP tool registry (Phase 1 skeleton).

The MCP server runs inside the Django process and calls ``services.py``
directly -- not HTTP, not shell (see terv.md 25. fejezet). The transport is
added in a later phase; this registry is the single source of truth for the
tools that will be exposed.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Callable


_REGISTRY: dict[str, Tool] = {}


def register(name: str, description: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        if name in _REGISTRY:
            raise ValueError(f"MCP tool already registered: {name}")
        _REGISTRY[name] = Tool(name=name, description=description, handler=func)
        return func

    return decorator


def all_tools() -> dict[str, Tool]:
    return dict(_REGISTRY)


def call(tool_name: str, /, **kwargs):
    # The tool identifier is positional-only so it never collides with a tool
    # parameter literally named ``name`` (e.g. ``create_workspace``).
    #
    # TODO(phase-7): enforce the same workspace-role permissions here as for the
    # web user (terv.md 20., 25. fejezet) before dispatching to the handler.
    # This is the single chokepoint every MCP tool must flow through; tools may
    # not opt out of it (`allow_shell` stays forbidden).
    if tool_name not in _REGISTRY:
        raise KeyError(f"Unknown MCP tool: {tool_name}")
    return _REGISTRY[tool_name].handler(**kwargs)
