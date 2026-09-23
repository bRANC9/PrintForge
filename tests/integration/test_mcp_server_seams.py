"""MCP transport seams (terv.md 25., 26.3).

``backend/mcp/tests/test_server.py`` already exercises the SDK bridge with the
in-process client. This top-level module keeps the documented guarantees visible
next to the rest of the seam tests:

* the server's tool list is exactly the registry (no orphan/ghost tools);
* a tool call must flow through the registry ``authorize`` gate, so a caller
  without the workspace role is rejected *before* the handler runs;
* the MCP tool and the DRF endpoint reach the same service layer and produce the
  same persisted row.

No network: the in-process SDK client talks to ``build_server()`` directly.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from factories import UserFactory, WorkspaceFactory
from rest_framework.test import APIClient

from mcp.server import build_server
from mcp.tools import all_tools
from projects.models import Project

pytestmark = pytest.mark.django_db(transaction=True)


def _list_tools(server):
    return asyncio.run(server.list_tools())


async def _client_call(server, name: str, arguments: dict):
    from mcp.client.client import Client

    async with Client(server) as client:
        return await client.call_tool(name, arguments)


def _result_payload(result):
    if result.structured_content is not None:
        return result.structured_content.get("result", result.structured_content)
    text = next(block.text for block in result.content if getattr(block, "type", None) == "text")
    return json.loads(text)


def test_server_exposes_exactly_the_registry_tools():
    tools = {tool.name: tool for tool in _list_tools(build_server())}

    assert set(tools) == set(all_tools())
    for name, registered in all_tools().items():
        assert tools[name].description == registered.description


def test_identity_parameters_are_hidden_from_every_schema():
    tools = {tool.name: tool for tool in _list_tools(build_server())}
    hidden = {"user_id", "owner_id"}

    for name in all_tools():
        assert hidden.isdisjoint(tools[name].input_schema.get("properties", {})), name


def test_tool_call_is_rejected_by_the_authorize_gate(settings):
    workspace = WorkspaceFactory()
    outsider = UserFactory()
    settings.MCP_SERVICE_USER_ID = outsider.id
    server = build_server()

    result = asyncio.run(
        _client_call(server, "create_project", {"workspace_id": workspace.id, "name": "Nope"})
    )

    assert result.is_error is True
    assert not Project.objects.filter(workspace=workspace, name="Nope").exists()


def test_authorized_tool_call_creates_the_row_via_the_same_service(settings):
    workspace = WorkspaceFactory()
    owner = workspace.owner
    settings.MCP_SERVICE_USER_ID = owner.id
    server = build_server()

    result = asyncio.run(
        _client_call(server, "create_project", {"workspace_id": workspace.id, "name": "Via MCP"})
    )

    assert result.is_error is False
    payload = _result_payload(result)
    created = Project.objects.get(pk=payload["id"])
    assert created.name == "Via MCP"
    assert created.workspace_id == workspace.id
    assert created.created_by_id == owner.id


def test_mcp_and_api_create_projects_through_the_same_service(settings, workspace):
    """The transported call and the HTTP call must land the same kind of row."""
    owner = workspace.owner
    settings.MCP_SERVICE_USER_ID = owner.id

    mcp_payload = _result_payload(
        asyncio.run(
            _client_call(
                build_server(),
                "create_project",
                {"workspace_id": workspace.id, "name": "Same service"},
            )
        )
    )

    api = APIClient()
    api.force_authenticate(user=owner)
    response = api.post(
        "/api/v1/projects/",
        {"workspace": workspace.id, "name": "Same service via API"},
        format="json",
    )
    assert response.status_code == 201, response.content
    api_project = Project.objects.get(pk=response.json()["id"])
    mcp_project = Project.objects.get(pk=mcp_payload["id"])

    assert mcp_project.workspace_id == api_project.workspace_id == workspace.id
    assert mcp_project.created_by_id == api_project.created_by_id == owner.id
