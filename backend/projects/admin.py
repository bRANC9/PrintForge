from django.contrib import admin

from .models import Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "created_by", "updated_at")
    list_filter = ("workspace",)
    search_fields = ("name", "description")
