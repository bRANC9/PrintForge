# PrintForge

Self-hosted, open-source 3D printing workspace: from a natural-language prompt
to a parametric CAD model, to STL, to a browser preview — and later, slicing
and printing.

> Your hardware, your data, your model.

The full design lives in [`terv.md`](./terv.md).

## Stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.13+ |
| Backend | Django + Django REST Framework (`/api/v1`) |
| DB | PostgreSQL + pgvector |
| Queue | Celery + Redis |
| Frontend | Django Templates + Alpine.js + `fetch`/JSON (no HTMX) |
| 3D viewer | Three.js |
| AI | Ollama (Qwen3-Coder 30B-A3B; fallback Qwen2.5-Coder 7B) |
| Embedding | bge-m3 (fallback nomic-embed-text) |
| CAD | OpenSCAD CLI (later CadQuery, build123d, FreeCAD) |
| Slicing | PrusaSlicer CLI (MVP), OrcaSlicer later |
| MCP | in-process, built on `services.py` |
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
