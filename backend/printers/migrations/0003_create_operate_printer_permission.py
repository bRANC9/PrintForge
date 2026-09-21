"""Create the ``printers.operate_printer`` permission (terv.md 20. fejezet).

Printer control is a separate permission (``PRINTER_OPERATOR``). Django only
auto-creates the default add/change/delete/view permissions, so the custom
``operate_printer`` codename that ``printers.permissions.user_can_operate_printer``
checks via ``user.has_perm("printers.operate_printer")`` must be seeded here.

The permission is attached to the ``printers.Printer`` content type. ``has_perm``
matches on ``app_label.codename``, so the exact model does not matter, but
``Printer`` is the object the permission governs. The operation is idempotent
(``get_or_create``) and reversed by deleting the row.
"""

from django.conf import settings
from django.db import migrations

PERMISSION_CODENAME = "operate_printer"
PERMISSION_NAME = "Can operate printers"


def create_operate_printer_permission(apps, schema_editor):
    content_type_model = apps.get_model("contenttypes", "ContentType")
    permission_model = apps.get_model("auth", "Permission")
    printer_model = apps.get_model("printers", "Printer")

    content_type = content_type_model.objects.get_for_model(printer_model)
    permission_model.objects.get_or_create(
        content_type=content_type,
        codename=PERMISSION_CODENAME,
        defaults={"name": PERMISSION_NAME},
    )


def remove_operate_printer_permission(apps, schema_editor):
    content_type_model = apps.get_model("contenttypes", "ContentType")
    permission_model = apps.get_model("auth", "Permission")
    printer_model = apps.get_model("printers", "Printer")

    content_type = content_type_model.objects.get_for_model(printer_model)
    permission_model.objects.filter(
        content_type=content_type,
        codename=PERMISSION_CODENAME,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("printers", "0002_printjob_printer_profile_printjob_slicing_json"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(
            create_operate_printer_permission,
            remove_operate_printer_permission,
        ),
    ]
