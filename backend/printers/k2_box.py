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
  / ``brand`` / ``present`` / ``loaded`` / ``external``). This is the preferred
  shape and is parsed first.
* the stock Creality schema -- per-unit ``T1``..``Tn`` objects, each holding
  four parallel per-slot arrays (``material_type`` / ``color_value`` /
  ``vender``) alongside unit telemetry (``state``, ``temperature``,
  ``dry_and_humidity``, ``filament``, ``remain_len``, ...). It carries no
  ``slots`` list. This module now parses that shape as a best-effort fallback.

The stock mapping is deliberately conservative: ``material_type`` is a Creality
material *code* (``"101001"``), not a human name, and ``vender`` is raw RFID
vendor data, so both are carried through verbatim rather than decoded -- the
community code tables disagree between sources. ``name`` is left empty because
the stock schema has no per-slot spool name, and ``state`` is the owning unit's
connection state (there is no confirmed per-slot state field).

When neither shape is parseable the function returns ``[]`` and the K2
transport falls back to the proprietary WebSocket reader in
:mod:`printers.k2_websocket`.

Because the schema is firmware-dependent, :func:`slots_from_box_object` is
deliberately defensive: any unexpected shape yields ``[]`` instead of raising.
A printer without a CFS must never break the caller, and the caller must be
able to tell "no CFS" apart from "the query failed" (the latter is an exception
raised by the Moonraker client, not by this parser).
"""

from __future__ import annotations

import re
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

#: Stock-schema CFS unit key: ``T1``..``Tn`` (case-insensitive).
_UNIT_KEY = re.compile(r"^T(\d+)$", re.IGNORECASE)

#: Number of bays a single stock CFS unit exposes. Used to derive a flat,
#: collision-free ``index`` across units (``(unit - 1) * 4 + bay``) -- the same
#: numbering the WebSocket parser uses -- and to bound a malformed array.
_SLOTS_PER_UNIT = 4

#: Stock-schema per-unit brand arrays. The firmware misspells the vendor field
#: as ``vender``; both spellings are accepted.
_STOCK_BRAND_KEYS = ("vendor", "vender")


def slots_from_box_object(payload: Any) -> list[CfsSlot]:
    """Map a Moonraker ``[box]`` object-query payload to CFS slots.

    ``payload`` may be the full ``/printer/objects/query?box`` response
    (``{"result": {"status": {"box": {...}}}}``), just its ``result``
    (``{"status": {"box": {...}}}}``) or the ``box`` object itself.

    The community "flat" schema (``box["slots"]``) is tried first; when it
    yields no slots the stock Creality schema (per-unit ``T1``..``Tn`` with
    parallel arrays) is parsed as a fallback. Both branches are defensive: a
    missing ``box``, a malformed slot or an unknown shape degrades to ``[]`` and
    the function never raises.

    Entries flagged ``external: true`` (the external spool holder) are skipped,
    mirroring :func:`printers.k2_websocket.parse_boxs_info`, which drops the
    spool holder because :class:`~printers.base.CfsSlot` describes a CFS bay.

    Returns the slots sorted by ``index``.
    """
    box = _extract_box(payload)
    if not isinstance(box, dict):
        return []

    slots = _flat_slots(box)
    if slots:
        return slots
    return _stock_slots(box)


def _flat_slots(box: dict[str, Any]) -> list[CfsSlot]:
    """Parse the community flat schema (``box["slots"]``), or ``[]``."""
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


def _stock_slots(box: dict[str, Any]) -> list[CfsSlot]:
    """Parse the stock Creality schema (per-unit ``T1``..``Tn``), or ``[]``.

    Units are visited in numeric order so the derived indices are stable even
    when the payload's key order is arbitrary. Unknown keys and non-object
    units are ignored.
    """
    units: list[tuple[int, dict[str, Any]]] = []
    for key, value in box.items():
        number = _unit_number(key)
        if number is None or not isinstance(value, dict):
            continue
        units.append((number, value))
    units.sort(key=lambda item: item[0])

    slots: list[CfsSlot] = []
    for number, unit in units:
        slots.extend(_slots_from_unit(unit, number))
    slots.sort(key=lambda slot: slot.index)
    return slots


def _slots_from_unit(unit: dict[str, Any], number: int) -> list[CfsSlot]:
    """Expand one stock unit's parallel arrays into up to four CFS slots."""
    materials = unit.get("material_type")
    colors = unit.get("color_value")
    brands = _first_list(unit, _STOCK_BRAND_KEYS)

    count = min(
        _SLOTS_PER_UNIT,
        max(_list_length(materials), _list_length(colors), _list_length(brands)),
    )
    if count <= 0:
        return []

    unit_state = unit.get("state")
    slots: list[CfsSlot] = []
    for position in range(count):
        material = _clean_text(_at(materials, position))
        color = _clean_text(_at(colors, position))
        brand = _clean_text(_at(brands, position))
        slots.append(
            CfsSlot(
                index=(number - 1) * _SLOTS_PER_UNIT + position,
                material=material,
                brand=brand,
                name="",
                color=color,
                state=_slot_state(unit_state, position),
                empty=not (material or color or brand),
            )
        )
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
    # ...or the box object itself, in either schema.
    if "slots" in payload or _has_unit_keys(payload):
        return payload
    return None


def _has_unit_keys(mapping: dict[str, Any]) -> bool:
    """Whether ``mapping`` looks like a stock box (has a ``T<n>`` key)."""
    return any(_unit_number(key) is not None for key in mapping)


def _unit_number(key: Any) -> int | None:
    """Return the positive unit number from a ``T<n>`` key, else ``None``."""
    if not isinstance(key, str):
        return None
    match = _UNIT_KEY.match(key.strip())
    if match is None:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def _first_list(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first list-valued field among ``keys``, else ``None``."""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, list):
            return value
    return None


def _list_length(value: Any) -> int:
    """Length of a list, or ``0`` for anything else."""
    return len(value) if isinstance(value, list) else 0


def _at(value: Any, position: int) -> Any:
    """Return ``value[position]`` for a list, else ``None`` (out-of-range safe)."""
    if isinstance(value, list) and 0 <= position < len(value):
        return value[position]
    return None


def _slot_state(unit_state: Any, position: int) -> str:
    """Slot state: a per-slot array element when given, else the unit state."""
    if isinstance(unit_state, list):
        return _clean_state(_at(unit_state, position))
    return _clean_state(unit_state)


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
