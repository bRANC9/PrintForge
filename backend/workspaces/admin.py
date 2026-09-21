from django.contrib import admin

from .models import Workspace, WorkspaceMember


class WorkspaceMemberInline(admin.TabularInline):
    model = WorkspaceMember
    extra = 0


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "created_at")
    search_fields = ("name",)
    inlines = [WorkspaceMemberInline]


@admin.register(WorkspaceMember)
class WorkspaceMemberAdmin(admin.ModelAdmin):
    list_display = ("workspace", "user", "role")
    list_filter = ("role",)
