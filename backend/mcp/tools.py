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
    #: Optional permission gate, invoked with the same keyword arguments as the
    #: handler *before* it runs. It must raise (e.g.
    #: :class:`~django.core.exceptions.PermissionDenied`) when the caller is not
    #: allowed. ``None`` means "no workspace-role check" and is only used by the
    #: phase-1 skeleton tools.
    authorize: Callable | None = None


_REGISTRY: dict[str, Tool] = {}


def register(
    name: str,
    description: str,
    *,
    authorize: Callable | None = None,
) -> Callable:
    def decorator(func: Callable) -> Callable:
        if name in _REGISTRY:
            raise ValueError(f"MCP tool already registered: {name}")
        _REGISTRY[name] = Tool(
            name=name,
            description=description,
            handler=func,
            authorize=authorize,
        )
        return func

    return decorator


def all_tools() -> dict[str, Tool]:
    return dict(_REGISTRY)


def call(tool_name: str, /, **kwargs):
    # The tool identifier is positional-only so it never collides with a tool
    # parameter literally named ``name`` (e.g. ``create_workspace``).
    #
    # Single chokepoint every MCP tool must flow through (terv.md 20., 25.
    # fejezet): when a tool declares an ``authorize`` gate it is enforced here,
    # with the same workspace-role rules as the web user, *before* the handler
    # runs. Tools cannot opt out of their own gate and `allow_shell` stays
    # forbidden.
    tool = _REGISTRY.get(tool_name)
    if tool is None:
        raise KeyError(f"Unknown MCP tool: {tool_name}")
    if tool.authorize is not None:
        tool.authorize(**kwargs)
    return tool.handler(**kwargs)
