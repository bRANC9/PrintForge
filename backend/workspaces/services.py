from django.db import transaction

from accounts.models import User

from .models import Workspace, WorkspaceMember, WorkspaceRole


@transaction.atomic
def create_workspace(*, name: str, owner: User) -> Workspace:
    """Create a workspace and make the owner an explicit OWNER member."""
    workspace = Workspace.objects.create(name=name, owner=owner)
    WorkspaceMember.objects.create(workspace=workspace, user=owner, role=WorkspaceRole.OWNER)
    return workspace


def add_member(
    *, workspace: Workspace, user: User, role: str = WorkspaceRole.MEMBER
) -> WorkspaceMember:
    member, _created = WorkspaceMember.objects.update_or_create(
        workspace=workspace,
        user=user,
        defaults={"role": role},
    )
    return member


def workspaces_for_user(user: User):
    return Workspace.objects.filter(members__user=user).distinct()
