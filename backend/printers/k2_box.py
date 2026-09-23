"""CFS parsing from Moonraker's ``[box]`` Klipper object (terv.md 13. fejezet).

Reality check -- the K2 ``[box]`` object
----------------------------------------

The Creality K2 series runs Klipper with a stock Moonraker, and Creality's own
``[box]`` Klipper module exposes the Creality Filament System (CFS) state
through the standard object-query API::

    GET /printer/objects/query?box

This is the **primary** CFS source: it is the documented Klipper/Moonraker
surface rather than a bespoke socket, so it needs no reverse-engineered
framing. It is, however, still firmware-dependent -- the module ships two
payload shapes in the wild:

* the community "flat" schema -- a single ``box["slots"]`` list of
  self-describing slot objects (``index`` / ``name`` / ``material`` / ``color``
  / ``brand`` / ``present`` / ``loaded`` / ``external``). This is the shape
  this module maps.
* the stock Creality schema -- per-unit ``T1``..``T4`` objects holding parallel
  ``material_type`` / ``color_value`` / ``vendor`` arrays. That shape carries no
  ``slots`` list and is intentionally **not** parsed here: this module returns
  ``[]`` for it (the K2 transport then falls back to the proprietary WebSocket
  reader in :mod:`printers.k2_websocket`), rather than guessing.

Because the schema is firmware-dependent, :func:`slots_from_box_object` is
deliberately defensive: any unexpected shape yields ``[]`` instead of raising.
A printer without a CFS must never break the caller, and the caller must be
able to tell "no CFS" apart from "the query failed" (the latter is an exception
raised by the Moonraker client, not by this parser).
"""

from __future__ import annotations

from typing import Any

from .base import CfsSlot

__all__ = ["slots_from_box_object"]

#: Text values the box module uses as a "no filament" sentinel. Compared
#: case-insensitively after stripping whitespace.
_SENTINELS = frozenset({"", "none", "unknown", "null", "n/a", "na", "-1", "unset"})

#: Per-slot ``state`` strings that mean "no spool present".
_EMPTY_STATES = frozenset({"empty", "none", "absent", "unloaded", "0", "false", "no"})

#: Per-slot ``state`` strings that mean "a spool is present".
_LOADED_STATES = frozenset({"loaded", "present", "ready", "1", "true", "yes"})


def slots_from_box_object(payload: Any) -> list[CfsSlot]:
    """Map a Moonraker ``[box]`` object-query payload to CFS slots.

    ``payload`` may be the full ``/printer/objects/query?box`` response
    (``{"result": {"status": {"box": {...}}}}``), just its ``result``
    (``{"status": {"box": {...}}}}``) or the ``box`` object itself. The CFS
    state is read from ``box["slots"]`` (the community "flat" schema).

    The parser never raises: a missing ``box``, a missing/non-list ``slots``, a
    non-dict slot or a partial slot all degrade to ``[]``/defaults. Entries
    flagged ``external: true`` (the external spool holder) are skipped, mirroring
    :func:`printers.k2_websocket.parse_boxs_info`, which drops the spool holder
    because :class:`~printers.base.CfsSlot` describes a CFS bay.

    Returns the slots sorted by ``index``.
    """
    box = _extract_box(payload)
    if not isinstance(box, dict):
        return []

    raw_slots = box.get("slots")
    if not isinstance(raw_slots, list):
        return []

    slots: list[CfsSlot] = []
    for position, raw in enumerate(raw_slots):
        if not isinstance(raw, dict):
            continue
        if _as_bool(raw.get("external")):
            continue  # external spool holder, not a CFS bay
        slots.append(_slot_from_mapping(raw, position))
    slots.sort(key=lambda slot: slot.index)
    return slots


def _extract_box(payload: Any) -> Any:
    """Return the ``box`` object from a response/result/box payload, else ``None``."""
    if not isinstance(payload, dict):
        return None
    # Accept the full /printer/objects/query envelope...
    if isinstance(payload.get("result"), dict):
        payload = payload["result"]
    # ...or its bare result ({"status": {"box": ...}})...
    status = payload.get("status")
    if isinstance(status, dict) and "box" in status:
        return status["box"]
    # ...or the box object itself.
    if "slots" in payload:
        return payload
    return None


def _slot_from_mapping(raw: dict[str, Any], position: int) -> CfsSlot:
    """Build one :class:`CfsSlot` from a flat-schema slot mapping.

    ``index`` prefers the reported value and falls back to the slot's position
    in the list, so a sparse payload cannot produce a duplicate/absent index.
    """
    material = _clean_text(raw.get("material"))
    brand = _clean_text(raw.get("brand"))
    name = _clean_text(raw.get("name"))
    state_value = raw.get("state")
    if state_value is None:
        state_value = raw.get("status")
    return CfsSlot(
        index=_as_int(raw.get("index"), default=position),
        material=material,
        brand=brand,
        name=name,
        color=_clean_text(raw.get("color")),
        state=_clean_state(state_value),
        empty=_slot_is_empty(raw, material=material, name=name, brand=brand),
    )


def _slot_is_empty(raw: dict[str, Any], *, material: str, name: str, brand: str) -> bool:
    """Decide whether a flat-schema slot holds no spool.

    ``loaded``/``present`` are authoritative when present (the schema documents
    them as driving slot status). A numeric/string ``state`` is consulted next,
    and only as a last resort is presence inferred from a meaningful
    material/name/brand. This keeps a partial payload from being read as loaded.
    """
    for key in ("loaded", "present"):
        value = raw.get(key)
        if isinstance(value, bool):
            return not value

    state = raw.get("state")
    if isinstance(state, bool):
        return not state
    if isinstance(state, (int, float)):
        return state == 0
    if isinstance(state, str):
        token = state.strip().lower()
        if token in _EMPTY_STATES:
            return True
        if token in _LOADED_STATES:
            return False

    return not (material or name or brand)


def _clean_text(value: Any) -> str:
    """Return a trimmed string field, or ``""`` for anything that is not a string.

    Only strings are accepted: a number or a nested structure is never a material
    name/brand, and coercing it would make an empty bay look loaded. Sentinel
    values (``"None"``, ``"unknown"``, ...) also collapse to ``""``.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return "" if text.lower() in _SENTINELS else text


def _clean_state(value: Any) -> str:
    """Return a scalar state as text, or ``""`` when absent/non-scalar."""
    if value is None or isinstance(value, bool):
        return ""
    if not isinstance(value, (str, int, float)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in _SENTINELS else text


def _as_int(value: Any, *, default: int) -> int:
    """Return ``value`` as an int, or ``default`` when it is not a valid number."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    """Interpret a JSON-ish value as a boolean, defaulting to ``False``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False
