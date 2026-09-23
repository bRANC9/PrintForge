from django.contrib import admin

from .models import Skill


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "kind",
        "object_kind",
        "is_builtin",
        "is_public",
    )
    search_fields = ("name", "slug", "description", "object_kind")
    list_filter = ("kind", "is_builtin", "is_public", "workspace")
    filter_horizontal = ("tags",)
