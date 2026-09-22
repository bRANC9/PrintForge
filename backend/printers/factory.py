"""Printer backend registry / factory (terv.md 13. fejezet).

The core resolves a backend by ``Printer.backend`` and never imports a concrete
adapter directly. Adding a printer brand is a registry entry plus an adapter
module -- the queue, API and MCP tools stay unchanged.
"""

from __future__ import annotations

from .base import PrinterBackend
from .creality_k2 import CrealityK2Backend
from .moonraker import MoonrakerBackend
from .octoprint import OctoPrintBackend
from .stubs import BambuBackend

__all__ = [
    "BACKEND_REGISTRY",
    "UnknownPrinterBackendError",
    "get_printer_backend",
    "register_backend",
    "supported_backends",
]


class UnknownPrinterBackendError(ValueError):
    """``Printer.backend`` is empty or not present in the registry."""


#: Registry key -> adapter class. Keys are matched case-insensitively after
#: stripping whitespace. Aliases (``k2``, ``creality``) are intentional.
BACKEND_REGISTRY: dict[str, type[PrinterBackend]] = {
    "creality_k2": CrealityK2Backend,
    "k2": CrealityK2Backend,
    "creality": CrealityK2Backend,
    "moonraker": MoonrakerBackend,
    "octoprint": OctoPrintBackend,
    "bambu": BambuBackend,
}


def register_backend(name: str, backend_cls: type[PrinterBackend]) -> None:
    """Register (or replace) an adapter under ``name``.

    Intended for tests and third-party adapters; the built-in keys are seeded in
    :data:`BACKEND_REGISTRY`.
    """
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("Backend name must not be empty")
    if not (isinstance(backend_cls, type) and issubclass(backend_cls, PrinterBackend)):
        raise TypeError("backend_cls must be a PrinterBackend subclass")
    BACKEND_REGISTRY[key] = backend_cls


def supported_backends() -> list[str]:
    """Sorted list of registry keys (for admin/UI and error messages)."""
    return sorted(BACKEND_REGISTRY)


def get_printer_backend(printer: object) -> PrinterBackend:
    """Instantiate the adapter configured on ``printer``.

    Raises :class:`UnknownPrinterBackendError` for a blank or unknown
    ``Printer.backend`` instead of silently falling back to a default.
    """
    key = (getattr(printer, "backend", "") or "").strip().lower()
    if not key:
        raise UnknownPrinterBackendError(
            "Printer has no backend set; supported backends: " + ", ".join(supported_backends())
        )
    backend_cls = BACKEND_REGISTRY.get(key)
    if backend_cls is None:
        raise UnknownPrinterBackendError(
            f"Unknown printer backend {key!r}; supported backends: "
            + ", ".join(supported_backends())
        )
    return backend_cls(printer)
