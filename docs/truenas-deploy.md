# TrueNAS SCALE 25.10 (Goldeye) deployment

Deploy PrintForge by **pulling** the prebuilt image from GitHub Container
Registry. No build happens on the NAS; the already-running Watchtower (on the
host) picks up new `:latest` images automatically.

- Image: `ghcr.io/branc9/printforge:latest`
- Published by `.github/workflows/release.yml` (push to `main`, `v*` tags, or
  manual `workflow_dispatch`).
- Only the `web` port (`8080`) is published. `db`, `redis` and `ollama` stay on
  the internal network — **never expose `11434`**.

> The CAD/slicing sandbox images are not published yet; only the Django
> (web/worker) image is.

---

## 0. Prerequisites

- TrueNAS SCALE 25.10 with the native Docker + `docker compose` plugin.
- SSH access (recommended over the UI "Custom App" flow).
- A pool dataset for persistent data.

## 1. Create the dataset and env file

```bash
# persistent data lives on the pool, NOT on the (immutable) root filesystem
mkdir -p /mnt/<pool>/apps/printforge

# clone the repo next to it (compose files + docker/db/init live here)
cd /mnt/<pool>/apps/printforge
git clone https://github.com/bRANC9/PrintForge.git repo

# env file stays on the pool
cp repo/.env.example .env
```

Edit `/mnt/<pool>/apps/printforge/.env` — at minimum:

```env
DATA_ROOT=/mnt/<pool>/apps/printforge
DJANGO_SECRET_KEY=<long-random-string>
POSTGRES_PASSWORD=<strong-password>
DJANGO_ALLOWED_HOSTS=truenas.lan,192.168.1.10
DJANGO_CSRF_TRUSTED_ORIGINS=https://printforge.example.com
WEB_PORT=8080
# default; leave as-is for auto-updates, pin a tag to roll back (see step 6)
PRINTFORGE_IMAGE=ghcr.io/branc9/printforge:latest
```

`DATA_ROOT` ends up holding `pg/`, `redis/`, `media/` and (internal Ollama
mode) `ollama/`.

## 2. Validate the configuration

```bash
cd /mnt/<pool>/apps/printforge/repo
docker compose --env-file ../.env -f docker-compose.prod.yml config --quiet
docker compose --env-file ../.env \
  -f docker-compose.prod.yml -f docker-compose.ollama.yml config --quiet
```

## 3. Start the stack

### External Ollama (separate host / GPU box)

```bash
docker compose --env-file ../.env -f docker-compose.prod.yml up -d
```

Point the app at it with `OLLAMA_BASE_URL=http://<ollama-host>:11434` in `.env`
(left out, the default is `http://host.docker.internal:11434`).

### Internal Ollama (runs on this NAS)

For an NVIDIA GPU, first uncomment the `deploy:` block in
`docker-compose.ollama.yml` and make sure the NVIDIA container toolkit is
configured for Docker. Then:

```bash
docker compose --env-file ../.env \
  -f docker-compose.prod.yml -f docker-compose.ollama.yml up -d
```

The override adds the `ollama` service and forces `OLLAMA_BASE_URL` to
`http://ollama:11434` for `web` and `worker`.

### First run

The `vector` extension is created automatically on the first database init by
`docker/db/init/01-extensions.sql` (only when the `pg/` dataset is empty).

```bash
docker compose --env-file ../.env -f docker-compose.prod.yml \
  exec web python manage.py createsuperuser
```

Web UI: `http://<truenas-host>:8080` · API: `http://<truenas-host>:8080/api/v1/health/`

## 4. Registry access

The package must be **public** on GHCR for the NAS to pull without credentials.
Make it public once: GitHub → repo → **Packages** → `printforge` → *Package
settings* → *Change visibility* → **Public**.

If you keep it private, log in on the NAS once (PAT with `read:packages`):

```bash
echo "<PAT>" | docker login ghcr.io -u <github-user> --password-stdin
```

## 5. Automatic updates (Watchtower)

Watchtower is already running on the host — **do not add a Watchtower service**.
The `web` and `worker` containers are labelled:

```yaml
labels:
  com.centurylinklabs.watchtower.enable: "true"
```

Whenever `release.yml` pushes a new `:latest`, Watchtower pulls it and recreates
the two containers. Notes:

- If Watchtower runs with `--label-enable`, only these labelled containers are
  updated (intended). Without it, all containers are monitored and the label is
  harmless.
- Watchtower only recreates the app containers; it never touches the DB data.
- To force an update immediately instead of waiting for the poll interval:

  ```bash
  docker ps --format '{{.Names}}' | grep -i watchtower
  docker exec <watchtower-container> /watchtower --run-once --cleanup
  ```

## 6. Rollback

Pin `PRINTFORGE_IMAGE` to an immutable build tag, then recreate:

```bash
# tags published by every build: sha-<short>, semver (v1.2.3 -> 1.2.3 / 1.2)
sed -i 's#^PRINTFORGE_IMAGE=.*#PRINTFORGE_IMAGE=ghcr.io/branc9/printforge:sha-1a2b3c4#' .env
docker compose --env-file ../.env -f docker-compose.prod.yml up -d
```

Because the compose file sets `pull_policy: always`, `up -d` pulls the pinned
tag. Watchtower follows the image reference, so a **pinned tag is not
auto-updated** — set `PRINTFORGE_IMAGE` back to `...:latest` to resume automatic
updates. List available tags on the GitHub **Packages** page
(`https://github.com/bRANC9/PrintForge/pkgs/container/printforge`).

## 7. Useful commands

```bash
# logs
docker compose --env-file ../.env -f docker-compose.prod.yml logs -f web worker

# stop (keeps data)
docker compose --env-file ../.env -f docker-compose.prod.yml down

# update compose files (optional)
cd /mnt/<pool>/apps/printforge/repo && git pull
```

## Notes

- Multi-arch is **not** enabled; the image is `linux/amd64` only, matching
  TrueNAS SCALE on x86-64.
- The `db` and `redis` images are unchanged from the base stack
  (`pgvector/pgvector:pg16`, `redis:7-alpine`) and are pinned by tag.
- Never publish `11434`; keep Ollama on the internal Compose network.
