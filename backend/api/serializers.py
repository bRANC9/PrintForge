import re

from rest_framework import serializers

from accounts.models import User
from agents.models import AgentRun
from configuration.services import SETTING_NAMES
from designs.models import ModelVersion
from notifications.models import Notification
from printers.models import Printer, PrintJob, PrintJobStatus
from projects.models import Project, ProjectShare, Rating, Tag
from slicers.models import (
    BuildPlate,
    FilamentProfile,
    PlateItem,
    PrinterProfile,
    ProcessProfile,
)
from workspaces.models import Workspace, WorkspaceMember


class WorkspaceMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkspaceMember
        fields = ["id", "user", "role"]


class WorkspaceSerializer(serializers.ModelSerializer):
    members = WorkspaceMemberSerializer(many=True, read_only=True)

    class Meta:
        model = Workspace
        fields = ["id", "name", "owner", "members", "created_at", "updated_at"]
        read_only_fields = ["owner", "created_at", "updated_at"]


class TagNameListField(serializers.Field):
    """M2M tag field: reads slugs, accepts a list of tag *names* on write.

    Tag rows are created by ``projects.services.set_project_tags`` from the
    view, so this field only validates/normalises the payload (no ORM writes).
    """

    default_error_messages = {
        "not_a_list": "Expected a list of tag names.",
        "invalid_name": "Tag names must be non-empty strings.",
    }

    def to_representation(self, value):
        return [tag.slug for tag in value.all()]

    def to_internal_value(self, data):
        if not isinstance(data, list):
            self.fail("not_a_list")
        names: list[str] = []
        for item in data:
            if not isinstance(item, str) or not item.strip():
                self.fail("invalid_name")
            names.append(item.strip())
        return names


class ProjectSerializer(serializers.ModelSerializer):
    tags = TagNameListField(required=False)

    class Meta:
        model = Project
        fields = [
            "id",
            "workspace",
            "name",
            "description",
            "is_public",
            "license",
            "tags",
            "download_count",
            "description_source",
            "tags_source",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "created_by",
            "created_at",
            "updated_at",
            "is_public",
            "download_count",
            "description_source",
            "tags_source",
        ]


class CommunityProjectSerializer(ProjectSerializer):
    """Read-only public projection with the aggregate rating for the UI."""

    rating = serializers.SerializerMethodField()
    license_display = serializers.CharField(source="get_license_display", read_only=True)

    class Meta(ProjectSerializer.Meta):
        fields = [*ProjectSerializer.Meta.fields, "rating", "license_display"]

    def get_rating(self, obj: Project) -> dict:
        average = getattr(obj, "average_rating", None)
        count = getattr(obj, "rating_count", 0) or 0
        return {
            "average": round(float(average), 2) if average is not None else None,
            "count": int(count),
        }


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name", "slug", "created_at"]
        read_only_fields = fields


class PrinterSerializer(serializers.ModelSerializer):
    """Read-only printer registry row.

    Deliberately omits ``host`` and ``api_key``: the host is operational detail
    and the key is a secret that must never leave the server.
    """

    class Meta:
        model = Printer
        fields = ["id", "name", "backend", "is_active", "created_at"]
        read_only_fields = fields


class RatingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rating
        fields = ["id", "project", "user", "score", "created_at", "updated_at"]
        read_only_fields = ["id", "project", "user", "created_at", "updated_at"]


class RatingWriteSerializer(serializers.Serializer):
    """Write payload for ``POST /api/v1/projects/{id}/rate/``."""

    score = serializers.IntegerField(min_value=1, max_value=5)


class ProjectShareSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectShare
        fields = [
            "id",
            "project",
            "shared_with",
            "token",
            "created_by",
            "created_at",
            "expires_at",
        ]
        read_only_fields = ["id", "project", "token", "created_by", "created_at"]


class ProjectShareCreateSerializer(serializers.Serializer):
    """Write payload for ``POST /api/v1/projects/{id}/share/``."""

    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), required=False, allow_null=True
    )
    create_link = serializers.BooleanField(required=False, default=False)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)


class ModelVersionSerializer(serializers.ModelSerializer):
    """Read-only representation of a :class:`designs.ModelVersion`."""

    status = serializers.SerializerMethodField()

    class Meta:
        model = ModelVersion
        fields = [
            "id",
            "project",
            "version",
            "prompt",
            "specification_json",
            "validation_json",
            "scad_file",
            "stl_file",
            "glb_file",
            "preview_image",
            "reference_image",
            "reference_note",
            "status",
            "created_by",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "project",
            "version",
            "prompt",
            "specification_json",
            "validation_json",
            "scad_file",
            "stl_file",
            "glb_file",
            "preview_image",
            "reference_image",
            "reference_note",
            "status",
            "created_by",
            "created_at",
        ]

    def get_status(self, obj: ModelVersion) -> str:
        return (obj.validation_json or {}).get("status") or "pending"


class VersionCreateSerializer(serializers.Serializer):
    """Write payload for ``POST /api/v1/projects/{id}/versions/``.

    Accepts JSON or multipart (for the optional ``reference_image`` upload,
    terv.md 27. fejezet).
    """

    prompt = serializers.CharField(required=False, allow_blank=True, default="")
    specification_json = serializers.JSONField(required=False)
    reference_note = serializers.CharField(required=False, allow_blank=True, default="")
    reference_image = serializers.ImageField(required=False, allow_null=True)


class AgentRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentRun
        fields = [
            "id",
            "project",
            "status",
            "user_prompt",
            "state_json",
            "started_at",
            "completed_at",
            "error",
            "created_at",
        ]
        read_only_fields = fields


class PrintJobSerializer(serializers.ModelSerializer):
    """Read-only print-queue entry with the denormalised names the UI shows."""

    printer_name = serializers.SerializerMethodField()
    filament_name = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()
    version = serializers.SerializerMethodField()

    class Meta:
        model = PrintJob
        fields = [
            "id",
            "project",
            "project_name",
            "model_version",
            "version",
            "build_plate",
            "printer",
            "printer_name",
            "filament",
            "filament_name",
            "slicer_profile",
            "printer_profile",
            "status",
            "priority",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_printer_name(self, obj: PrintJob) -> str:
        return obj.printer.name

    def get_filament_name(self, obj: PrintJob) -> str | None:
        return obj.filament.name if obj.filament_id else None

    def get_project_name(self, obj: PrintJob) -> str:
        return obj.project.name

    def get_version(self, obj: PrintJob) -> int | None:
        return obj.model_version.version if obj.model_version_id else None


class PrintJobCreateSerializer(serializers.Serializer):
    """Write payload for ``POST /api/v1/print-jobs/``.

    A job targets exactly one of ``model_version`` / ``build_plate`` (the DB
    check constraint only requires *at least* one; the API keeps the payload
    unambiguous).
    """

    project = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all())
    model_version = serializers.PrimaryKeyRelatedField(
        queryset=ModelVersion.objects.all(), required=False, allow_null=True
    )
    build_plate = serializers.PrimaryKeyRelatedField(
        queryset=BuildPlate.objects.all(), required=False, allow_null=True
    )
    printer = serializers.PrimaryKeyRelatedField(queryset=Printer.objects.all())
    filament = serializers.PrimaryKeyRelatedField(
        queryset=FilamentProfile.objects.all(), required=False, allow_null=True
    )
    slicer_profile = serializers.PrimaryKeyRelatedField(
        queryset=ProcessProfile.objects.all(), required=False, allow_null=True
    )
    printer_profile = serializers.PrimaryKeyRelatedField(
        queryset=PrinterProfile.objects.all(), required=False, allow_null=True
    )
    priority = serializers.IntegerField(required=False, default=0, min_value=0)

    def validate(self, attrs):
        has_model = attrs.get("model_version") is not None
        has_plate = attrs.get("build_plate") is not None
        if has_model == has_plate:
            raise serializers.ValidationError(
                "Provide exactly one of 'model_version' or 'build_plate'."
            )
        return attrs


class BuildPlateSerializer(serializers.ModelSerializer):
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = BuildPlate
        fields = [
            "id",
            "project",
            "name",
            "printer_profile",
            "settings_json",
            "created_by",
            "created_at",
            "updated_at",
            "item_count",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at", "item_count"]
        validators = [
            serializers.UniqueTogetherValidator(
                queryset=BuildPlate.objects.all(),
                fields=["project", "name"],
            )
        ]

    def get_item_count(self, obj: BuildPlate) -> int:
        return obj.items.count()


class PlateItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlateItem
        fields = [
            "id",
            "build_plate",
            "model_version",
            "position_x",
            "position_y",
            "position_z",
            "rotation_z",
            "scale",
            "settings_json",
            "created_at",
        ]
        read_only_fields = ["id", "build_plate", "created_at"]


class PrintJobTransitionSerializer(serializers.Serializer):
    """Write payload for ``POST /api/v1/print-jobs/{id}/transition/``."""

    status = serializers.ChoiceField(choices=PrintJobStatus.choices)


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "kind", "message", "url", "is_read", "created_at"]
        read_only_fields = fields


class SettingsUpdateSerializer(serializers.Serializer):
    """Dynamic ``{name: value, ...}`` payload for ``PATCH /api/v1/settings/``.

    Unknown names are rejected here; value coercion/validation is handled by
    ``configuration.services.update_settings``.
    """

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Expected an object of setting overrides.")
        unknown = sorted(set(data) - set(SETTING_NAMES))
        if unknown:
            raise serializers.ValidationError({name: "Unknown setting." for name in unknown})
        return dict(data)


#: Ollama model tags allow letters, digits and ``._:/-`` (e.g. ``library/llama3:8b``).
_OLLAMA_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]+$")


def validate_ollama_model_name(value: str) -> str:
    """Strict, shared validation for Ollama model names.

    Used by the pull / use / delete endpoints (all go through
    :class:`OllamaModelNameSerializer`). Rejects empty/whitespace-only names,
    names longer than 200 chars, leading/trailing ``/``, empty/``.``/``..`` path
    segments and any character outside ``[A-Za-z0-9._:/-]``.
    """
    if not value:
        raise serializers.ValidationError("A model name is required.")
    if len(value) > 200:
        raise serializers.ValidationError("Model name must be at most 200 characters.")
    if not _OLLAMA_MODEL_NAME_PATTERN.match(value):
        raise serializers.ValidationError("Invalid model name.")
    if value.startswith("/") or value.endswith("/"):
        raise serializers.ValidationError("Invalid model name.")
    if any(segment in ("", ".", "..") for segment in value.split("/")):
        raise serializers.ValidationError("Invalid model name.")
    return value


class OllamaModelNameSerializer(serializers.Serializer):
    """Write payload for Ollama model pull/use (and the DELETE ``name`` param)."""

    name = serializers.CharField(
        max_length=200,
        trim_whitespace=False,
        validators=[validate_ollama_model_name],
    )
