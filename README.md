# PrintForge

Self-hosted, open-source 3D printing workspace: from a natural-language prompt
to a parametric CAD model, to STL, to a browser preview — and later, slicing
and printing.

> Your hardware, your data, your model.

The full design lives in [`terv.md`](./terv.md).

## Features

- **Natural language → parametric CAD.** A prompt becomes a structured
  specification; the OpenSCAD backend renders it as CSG primitives
  (`box`/`cylinder`/`sphere`/`cone` plus a 2D `extrude` profile). Reusable
  **skills** (guidance or template recipes) steer generation, and the planner
  can ask clarifying questions or record auditable assumptions.
- **Vision self-check.** After validation the pipeline renders a preview and,
  when the chosen model is vision-capable, asks it to compare the result with
  the request; a bounded retry can revise the specification.
- **Visual-prompt editing.** Click the model in the viewer to annotate a point
  or region with an instruction; the AI turns it into validated parametric
  features as a new version.
- **Version history.** Versions are immutable; regenerate or edit any history
  entry into a new version with a `parent_version` chain.
- **Workspace-first UI.** The home page is the workspace list; items (projects)
  live inside a workspace.
- **Community library.** Public models with sharing, search, tags, ratings,
  downloads, licenses and optional AI-filled descriptions.
- **Slicing & print queue.** PrusaSlicer CLI (single model or multi-object
  build plates) with printer/filament/process profiles, time/filament
  estimates, and a printer queue with Creality K2 (CFS), Moonraker and
  OctoPrint backends.
- **MCP tools.** The same `services.py` logic exposed over the official MCP SDK
  (stdio / streamable HTTP), with the workspace-role gate enforced.
- **Optional self-hosted web search.** A SearXNG-compatible JSON backend with
  `trafilatura` page extraction, for the Research agent.

## Stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.13+ |
| Backend | Django + Django REST Framework (`/api/v1`) |
| DB | PostgreSQL + pgvector |
| Queue | Celery + Redis |
| Frontend | Django Templates + Alpine.js + `fetch`/JSON (no HTMX) |
| 3D viewer | Three.js |
| AI | Ollama (Qwen3-Coder 30B-A3B; fallback Qwen2.5-Coder 7B); optional OpenAI-compatible provider (`openai` SDK) |
| Embedding | bge-m3 (fallback nomic-embed-text) |
| Web search | optional self-hosted SearXNG (JSON) + `trafilatura` |
| CAD | OpenSCAD CLI (CSG primitives incl. 2D `extrude`; later CadQuery, build123d, FreeCAD) |
| Slicing | PrusaSlicer CLI (MVP), OrcaSlicer later |
| Storage | local filesystem (default) or S3/MinIO via `django-storages` + `boto3` |
| MCP | in-process, official `mcp` SDK (stdio / streamable HTTP) |
| Deploy | Docker Compose, two variants (external / internal Ollama) |

## Quickstart (Docker)

```bash
cp .env.example .env
# edit .env (at minimum DJANGO_SECRET_KEY and POSTGRES_PASSWORD)

# External Ollama (recommended when Ollama runs elsewhere / on a GPU host)
docker compose up -d

# Internal Ollama
docker compose -f docker-compose.yml -f docker-compose.ollama.yml up -d

docker compose exec web python manage.py createsuperuser
```

Web UI: <http://localhost:8080> · API: <http://localhost:8080/api/v1/health/>

### Optional add-ons

All of these are opt-in; the base stack runs without them. Set the matching
variables from `.env.example`.

- **Self-hosted web search (SearXNG)** — for the Research agent:

  ```bash
  # set server.secret_key in docker/searxng/settings.yml first
  docker compose -f docker-compose.yml -f docker-compose.search.yml up -d
  ```

  The override adds a `searxng` service on the internal network only (no
  published port) and sets `SEARCH_BACKEND=searxng` /
  `SEARXNG_BASE_URL=http://searxng:8080` for `web` and `worker`. Swap
  `docker-compose.yml` for `docker-compose.prod.yml` to use it with the
  production stack. A bare `docker compose up -d` never starts it.
- **S3 / MinIO storage** — set `STORAGE_BACKEND=s3` and the `AWS_*` variables
  (`AWS_STORAGE_BUCKET_NAME`, `AWS_S3_ENDPOINT_URL`, `AWS_S3_REGION_NAME`,
  `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`). The default
  `STORAGE_BACKEND=local` keeps files on disk and is unchanged.
- **OpenAI-compatible LLM provider** — set `LLM_PROVIDER=openai` plus
  `OPENAI_BASE_URL` and `OPENAI_API_KEY`. This works for hosted OpenAI or any
  OpenAI-compatible gateway; Ollama stays the default (`LLM_PROVIDER=ollama`).
- **OrcaSlicer slicing backend (opt-in)** — the optional second slicer uses the
  headless OrcaSlicer sandbox image
  `ghcr.io/branc9/printforge-orcaslicer` (a wxWidgets/GL app wrapped in Xvfb).
  Enable it with `SLICER_BACKEND=orca` in `.env`; `ORCASLICER_IMAGE` already
  defaults to the published image (set `SLICER_IMAGE` only to pin both backends
  to one image). PrusaSlicer remains the default and the Orca image is only
  pulled when a job selects this backend. See
  [`docker/slicer-orca/README.md`](docker/slicer-orca/README.md).

### TrueNAS SCALE 25.10 (Goldeye)

- Native Docker + `docker compose` is available; the GPU can be passed to
  Ollama (NVIDIA container toolkit + the `deploy.resources.reservations.devices`
  block in `docker-compose.ollama.yml`).
- The root filesystem is immutable: keep all persistent data on a pool
  dataset. Set in `.env`:

  ```env
  DATA_ROOT=/mnt/<pool>/apps/printforge
  ```

  This holds `pg/`, `redis/`, `media/` and (in internal mode) `ollama/`.
- Only publish the `web` port. `db`, `redis` and `ollama` stay on the internal
  network — never expose `11434`.

## Production deploy (GHCR + Watchtower)

Releases publish the Django (web/worker) image to GitHub Container Registry as
`ghcr.io/branc9/printforge`, plus the CAD/slicing sandbox images
`ghcr.io/branc9/printforge-openscad`,
`ghcr.io/branc9/printforge-prusaslicer` and
`ghcr.io/branc9/printforge-orcaslicer` (same tag scheme). On a host that already
runs Watchtower (e.g. TrueNAS), deploy the pull-based stack instead of building:

```bash
docker compose -f docker-compose.prod.yml up -d
# internal Ollama:
docker compose -f docker-compose.prod.yml -f docker-compose.ollama.yml up -d
```

For the TrueNAS SCALE **Custom App UI**, paste
[`docker-compose.truenas.yml`](./docker-compose.truenas.yml) instead — a minimal
self-contained YAML (YAML anchors keep each value in one place; no build, no
env-file, no relative mounts, no interpolation). Edit five values: the DB
password, Django secret key, allowed hosts, `OLLAMA_BASE_URL` (defaults to the
Docker host — change it if Ollama runs elsewhere) and the `/mnt/<POOL>/...` paths.

`docker-compose.prod.yml` is hardened for production: `DJANGO_DEBUG` defaults to
`false`, and the HTTPS flags (`DJANGO_SECURE_SSL_REDIRECT`,
`DJANGO_SESSION_COOKIE_SECURE`, `DJANGO_CSRF_COOKIE_SECURE`,
`DJANGO_SECURE_HSTS_SECONDS`, `DJANGO_SECURE_PROXY_SSL_HEADER`) default to off.
Before the first `up`, set `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`,
`DJANGO_ALLOWED_HOSTS` (and `DJANGO_CSRF_TRUSTED_ORIGINS` when behind TLS) — see
the `.env.example` "PRODUCTION (TrueNAS)" block. The repo is private, so the
GHCR packages are private by default: either make them public or run
`docker login ghcr.io` on the NAS (this also covers Watchtower's pulls).

Model generation and slicing run as **ephemeral sandbox containers** launched by
the `worker` (Docker-out-of-Docker). This needs three things, all wired up:
the app image ships the Docker CLI, `worker` mounts `/var/run/docker.sock`, and
a scratch directory is bind-mounted at an identical absolute path with
`TMPDIR` pointed at it (`SANDBOX_WORK_DIR`, e.g.
`/mnt/<pool>/apps/printforge/scratch` on TrueNAS). After install, set
**CAD mód = docker** and **Slicing mód = docker** on the app's Settings page. The
socket is the main security trade-off; see section 9 of the runbook.

See [`docs/truenas-deploy.md`](docs/truenas-deploy.md) for the full runbook
(pre-flight, dataset layout, `.env`, verification, reverse-proxy/HTTPS flags,
Watchtower behaviour, the CAD/slicing sandbox, rollback via a pinned
`PRINTFORGE_IMAGE`).

## Local development (uv)

```bash
cd backend
uv sync                       # installs deps + creates .venv
uv run python manage.py migrate
uv run python manage.py runserver
```

Without Docker you can run against sqlite:

```bash
DATABASE_URL=sqlite:///db.sqlite3 uv run python manage.py migrate
```

## Quality gates

```bash
cd backend
uv run ruff check .           # Python lint
uv run ruff format --check .  # Python formatting
uv run djlint ../frontend/templates --check   # Django templates
uv run pytest                 # tests
```

## Layout

```
backend/     Django project + apps (config, accounts, workspaces, projects,
             designs, agents, files, printers, slicers, api, mcp)
frontend/    templates, static assets, js
workers/     out-of-process CAD / slicing / validation workers
docker/      Dockerfiles + db init scripts
tests/       unit / integration / e2e
```

## License

AGPL-3.0 (see [`LICENSE`](./LICENSE)). The license choice should be confirmed
with legal review before publishing.
