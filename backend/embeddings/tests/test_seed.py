"""Tests for the built-in RAG seed dataset.

Guards against invented or physically impossible values: the ISO tables must be
internally consistent and match the published reference values.
"""

from __future__ import annotations

import pytest

from embeddings.loaders import normalize_document
from embeddings.seed import MATERIALS, SCREW_DIMENSIONS, SEED_DOCUMENTS, seed_documents


def test_seed_dataset_is_small_but_present():
    assert len(SEED_DOCUMENTS) >= 5
    assert len(seed_documents()) == len(SEED_DOCUMENTS)


def test_seed_documents_are_valid_and_sourced():
    for document in SEED_DOCUMENTS:
        normalized = normalize_document(document, origin=document["title"])
        assert normalized["title"]
        assert normalized["content"].strip()
        assert normalized["source_url"].startswith("http"), document["title"]
        assert normalized["source_type"] in ("standard", "datasheet", "web", "manual")


def test_seed_covers_iso_screws_and_four_materials():
    titles = " ".join(document["title"] for document in SEED_DOCUMENTS)
    assert "ISO" in titles
    for material in ("PLA", "PETG", "ABS", "TPU"):
        assert material in titles


def test_screw_dimensions_are_physically_sane():
    assert [row["size"] for row in SCREW_DIMENSIONS] == [
        "M2",
        "M2.5",
        "M3",
        "M4",
        "M5",
        "M6",
        "M8",
    ]
    for row in SCREW_DIMENSIONS:
        assert 0 < row["pitch_mm"] < row["diameter_mm"]
        assert row["tap_drill_mm"] < row["diameter_mm"]
        assert row["clearance_close_mm"] > row["diameter_mm"]
        assert row["clearance_close_mm"] < row["clearance_normal_mm"] < row["clearance_loose_mm"]


@pytest.mark.parametrize(
    ("size", "pitch", "tap", "normal"),
    [
        ("M2", 0.40, 1.6, 2.4),
        ("M3", 0.50, 2.5, 3.4),
        ("M5", 0.80, 4.2, 5.5),
        ("M8", 1.25, 6.8, 9.0),
    ],
)
def test_known_iso_values(size, pitch, tap, normal):
    row = next(item for item in SCREW_DIMENSIONS if item["size"] == size)
    assert row["pitch_mm"] == pytest.approx(pitch)
    assert row["tap_drill_mm"] == pytest.approx(tap)
    assert row["clearance_normal_mm"] == pytest.approx(normal)


def test_seed_content_embeds_the_table_values():
    content = " ".join(document["content"] for document in SEED_DOCUMENTS)
    for token in ("0.50 mm", "2.50 mm", "3.4 mm", "1.25 mm", "6.80 mm", "9.0 mm"):
        assert token in content


def test_material_ranges_are_plausible():
    assert {material["name"] for material in MATERIALS} == {"PLA", "PETG", "ABS", "TPU"}
    for material in MATERIALS:
        nozzle_lo, nozzle_hi = material["nozzle_c"]
        bed_lo, bed_hi = material["bed_c"]
        assert 150 <= nozzle_lo < nozzle_hi <= 350, material["name"]
        assert 0 <= bed_lo < bed_hi <= 150, material["name"]
        assert material["properties"].strip()
        assert material["source_url"].startswith("http")
