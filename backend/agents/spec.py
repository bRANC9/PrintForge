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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

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

# ---------------------------------------------------------------------------
# Bounds for the 2D ``extrude`` primitive (docs/skills.md 5).
# ---------------------------------------------------------------------------

#: Minimum number of 2D profile points needed to describe a polygon.
MIN_PROFILE_POINTS = 3
#: Maximum number of 2D profile points accepted for one primitive (bounded payload).
MAX_PROFILE_POINTS = 256
#: Bounds for a 2D profile coordinate in the XZ plane (mm); shares the primitive
#: position range so a profile cannot place geometry outside the build volume.
MIN_PROFILE_COORD_MM = MIN_PRIMITIVE_POSITION_MM
MAX_PROFILE_COORD_MM = MAX_PRIMITIVE_POSITION_MM
#: Bounds for the optional top-edge rounding radius of an ``extrude`` primitive (mm).
MIN_ROUND_RADIUS_MM = 0.0
MAX_ROUND_RADIUS_MM = 100.0


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


class Vec2(BaseModel):
    """A point of a 2D profile in the XZ plane, millimetres (docs/skills.md 5).

    The ``extrude`` primitive renders its ``profile`` with
    ``linear_extrude(height) polygon(points)``, so the outline is an inline
    point list -- never a file, never ``import()``/``surface()``.
    """

    model_config = ConfigDict(extra="forbid")

    x: float
    y: float


def _zero_vec3() -> Vec3:
    """Return a fresh origin vector (``Field(default_factory=...)`` helper)."""
    return Vec3(x=0.0, y=0.0, z=0.0)


def _check_vec3_range(name: str, value: Vec3, low: float, high: float) -> None:
    """Reject a :class:`Vec3` with any component outside ``[low, high]``."""
    for axis, component in (("x", value.x), ("y", value.y), ("z", value.z)):
        if not low <= component <= high:
            raise ValueError(f"{name}.{axis} must be between {low} and {high}, got {component}")


def _check_vec2_range(name: str, value: Vec2, low: float, high: float) -> None:
    """Reject a :class:`Vec2` with any component outside ``[low, high]``."""
    for axis, component in (("x", value.x), ("y", value.y)):
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

    type: Literal["box", "cylinder", "sphere", "cone", "extrude"] = Field(
        description="Primitive shape.",
    )
    role: Literal["add", "subtract"] = Field(
        default="add",
        description="add adds material, subtract cuts it away.",
    )
    position: Vec3 = Field(
        default_factory=_zero_vec3,
        description="Primitive centre in mm; the part rests on the build plate (minimum Z = 0).",
    )
    rotation: Vec3 = Field(
        default_factory=_zero_vec3,
        description="XYZ rotation in degrees.",
    )
    width: float | None = Field(
        default=None,
        ge=MIN_PRIMITIVE_MM,
        le=MAX_PRIMITIVE_MM,
        description="Box size along X in mm (required for type box).",
    )
    depth: float | None = Field(
        default=None,
        ge=MIN_PRIMITIVE_MM,
        le=MAX_PRIMITIVE_MM,
        description="Box size along Y in mm (required for type box).",
    )
    height: float | None = Field(
        default=None,
        ge=MIN_PRIMITIVE_MM,
        le=MAX_PRIMITIVE_MM,
        description=(
            "Box size along Z, or the cylinder/cone height, in mm "
            "(required for box, cylinder and cone)."
        ),
    )
    diameter: float | None = Field(
        default=None,
        ge=MIN_PRIMITIVE_MM,
        le=MAX_PRIMITIVE_MM,
        description=(
            "Cylinder/sphere/cone diameter in mm (required for cylinder, sphere and cone)."
        ),
    )
    profile: list[Vec2] = Field(
        default_factory=list,
        max_length=MAX_PROFILE_POINTS,
        description=(
            "2D outline points in the XZ plane, in mm (required for type extrude, "
            "at least 3 points); the CAD backend extrudes it to height."
        ),
    )
    wall_thickness: float | None = Field(
        default=None,
        ge=MIN_PRIMITIVE_MM,
        le=MAX_WALL_MM,
        description=(
            "Hollow-wall thickness in mm for an extrude primitive; when set the "
            "profile is offset inwards to create an outer shell."
        ),
    )
    round_radius: float | None = Field(
        default=None,
        ge=MIN_ROUND_RADIUS_MM,
        le=MAX_ROUND_RADIUS_MM,
        description="Optional top-edge rounding radius in mm (extrude primitive).",
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

    @field_validator("profile")
    @classmethod
    def _bound_profile(cls, value: list[Vec2]) -> list[Vec2]:
        for index, point in enumerate(value):
            _check_vec2_range(
                f"profile[{index}]", point, MIN_PROFILE_COORD_MM, MAX_PROFILE_COORD_MM
            )
        return value

    @model_validator(mode="after")
    def _require_profile_for_extrude(self) -> Primitive:
        """An ``extrude`` primitive needs a polygon, so at least 3 profile points.

        Other primitive types keep their existing, loose field requirements:
        this validator deliberately does not constrain them.
        """
        if self.type == "extrude" and len(self.profile) < MIN_PROFILE_POINTS:
            raise ValueError(
                f"type 'extrude' requires at least {MIN_PROFILE_POINTS} profile points, "
                f"got {len(self.profile)}"
            )
        return self


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
        description="Cut depth / boss height in mm; required for every operation.",
    )
    width: float | None = Field(
        default=None,
        ge=MIN_OPERATION_MM,
        le=MAX_OPERATION_SIZE_MM,
        description="Required for kinds pocket, cut and add; ignored otherwise.",
    )
    height: float | None = Field(
        default=None,
        ge=MIN_OPERATION_MM,
        le=MAX_OPERATION_SIZE_MM,
        description="Required for kinds pocket, cut and add; ignored otherwise.",
    )
    diameter: float | None = Field(
        default=None,
        ge=MIN_OPERATION_MM,
        le=MAX_OPERATION_DIAMETER_MM,
        description="Required for kinds hole, boss and slot; ignored otherwise.",
    )
    length: float | None = Field(
        default=None,
        ge=MIN_OPERATION_MM,
        le=MAX_OPERATION_SIZE_MM,
        description="Required for the slot kind; ignored otherwise.",
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

    @model_validator(mode="after")
    def _fill_extrude_height(self) -> ModelSpecification:
        """Default a missing ``extrude`` height to the object height.

        Small local models frequently emit an ``extrude`` primitive with
        ``height: null`` even though the CAD backend needs it. A 2D extrusion's
        height *is* the object's height, so the missing value is taken from
        ``dimensions.height`` (clamped to the printable bounds). This keeps the
        LLM<->CAD contract valid without weakening the CAD validation, and is
        idempotent. ``wall_thickness`` is deliberately **not** guessed: absent
        means a solid extrusion.
        """
        for primitive in self.primitives:
            if primitive.type != "extrude" or primitive.height is not None:
                continue
            primitive.height = min(max(self.dimensions.height, MIN_PRIMITIVE_MM), MAX_PRIMITIVE_MM)
        return self

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
# Planner clarification contract (docs/planner-clarification.md 1).
# ---------------------------------------------------------------------------

#: Maximum length of a clarification question.
MAX_CLARIFICATION_QUESTION = 300
#: Maximum length of the Planner's assumed answer (or the user's reply).
MAX_CLARIFICATION_ANSWER = 600
#: Maximum length of the dotted field path a clarification refers to.
MAX_CLARIFICATION_FIELD = 120


class Clarification(BaseModel):
    """One question the Planner asked (or silently assumed) about the request.

    ``kind="assumed"`` means the Planner filled ``answer`` with its own guess
    and the run continues; ``kind="needs_user_input"`` means it deliberately
    left ``answer`` empty and, under the ``ask`` policy, the run stops until the
    user answers (docs/planner-clarification.md 1). ``field`` is an optional
    dotted path such as ``"dimensions.width"``.

    This is a Planner-only contract: it is part of ``PlannerPlan``, never of
    :class:`ModelSpecification`, so it never reaches the CAD backend and the
    strict LLM<->CAD contract (terv.md 8. fejezet) stays unchanged.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(
        max_length=MAX_CLARIFICATION_QUESTION,
        description="The question put to the user (or the assumption made explicit).",
    )
    answer: str = Field(
        default="",
        max_length=MAX_CLARIFICATION_ANSWER,
        description="The Planner's assumed answer; empty when the user must answer.",
    )
    kind: Literal["assumed", "needs_user_input"] = Field(
        default="assumed",
        description=(
            "assumed means the Planner guessed an answer and the run continues; "
            "needs_user_input means it must ask the user."
        ),
    )
    field: str = Field(
        default="",
        max_length=MAX_CLARIFICATION_FIELD,
        description='Optional dotted spec path the question is about, e.g. "dimensions.width".',
    )


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
