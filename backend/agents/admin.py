from django.contrib import admin

from .models import AgentRun


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "status", "created_at")
    list_filter = ("status",)
    readonly_fields = ("created_at",)
