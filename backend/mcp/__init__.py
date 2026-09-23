"""PrintForge in-process MCP Django app.

The app package is named ``mcp``, which collides with the officially installed
``mcp`` Python SDK (terv.md 25. fejezet). Django must resolve ``mcp`` to *this*
package (``mcp.apps``, ``mcp.tools`` ...), so we keep the name and additionally
expose the SDK's submodules as ``mcp.<submodule>`` by appending the SDK's own
package directory to this package's search path.

``mcp.server`` is the one submodule name that exists in both packages; this app
ships its own :mod:`mcp.server` transport module, which takes precedence. That
module then re-exposes the SDK's ``mcp.server`` package contents so imports such
as ``from mcp.server import Server`` keep working (see :mod:`mcp.server`).

Only the transport needs the SDK; the rest of the app (registry, permissions)
imports nothing from it.
"""

from __future__ import annotations

import importlib.metadata
import pathlib
import sys

__all__ = ["sdk_root"]


def _discover_sdk_root() -> pathlib.Path | None:
    """Locate the installed ``mcp`` SDK package directory, if present.

    Uses the distribution metadata first (exact, independent of ``sys.path``
    ordering) and falls back to scanning ``sys.path`` for a directory that looks
    like the SDK (it must contain ``client`` and ``server/mcpserver``), which
    also distinguishes it from this local app package.
    """
    try:
        located = importlib.metadata.distribution("mcp").locate_file("mcp")
    except importlib.metadata.PackageNotFoundError:
        located = None
    if located is not None:
        candidate = pathlib.Path(str(located))
        if candidate.is_dir():
            return candidate

    for entry in sys.path:
        if not entry:
            continue
        candidate = pathlib.Path(entry) / "mcp"
        if (candidate / "client").is_dir() and (candidate / "server" / "mcpserver").is_dir():
            return candidate
    return None


_sdk_root_path = _discover_sdk_root()

# Extend this package's search path with the SDK so ``import mcp.types``,
# ``mcp.shared``, ``mcp.client`` ... resolve to the SDK. Local modules always win
# because this directory stays first in ``__path__``.
if _sdk_root_path is not None and str(_sdk_root_path) not in __path__:
    __path__.append(str(_sdk_root_path))


def sdk_root() -> pathlib.Path | None:
    """Return the installed MCP SDK package directory, or ``None`` if missing."""
    return _sdk_root_path
