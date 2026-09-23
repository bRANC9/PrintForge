"""Run the in-process PrintForge MCP server (terv.md 25. fejezet).

The server runs inside the Django process and calls ``services.py`` directly; it
never shells out. Because MCP has no Django request/user, the acting identity is
resolved from ``MCP_SERVICE_USER_ID`` (setting or environment). The workspace-role
``authorize`` gate still runs for that principal on every tool call -- the
transport never bypasses it.

Examples::

    # stdio (for a local MCP client such as an editor plugin)
    MCP_SERVICE_USER_ID=1 uv run python manage.py mcp_server --transport stdio

    # Streamable HTTP
    MCP_SERVICE_USER_ID=1 uv run python manage.py mcp_server \
        --transport streamable-http --host 127.0.0.1 --port 8000 --path /mcp
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandParser

from mcp.server import build_server, resolve_identity_user_id


class Command(BaseCommand):
    help = (
        "Serve the registered PrintForge MCP tools. Identity comes from "
        "MCP_SERVICE_USER_ID; the workspace-role gate still applies."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--transport",
            choices=("stdio", "streamable-http"),
            default="stdio",
            help="MCP transport to serve (default: stdio).",
        )
        parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host.")
        parser.add_argument("--port", type=int, default=8000, help="HTTP bind port.")
        parser.add_argument(
            "--path",
            default="/mcp",
            help="Streamable HTTP endpoint path (default: /mcp).",
        )

    def handle(self, *args, **options) -> None:
        if resolve_identity_user_id() is None:
            # stderr is safe for stdio (stdout carries the MCP protocol).
            self.stderr.write(
                self.style.WARNING(
                    "MCP_SERVICE_USER_ID is not set: identity-dependent tools will "
                    "refuse to run. Set it to the service user's numeric id."
                )
            )

        server = build_server()
        transport = options["transport"]
        if transport == "stdio":
            server.run(transport="stdio")
        else:
            server.run(
                transport="streamable-http",
                host=options["host"],
                port=options["port"],
                streamable_http_path=options["path"],
            )
