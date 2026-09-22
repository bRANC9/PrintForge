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

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer

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

# ---------------------------------------------------------------------------
# Bounds for annotation-driven parametric operations (docs/visual-editing.md 3.1).
# ---------------------------------------------------------------------------

#: Smallest printable feature (below a 0.4 mm nozzle this is not printable).
MIN_OPERATION_MM = 0.4
#: Upper bound for a cut/extrusion depth (mm).
MAX_OPERATION_DEPTH_MM = 200.0
#: Upper bound for planar operation dimensions (width/height/length, mm).
MAX_OPERATION_SIZE_MM = 500.0
#: Upper bound for a cylindrical operation diameter (mm).
MAX_OPERATION_DIAMETER_MM = 200.0
#: Maximum length of the optional human-readable operation label.
MAX_OPERATION_LABEL = 120

# ---------------------------------------------------------------------------
# Bounds for prompt-driven CSG primitives (docs/cad-primitives.md 1).
# ---------------------------------------------------------------------------

#: Smallest printable primitive edge/diameter (below a 0.4 mm nozzle this is not printable).
MIN_PRIMITIVE_MM = 0.4
#: Upper bound for a primitive size to catch unit mistakes (cm vs mm).
MAX_PRIMITIVE_MM = 1000.0
#: Bounds for a primitive centre in model coordinates (mm).
MIN_PRIMITIVE_POSITION_MM = -1000.0
MAX_PRIMITIVE_POSITION_MM = 1000.0
#: Bounds for a primitive rotation in degrees (XYZ order).
MIN_PRIMITIVE_ROTATION_DEG = -360.0
MAX_PRIMITIVE_ROTATION_DEG = 360.0
#: Maximum length of the optional human-readable primitive label.
MAX_PRIMITIVE_LABEL = 120
#: Maximum number of primitives in a single specification.
MAX_PRIMITIVE_COUNT = 64


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


class Vec3(BaseModel):
    """A point or direction in model coordinates, millimetres (docs 3.1)."""

    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    z: float


def _zero_vec3() -> Vec3:
    """Return a fresh origin vector (``Field(default_factory=...)`` helper)."""
    return Vec3(x=0.0, y=0.0, z=0.0)


def _check_vec3_range(name: str, value: Vec3, low: float, high: float) -> None:
    """Reject a :class:`Vec3` with any component outside ``[low, high]``."""
    for axis, component in (("x", value.x), ("y", value.y), ("z", value.z)):
        if not low <= component <= high:
            raise ValueError(f"{name}.{axis} must be between {low} and {high}, got {component}")


class Primitive(BaseModel):
    """One validated CSG primitive the LLM composes into a model.

    The LLM emits primitives -- never OpenSCAD code -- and the CAD backend
    re-validates and clamps every value before building the union/difference
    tree (docs/cad-primitives.md 1). ``position`` is the centre of the shape in
    model coordinates and ``rotation`` is in degrees, applied in XYZ order.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["box", "cylinder", "sphere", "cone"] = Field(
        description="Primitive shape.",
    )
    role: Literal["add", "subtract"] = Field(
        default="add",
        description='"add" contributes material, "subtract" removes it.',
    )
    position: Vec3 = Field(
        default_factory=_zero_vec3,
        description="Centre of the primitive in model coordinates (mm).",
    )
    rotation: Vec3 = Field(
        default_factory=_zero_vec3,
        description="Rotation in degrees, XYZ order.",
    )
    width: float | None = Field(
        default=None,
        ge=MIN_PRIMITIVE_MM,
        le=MAX_PRIMITIVE_MM,
        description="Box X size in mm.",
    )
    depth: float | None = Field(
        default=None,
        ge=MIN_PRIMITIVE_MM,
        le=MAX_PRIMITIVE_MM,
        description="Box Y size in mm.",
    )
    height: float | None = Field(
        default=None,
        ge=MIN_PRIMITIVE_MM,
        le=MAX_PRIMITIVE_MM,
        description="Box Z size / cylinder or cone height in mm.",
    )
    diameter: float | None = Field(
        default=None,
        ge=MIN_PRIMITIVE_MM,
        le=MAX_PRIMITIVE_MM,
        description="Cylinder, sphere or cone diameter in mm.",
    )
    label: str = Field(
        default="",
        max_length=MAX_PRIMITIVE_LABEL,
        description="Optional short human-readable label.",
    )

    @field_validator("position")
    @classmethod
    def _bound_position(cls, value: Vec3) -> Vec3:
        _check_vec3_range("position", value, MIN_PRIMITIVE_POSITION_MM, MAX_PRIMITIVE_POSITION_MM)
        return value

    @field_validator("rotation")
    @classmethod
    def _bound_rotation(cls, value: Vec3) -> Vec3:
        _check_vec3_range("rotation", value, MIN_PRIMITIVE_ROTATION_DEG, MAX_PRIMITIVE_ROTATION_DEG)
        return value


class EditOperation(BaseModel):
    """One validated parametric feature derived from a visual annotation.

    The LLM emits operations -- never OpenSCAD code -- and the CAD backend
    re-validates and clamps every value (docs/visual-editing.md 3.1, 3.3).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["hole", "pocket", "boss", "slot", "cut", "add"] = Field(
        description="Feature type: subtraction (hole/pocket/cut/slot) or addition (boss/add).",
    )
    origin: Vec3 = Field(description="Feature origin in model coordinates (mm).")
    normal: Vec3 = Field(
        description="Operation axis; need not be unit length (the CAD backend normalises).",
    )
    depth: float = Field(
        ge=MIN_OPERATION_MM,
        le=MAX_OPERATION_DEPTH_MM,
        description="Cut depth / boss height in mm.",
    )
    width: float | None = Field(
        default=None,
        ge=MIN_OPERATION_MM,
        le=MAX_OPERATION_SIZE_MM,
        description="Rectangular feature width in mm (pocket/cut/add).",
    )
    height: float | None = Field(
        default=None,
        ge=MIN_OPERATION_MM,
        le=MAX_OPERATION_SIZE_MM,
        description="Rectangular feature height in mm (pocket/cut/add).",
    )
    diameter: float | None = Field(
        default=None,
        ge=MIN_OPERATION_MM,
        le=MAX_OPERATION_DIAMETER_MM,
        description="Cylindrical feature diameter in mm (hole/boss/slot).",
    )
    length: float | None = Field(
        default=None,
        ge=MIN_OPERATION_MM,
        le=MAX_OPERATION_SIZE_MM,
        description="Slot length in mm.",
    )
    label: str = Field(
        default="",
        max_length=MAX_OPERATION_LABEL,
        description="Optional short human-readable label.",
    )


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
    operations: list[EditOperation] = Field(
        default_factory=list,
        description="Validated parametric features applied to the base part (visual prompts).",
    )
    primitives: list[Primitive] = Field(
        default_factory=list,
        max_length=MAX_PRIMITIVE_COUNT,
        description=(
            "Validated CSG primitives composed by the LLM; empty keeps the built-in template "
            "(docs/cad-primitives.md 1)."
        ),
    )

    @field_validator("material")
    @classmethod
    def _normalise_material(cls, value: str) -> str:
        """Upper-case the material so downstream CAD logic can match reliably."""
        return value.strip().upper()

    @model_serializer(mode="wrap")
    def _omit_empty_collections(
        self, handler: Callable[[ModelSpecification], dict[str, Any]]
    ) -> dict[str, Any]:
        """Keep the base specification canonical by omitting empty optional lists.

        Both fields always exist on the model and in the JSON schema; they are
        only left out of the serialised payload when empty, so a spec without
        features or primitives round-trips byte-for-byte to the terv.md 8.
        example and the CAD backend keeps its legacy template behaviour.
        """
        data = handler(self)
        if not self.operations:
            data.pop("operations", None)
        if not self.primitives:
            data.pop("primitives", None)
        return data

    @classmethod
    def to_json_schema(cls) -> dict[str, Any]:
        """Return the JSON Schema used to prompt the LLM.

        Handy for backends that support structured output / JSON mode.
        """
        return cls.model_json_schema()

    @classmethod
    def example(cls) -> dict[str, Any]:
        """Return the terv.md 8. example as a validated, normalised dict.

        The base example carries no features or primitives, so the empty
        ``operations``/``primitives`` fields are omitted by the model serializer
        and the payload stays byte-for-byte the terv.md 8. example.
        """
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


# ---------------------------------------------------------------------------
# Vision self-check review contract (docs/vision-self-check.md 3).
# ---------------------------------------------------------------------------

#: Maximum number of issues a single review may report.
MAX_REVIEW_ISSUES = 20
#: Maximum length of the review summary.
MAX_REVIEW_SUMMARY = 500


class ReviewResult(BaseModel):
    """Structured verdict returned by the vision self-check.

    The review node asks a vision-capable provider to compare the rendered
    preview against the original request and returns this validated payload.
    Like :class:`ModelSpecification`, it is a strict contract: no extra keys,
    whitespace-trimmed strings and bounded collections (docs/vision-self-check.md
    3). ``matches`` is required; ``issues``/``summary`` are optional and
    default to empty.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    matches: bool = Field(
        description="True when the rendered preview matches the original request.",
    )
    issues: list[str] = Field(
        default_factory=list,
        max_length=MAX_REVIEW_ISSUES,
        description="Concrete discrepancies; empty when the preview matches.",
    )
    summary: str = Field(
        default="",
        max_length=MAX_REVIEW_SUMMARY,
        description="Short human-readable summary of the verdict.",
    )

    @classmethod
    def to_json_schema(cls) -> dict[str, Any]:
        """Return the JSON Schema used to prompt the vision LLM."""
        return cls.model_json_schema()
