"""K2 CFS transport seams: the real Moonraker ``[box]`` query and the fallback.

``backend/printers/tests/test_creality_k2.py`` stubs ``query_box`` entirely and
``test_k2_box.py`` pins the parser. This module covers the HTTP seam in between:
the **real** :class:`~printers.moonraker.MoonrakerClient` (only its ``opener``
faked) must hit the documented ``/printer/objects/query?box`` endpoint and its
response must be parsed, with the proprietary WebSocket reader only used when the
``[box]`` object is absent/empty/unreachable. No socket is ever opened.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from printers.base import CfsSlot, PrinterConnectionError
from printers.creality_k2 import MoonrakerK2Transport
from printers.moonraker import MoonrakerClient


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _FakeOpener:
    """Records the requested URLs and returns canned bodies (or raises)."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def __call__(self, request, *, timeout: float = 0.0) -> _FakeResponse:
        url = request.full_url
        self.urls.append(url)
        value = self.responses.get(url)
        if value is None:
            for prefix, candidate in self.responses.items():
                if prefix in url:
                    value = candidate
                    break
        if value is None:
            raise AssertionError(f"unexpected URL {url}")
        if isinstance(value, Exception):
            raise value
        body = value.encode("utf-8") if isinstance(value, str) else value
        return _FakeResponse(body)


BOX_PAYLOAD = {
    "result": {
        "status": {
            "box": {
                "slots": [
                    {
                        "index": 0,
                        "material": "PLA",
                        "brand": "Creality",
                        "name": "Hyper PLA",
                        "color": "#1a1a1a",
                        "present": True,
                        "loaded": True,
                    }
                ]
            }
        }
    }
}

#: The stock Creality schema (per-unit arrays, no ``slots`` list): parsed directly.
STOCK_CREALITY_PAYLOAD = {"result": {"status": {"box": {"T1": {"material_type": ["PLA"]}}}}}


def _transport(opener: _FakeOpener, *, reader) -> MoonrakerK2Transport:
    client = MoonrakerClient("http://k2.local:7125", opener=opener)
    return MoonrakerK2Transport("k2.local", timeout=2.0, client=client, cfs_reader=reader)


def test_fetch_cfs_slots_prefers_the_real_box_object_query():
    opener = _FakeOpener(
        {"http://k2.local:7125/printer/objects/query?box": json.dumps(BOX_PAYLOAD)}
    )
    reader_calls: list[dict] = []

    def reader(*args, **kwargs):
        reader_calls.append(kwargs)
        raise AssertionError("the WebSocket fallback must not run when [box] answers")

    slots = _transport(opener, reader=reader).fetch_cfs_slots()

    assert opener.urls == ["http://k2.local:7125/printer/objects/query?box"]
    assert [(slot.index, slot.material, slot.brand, slot.empty) for slot in slots] == [
        (0, "PLA", "Creality", False)
    ]
    assert reader_calls == []


def test_stock_creality_box_shape_is_parsed_without_the_websocket():
    opener = _FakeOpener(
        {"http://k2.local:7125/printer/objects/query?box": json.dumps(STOCK_CREALITY_PAYLOAD)}
    )

    def reader(*args, **kwargs):
        raise AssertionError("the stock [box] schema parses, so the WebSocket must not run")

    slots = _transport(opener, reader=reader).fetch_cfs_slots()

    assert [(slot.index, slot.material, slot.empty) for slot in slots] == [(0, "PLA", False)]
    assert opener.urls == ["http://k2.local:7125/printer/objects/query?box"]


def test_missing_box_object_falls_back_to_the_websocket():
    opener = _FakeOpener(
        {"http://k2.local:7125/printer/objects/query?box": json.dumps({"result": {"status": {}}})}
    )
    ws_slots = [CfsSlot(index=0, empty=True)]

    transport = _transport(opener, reader=lambda host, *, port, timeout: ws_slots)

    assert transport.fetch_cfs_slots() == ws_slots


def test_unreachable_moonraker_falls_back_to_the_websocket():
    opener = _FakeOpener(
        {
            "http://k2.local:7125/printer/objects/query?box": PrinterConnectionError(
                "cannot reach K2"
            )
        }
    )
    ws_slots = [CfsSlot(index=1, material="ABS", empty=False)]

    transport = _transport(opener, reader=lambda host, *, port, timeout: ws_slots)

    assert transport.fetch_cfs_slots() == ws_slots


def test_real_client_round_trips_a_box_query_payload():
    """The client strips the envelope and the parser accepts ``result`` directly."""
    opener = _FakeOpener(
        {"http://k2.local:7125/printer/objects/query?box": json.dumps(BOX_PAYLOAD)}
    )
    client = MoonrakerClient("http://k2.local:7125", opener=opener)

    result = client.query_box()

    assert result == BOX_PAYLOAD["result"]
    assert opener.urls == ["http://k2.local:7125/printer/objects/query?box"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (BOX_PAYLOAD, 1),
        (STOCK_CREALITY_PAYLOAD, 1),
        ({"result": {"status": {"box": {"unknown": 1}}}}, 0),
        ({"not": "a box"}, 0),
    ],
)
def test_parser_only_reports_parseable_slots(payload, expected):
    from printers.k2_box import slots_from_box_object

    assert len(slots_from_box_object(payload)) == expected
