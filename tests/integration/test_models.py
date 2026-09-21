import pytest
from django.contrib.auth import get_user_model

from designs.services import create_next_version
from projects.services import create_project
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


def test_create_workspace_creates_owner_membership(user):
    workspace = create_workspace(name="Lab", owner=user)
    member = workspace.members.get()
    assert member.user == user
    assert member.role == WorkspaceRole.OWNER


def test_add_member_is_idempotent(user):
    workspace = create_workspace(name="Lab", owner=user)
    other = User.objects.create_user(username="bob", email="bob@example.com", password="pw")
    add_member(workspace=workspace, user=other, role=WorkspaceRole.VIEWER)
    add_member(workspace=workspace, user=other, role=WorkspaceRole.MEMBER)
    assert workspace.members.filter(user=other).count() == 1
    assert workspace.members.get(user=other).role == WorkspaceRole.MEMBER


def test_project_and_version_numbering(user):
    workspace = create_workspace(name="Lab", owner=user)
    project = create_project(workspace=workspace, name="Phone holder", created_by=user)
    v1 = create_next_version(project=project, prompt="first", created_by=user)
    v2 = create_next_version(project=project, prompt="second", created_by=user)
    assert (v1.version, v2.version) == (1, 2)
    assert project.versions.order_by("version").first().prompt == "first"
