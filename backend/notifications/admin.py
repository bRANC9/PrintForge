from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "kind", "is_read", "created_at")
    list_filter = ("kind", "is_read")
    search_fields = ("message", "user__username")
    readonly_fields = ("created_at",)
