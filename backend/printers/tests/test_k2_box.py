"""Tests for the Moonraker ``[box]`` -> CFS slot parser -- no network.

The box payload shape is firmware-dependent, so the parser is exercised against
the full envelope, the bare result, the bare box object and a pile of malformed
shapes. Two schemas are covered: the community "flat" ``slots[]`` list (tried
first) and the stock Creality per-unit ``T1``..``Tn`` parallel arrays (the
fallback). The contract is: map what is there, never raise, and return ``[]``
when nothing is parseable.
"""

from __future__ import annotations

import pytest

from printers.base import CfsSlot
from printers.k2_box import slots_from_box_object

# A representative flat-schema box: two CFS bays, one empty bay and the external
# spool holder (which must be skipped).
BOX_RESPONSE = {
    "result": {
        "status": {
            "box": {
                "api_version": 1,
                "loaded_slot": 0,
                "slot_filament_mask": 15,
                "slots": [
                    {
                        "index": 0,
                        "name": "Hyper PLA",
                        "material": "PLA",
                        "color": "#1a1a1a",
                        "brand": "Creality",
                        "present": True,
                        "loaded": True,
                    },
                    {
                        "index": 1,
                        "name": "PETG",
                        "material": "PETG",
                        "color": "#00ff00",
                        "brand": "None",
                        "present": False,
                        "loaded": False,
                    },
                    {
                        "index": 2,
                        "material": "",
                        "brand": "unknown",
                        "name": "None",
                        "color": "none",
                    },
                    {
                        "index": 3,
                        "external": True,
                        "present": True,
                        "material": "ABS",
                        "brand": "eSun",
                    },
                ],
            }
        }
    }
}


# ---------------------------------------------------------------------------
# Happy path / field mapping
# ---------------------------------------------------------------------------


def test_maps_flat_slots_and_skips_the_external_holder():
    slots = slots_from_box_object(BOX_RESPONSE)

    assert [slot.index for slot in slots] == [0, 1, 2]  # external holder dropped
    assert slots[0] == CfsSlot(
        index=0,
        material="PLA",
        brand="Creality",
        name="Hyper PLA",
        color="#1a1a1a",
        state="",
        empty=False,
    )
    # "None"/"unknown" sentinels collapse to empty strings.
    assert slots[1].brand == ""
    assert slots[1].empty is True
    assert slots[2].material == ""
    assert slots[2].name == ""
    assert slots[2].empty is True


@pytest.mark.parametrize(
    "payload",
    [
        {"status": {"box": {"slots": [{"index": 0, "material": "PLA"}]}}},  # bare result
        {"slots": [{"index": 0, "material": "PLA"}]},  # bare box object
        {"result": {"status": {"box": {"slots": [{"index": 0, "material": "PLA"}]}}}},  # envelope
    ],
)
def test_accepts_envelope_result_and_box_shapes(payload):
    slots = slots_from_box_object(payload)
    assert [slot.index for slot in slots] == [0]
    assert slots[0].material == "PLA"


def test_uses_list_position_when_index_is_missing_or_invalid():
    payload = {
        "status": {
            "box": {
                "slots": [
                    {"material": "PLA"},
                    {"index": "not-a-number", "material": "PETG"},
                    {"index": "5", "material": "ABS"},
                ]
            }
        }
    }

    slots = slots_from_box_object(payload)

    assert [slot.index for slot in slots] == [0, 1, 5]


def test_sorts_slots_by_index():
    payload = {
        "status": {
            "box": {
                "slots": [
                    {"index": 3, "material": "D"},
                    {"index": 0, "material": "A"},
                    {"index": 2, "material": "C"},
                ]
            }
        }
    }

    assert [slot.index for slot in slots_from_box_object(payload)] == [0, 2, 3]


@pytest.mark.parametrize(
    ("raw", "expected_empty"),
    [
        ({"loaded": True}, False),
        ({"loaded": False}, True),
        ({"present": True}, False),
        ({"present": False}, True),
        ({"state": "loaded"}, False),
        ({"state": "empty"}, True),
        ({"state": 1}, False),
        ({"state": 0}, True),
        ({"material": "PLA"}, False),  # inferred from a meaningful material
        ({"name": "None", "brand": "unknown"}, True),  # all sentinels -> empty
    ],
)
def test_presence_signals_drive_empty(raw, expected_empty):
    payload = {"status": {"box": {"slots": [raw]}}}
    assert slots_from_box_object(payload)[0].empty is expected_empty


def test_state_is_carried_through_as_reported():
    payload = {"status": {"box": {"slots": [{"index": 0, "state": "loaded"}]}}}
    assert slots_from_box_object(payload)[0].state == "loaded"


def test_non_dict_slots_are_skipped():
    payload = {
        "status": {
            "box": {
                "slots": [
                    "not a dict",
                    {"index": 1, "material": "PLA"},
                    None,
                ]
            }
        }
    }

    slots = slots_from_box_object(payload)

    assert [slot.index for slot in slots] == [1]


# ---------------------------------------------------------------------------
# Defensive: never raise, [] on unexpected shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "not a payload",
        42,
        {},
        {"result": "nope"},
        {"result": None},
        {"result": {}},
        {"result": {"status": None}},
        {"result": {"status": {}}},
        {"result": {"status": {"box": None}}},
        {"result": {"status": {"box": []}}},
        {"result": {"status": {"box": {}}}},
        {"result": {"status": {"box": {"slots": "nope"}}}},
        {"result": {"status": {"box": {"slots": {}}}}},
    ],
)
def test_unexpected_shapes_yield_an_empty_list(payload):
    assert slots_from_box_object(payload) == []


def test_empty_slots_list_yields_an_empty_list():
    assert slots_from_box_object({"status": {"box": {"slots": []}}}) == []


def test_never_raises_on_arbitrary_slot_values():
    payload = {
        "status": {
            "box": {
                "slots": [
                    {
                        "index": [],
                        "material": {"nested": True},
                        "brand": 3.14,
                        "name": None,
                        "color": ["#fff"],
                        "present": "maybe",
                    }
                ]
            }
        }
    }

    slots = slots_from_box_object(payload)

    assert len(slots) == 1
    assert slots[0].index == 0  # unparseable index -> position
    assert slots[0].empty is True  # no authoritative signal and no usable text


# ---------------------------------------------------------------------------
# Stock Creality schema -- per-unit T1..Tn with parallel arrays
# ---------------------------------------------------------------------------

# A representative stock box: two connected units (T1/T2), each with four
# parallel arrays. Unit T2's bays are all empty. ``vender`` is the firmware's
# misspelling of "vendor"; sentinels ("none"/"unknown"/"-1") mean no spool.
STOCK_BOX_RESPONSE = {
    "result": {
        "status": {
            "box": {
                "filament": "None",
                "auto_refill": 1,
                "map": {"T1A": "T1A", "T1B": "T1B"},
                "same_material": [["101001", "0000000", ["T1A"], "PLA"]],
                "T1": {
                    "state": "connect",
                    "filament": "None",
                    "temperature": "27",
                    "dry_and_humidity": "48",
                    "vender": ["Creality", "unknown", "none", "-1"],
                    "remain_len": ["35", "57", "52", "0"],
                    "color_value": ["0000000", "0FFFFFF", "none", "-1"],
                    "material_type": ["101001", "106002", "unknown", "-1"],
                },
                "T2": {
                    "state": "None",
                    "vender": ["none", "none", "none", "none"],
                    "color_value": ["none", "none", "none", "none"],
                    "material_type": ["none", "none", "none", "none"],
                },
            }
        }
    }
}


def test_maps_stock_units_with_parallel_arrays():
    slots = slots_from_box_object(STOCK_BOX_RESPONSE)

    # Two units x four bays, flat indices like the WebSocket parser: 0..7.
    assert [slot.index for slot in slots] == [0, 1, 2, 3, 4, 5, 6, 7]

    # T1A: populated, all three fields reported verbatim (no code decoding).
    assert slots[0] == CfsSlot(
        index=0,
        material="101001",
        brand="Creality",
        name="",
        color="0000000",
        state="connect",
        empty=False,
    )
    # T1B: material present, vendor sentinel -> brand collapses, still loaded.
    assert slots[1].material == "106002"
    assert slots[1].brand == ""
    assert slots[1].color == "0FFFFFF"
    assert slots[1].empty is False
    # T1C/T1D: sentinels everywhere -> empty (state still carried).
    assert slots[2].empty is True
    assert slots[2].state == "connect"
    assert slots[3].empty is True
    # T2: unit state "None" collapses to "", and every bay is empty.
    assert [slot.index for slot in slots[4:]] == [4, 5, 6, 7]
    assert all(slot.empty for slot in slots[4:])
    assert all(slot.state == "" for slot in slots[4:])


@pytest.mark.parametrize(
    ("box", "expected"),
    [
        # A lone material array (the shape pinned by the transport seam tests).
        ({"T1": {"material_type": ["PLA"]}}, [(0, "PLA", "", "", False)]),
        # Color only: the material name is unknown but a spool is present.
        ({"T1": {"color_value": ["0C12E1F"]}}, [(0, "", "", "0C12E1F", False)]),
        # Unit number drives the base index; the bay position offsets it.
        ({"T3": {"material_type": ["ABS"]}}, [(8, "ABS", "", "", False)]),
        # The correctly-spelled "vendor" is accepted as well as "vender".
        (
            {"T1": {"vendor": ["Prusament"], "color_value": ["0FFFFFF"]}},
            [(0, "", "Prusament", "0FFFFFF", False)],
        ),
    ],
)
def test_stock_partial_units_map_positionally(box, expected):
    slots = slots_from_box_object({"status": {"box": box}})

    assert [(s.index, s.material, s.brand, s.color, s.empty) for s in slots] == expected


def test_stock_schema_accepts_the_bare_box_and_bare_result_shapes():
    bare_box = {"T1": {"material_type": ["PLA"]}, "T2": {"material_type": ["PETG"]}}
    bare_result = {"status": {"box": bare_box}}

    for payload in (bare_box, bare_result):
        slots = slots_from_box_object(payload)
        assert [slot.material for slot in slots] == ["PLA", "PETG"]


def test_stock_per_slot_state_array_is_used_when_present():
    box = {"T1": {"material_type": ["PLA", "PETG"], "state": ["loaded", "empty"]}}

    slots = slots_from_box_object({"status": {"box": box}})

    assert [slot.state for slot in slots] == ["loaded", "empty"]


def test_stock_arrays_longer_than_four_are_capped_per_unit():
    box = {
        "T1": {"material_type": [f"M{index}" for index in range(6)]},
        "T2": {"material_type": ["PETG"]},
    }

    slots = slots_from_box_object({"status": {"box": box}})

    # T1 is clamped to its four bays so T2 cannot collide at index 4.
    assert [slot.index for slot in slots] == [0, 1, 2, 3, 4]
    assert slots[4].material == "PETG"


def test_flat_schema_still_wins_when_both_shapes_are_present():
    box = {
        "slots": [{"index": 7, "material": "FLAT", "present": True}],
        "T1": {"material_type": ["STOCK"]},
    }

    slots = slots_from_box_object({"status": {"box": box}})

    assert len(slots) == 1
    assert slots[0].index == 7
    assert slots[0].material == "FLAT"


def test_empty_flat_slots_fall_through_to_the_stock_schema():
    box = {"slots": [], "T1": {"material_type": ["STOCK"]}}

    slots = slots_from_box_object({"status": {"box": box}})

    assert [slot.material for slot in slots] == ["STOCK"]


@pytest.mark.parametrize(
    "box",
    [
        {"T1": {}},
        {"T1": {"state": "connect"}},
        {"T1": {"material_type": "not-a-list"}},
        {"T1": {"material_type": {"nested": True}}},
        {"T1": {"material_type": []}},
        {"T1": "not-an-object"},
        {"T0": {"material_type": ["PLA"]}},  # unit numbers are 1-based
        {"T": {"material_type": ["PLA"]}},
        {"TA": {"material_type": ["PLA"]}},
        {"not-a-unit": {"material_type": ["PLA"]}},
    ],
)
def test_unparseable_stock_units_yield_no_slots(box):
    assert slots_from_box_object({"status": {"box": box}}) == []


def test_stock_never_raises_on_arbitrary_array_values():
    box = {
        "T1": {
            "material_type": [{"nested": True}, None, 3.14, ""],
            "color_value": [["#fff"], "none", "-1", "unknown"],
            "vender": [42, "unknown", None, "none"],
            "state": {"not": "scalar"},
        }
    }

    slots = slots_from_box_object({"status": {"box": box}})

    assert [slot.index for slot in slots] == [0, 1, 2, 3]
    assert all(slot.empty for slot in slots)
    assert all(slot.state == "" for slot in slots)
