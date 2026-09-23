"""Tests for the Moonraker ``[box]`` -> CFS slot parser -- no network.

The box payload shape is firmware-dependent, so the parser is exercised against
the full envelope, the bare result, the bare box object and a pile of malformed
shapes. The contract is: map what is there, never raise, and return ``[]`` when
the payload does not carry a ``slots`` list.
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
