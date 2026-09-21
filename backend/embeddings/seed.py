"""Built-in RAG seed dataset (terv.md 5., 7. fejezet).

A deliberately small, factual and sourced reference set for 3D-printing / CAD
work. Every number is a published standard or manufacturer value -- nothing is
invented. Sources:

* ISO 261 / ISO 262 -- ISO metric coarse thread series (pitch).
* ISO 2306 -- tap drill diameters for ISO metric threads (~75 % engagement).
* ISO 273:1979 -- clearance holes for bolts and screws (close/normal/loose).
* Prusament PLA / PETG technical datasheets (Prusa Polymers).
* Common FDM slicer ranges for ABS / TPU (values marked as typical ranges).

``manage.py rag_ingest --seed`` ingests exactly these documents. All values are
reference values -- reviewers should confirm them against the current standard
or the filament supplier's datasheet.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# ISO metric coarse screw dimensions (M2-M8)
# ---------------------------------------------------------------------------
#
# diameter/pitch: ISO 261 / ISO 262. tap_drill: ISO 2306 (~75 % thread).
# clearance_*: ISO 273:1979 close / normal (medium) / loose series.
SCREW_DIMENSIONS: list[dict[str, Any]] = [
    {
        "size": "M2",
        "diameter_mm": 2.0,
        "pitch_mm": 0.40,
        "tap_drill_mm": 1.6,
        "clearance_close_mm": 2.2,
        "clearance_normal_mm": 2.4,
        "clearance_loose_mm": 2.6,
    },
    {
        "size": "M2.5",
        "diameter_mm": 2.5,
        "pitch_mm": 0.45,
        "tap_drill_mm": 2.05,
        "clearance_close_mm": 2.7,
        "clearance_normal_mm": 2.9,
        "clearance_loose_mm": 3.1,
    },
    {
        "size": "M3",
        "diameter_mm": 3.0,
        "pitch_mm": 0.50,
        "tap_drill_mm": 2.5,
        "clearance_close_mm": 3.2,
        "clearance_normal_mm": 3.4,
        "clearance_loose_mm": 3.6,
    },
    {
        "size": "M4",
        "diameter_mm": 4.0,
        "pitch_mm": 0.70,
        "tap_drill_mm": 3.3,
        "clearance_close_mm": 4.3,
        "clearance_normal_mm": 4.5,
        "clearance_loose_mm": 4.8,
    },
    {
        "size": "M5",
        "diameter_mm": 5.0,
        "pitch_mm": 0.80,
        "tap_drill_mm": 4.2,
        "clearance_close_mm": 5.3,
        "clearance_normal_mm": 5.5,
        "clearance_loose_mm": 5.8,
    },
    {
        "size": "M6",
        "diameter_mm": 6.0,
        "pitch_mm": 1.00,
        "tap_drill_mm": 5.0,
        "clearance_close_mm": 6.4,
        "clearance_normal_mm": 6.6,
        "clearance_loose_mm": 7.0,
    },
    {
        "size": "M8",
        "diameter_mm": 8.0,
        "pitch_mm": 1.25,
        "tap_drill_mm": 6.8,
        "clearance_close_mm": 8.4,
        "clearance_normal_mm": 9.0,
        "clearance_loose_mm": 10.0,
    },
]

# ---------------------------------------------------------------------------
# Common FDM materials (typical ranges)
# ---------------------------------------------------------------------------
#
# nozzle_c / bed_c are typical ranges. Numerical properties come from the
# linked manufacturer datasheet; ABS/TPU properties are qualitative to avoid
# quoting an unsourced figure.
MATERIALS: list[dict[str, Any]] = [
    {
        "name": "PLA",
        "full_name": "polylactic acid",
        "nozzle_c": (200, 220),
        "bed_c": (40, 60),
        "cooling_fan": "~100%",
        "enclosure": "not required",
        "properties": (
            "easy to print, low odour, low warping, relatively brittle, low heat "
            "resistance (heat deflection ~55 C), density ~1.24 g/cm3; not suited "
            "for long-term hot or outdoor use"
        ),
        "source_url": "https://prusament.com/wp-content/uploads/2022/10/PLA_Prusament_TDS_2021_10_EN.pdf",
    },
    {
        "name": "PETG",
        "full_name": "polyethylene terephthalate glycol",
        "nozzle_c": (240, 260),
        "bed_c": (70, 90),
        "cooling_fan": "30-50%",
        "enclosure": "not required",
        "properties": (
            "tougher and slightly more flexible than PLA, good chemical and UV "
            "resistance, good layer adhesion, prone to stringing, heat deflection "
            "~68 C, density ~1.27 g/cm3; hygroscopic, dry the filament before use"
        ),
        "source_url": "https://www.prusa3d.com/file/3464925/prusament-petg-technical-data-sheet.pdf",
    },
    {
        "name": "ABS",
        "full_name": "acrylonitrile butadiene styrene",
        "nozzle_c": (230, 260),
        "bed_c": (90, 110),
        "cooling_fan": "0-20%",
        "enclosure": "recommended/required (reduces warping)",
        "properties": (
            "high impact strength, higher heat resistance than PLA and PETG, "
            "sandable and acetone-smoothable; warps without a heated enclosure and "
            "emits styrene/VOC fumes, so ventilation or filtration is needed"
        ),
        "source_url": "https://printdesk3d.com/slicer-settings",
    },
    {
        "name": "TPU",
        "full_name": "thermoplastic polyurethane",
        "nozzle_c": (210, 235),
        "bed_c": (30, 50),
        "cooling_fan": "30-60%",
        "enclosure": "not required",
        "properties": (
            "flexible and elastic, abrasion resistant, good for gaskets, grips and "
            "wheels; Shore hardness depends on the grade (95A is common). Print "
            "slowly (15-40 mm/s); a direct-drive extruder is recommended because "
            "flexible filament is hard to push through a long Bowden tube"
        ),
        "source_url": "https://printdesk3d.com/slicer-settings",
    },
]

ISO_THREAD_SOURCE = "https://en.wikipedia.org/wiki/ISO_metric_screw_thread"
ISO_273_SOURCE = "https://www.iso.org/standard/4183.html"


def _screw_thread_content() -> str:
    lines = [
        "ISO 261 / ISO 262 define the ISO metric coarse thread series; the coarse "
        "pitch is the standard default for general-purpose fasteners.",
        "",
        "Nominal diameter | coarse pitch | tap drill (~75 % thread)",
    ]
    lines.extend(
        f"{row['size']} | {row['pitch_mm']:.2f} mm | {row['tap_drill_mm']:.2f} mm"
        for row in SCREW_DIMENSIONS
    )
    lines += [
        "",
        "Tap drill rule of thumb (ISO 2306): tap drill = nominal diameter - coarse "
        "pitch, rounded to the nearest standard drill size; this gives roughly 75 % "
        "thread engagement.",
        "Reference values only. Verify against the current ISO standard or the tap "
        "manufacturer's recommendation.",
    ]
    return "\n".join(lines)


def _clearance_hole_content() -> str:
    lines = [
        "ISO 273:1979 specifies clearance holes for bolts and screws in three "
        "series: close, normal (medium) and loose fit. Normal fit is the default "
        "for general assembly.",
        "",
        "Nominal thread | close | normal | loose",
    ]
    lines.extend(
        f"{row['size']} x {row['pitch_mm']:.2f} | {row['clearance_close_mm']:.1f} mm "
        f"| {row['clearance_normal_mm']:.1f} mm | {row['clearance_loose_mm']:.1f} mm"
        for row in SCREW_DIMENSIONS
    )
    lines += [
        "",
        "Reference values only (dimensions in mm). Verify against the current "
        "standard or the fastener supplier.",
    ]
    return "\n".join(lines)


def _material_content(material: dict[str, Any]) -> str:
    nozzle_lo, nozzle_hi = material["nozzle_c"]
    bed_lo, bed_hi = material["bed_c"]
    return "\n".join(
        [
            f"{material['name']} ({material['full_name']}) - typical FDM print settings "
            "(typical ranges; always check your filament supplier's datasheet):",
            f"- Nozzle temperature: {nozzle_lo}-{nozzle_hi} C",
            f"- Heated bed: {bed_lo}-{bed_hi} C",
            f"- Cooling fan: {material['cooling_fan']}",
            f"- Enclosure: {material['enclosure']}",
            f"Typical properties: {material['properties']}.",
        ]
    )


def _build_seed_documents() -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = [
        {
            "title": "ISO metric coarse threads M2-M8: pitch and tap drill",
            "content": _screw_thread_content(),
            "source_url": ISO_THREAD_SOURCE,
            "source_type": "standard",
        },
        {
            "title": "ISO 273 clearance holes M2-M8: close, normal and loose fit",
            "content": _clearance_hole_content(),
            "source_url": ISO_273_SOURCE,
            "source_type": "standard",
        },
    ]
    documents.extend(
        {
            "title": f"{material['name']} (FDM) print settings and typical properties",
            "content": _material_content(material),
            "source_url": material["source_url"],
            "source_type": "datasheet",
        }
        for material in MATERIALS
    )
    return documents


#: Canonical seed dataset consumed by ``manage.py rag_ingest --seed``.
SEED_DOCUMENTS: list[dict[str, Any]] = _build_seed_documents()


def seed_documents() -> list[dict[str, Any]]:
    """Return a fresh copy of :data:`SEED_DOCUMENTS` (safe to mutate)."""
    return [dict(document) for document in SEED_DOCUMENTS]
