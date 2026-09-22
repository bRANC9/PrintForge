from django.contrib import admin

from .models import (
    BuildPlate,
    FilamentProfile,
    PlateItem,
    PrinterProfile,
    ProcessProfile,
)


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


@admin.register(BuildPlate)
class BuildPlateAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "printer_profile", "created_by", "updated_at")
    list_filter = ("printer_profile",)
    search_fields = ("name", "project__name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(PlateItem)
class PlateItemAdmin(admin.ModelAdmin):
    list_display = (
        "build_plate",
        "model_version",
        "position_x",
        "position_y",
        "position_z",
        "rotation_z",
        "scale",
    )
    list_filter = ("build_plate",)
    search_fields = ("build_plate__name", "model_version__project__name")
    readonly_fields = ("created_at",)
