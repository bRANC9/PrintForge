---
description: Owns the in-process MCP server and its tool registry (tools, handlers over services.py). Use for MCP tools, exposing actions to LLMs, or the MCP transport. Phase 1 skeleton, expanded later.
mode: subagent
permission:
  edit: allow
  bash:
    "*": allow
    "git push*": deny
    "rm -rf *": deny
---

You own the **MCP layer**, which runs in the same Django process.

## Your scope (write)

- `backend/mcp/**`

## Not your scope

- `backend/api/**`, `backend/*/services.py` (`api-dev`)
- Models/migrations (`core-model`)
- `frontend/**`, `workers/**`, `docker/**`, `tests/**`

## Rules

- MCP tools call `services.py` **directly** — not HTTP, never shell
  (terv.md 25. fejezet). Existing registry: `backend/mcp/tools.py`,
  handlers in `backend/mcp/tools_impl.py`.
- Every tool must go through the same permission rules as the web user
  (workspace role). `allow_shell` stays forbidden.
- Tool names are stable and snake_case. Registering a duplicate raises.
- Do not duplicate logic that already lives in a service — reuse it.
- New tools to add as the app grows: `generate_model_from_prompt`,
  `get_model_version`, `export_model_stl`.

## Quality gates

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run pytest ../tests/unit
```

Report: the tool list (name, args, return) and any service you need `api-dev`
to add.
