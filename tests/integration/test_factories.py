"""Sanity tests for the shared ``tests/factories.py`` helpers.

The factories encode real invariants (hashed passwords, owner membership,
owner-created projects, validated specifications); if those drift the rest of
the suite would be testing unrealistic data, so pin them here.
"""

from __future__ import annotations

import pytest
from factories import (
    ModelVersionFactory,
    ProjectFactory,
    UserFactory,
    WorkspaceFactory,
    WorkspaceMemberFactory,
)

from agents.spec import ModelSpecification
from workspaces.models import WorkspaceRole

pytestmark = pytest.mark.django_db


def test_user_factory_hashes_the_password():
    user = UserFactory()

    assert user.password != "test-pass-123"
    assert user.check_password("test-pass-123") is True


def test_workspace_factory_adds_owner_as_owner_member():
    workspace = WorkspaceFactory()

    member = workspace.members.get(user=workspace.owner)
    assert member.role == WorkspaceRole.OWNER


def test_workspace_factory_can_skip_owner_membership():
    workspace = WorkspaceFactory(owner_membership=False)

    assert workspace.members.count() == 0


def test_workspace_member_factory_defaults_to_member():
    member = WorkspaceMemberFactory()

    assert member.role == WorkspaceRole.MEMBER
    assert member.workspace.members.filter(pk=member.pk).exists()


def test_project_factory_created_by_defaults_to_workspace_owner():
    project = ProjectFactory()

    assert project.created_by_id == project.workspace.owner_id


def test_model_version_factory_carries_a_valid_specification():
    version = ModelVersionFactory()

    validated = ModelSpecification.model_validate(version.specification_json)
    assert validated.object == "phone_holder"
    assert validated.material == "PETG"
    assert version.created_by_id == version.project.created_by_id


def test_model_version_factory_batch_gets_unique_versions():
    project = ProjectFactory()

    versions = ModelVersionFactory.create_batch(2, project=project)

    numbers = sorted(version.version for version in versions)
    assert len(set(numbers)) == 2
    assert project.versions.count() == 2
