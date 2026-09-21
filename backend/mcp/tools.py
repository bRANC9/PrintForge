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


def call(name: str, **kwargs):
    if name not in _REGISTRY:
        raise KeyError(f"Unknown MCP tool: {name}")
    return _REGISTRY[name].handler(**kwargs)
