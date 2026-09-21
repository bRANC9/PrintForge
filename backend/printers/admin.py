from django.contrib import admin

from .models import Printer, PrintJob


@admin.register(Printer)
class PrinterAdmin(admin.ModelAdmin):
    list_display = ("name", "backend", "host", "is_active", "created_at")
    list_filter = ("backend", "is_active")
    search_fields = ("name", "host")
    readonly_fields = ("created_at",)


@admin.register(PrintJob)
class PrintJobAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "printer", "status", "priority", "created_at")
    list_filter = ("status", "printer")
    search_fields = ("project__name",)
    readonly_fields = ("created_at", "updated_at")
