"""factory_boy factories for the PrintForge test suite.

Everything here is deterministic (sequences only, no ``Faker`` / randomness), so
the same suite behaves identically on sqlite locally and PostgreSQL + pgvector
in CI. Factories mirror the invariants of the real services:

* :class:`WorkspaceFactory` makes the owner an explicit ``OWNER``
  ``WorkspaceMember`` -- exactly like ``workspaces.services.create_workspace``;
* :class:`ProjectFactory` defaults ``created_by`` to the workspace owner (a
  member), but callers can always override it.

These factories create database rows, so any test using them needs the
``django_db`` marker (or a fixture that pulls in ``db``).
"""

from __future__ import annotations

import factory
from django.contrib.auth import get_user_model

from agents.spec import ModelSpecification
from designs.models import ModelVersion
from projects.models import Project
from workspaces.models import Workspace, WorkspaceMember, WorkspaceRole

User = get_user_model()


class UserFactory(factory.django.DjangoModelFactory):
    """A login-capable user with a hashed password."""

    class Meta:
        model = User

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda obj: f"{obj.username}@example.com")
    display_name = factory.LazyAttribute(lambda obj: obj.username.replace("_", " ").title())
    password = factory.django.Password("test-pass-123")


class WorkspaceFactory(factory.django.DjangoModelFactory):
    """A workspace whose owner is also an explicit ``OWNER`` member."""

    class Meta:
        model = Workspace
        skip_postgeneration_save = True

    name = factory.Sequence(lambda n: f"Workspace {n}")
    owner = factory.SubFactory(UserFactory)

    @factory.post_generation
    def owner_membership(self, create, extracted, **kwargs):  # noqa: ANN001
        """Recreate the ``create_workspace`` invariant (owner is a member).

        Pass ``owner_membership=False`` to skip it when a test wants a workspace
        without any membership row.
        """
        if not create or extracted is False:
            return
        WorkspaceMember.objects.get_or_create(
            workspace=self,
            user=self.owner,
            defaults={"role": WorkspaceRole.OWNER},
        )


class WorkspaceMemberFactory(factory.django.DjangoModelFactory):
    """Membership row joining a user to a workspace with a role."""

    class Meta:
        model = WorkspaceMember

    workspace = factory.SubFactory(WorkspaceFactory)
    user = factory.SubFactory(UserFactory)
    role = WorkspaceRole.MEMBER


class ProjectFactory(factory.django.DjangoModelFactory):
    """A project inside a workspace, created by the workspace owner by default."""

    class Meta:
        model = Project

    workspace = factory.SubFactory(WorkspaceFactory)
    name = factory.Sequence(lambda n: f"Project {n}")
    description = ""
    created_by = factory.SelfAttribute("workspace.owner")


class ModelVersionFactory(factory.django.DjangoModelFactory):
    """A model version carrying the validated terv.md 8. example specification."""

    class Meta:
        model = ModelVersion

    project = factory.SubFactory(ProjectFactory)
    version = factory.Sequence(lambda n: n + 1)
    prompt = factory.Sequence(lambda n: f"prompt {n}")
    specification_json = factory.LazyFunction(lambda: ModelSpecification.example())
    validation_json = factory.LazyFunction(dict)
    created_by = factory.SelfAttribute("project.created_by")
