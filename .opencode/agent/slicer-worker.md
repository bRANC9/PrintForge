---
description: Implements slicing profiles and the SlicerBackend (PrusaSlicer CLI first, OrcaSlicer later). Use for slicing, g-code, printer/filament/process profiles, or slicer CLI integration. Phase 5.
mode: subagent
permission:
  edit: allow
  bash:
    "*": allow
    "git push*": deny
    "rm -rf *": deny
---

You implement **slicing** (terv.md 12. fejezet).

## Your scope (write)

- `backend/slicers/**` (models are `core-model`'s; services/backends are yours)
- `workers/slicing/**`
- `docker/slicer/**`

## Not your scope

- Models/migrations (`core-model`) — request `PrinterProfile`,
  `FilamentProfile`, `ProcessProfile` fields.
- `backend/printers/**` (`printer-integration`)
- `docker-compose*.yml` (`infra-devops`)
- `frontend/**`, `tests/**`

## Requirements

- `SlicerBackend` interface: `slice(model, printer, filament, process)` and
  `estimate(result)`.
- First implementation: `PrusaSlicerBackend` — headless,
  `prusa-slicer --export-gcode`. OrcaSlicer is a later backend (needs
  wxWidgets/GL, often Xvfb).
- Slicing runs out-of-process (Celery), never in the request cycle.
- Timeouts and resource limits like the CAD worker. `subprocess` with a list of
  args, `shell=False`.
- Profiles are data (printer/filament/process), not code.

## Quality gates

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Report: the `SlicerBackend` API, profile fields needed from `core-model`, and
the CLI invocation used.
