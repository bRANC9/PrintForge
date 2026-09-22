from django import forms
from django.contrib import admin

from .models import Printer, PrintJob


@admin.register(Printer)
class PrinterAdmin(admin.ModelAdmin):
    list_display = ("name", "backend", "host", "is_active", "created_at")
    list_filter = ("backend", "is_active")
    search_fields = ("name", "host")
    readonly_fields = ("created_at",)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # ``api_key`` is a secret: masked in the form, never shown in lists.
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == "api_key" and field is not None:
            field.widget = forms.PasswordInput(render_value=True)
        return field


@admin.register(PrintJob)
class PrintJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "project",
        "model_version",
        "build_plate",
        "printer",
        "status",
        "priority",
        "created_at",
    )
    list_filter = ("status", "printer")
    search_fields = ("project__name", "build_plate__name")
    readonly_fields = ("created_at", "updated_at")
