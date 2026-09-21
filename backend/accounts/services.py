"""User services.

Business logic lives here, not in views, so the same functions can be called
by the DRF API and (later) by MCP tools.
"""

from django.contrib.auth import get_user_model

User = get_user_model()


def create_user(*, username: str, email: str, password: str, **extra) -> "User":
    return User.objects.create_user(username=username, email=email, password=password, **extra)


def get_user_by_email(email: str) -> "User | None":
    return User.objects.filter(email__iexact=email).first()
