from rest_framework import serializers

from agents.models import AgentRun
from designs.models import ModelVersion
from notifications.models import Notification
from printers.models import Printer, PrintJob, PrintJobStatus
from projects.models import Project
from slicers.models import FilamentProfile, PrinterProfile, ProcessProfile
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


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = [
            "id",
            "workspace",
            "name",
            "description",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_by", "created_at", "updated_at"]


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
            "status",
            "created_by",
            "created_at",
        ]

    def get_status(self, obj: ModelVersion) -> str:
        return (obj.validation_json or {}).get("status") or "pending"


class VersionCreateSerializer(serializers.Serializer):
    """Write payload for ``POST /api/v1/projects/{id}/versions/``."""

    prompt = serializers.CharField(required=False, allow_blank=True, default="")
    specification_json = serializers.JSONField(required=False)


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

    def get_version(self, obj: PrintJob) -> int:
        return obj.model_version.version


class PrintJobCreateSerializer(serializers.Serializer):
    """Write payload for ``POST /api/v1/print-jobs/``."""

    project = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all())
    model_version = serializers.PrimaryKeyRelatedField(queryset=ModelVersion.objects.all())
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


class PrintJobTransitionSerializer(serializers.Serializer):
    """Write payload for ``POST /api/v1/print-jobs/{id}/transition/``."""

    status = serializers.ChoiceField(choices=PrintJobStatus.choices)


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "kind", "message", "url", "is_read", "created_at"]
        read_only_fields = fields
