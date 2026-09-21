from rest_framework import serializers

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
