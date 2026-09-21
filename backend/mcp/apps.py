from django.apps import AppConfig


class McpConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mcp"

    def ready(self) -> None:
        # Importing the module registers the tools in the in-process registry.
        from . import tools_impl  # noqa: F401
