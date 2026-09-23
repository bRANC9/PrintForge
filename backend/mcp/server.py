"""MCP transport: expose the in-process tool registry over the official MCP SDK.

The registry in :mod:`mcp.tools` is the single source of truth for the tools
(terv.md 25. fejezet). This module builds an :class:`~mcp.server.mcpserver.MCPServer`
from :func:`mcp.tools.all_tools` and, for every registered :class:`~mcp.tools.Tool`,
registers an MCP tool whose name/description match and whose handler funnels
straight back through :func:`mcp.tools.call`. That keeps the ``authorize`` gate
(and the prohibition on shell access) on the single chokepoint, so MCP callers
obey exactly the same workspace-role rules as the web user.

Identity
--------
MCP has no Django request and therefore no ``request.user``. The transport
resolves one explicit principal from ``MCP_SERVICE_USER_ID`` (a Django setting
or environment variable) and injects it as the ``user_id`` / ``owner_id``
argument of every tool that needs one. Any client-supplied value for those
parameters is dropped: a caller cannot impersonate another user, and the
workspace-role gate still runs for the configured principal.

Package-name note
-----------------
This Django app is itself named ``mcp`` and shadows the installed SDK of the
same name. :mod:`mcp` (the package ``__init__``) appends the SDK directory to the
package search path, and this module acts as the SDK's ``mcp.server`` package
shim: it gives itself the SDK server directory as ``__path__`` and re-exports the
SDK server names, so ``import mcp.server.mcpserver`` and
``from mcp.server import Server`` both resolve to the SDK.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import logging
import os
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from . import sdk_root as _discover_sdk_root
from .tools import Tool, all_tools, call

logger = logging.getLogger(__name__)

__all__ = [
    "CacheHint",
    "IDENTITY_PARAMETERS",
    "IDENTITY_SETTING",
    "InitializationOptions",
    "MCPServer",
    "NotificationOptions",
    "Server",
    "ServerRequestContext",
    "build_server",
    "make_tool_wrapper",
    "resolve_identity_user_id",
]

# ---------------------------------------------------------------------------
# SDK bridge (see module docstring)
# ---------------------------------------------------------------------------

_SDK_ROOT = _discover_sdk_root()
if _SDK_ROOT is None:  # pragma: no cover - environment misconfiguration
    raise ImportError("The 'mcp' Python SDK is not installed; the MCP transport cannot be built.")

# ``__path__`` turns this module into the SDK's ``mcp.server`` package for the
# import system, so ``mcp.server.<submodule>`` (mcpserver, stdio, lowlevel, ...)
# loads from the SDK while this file still wins for the bare ``mcp.server`` name.
__path__ = [str(_SDK_ROOT / "server")]  # noqa: F821 - special module attribute

_caching_mod = importlib.import_module("mcp.server.caching")
_context_mod = importlib.import_module("mcp.server.context")
_lowlevel_mod = importlib.import_module("mcp.server.lowlevel")
_mcpserver_mod = importlib.import_module("mcp.server.mcpserver")
_models_mod = importlib.import_module("mcp.server.models")

# Re-export the SDK's ``mcp.server`` package surface (matches the SDK's own
# ``mcp/server/__init__.py``) so SDK modules doing ``from mcp.server import ...``
# keep working with this shim in place.
CacheHint = _caching_mod.CacheHint
ServerRequestContext = _context_mod.ServerRequestContext
Server = _lowlevel_mod.Server
NotificationOptions = _lowlevel_mod.NotificationOptions
MCPServer = _mcpserver_mod.MCPServer
InitializationOptions = _models_mod.InitializationOptions


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

#: Tool arguments that carry the acting user. The transport always overwrites
#: them with the configured principal; client-supplied values are ignored.
IDENTITY_PARAMETERS: frozenset[str] = frozenset({"user_id", "owner_id"})

#: Setting / environment variable holding the service user's numeric id.
IDENTITY_SETTING = "MCP_SERVICE_USER_ID"

#: Runtime-editable setting name in ``configuration.services`` (terv.md 19.).
_IDENTITY_RUNTIME_SETTING = "mcp_service_user_id"


def _identity_from_runtime_settings() -> Any:
    """Read the identity through ``configuration.services.get_setting``.

    The import is deferred (lazy) so this transport stays importable before the
    configuration app is ready. ``get_setting`` already resolves the DB override
    first, then the Django setting, then the environment variable, so a runtime
    override takes effect without a restart. If the settings store cannot be
    reached -- no database/cache, or the app is not installed -- this returns
    ``None`` so :func:`resolve_identity_user_id` can fall back to reading the
    Django setting / environment directly.
    """
    try:
        from configuration.services import get_setting
    except Exception:  # noqa: BLE001 - optional app / import-time misconfiguration
        logger.debug("configuration.services unavailable; using settings/env", exc_info=True)
        return None
    try:
        return get_setting(_IDENTITY_RUNTIME_SETTING)
    except Exception:  # noqa: BLE001 - settings store unreachable: use settings/env
        logger.debug(
            "could not read %s via configuration.services; using settings/env",
            _IDENTITY_RUNTIME_SETTING,
            exc_info=True,
        )
        return None


def resolve_identity_user_id() -> int | None:
    """Return the configured MCP principal's user id, or ``None`` when unset.

    Resolution order:

    1. ``configuration.services.get_setting("mcp_service_user_id")`` -- the
       runtime/DB override, then the ``MCP_SERVICE_USER_ID`` Django setting,
       then the environment variable (terv.md 19. fejezet);
    2. the ``MCP_SERVICE_USER_ID`` Django setting;
    3. the ``MCP_SERVICE_USER_ID`` environment variable.

    A non-integer value raises
    :class:`~django.core.exceptions.ImproperlyConfigured` so a typo fails loudly
    instead of silently resolving to no identity.
    """
    configured = _identity_from_runtime_settings()
    if configured is None or configured == "":
        configured = getattr(settings, IDENTITY_SETTING, None)
    if configured is None or configured == "":
        configured = os.environ.get(IDENTITY_SETTING) or None
    if configured is None:
        return None
    try:
        return int(configured)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(
            f"{IDENTITY_SETTING} must be an integer user id, got {configured!r}."
        ) from exc


# ---------------------------------------------------------------------------
# Registry -> MCP tool bridge
# ---------------------------------------------------------------------------


def make_tool_wrapper(tool: Tool) -> Callable[..., Any]:
    """Build the MCP-facing callable for a registered :class:`Tool`.

    The wrapper exposes the handler's own signature (minus the identity
    parameters, which clients must not supply) so the SDK derives the JSON input
    schema from the real type hints. Calling it always goes through
    :func:`mcp.tools.call`, which enforces ``tool.authorize`` before the handler.
    """
    handler = tool.handler
    if inspect.iscoroutinefunction(handler):
        raise TypeError(
            f"MCP tool {tool.name!r} is async, but the registry dispatcher "
            "(mcp.tools.call) is synchronous; provide a sync handler."
        )

    signature = inspect.signature(handler, eval_str=True)
    identity_params = tuple(name for name in signature.parameters if name in IDENTITY_PARAMETERS)
    exposed_parameters = [
        parameter
        for name, parameter in signature.parameters.items()
        if name not in IDENTITY_PARAMETERS
    ]

    @functools.wraps(handler)
    def wrapper(**kwargs: Any) -> Any:
        # Never trust a client-supplied identity: drop it and inject the
        # configured principal instead.
        for name in IDENTITY_PARAMETERS:
            kwargs.pop(name, None)
        if identity_params:
            user_id = resolve_identity_user_id()
            if user_id is None:
                raise ImproperlyConfigured(
                    f"MCP tool {tool.name!r} needs an authenticated identity, but "
                    f"{IDENTITY_SETTING} (settings or environment) is not set. MCP has no "
                    "Django request/user; configure a service user so the workspace-role "
                    "gate can run."
                )
            for name in identity_params:
                kwargs[name] = user_id
        return call(tool.name, **kwargs)

    # ``inspect.signature`` prefers ``__signature__`` over the ``__wrapped__``
    # set by ``functools.wraps``, so the SDK sees the identity-free signature.
    wrapper.__signature__ = signature.replace(parameters=exposed_parameters)
    return wrapper


def build_server(
    *,
    name: str = "printforge",
    instructions: str | None = None,
) -> MCPServer:
    """Build an :class:`MCPServer` exposing every registered PrintForge tool."""
    server = MCPServer(name=name, instructions=instructions)
    for tool_name, tool in all_tools().items():
        server.add_tool(
            make_tool_wrapper(tool),
            name=tool_name,
            description=tool.description,
        )
    return server
