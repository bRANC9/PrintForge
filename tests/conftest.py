"""Shared fixtures for the top-level ``tests/`` tree.

The colocated per-app tests (``backend/agents/llm/tests`` etc.) have their own
``conftest`` scope and do not inherit these fixtures. Only reusable *data*
fixtures live here; behaviour-specific setup stays next to the tests that need
it.
"""

from __future__ import annotations

import pytest
from factories import (
    FilamentProfileFactory,
    ModelVersionFactory,
    NotificationFactory,
    PrinterFactory,
    PrinterProfileFactory,
    PrintJobFactory,
    ProcessProfileFactory,
    ProjectFactory,
    UserFactory,
    WorkspaceFactory,
    WorkspaceMemberFactory,
)
from rest_framework.test import APIClient

__all__ = [
    "FilamentProfileFactory",
    "ModelVersionFactory",
    "NotificationFactory",
    "PrintJobFactory",
    "PrinterFactory",
    "PrinterProfileFactory",
    "ProcessProfileFactory",
    "ProjectFactory",
    "UserFactory",
    "WorkspaceFactory",
    "WorkspaceMemberFactory",
]


@pytest.fixture(autouse=True)
def _fast_password_hasher(settings):
    """Use MD5 for password hashing inside the test run.

    Django's default PBKDF2 hasher is deliberately slow (~1s per call). The
    factories create a fresh user (with a password) for most tests, and that
    single ``set_password`` call dominated per-test setup. MD5 makes hashing
    effectively free while still exercising the real ``User.set_password`` /
    ``check_password`` code paths.

    This is **test-only**: it overrides ``PASSWORD_HASHERS`` at runtime through
    pytest-django's ``settings`` fixture, so production configuration
    (``config/settings.py``) keeps the secure PBKDF2 default. The colocated
    ``backend/*/tests`` get the same fixture from ``backend/conftest.py``.
    """
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def user():
    """A single persisted user."""
    return UserFactory()


@pytest.fixture
def other_user():
    """A second user, useful for 'not the caller' assertions."""
    return UserFactory()


@pytest.fixture
def workspace(user):
    """A workspace owned by :func:`user` (owner is an OWNER member)."""
    return WorkspaceFactory(owner=user)


@pytest.fixture
def project(workspace):
    """A project in :func:`workspace`, created by its owner."""
    return ProjectFactory(workspace=workspace, created_by=workspace.owner)


@pytest.fixture
def version(project):
    """A model version of :func:`project`, created by its creator."""
    return ModelVersionFactory(project=project, created_by=project.created_by)


@pytest.fixture
def api_client():
    """An unauthenticated DRF client."""
    return APIClient()


@pytest.fixture
def auth_client(user):
    """A DRF client authenticated as :func:`user`."""
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def local_storage(tmp_path):
    """A :class:`files.services.LocalStorage` rooted in the test's tmp dir."""
    from files.services import LocalStorage

    return LocalStorage(root=tmp_path)
