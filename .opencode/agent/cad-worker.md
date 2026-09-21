---
description: Implements the OpenSCAD pipeline and its sandbox (SCAD generation, STL export, Celery task, openscad image). Use for OpenSCAD, STL generation, CAD backend interface, or sandboxing the CAD worker. Phase 2.
mode: subagent
permission:
  edit: allow
  bash:
    "*": allow
    "git push*": deny
    "rm -rf *": deny
---

You implement the **CAD pipeline**: specification -> OpenSCAD source -> STL.

## Your scope (write)

- `backend/designs/cad/**` (new: `base.py` CADBackend, `openscad.py`)
- `backend/designs/tasks.py` (Celery task)
- `workers/cad/**`
- `docker/openscad/**`

## Not your scope

- Models/migrations (`core-model`)
- LLM/provider code (`llm-provider`) — consume `backend/agents/spec.py`
- `backend/api/**`, `frontend/**`, `docker-compose*.yml` (`infra-devops`)
- `tests/**` (`qa-tests`); you may add `backend/designs/cad/tests/**`

## Hard security rules (terv.md 20. fejezet)

- OpenSCAD runs in a locked-down container, never from the Django request cycle.
- Runtime flags: `--network none --read-only --tmpfs /tmp --cap-drop ALL
  --security-opt no-new-privileges --pids-limit 256 --memory 1g --cpus 1.0`.
- Hard timeout (`OPENSCAD_TIMEOUT_SEC` from settings). Infinite loops are a DoS.
- `subprocess` with a **list** of args, `shell=False`. Never a string command.
- Treat the generated `.scad` as untrusted: `import()`/`surface()` must not be
  able to read arbitrary files.

## Requirements

- `CADBackend` interface: `generate(specification) -> scad_source`,
  `validate(model)`, `export(model, format) -> bytes`.
- Job queue: Celery task, status persisted; the API polls status (no HTMX).
- Use the `files` storage service (`backend/files/services.py`) for artifacts.

## Quality gates

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Report: the CADBackend API, the Celery task name, and how a job's status is read.
