"""Filament density defaults and gram estimation.

Regression tests for a live slice where ``filament_g`` came out ``0.0`` because
PrusaSlicer logged ``filament_density = 0``: the profile omitted
``filament_density``, so the gram estimate was unusable even though the
extruded length (``filament used [mm]``) was meaningful.
"""

from __future__ import annotations

import pytest

from slicers.base import (
    DEFAULT_FILAMENT_DENSITY,
    MATERIAL_DENSITIES,
    filament_density_for,
    filament_grams_from_length,
    resolve_filament_density,
    with_default_filament_density,
)


@pytest.mark.parametrize(
    ("material", "expected"),
    [
        ("PLA", 1.24),
        ("PETG", 1.27),
        ("ABS", 1.04),
        ("ASA", 1.07),
        ("TPU", 1.21),
        ("PA", 1.14),
        ("Nylon", 1.14),
        ("PC", 1.20),
        ("pla", 1.24),
        ("PLA+", 1.24),
        ("petg-cf", 1.27),
        ("PA12", 1.14),
        ("Polycarbonate", 1.20),
    ],
)
def test_material_density_map(material, expected):
    assert filament_density_for(material) == pytest.approx(expected)


@pytest.mark.parametrize("material", ["", None, "  ", "Unobtainium", "MysteryBlend"])
def test_unknown_material_falls_back_to_default(material):
    assert filament_density_for(material) == pytest.approx(DEFAULT_FILAMENT_DENSITY)


def test_default_density_is_pla():
    assert DEFAULT_FILAMENT_DENSITY == 1.24
    assert MATERIAL_DENSITIES["PLA"] == 1.24


def test_explicit_inline_density_wins():
    settings = {"filament_density": 1.30, "temperature": 210}
    assert with_default_filament_density(settings, "PLA")["filament_density"] == 1.30
    assert resolve_filament_density(settings, "PLA") == 1.30


def test_explicit_override_density_wins():
    settings = {"overrides": {"filament_density": 1.29}}
    # The CLI override already wins at runtime, so no inline default is added.
    assert "filament_density" not in with_default_filament_density(settings, "PLA")
    assert resolve_filament_density(settings, "PLA") == 1.29


def test_default_density_is_injected_when_absent():
    result = with_default_filament_density({"temperature": 210}, "PETG")
    assert result["filament_density"] == pytest.approx(1.27)
    assert result["temperature"] == 210


def test_zero_density_is_treated_as_unset():
    injected = with_default_filament_density({"filament_density": 0}, "PLA")
    assert injected["filament_density"] == pytest.approx(1.24)


def test_injecting_a_default_does_not_mutate_the_input():
    settings = {"temperature": 210}
    with_default_filament_density(settings, "PLA")
    assert "filament_density" not in settings


def test_grams_from_length_uses_density_and_diameter():
    # 1000 mm of 1.75 mm PLA (1.24 g/cm3) ~ 2.98 g.
    grams = filament_grams_from_length(1000.0, 1.24, 1.75)
    assert grams == pytest.approx(2.98, abs=0.01)


def test_grams_from_length_scales_with_density():
    light = filament_grams_from_length(1000.0, 1.04, 1.75)
    heavy = filament_grams_from_length(1000.0, 1.27, 1.75)
    assert 0 < light < heavy
