"""Create the local dev superuser (idempotent) — mirrors `manage.py createsuperuser`."""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from accounts.models import User  # noqa: E402

USERNAME = os.environ.get("LOCAL_ADMIN_USERNAME", "branc9.kb@gmail.com")
EMAIL = os.environ.get("LOCAL_ADMIN_EMAIL", "branc9.kb@gmail.com")
PASSWORD = os.environ["LOCAL_ADMIN_PASSWORD"]

user, created = User.objects.get_or_create(
    username=USERNAME,
    defaults={"email": EMAIL, "is_staff": True, "is_superuser": True},
)
user.email = EMAIL
user.is_staff = True
user.is_superuser = True
user.set_password(PASSWORD)
user.save()

print(f"{'created' if created else 'updated'}: {user.username} (staff={user.is_staff})")
