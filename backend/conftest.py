"""Root conftest for the colocated ``backend/*/tests`` suites.

The top-level ``tests/`` tree has its own :mod:`tests.conftest`; these fixtures
intentionally do **not** cross the ``backend/`` boundary, so anything shared by
the colocated app tests lives here.

Only test-runner concerns belong here -- never application behaviour.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fast_password_hasher(settings):
    """Use MD5 for password hashing inside the test run.

    Django's default PBKDF2 hasher is deliberately slow (~1s per call). The
    factories create a fresh :class:`~accounts.models.User` (with a password)
    for most tests, and that single ``set_password`` call dominated per-test
    setup. MD5 makes hashing effectively free while still exercising the real
    ``User.set_password`` / ``check_password`` code paths.

    This is **test-only**: it overrides ``PASSWORD_HASHERS`` at runtime through
    pytest-django's ``settings`` fixture, so production configuration
    (``config/settings.py``) keeps the secure PBKDF2 default. Never enable this
    outside a test run.
    """
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
