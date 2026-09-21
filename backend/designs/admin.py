from django.contrib import admin

from .models import ModelVersion


@admin.register(ModelVersion)
class ModelVersionAdmin(admin.ModelAdmin):
    list_display = ("project", "version", "created_by", "created_at")
    list_filter = ("project",)
    readonly_fields = ("created_at",)
