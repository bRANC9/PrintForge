from django.contrib import admin

from .models import ModelDownload, Project, ProjectShare, Rating, Tag


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "workspace",
        "created_by",
        "is_public",
        "license",
        "download_count",
        "updated_at",
    )
    list_filter = ("workspace", "is_public", "license")
    search_fields = ("name", "description")
    filter_horizontal = ("tags",)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "created_at")
    search_fields = ("name", "slug")
    readonly_fields = ("created_at",)


@admin.register(ProjectShare)
class ProjectShareAdmin(admin.ModelAdmin):
    list_display = (
        "project",
        "shared_with",
        "token",
        "created_by",
        "created_at",
        "expires_at",
    )
    list_filter = ("created_at",)
    search_fields = ("project__name", "token")
    readonly_fields = ("created_at",)


@admin.register(Rating)
class RatingAdmin(admin.ModelAdmin):
    list_display = ("project", "user", "score", "created_at", "updated_at")
    list_filter = ("score",)
    search_fields = ("project__name", "user__username")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ModelDownload)
class ModelDownloadAdmin(admin.ModelAdmin):
    list_display = ("project", "model_version", "user", "created_at")
    list_filter = ("created_at",)
    search_fields = ("project__name", "user__username", "ip_hash")
    readonly_fields = ("created_at",)
