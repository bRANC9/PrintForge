"""Structured specification shared by the LLM layer and the CAD backend.

This mirrors terv.md 8. fejezet: the LLM and the CAD backend must exchange
validated data, **never free text**. The only contract between them is
:class:`ModelSpecification`; the CAD backend can rely on the field types and
bounds below without re-parsing prose.

Example payload (terv.md 8.)::

    {
      "object": "phone_holder",
      "dimensions": {"width": 70.6, "height": 147, "thickness": 7.6},
      "angle": 15,
      "wall_thickness": 4,
      "mounting": {"type": "M5", "count": 2},
      "material": "PETG"
    }
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Sane physical bounds (millimetres / degrees).
# ---------------------------------------------------------------------------

#: Largest bounding-box edge accepted (desktop-class printers, generous).
MAX_DIMENSION_MM = 1000.0
#: Minimum printable wall (below a 0.4 mm nozzle this is not printable).
MIN_WALL_MM = 0.4
#: Upper bound for wall thickness to catch unit mistakes (cm vs mm).
MAX_WALL_MM = 100.0
#: Tilt/angle upper bound.
MAX_ANGLE_DEG = 180.0
#: Upper bound for mounting points/holes.
MAX_MOUNTING_COUNT = 64


class Mounting(BaseModel):
    """How the part is fastened (terv.md 8.)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: str = Field(
        min_length=1,
        max_length=64,
        description='Mounting standard or hole type, e.g. "M5", "M3", "none".',
    )
    count: int = Field(
        ge=0,
        le=MAX_MOUNTING_COUNT,
        description="Number of mounting points/holes (0 when the part is not fastened).",
    )


class Dimensions(BaseModel):
    """Bounding-box dimensions of the produced object, in millimetres."""

    model_config = ConfigDict(extra="forbid")

    width: float = Field(gt=0, le=MAX_DIMENSION_MM, description="Width in mm.")
    height: float = Field(gt=0, le=MAX_DIMENSION_MM, description="Height in mm.")
    thickness: float = Field(gt=0, le=MAX_DIMENSION_MM, description="Thickness in mm.")


class ModelSpecification(BaseModel):
    """The single validated contract between the LLM and the CAD backend."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    object: str = Field(
        min_length=1,
        max_length=128,
        description='Machine-readable object kind, e.g. "phone_holder".',
    )
    dimensions: Dimensions
    angle: float = Field(
        ge=0,
        le=MAX_ANGLE_DEG,
        description="Tilt/draft angle in degrees (0 = straight).",
    )
    wall_thickness: float = Field(
        ge=MIN_WALL_MM,
        le=MAX_WALL_MM,
        description="Wall thickness in mm; must be printable.",
    )
    mounting: Mounting
    material: str = Field(
        min_length=1,
        max_length=64,
        description='Filament material, e.g. "PLA", "PETG", "ABS", "TPU".',
    )

    @field_validator("material")
    @classmethod
    def _normalise_material(cls, value: str) -> str:
        """Upper-case the material so downstream CAD logic can match reliably."""
        return value.strip().upper()

    @classmethod
    def to_json_schema(cls) -> dict[str, Any]:
        """Return the JSON Schema used to prompt the LLM.

        Handy for backends that support structured output / JSON mode.
        """
        return cls.model_json_schema()

    @classmethod
    def example(cls) -> dict[str, Any]:
        """Return the terv.md 8. example as a validated, normalised dict."""
        return cls.model_validate(
            {
                "object": "phone_holder",
                "dimensions": {"width": 70.6, "height": 147, "thickness": 7.6},
                "angle": 15,
                "wall_thickness": 4,
                "mounting": {"type": "M5", "count": 2},
                "material": "PETG",
            }
        ).model_dump()
