from django.contrib import admin

from .models import AppSettings, OllamaPull


@admin.register(AppSettings)
class AppSettingsAdmin(admin.ModelAdmin):
    """Editable singleton: the one row can be changed, never added/deleted."""

    list_display = ("id", "updated_at", "updated_by")
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        # Allow creating the row only if it does not exist yet.
        return not AppSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OllamaPull)
class OllamaPullAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "progress_percent", "created_at")
    list_filter = ("status",)
    search_fields = ("name",)
    readonly_fields = ("created_at", "started_at", "completed_at")
