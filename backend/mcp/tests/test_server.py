"""Tests for the MCP transport (registry -> official MCP SDK bridge).

Run explicitly with ``uv run pytest mcp/tests``: the backend ``testpaths``
setting only collects the top-level ``../tests`` directory plus ``.``.

The transport is exercised through the SDK's in-process client
(:class:`mcp.client.client.Client`) so the registry, the input schema, the
identity injection and the ``authorize`` gate are all covered end to end.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, PermissionDenied

from mcp.server import (
    IDENTITY_PARAMETERS,
    build_server,
    make_tool_wrapper,
    resolve_identity_user_id,
)
from mcp.tools import all_tools
from projects.models import Project
from workspaces.models import WorkspaceRole
from workspaces.services import add_member, create_workspace

User = get_user_model()

IDENTITY_SETTING = "MCP_SERVICE_USER_ID"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_tools(server):
    """Run the SDK's async ``list_tools`` from a sync test."""
    return asyncio.run(server.list_tools())


async def _client_call(server, name: str, arguments: dict):
    from mcp.client.client import Client

    async with Client(server) as client:
        return await client.call_tool(name, arguments)


def _result_payload(result):
    """Return a tool result's payload as a dict/list.

    The SDK publishes structured content for ``list[...]``/``dict[str, ...]``
    returns and a JSON text block for bare ``dict`` returns; accept either.
    """
    if result.structured_content is not None:
        return result.structured_content.get("result", result.structured_content)
    text = next(block.text for block in result.content if getattr(block, "type", None) == "text")
    return json.loads(text)


# ---------------------------------------------------------------------------
# Registry -> MCPServer mapping (no database)
# ---------------------------------------------------------------------------


def test_build_server_exposes_every_registered_tool():
    server = build_server()
    tools = {tool.name: tool for tool in _list_tools(server)}

    assert set(tools) == set(all_tools())
    for name, registered in all_tools().items():
        assert tools[name].description == registered.description


def test_input_schema_is_derived_from_the_handler():
    server = build_server()
    tools = {tool.name: tool for tool in _list_tools(server)}

    schema = tools["create_project"].input_schema
    assert schema["type"] == "object"
    assert schema["properties"]["workspace_id"]["type"] == "integer"
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["description"]["type"] == "string"
    assert set(schema["required"]) == {"workspace_id", "name"}


def test_identity_parameters_are_hidden_from_every_schema():
    server = build_server()
    tools = {tool.name: tool for tool in _list_tools(server)}

    for name in all_tools():
        properties = tools[name].input_schema.get("properties", {})
        assert IDENTITY_PARAMETERS.isdisjoint(properties), name


def test_wrapper_signature_omits_identity_parameters():
    wrapper = make_tool_wrapper(all_tools()["create_project"])
    parameters = inspect.signature(wrapper).parameters

    assert "user_id" not in parameters
    assert "workspace_id" in parameters


def test_sdk_bridge_resolves_the_installed_sdk():
    # ``mcp.server`` is this app's module, but it must behave as the SDK's
    # ``mcp.server`` package for submodule imports and re-exported names.
    import mcp.server as server_module

    assert server_module.MCPServer.__module__.startswith("mcp.server.mcpserver")
    assert server_module.Server.__module__.startswith("mcp.server.lowlevel")


# ---------------------------------------------------------------------------
# Identity resolution (no database)
# ---------------------------------------------------------------------------


def test_resolve_identity_prefers_setting_then_environment(settings, monkeypatch):
    monkeypatch.delenv(IDENTITY_SETTING, raising=False)
    settings.MCP_SERVICE_USER_ID = None
    assert resolve_identity_user_id() is None

    settings.MCP_SERVICE_USER_ID = 7
    assert resolve_identity_user_id() == 7

    settings.MCP_SERVICE_USER_ID = None
    monkeypatch.setenv(IDENTITY_SETTING, "9")
    assert resolve_identity_user_id() == 9


def test_resolve_identity_rejects_non_integer(settings, monkeypatch):
    monkeypatch.setenv(IDENTITY_SETTING, "not-a-number")
    with pytest.raises(ImproperlyConfigured):
        resolve_identity_user_id()


def test_resolve_identity_prefers_runtime_setting_override(settings, monkeypatch):
    """A DB/runtime override beats the Django setting and the environment."""
    settings.MCP_SERVICE_USER_ID = 7
    monkeypatch.setenv(IDENTITY_SETTING, "9")
    monkeypatch.setattr(
        "configuration.services.get_setting",
        lambda name: "42" if name == "mcp_service_user_id" else "",
    )

    assert resolve_identity_user_id() == 42


def test_resolve_identity_rejects_non_integer_runtime_override(settings, monkeypatch):
    """A bad runtime override fails loudly just like a bad setting/env value."""
    monkeypatch.setattr(
        "configuration.services.get_setting",
        lambda name: "not-a-number",
    )

    with pytest.raises(ImproperlyConfigured):
        resolve_identity_user_id()


def test_resolve_identity_falls_back_when_runtime_settings_unavailable(settings, monkeypatch):
    """If the settings store cannot be reached, read settings/env directly."""
    settings.MCP_SERVICE_USER_ID = 7

    def unavailable(name):
        raise RuntimeError("database access not allowed")

    monkeypatch.setattr("configuration.services.get_setting", unavailable)

    assert resolve_identity_user_id() == 7


# ---------------------------------------------------------------------------
# Database-backed fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def owner(transactional_db):
    return User.objects.create_user(username="owner", email="owner@example.com", password="pw")


@pytest.fixture
def member(transactional_db):
    return User.objects.create_user(username="member", email="member@example.com", password="pw")


@pytest.fixture
def outsider(transactional_db):
    return User.objects.create_user(
        username="outsider", email="outsider@example.com", password="pw"
    )


@pytest.fixture
def workspace(owner, member):
    workspace = create_workspace(name="Lab", owner=owner)
    add_member(workspace=workspace, user=member, role=WorkspaceRole.MEMBER)
    return workspace


# ---------------------------------------------------------------------------
# Gate wiring (direct, sync)
# ---------------------------------------------------------------------------


def test_wrapper_runs_authorize_before_the_handler(settings, workspace, outsider):
    settings.MCP_SERVICE_USER_ID = outsider.id
    wrapper = make_tool_wrapper(all_tools()["create_project"])

    with pytest.raises(PermissionDenied):
        wrapper(workspace_id=workspace.id, name="Denied")

    # The handler must not have run: nothing was created.
    assert not Project.objects.filter(workspace=workspace, name="Denied").exists()


def test_wrapper_injects_configured_identity_and_drops_client_supplied(settings, workspace, owner):
    settings.MCP_SERVICE_USER_ID = owner.id
    wrapper = make_tool_wrapper(all_tools()["create_project"])

    # The client-supplied user_id is ignored; the configured principal is used.
    result = wrapper(workspace_id=workspace.id, name="Mine", user_id=owner.id + 1)

    project = Project.objects.get(pk=result["id"])
    assert project.created_by_id == owner.id


def test_wrapper_refuses_identity_tool_without_a_configured_principal(
    settings, monkeypatch, workspace
):
    monkeypatch.delenv(IDENTITY_SETTING, raising=False)
    settings.MCP_SERVICE_USER_ID = None
    wrapper = make_tool_wrapper(all_tools()["create_project"])

    with pytest.raises(ImproperlyConfigured):
        wrapper(workspace_id=workspace.id, name="No identity")


def test_ungated_tool_runs_without_a_configured_principal(settings, monkeypatch, transactional_db):
    monkeypatch.delenv(IDENTITY_SETTING, raising=False)
    settings.MCP_SERVICE_USER_ID = None
    wrapper = make_tool_wrapper(all_tools()["search_public_projects"])

    assert wrapper(q="") == []


# ---------------------------------------------------------------------------
# Through the MCP layer (SDK in-process client)
# ---------------------------------------------------------------------------


def test_client_calls_a_tool_through_the_registry(settings, workspace, owner):
    settings.MCP_SERVICE_USER_ID = owner.id
    server = build_server()

    result = asyncio.run(
        _client_call(
            server,
            "create_project",
            {"workspace_id": workspace.id, "name": "Via MCP"},
        )
    )

    assert result.is_error is False
    payload = _result_payload(result)
    assert payload["name"] == "Via MCP"
    assert payload["workspace_id"] == workspace.id
    assert Project.objects.get(pk=payload["id"]).created_by_id == owner.id


def test_client_denies_identity_without_the_required_role(settings, workspace, outsider):
    settings.MCP_SERVICE_USER_ID = outsider.id
    server = build_server()

    result = asyncio.run(
        _client_call(
            server,
            "create_project",
            {"workspace_id": workspace.id, "name": "Nope"},
        )
    )

    assert result.is_error is True
    assert not Project.objects.filter(workspace=workspace, name="Nope").exists()


def test_client_cannot_spoof_the_identity(settings, workspace, owner, outsider):
    settings.MCP_SERVICE_USER_ID = outsider.id
    server = build_server()

    # The client passes the owner's id, but the transport ignores it and uses the
    # configured (outsider) principal, so the gate still denies the write.
    result = asyncio.run(
        _client_call(
            server,
            "create_project",
            {"workspace_id": workspace.id, "name": "Spoof", "user_id": owner.id},
        )
    )

    assert result.is_error is True
    assert not Project.objects.filter(workspace=workspace, name="Spoof").exists()
