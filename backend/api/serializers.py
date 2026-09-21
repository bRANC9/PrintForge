from rest_framework import serializers

from agents.models import AgentRun
from designs.models import ModelVersion
from projects.models import Project
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
