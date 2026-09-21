"""Unit tests: pure logic, no database."""

from mcp.tools import all_tools


def test_mcp_tools_are_registered():
    names = set(all_tools())
    assert {"create_workspace", "create_project", "list_projects"} <= names


def test_every_tool_has_a_description():
    for tool in all_tools().values():
        assert tool.description
        assert callable(tool.handler)
