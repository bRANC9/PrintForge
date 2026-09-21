from django.contrib import admin

from .models import FilamentProfile, PrinterProfile, ProcessProfile


@admin.register(PrinterProfile)
class PrinterProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "printer_model", "backend", "is_default", "created_at")
    list_filter = ("backend", "is_default")
    search_fields = ("name", "printer_model")
    readonly_fields = ("created_at",)


@admin.register(FilamentProfile)
class FilamentProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "material", "brand", "color", "created_at")
    list_filter = ("material", "brand")
    search_fields = ("name", "material", "brand", "color")
    readonly_fields = ("created_at",)


@admin.register(ProcessProfile)
class ProcessProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "layer_height", "created_at")
    search_fields = ("name",)
    readonly_fields = ("created_at",)
