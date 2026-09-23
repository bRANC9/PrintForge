import re

from rest_framework import serializers

from accounts.models import User
from agents.models import AgentRun
from configuration import model_catalog
from configuration.services import SETTING_NAMES
from designs.models import ModelVersion
from notifications.models import Notification
from printers.models import Printer, PrintJob, PrintJobStatus
from projects.models import Project, ProjectShare, Rating, Tag
from skills.models import Skill, SkillKind
from skills.services import skills_visible_to
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
    # Persistent skill assignment (docs/skills.md 2.). The queryset is scoped to
    # the caller's visible skills per request in ``get_fields`` below.
    skills = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Skill.objects.all(),
        required=False,
    )

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
            "skills",
            "download_count",
            "print_count",
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
            "print_count",
            "description_source",
            "tags_source",
        ]

    def get_fields(self):
        """Scope the writable ``skills`` picker to what the caller can see.

        ``PrimaryKeyRelatedField`` validates against a fixed queryset, so the
        declared all-skills queryset would let a caller attach a private skill
        from a workspace they are not a member of. The field is therefore
        rebuilt per request from
        :func:`skills.services.skills_visible_to` (built-ins + public + the
        caller's workspace skills).
        """
        fields = super().get_fields()
        request = self.context.get("request")
        user = getattr(request, "user", None) if request is not None else None
        if user is not None and getattr(user, "is_authenticated", False):
            fields["skills"] = serializers.PrimaryKeyRelatedField(
                many=True,
                queryset=skills_visible_to(user),
                required=False,
            )
        return fields


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


class SkillSerializer(serializers.ModelSerializer):
    """Skill CRUD projection (docs/skills.md 6.).

    ``tags`` uses the same name-based M2M field as projects; the view hands the
    names to ``skills.services`` (never a raw ``tags.set()``). ``slug`` is
    derived by the model and ``is_builtin`` / ``created_by`` are server-owned.
    """

    tags = TagNameListField(required=False)
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = Skill
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "kind",
            "kind_display",
            "template_key",
            "object_kind",
            "guidance",
            "defaults_json",
            "constraints_json",
            "tags",
            "workspace",
            "is_builtin",
            "is_public",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "slug",
            "kind_display",
            "is_builtin",
            "created_by",
            "created_at",
            "updated_at",
        ]

    def validate_kind(self, value):
        if value not in SkillKind.values:
            raise serializers.ValidationError("Unknown skill kind.")
        return value


class PrinterSerializer(serializers.ModelSerializer):
    """Printer registry row.

    ``host`` and ``api_key`` are operational/secret fields: they are only
    visible to staff (``host``) or never returned at all (``api_key``, which is
    write-only). Non-staff callers get the plain read projection.
    """

    api_key = serializers.CharField(
        write_only=True, required=False, allow_blank=True, max_length=255
    )

    class Meta:
        model = Printer
        fields = ["id", "name", "backend", "host", "api_key", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request is None or not getattr(request.user, "is_staff", False):
            fields.pop("host", None)
            fields.pop("api_key", None)
        return fields

    def validate_backend(self, value):
        from printers.factory import supported_backends

        if value and value.strip().lower() not in supported_backends():
            raise serializers.ValidationError(
                f"Unknown printer backend {value!r}; supported: " + ", ".join(supported_backends())
            )
        return value

    def update(self, instance, validated_data):
        # An empty api_key on update means "leave the stored secret unchanged".
        if validated_data.get("api_key", None) == "":
            validated_data.pop("api_key")
        return super().update(instance, validated_data)


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
    # Planner provenance (docs/planner-clarification.md 4-5.): the assumptions a
    # run made on the user's behalf and whether the result needs review. Both
    # live in ``validation_json``, so they are exposed read-only.
    assumptions = serializers.SerializerMethodField()
    review_required = serializers.SerializerMethodField()
    # The edit-chain parent is exposed as the raw id (docs/visual-editing.md
    # 3.4); the FK relation itself is never writable through the API.
    parent_version = serializers.IntegerField(
        source="parent_version_id", read_only=True, allow_null=True
    )

    class Meta:
        model = ModelVersion
        fields = [
            "id",
            "project",
            "version",
            "prompt",
            "specification_json",
            "validation_json",
            "assumptions",
            "review_required",
            "parent_version",
            "origin",
            "annotations_json",
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
            "assumptions",
            "review_required",
            "parent_version",
            "origin",
            "annotations_json",
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

    def get_assumptions(self, obj: ModelVersion) -> list:
        return list((obj.validation_json or {}).get("assumptions") or [])

    def get_review_required(self, obj: ModelVersion) -> bool:
        return bool((obj.validation_json or {}).get("review_required", False))


class AnnotationSerializer(serializers.Serializer):
    """One visual prompt annotation (docs/visual-editing.md 2., 3.4).

    Coordinates are mm in the original STL / OpenSCAD world space. The service
    layer (and, behind it, the LLM) only trusts the summarised ``region``, never
    the raw triangle list, but the payload shape is still validated here.
    """

    id = serializers.CharField(required=False, allow_blank=True, default="")
    kind = serializers.ChoiceField(choices=["point", "region"])
    point = serializers.ListField(
        child=serializers.FloatField(),
        min_length=3,
        max_length=3,
    )
    normal = serializers.ListField(
        child=serializers.FloatField(),
        min_length=3,
        max_length=3,
    )
    faces = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=list,
    )
    region = serializers.DictField(required=False, default=dict)
    instruction = serializers.CharField(required=False, allow_blank=True, default="")


class AnnotationEditSerializer(serializers.Serializer):
    """Write payload for ``POST /api/v1/versions/{id}/annotations/``."""

    prompt = serializers.CharField(max_length=2000)
    annotations = AnnotationSerializer(many=True, allow_empty=False)


class VersionCreateSerializer(serializers.Serializer):
    """Write payload for ``POST /api/v1/projects/{id}/versions/``.

    Accepts JSON or multipart (for the optional ``reference_image`` upload,
    terv.md 27. fejezet).

    ``skill_ids`` / ``auto_skill_selection`` are the per-run skill choice
    (docs/skills.md 3.): a manual id list wins, otherwise auto-selection picks
    the skills from the prompt. Both are optional and only forwarded to the
    agent workflow when it declares them (see ``api.views``).

    ``clarify`` selects the Planner's clarification policy
    (docs/planner-clarification.md 5.): ``"assume"`` (default) lets it guess and
    record assumptions, ``"ask"`` stops the run for user input when a critical
    value is missing. It is forwarded as the workflow's ``clarify_policy``.
    """

    prompt = serializers.CharField(required=False, allow_blank=True, default="")
    specification_json = serializers.JSONField(required=False)
    reference_note = serializers.CharField(required=False, allow_blank=True, default="")
    reference_image = serializers.ImageField(required=False, allow_null=True)
    # ``ListField`` accepts both a JSON array and repeated multipart keys
    # (``skill_ids=1&skill_ids=2``), which is how the UI submits the form.
    skill_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
    )
    auto_skill_selection = serializers.BooleanField(required=False, default=False)
    clarify = serializers.ChoiceField(
        choices=["ask", "assume"],
        required=False,
        default="assume",
    )


class VersionRegenerateSerializer(serializers.Serializer):
    """Write payload for ``POST /api/v1/versions/{id}/regenerate/``.

    Both fields are optional (docs/version-history-controls.md 3.):

    * no body -> plain regeneration of the base prompt;
    * ``prompt`` -> edited prompt, regenerated;
    * ``specification_json`` -> render that specification as a manual, derived
      version (the prompt becomes the version's label).
    """

    prompt = serializers.CharField(required=False, allow_blank=True, default="")
    specification_json = serializers.JSONField(required=False)


class ClarificationAnswerSerializer(serializers.Serializer):
    """One user answer to a Planner clarification (docs/planner-clarification.md 5.).

    ``field`` matches the dotted path of the original question (may be empty for
    questions without one); ``answer`` is the user's reply and is bounded to the
    same length the Planner contract allows (``agents.spec``).
    """

    field = serializers.CharField(required=False, allow_blank=True, default="", max_length=120)
    answer = serializers.CharField(max_length=600)


class ClarificationAnswersSerializer(serializers.Serializer):
    """Write payload for ``POST /api/v1/agent-runs/{id}/clarifications/``.

    At least one answer is required and at most 16 are accepted, matching the
    Planner's per-plan clarification bound (docs/planner-clarification.md 1.).
    """

    answers = ClarificationAnswerSerializer(many=True, allow_empty=False, max_length=16)


class AgentRunSerializer(serializers.ModelSerializer):
    """Read-only agent-run projection (docs/planner-clarification.md 5.).

    ``status`` surfaces the Planner's terminal ``"clarification"`` state, which
    the DB status alone cannot express: such a run is ``DONE`` (no version) with
    ``state_json["status"] == "clarification"``. ``clarifications`` (the pending
    questions) and ``assumptions`` (the Planner's own guesses) are read from
    ``state_json``.
    """

    status = serializers.SerializerMethodField()
    clarifications = serializers.SerializerMethodField()
    assumptions = serializers.SerializerMethodField()

    class Meta:
        model = AgentRun
        fields = [
            "id",
            "project",
            "status",
            "user_prompt",
            "state_json",
            "clarifications",
            "assumptions",
            "started_at",
            "completed_at",
            "error",
            "created_at",
        ]
        read_only_fields = fields

    def get_status(self, obj: AgentRun) -> str:
        if (obj.state_json or {}).get("status") == "clarification":
            return "clarification"
        return obj.status

    def get_clarifications(self, obj: AgentRun) -> list:
        return list((obj.state_json or {}).get("clarifications") or [])

    def get_assumptions(self, obj: AgentRun) -> list:
        return list((obj.state_json or {}).get("assumptions") or [])


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


class OllamaRecommendationQuerySerializer(serializers.Serializer):
    """Query params for the VRAM-based model advisor."""

    vram_gb = serializers.FloatField(min_value=1, max_value=2048)
    context = serializers.IntegerField(
        min_value=512,
        max_value=131072,
        required=False,
        default=8192,
    )
    category = serializers.ChoiceField(
        choices=list(model_catalog.CATEGORIES),
        required=False,
        allow_blank=True,
        default="",
    )


class OllamaRemoteQuerySerializer(serializers.Serializer):
    """Query params for the remote catalog proxy (ollama.com / Hugging Face)."""

    source = serializers.ChoiceField(choices=["ollama", "huggingface"])
    q = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    limit = serializers.IntegerField(min_value=1, max_value=50, required=False, default=20)


class OllamaModelNameSerializer(serializers.Serializer):
    """Write payload for Ollama model pull/use (and the DELETE ``name`` param)."""

    name = serializers.CharField(
        max_length=200,
        trim_whitespace=False,
        validators=[validate_ollama_model_name],
    )
