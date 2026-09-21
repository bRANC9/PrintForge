from django.contrib import admin

from .models import AppSettings


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
