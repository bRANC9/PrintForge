"""Printer models land in Phase 6 (see terv.md 13-14. fejezet).

The core app must not depend on a specific printer protocol; concrete
backends (CrealityK2, Moonraker, OctoPrint, Bambu) will be adapters behind a
``PrinterBackend`` interface.
"""
