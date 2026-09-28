"""Tests for :func:`designs.services.version_status`, the poller's view of a version.

``warnings`` joined this payload next to ``errors``: the advisory findings the
render paths record (a rendered envelope that contradicts the requested
dimensions, a multi-body mesh, a skipped primitive) have to travel in the same
response, or the UI can only show them after re-fetching the whole version
list. The key therefore has two states worth pinning separately: a version that
*has* something to say, and a clean one that must not grow an empty list --
"nothing to report" and "not reported" are different answers for a poller.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from designs.services import create_next_version, version_status
from projects.services import create_project
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()

#: The three findings the render paths can record, in the order they merge.
FINDINGS = [
    "rendered X extent 100.0mm differs from the requested width 40.0mm by +150% (tolerance +-25%)",
    "no usable primitives; synthesized a box from 'dimensions'",
    "mesh has 2 disconnected bodies",
]


@pytest.fixture
def user():
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


@pytest.fixture
def project(user):
    workspace = create_workspace(name="Lab", owner=user)
    return create_project(workspace=workspace, name="Holder", created_by=user)


@pytest.fixture
def version(project, user):
    return create_next_version(project=project, prompt="make a holder", created_by=user)


def _with_validation(version, validation):
    version.validation_json = validation
    version.save(update_fields=["validation_json"])
    return version


def test_status_reports_every_stored_warning(version):
    """The non-empty case: a version that has something to say says all of it.

    ``validation_json["warnings"]`` is written by three different producers --
    the dimension check, the generate step and the mesh import -- and the
    poller has no way to know which one wrote a given entry. Dropping the last
    one, re-ordering them or truncating the list would all hide a finding the
    user is meant to see.
    """
    _with_validation(
        version,
        {"status": "done", "stage": "done", "errors": [], "warnings": list(FINDINGS)},
    )

    assert version_status(version)["warnings"] == FINDINGS


def test_status_carries_warnings_next_to_errors(version):
    """They are separate channels: an advisory must not become a failure.

    A dimension finding lands on a version whose ``status`` is ``done`` and
    whose ``errors`` list is empty. Merging the two would make the UI show a
    red error for a part that printed fine.
    """
    _with_validation(
        version,
        {
            "status": "done",
            "stage": "done",
            "errors": [],
            "warnings": [FINDINGS[0]],
        },
    )

    status = version_status(version)

    assert status["status"] == "done"
    assert status["errors"] == []
    assert status["warnings"] == [FINDINGS[0]]


def test_a_clean_version_reports_an_empty_warning_list(version):
    """Missing and empty are the same answer, and it is a list."""
    _with_validation(version, {"status": "done", "stage": "done", "errors": []})

    status = version_status(version)

    assert status["warnings"] == []
    # A poller indexes the key, so a missing one would raise instead of showing
    # "nothing to report".
    assert "warnings" in status


def test_a_fresh_version_reports_the_pending_defaults(version):
    """A version nothing has touched yet still answers every key."""
    assert version.validation_json == {}

    assert version_status(version) == {
        "status": "pending",
        "stage": "pending",
        "errors": [],
        "warnings": [],
    }


def test_the_warning_list_is_a_copy(version):
    """Mutating the response must not rewrite the stored findings.

    The payload is built with ``list(...)`` on purpose: a caller (a view
    serializer, a test, the MCP tool) that appends to it would otherwise write
    through to ``validation_json`` on the instance it was handed.
    """
    _with_validation(version, {"status": "done", "errors": [], "warnings": [FINDINGS[0]]})

    status = version_status(version)
    status["warnings"].append("injected")
    status["errors"].append("injected")

    assert version.validation_json["warnings"] == [FINDINGS[0]]
    assert version.validation_json["errors"] == []


def test_the_payload_shape_is_stable(version):
    """Four keys, always: the response is a documented contract.

    ``errors``/``status``/``stage`` are consumed by the viewer and the MCP
    tools, so a key that appears only for some versions (as ``warnings`` did
    before it was added everywhere) breaks every reader that iterates it.
    """
    _with_validation(
        version,
        {"status": "failed", "stage": "export", "errors": ["boom"], "warnings": []},
    )

    assert set(version_status(version)) == {"status", "stage", "errors", "warnings"}
    assert set(version_status(create_next_version(project=version.project))) == {
        "status",
        "stage",
        "errors",
        "warnings",
    }
