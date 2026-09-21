from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user.

    Extends Django's user so we can add fields later without a painful
    migration. Login is by username in Phase 1; email is unique.
    """

    email = models.EmailField("email address", unique=True)
    display_name = models.CharField(max_length=150, blank=True)

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"

    def __str__(self) -> str:
        return self.display_name or self.get_username()
