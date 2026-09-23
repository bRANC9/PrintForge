# AI 3D Print Hub – Development Plan

## 1. Cél

Open-source, self-hosted 3D printing platform, amelyben:

- természetes nyelvű promptból parametrikus CAD modell készül;
- az AI szükség esetén webes kutatást végez;
- az AI több lépéses agent workflow-ban dolgozik;
- OpenSCAD generálja a műszaki geometriát;
- a modell böngészőben 3D-ben megtekinthető;
- a modell verziózható és workspace-ekben tárolható;
- OrcaSlicerrel szeletelhető;
- később közvetlenül 3D nyomtatóra küldhető;
- több felhasználó és több printer kezelhető;
- alapértelmezésben teljesen self-hosted;
- lokális LLM használható Ollamán keresztül;
- külső LLM/API opcionális.

Alapelv:

> A saját hardvered, a saját adataid, a saját AI-modelled.

A projekt célja nem egy újabb kötelező cloud SaaS, hanem egy otthon vagy kisebb közösségben futtatható open-source 3D-printing workspace.

---

## 2. MVP cél

Az első verzió ne kezelje még a nyomtatót.

### MVP v0.1

1. Django webalkalmazás
2. PostgreSQL
3. User authentication
4. Workspace-ek
5. Projektek
6. Prompt megadása
7. Ollama integráció
8. Qwen3-Coder támogatás (alap: 30B-A3B; kisebb géphez Qwen2.5-Coder 7B fallback)
9. AI → OpenSCAD
10. OpenSCAD → STL
11. STL → 3D preview (Three.js STLLoader)
12. Three.js viewer
13. SCAD/STL letöltés
14. Projektverziók
15. Docker Compose alapú lokális telepítés
16. uv alapú dependency-kezelés
17. Tesztek (pytest) + CI

A v0.1 célja:

> „Beírom, hogy mit szeretnék, és néhány percen belül látom a létrehozott modellt a böngészőben és letölthetem STL-ként.”

---

# 3. Technológiai stack

## Backend

- Python 3.13+
- **uv** a dependency- és környezetkezeléshez (`pyproject.toml` + `uv.lock`)
- Django
- Django REST Framework – JSON API (`/api/v1/...`) a frontendnek és az MCP-nek
- PostgreSQL **+ pgvector** (vektoros kereséshez, RAG/Research agent)
- Celery + Redis a háttérfeladatokhoz (queue, lásd 9. fejezet)
- `mcp` (hivatalos Python SDK) – az in-process MCP tool registry
  stdio / streamable-http transzportja (lásd 25., 37. fejezet)

Megjegyzés: a vektoros tárolásra **külön vektor-DB nem kell** – a pgvector
ugyanabban a Postgresben adja a hasonlósági keresést, így nincs új
szolgáltatás és nincs adatszinkron a relációs adatokkal.

## Frontend

Első verzió:

- Django Templates (shell / layout)
- Alpine.js (interakció, állapot)
- `fetch` + JSON API (**nincs HTMX**)
- Three.js (csak a 3D viewer)

Nem szükséges React/Next.js, és nincs külön frontend build-lánc.

Az üzleti logika `services.py`-okban él, nem a view-kban. Ez teszi
lehetővé, hogy később az MCP toolok ugyanazt a logikát hívják
(lásd 25. fejezet).

A Three.js csak a 3D viewerért felel.

## AI

Elsődleges:

- Ollama
- Qwen3-Coder 30B-A3B (MoE, jó minőség)

Kisebb GPU/CPU esetén fallback:

- Qwen2.5-Coder 7B

Megjegyzés: önálló „Qwen3-Coder 8B” modell nem létezik; a Qwen3-Coder
család 30B-A3B és 480B-A35B méretben érhető el Ollamán.

### Embedding (RAG / Research agent)

- Alapértelmezett: **`bge-m3`** (multilingvális, 1024 dim) – a magyar
  dokumentumok és gyártói specifikációk miatt
- Könnyű fallback: `nomic-embed-text` (768 dim, angol-központú)
- A dimenziót env vezérli (`EMBEDDING_DIM`), hogy a modell csere
  ne törje a pgvector sémát
- Csak Ollamán keresztül indul, cloud embedding nem kötelező

Model adapter:

```text
LLMProvider
├── OllamaProvider
├── OpenAICompatibleProvider   # megvalósítva (openai SDK)
└── AnthropicProvider (később)
```

Az `OpenAICompatibleProvider` a hivatalos `openai` SDK-ra épül, és bármely
OpenAI-kompatibilis gateway-jel működik (`LLM_PROVIDER=openai`,
`OPENAI_BASE_URL`, `OPENAI_API_KEY`). Az Ollama marad az alapértelmezett.
Így az alkalmazás ne legyen Ollama-specifikus.

### Web research

A Research agent a lokális RAG mellett opcionálisan egy **SearXNG-kompatibilis
JSON API-t** kérdez (self-hosted, `SEARCH_BACKEND=searxng` +
`SEARXNG_BASE_URL`), a találati oldalak fő szövegét pedig a **`trafilatura`**
nyeri ki. A hívás best-effort: hálózati hiba vagy üres konfiguráció esetén
üres találat, a generálás nem áll meg. Az oldalletöltés SSRF-védett (csak
nyilvános http/https host), és a worker processzben fut, nem az OpenSCAD
sandboxban (lásd 36. fejezet).

### Döntési modell – Laya (későbbi jelölt, ki kell értékelni)

A [Laya](https://github.com/NandhaKishorM/laya) egy multilingvális,
**nem-generatív** döntési modell (`choice` / `score` / `noul`) egyetlen
forward passban, Apache-2.0. **Nem** CAD-generátor, csak döntéseket hoz.

Hol lehetne használni (Phase 4+):

- Planner agent: `needs_research`, `object_type`, `complexity`,
  `needs_clarification`
- Guardrails: kész `guard_questions()` prompt-injection / jailbreak ellen
- Validator triage: retry vs fail, hiba súlyossága
- Printer/slicer profil választás (Phase 5/6)

Feltételek / kockázatok:

- A base checkpointok **zero-shot közel randomok**; az érték domain
  **fine-tuningból** jön (4–5 óra 2×T4-en) → csak akkor éri meg, ha
  hajlandóak vagyunk tanítani.
- Magyar nyelvhez `laya-multilingual` kell (az angol checkpoint nem-Latin
  íráson 0.000 accuracy).
- **Második modell** az Ollama mellé (PyTorch + transformers, pár GB
  RAM/VRAM a TrueNAS-on).
- Confidence előli branch előtt temperature-kalibráció kötelező.

Amíg nincs fine-tune, a Planner ugyanezeket a döntéseket a meglévő
Qwen-nel, strukturált prompttal / function calling-gal is meg tudja
hozni, extra modell nélkül. Ezért az MVP-ből kimarad, és Phase 4-ben
kiértékeljük.

## CAD

Első backend:

- OpenSCAD CLI

Későbbi backends:

- CadQuery
- build123d
- FreeCAD

A generált geometria primitív-alapú CSG: `box`, `cylinder`, `sphere`, `cone`
és a 2D **`extrude`** (`profile` pontsor + `wall_thickness` / `round_radius` /
`height`), `role: add | subtract` (lásd 30., 39. fejezet).

CAD interface:

```python
class CADBackend:
    def generate(self, specification):
        ...

    def validate(self, model):
        ...

    def export(self, model, format):
        ...
```

## Mesh

- trimesh
- szükség esetén manifold/geometry validation

Megjegyzés: az MVP-hez nem kötelező külön GLB lépés – a Three.js
`STLLoader`-rel közvetlenül az STL renderelhető. A GLB (trimesh export)
opcionális, későbbi optimalizálás.

## Slicing

Első backend:

- PrusaSlicer CLI (headless, `--export-gcode`) – egyszerűbben
  konténerizálható, stabil CLI

Később:

- OrcaSlicer CLI (wxWidgets/GL függőségek, headless gyakran Xvfb-t
  igényel – nagyobb integrációs kockázat)
- további slicer backends

## Storage

Alapértelmezett:

- lokális filesystem vagy Docker volume (`STORAGE_BACKEND=local`)

Opcionális, már elérhető:

- S3 / MinIO (self-hosted, S3-kompatibilis objektumtár) –
  `STORAGE_BACKEND=s3`, `django-storages` + `boto3`, `AWS_*` env-ekkel

Megjegyzés: a MinIO **nem adatbázis és nem vektoros tár**. Fájlok
(STL/GLB/SCAD, preview képek) tárolására való, ugyanazzal az S3 API-val,
amit a felhő is használ. A storage backend interface mögött van, így a
`local` ↔ `s3` átállás kódváltás nélkül megy. Az „S3 mint adatbázis”
(relációs/vektoros adatok S3-ban) továbbra sem cél.

---

# 4. Architektúra

```text
                         Browser
                            |
                            v
                    +---------------+
                    |    Django     |
                    |               |
                    | Auth          |
                    | Workspace     |
                    | Projects      |
                    | Files         |
                    | UI            |
                    +-------+-------+
                            |
                    +-------+-------+
                    |               |
                    v               v
               Three.js        Agent API
                    |               |
                    |               v
                    |         +-----------+
                    |         | LangGraph |
                    |         +-----+-----+
                    |               |
                    |       +-------+-------+
                    |       |       |       |
                    |       v       v       v
                    |   Research   CAD   Validator
                    |      |       |       |
                    |      v       v       v
                    |     Web   OpenSCAD  trimesh
                    |              |
                    |              v
                    |             STL
                    |              |
                    +--------------+
```

---

# 5. Django adatmodell

## User

Django saját user modellje.

## Workspace

```text
Workspace
- id
- name
- owner
- created_at
- updated_at
```

## WorkspaceMember

```text
WorkspaceMember
- workspace
- user
- role
```

Role-ok:

```text
OWNER
ADMIN
MEMBER
VIEWER
```

## Project

```text
Project
- id
- workspace
- name
- description
- created_by
- created_at
- updated_at
```

## ModelVersion

```text
ModelVersion
- id
- project
- version
- prompt
- specification_json
- scad_file
- stl_file
- glb_file
- preview_image
- validation_json
- created_by
- created_at
```

## AgentRun

```text
AgentRun
- id
- project
- status
- user_prompt
- state_json
- started_at
- completed_at
- error
```

Az AgentRun tárolja az adott AI workflow állapotát.

## KnowledgeDocument

```text
KnowledgeDocument
- id
- source_type     # standard | datasheet | web | manual
- source_url
- title
- content         # nyers szöveg
- company_id      # opcionális: workspace/company scope
- created_at
```

## EmbeddingChunk

```text
EmbeddingChunk
- id
- document        # FK -> KnowledgeDocument
- chunk_index
- content
- embedding       # vector(EMBEDDING_DIM)  <- pgvector
- model           # pl. bge-m3
- created_at
```

Megjegyzés: az `EmbeddingChunk.embedding` a pgvector `vector` típusa,
HNSW index-szel a hasonlósági kereséshez. A dimenziót a `EMBEDDING_DIM`
env adja; modellcsere esetén a chunkokat újra kell embeddelni.

---

# 6. AI workflow

A felhasználó:

> Készíts egy falra szerelhető telefontartót Samsung S24-hez, 15 fokos döntéssel, M5 csavarokkal.

A workflow:

```text
USER PROMPT
    |
    v
PLANNER
    |
    +----> Need phone dimensions
    |             |
    |             v
    |        RESEARCH AGENT
    |             |
    |             v
    |        structured data
    |
    v
SPECIFICATION
    |
    v
CAD AGENT
    |
    v
OpenSCAD
    |
    v
STL
    |
    v
VALIDATOR
    |
    +---- invalid ---> CAD AGENT
    |
    v
VALID
    |
    v
3D PREVIEW
```

---

# 7. Agentek

## Planner Agent

Feladata:

- prompt értelmezése;
- követelmények kinyerése;
- szükséges kutatás felismerése;
- szükséges agentek kiválasztása;
- strukturált specification létrehozása.

Nem generáljon közvetlenül STL-t.

## Research Agent

Toolok:

- web search;
- URL/page extraction.

Feladata:

- termékadatok;
- méretek;
- szabványos méretek;
- csavarok;
- csatlakozók;
- gyártói specifikációk.

Output:

```json
{
  "source": "...",
  "data": {
    "width_mm": 70.6,
    "height_mm": 147.0,
    "thickness_mm": 7.6
  }
}
```

A forrásokat meg kell őrizni.

## CAD Agent

Input:

- user requirements;
- research results;
- structured specification.

Output:

- OpenSCAD source.

A CAD agent ne közvetlenül mesh-t generáljon.

## Validator Agent

Ellenőrizze:

- manifold;
- self intersection;
- zárt mesh;
- minimum wall thickness;
- minimum feature size;
- furatok;
- nyomtathatósági problémák.

Ha probléma van, strukturált hibát adjon vissza.

---

# 8. Structured specification

Az LLM és a CAD backend között ne szabad szöveges kommunikáció legyen.

Példa:

```json
{
  "object": "phone_holder",
  "dimensions": {
    "width": 70.6,
    "height": 147,
    "thickness": 7.6
  },
  "angle": 15,
  "wall_thickness": 4,
  "mounting": {
    "type": "M5",
    "count": 2
  },
  "material": "PETG"
}
```

Ez legyen validálható Pydantic modellel.

---

# 9. OpenSCAD worker

A backend ne közvetlenül a Django processzből futtassa a külső programot.

Javasolt:

```text
Django
  |
  v
Job Queue
  |
  v
CAD Worker
  |
  v
OpenSCAD container
```

A queue-t ne Django in-process taskkal oldjuk meg (nem éli túl a
restartot és blokkolja a web workert). Mivel a Redis amúgy is a
stackben van, a workerhez Celery (vagy huey / django-q2) használatos.
A státuszt a UI `fetch` + JSON pollinggal követi.

Parancs példa:

```bash
openscad \
  -o /workspace/output/model.stl \
  /workspace/input/model.scad
```

Minden user inputot sandboxolni kell.

Soha ne engedjünk tetszőleges shell parancsot az LLM-nek.

---

# 10. 3D Viewer

A browserbe:

```text
STL/GLB
   |
   v
Three.js
   |
   +-- rotate
   +-- zoom
   +-- pan
   +-- grid
   +-- axes
   +-- dimensions
```

A webes preview formátuma lehet GLB.

Az STL maradjon letölthető gyártási formátum.

---

# 11. Verziózás

Minden AI módosítás új verzió.

```text
Project: Phone Holder

v1
Initial model

v2
Added M5 holes

v3
Changed angle to 20°

v4
Increased wall thickness

v5
Final
```

Egy verzió tartalmazza:

- prompt;
- specification;
- SCAD;
- STL;
- GLB;
- validation;
- agent run;
- creation timestamp.

---

# 12. Slicing

Az MVP után:

```text
STL / 3MF
    |
    v
Printer selection
    |
    v
Material selection
    |
    v
OrcaSlicer
    |
    v
G-code / 3MF
```

A slicer konfiguráció legyen külön tárolva:

```text
PrinterProfile
FilamentProfile
ProcessProfile
```

A slicing engine legyen interface mögött:

```python
class SlicerBackend:
    def slice(self, model, printer, filament, process):
        ...

    def estimate(self, result):
        ...
```

Első implementation:

```text
PrusaSlicerBackend     # MVP: headless, egyszerűbb
```

Később:

```text
OrcaSlicerBackend      # wxWidgets/GL, headless gyakran Xvfb igény
```

---

# 13. Creality K2 Pro + CFS

Első konkrét printer integration:

```text
Creality K2 Pro
        |
        v
       CFS
```

A rendszer célja:

- printer online/offline státusz;
- CFS slotok;
- filament típus;
- gyártó;
- név;
- szín;
- slot állapot;
- filament hozzárendelés.

Példa UI:

```text
K2 Pro
Online

CFS
--------------------------------
Slot 1   Hyper PLA    Black
Slot 2   PETG        White
Slot 3   Hyper PLA    Red
Slot 4   Empty
--------------------------------
```

Fontos:

A Creality-specifikus kommunikáció külön adapter legyen:

```text
PrinterBackend
├── CrealityK2Backend
├── MoonrakerBackend
├── OctoPrintBackend
└── BambuBackend
```

A core alkalmazás ne függjön a K2 protokolltól.

---

# 14. Print Queue

```text
PrintJob
- id
- project
- model_version
- printer
- filament
- slicer_profile
- gcode
- status
- priority
- created_by
- created_at
```

Állapotok:

```text
QUEUED
PREPARING
SLICING
READY
PRINTING
PAUSED
COMPLETED
FAILED
CANCELLED
```

Több printer esetén:

```text
              Print Queue
                   |
       +-----------+-----------+
       |           |           |
       v           v           v
     K2 Pro      Printer B    Printer C
```

---

# 15. Sharing

Workspace szinten:

```text
Private
Workspace
Public
```

Public modell később:

```text
Model
- author
- description
- tags
- license
- downloads
- versions
```

Fontos: a felhasználó választhassa ki a licencet.

---

# 16. Open-source

Javasolt:

```text
License: AGPL-3.0
```

Indok:

A projekt célja self-hosted open-source alkalmazás. Az AGPL segíthet abban, hogy módosított hálózati szolgáltatásként terjesztett változatoknál is megmaradjanak az open-source kötelezettségek.

A licenc véglegesítése előtt érdemes jogi ellenőrzést végezni.

---

# 17. Repository struktúra

```text
ai-3d-print-hub/
│
├── backend/
│   ├── manage.py
│   ├── pyproject.toml   # uv: függőségek + tool config (ruff, pytest)
│   ├── uv.lock
│   ├── config/
│   ├── accounts/
│   ├── workspaces/
│   ├── projects/
│   ├── designs/         # korábban "models" – kerüli a django.db.models ütközést
│   ├── agents/
│   ├── api/             # DRF: /api/v1 (frontend + MCP közös felület)
│   ├── mcp/             # MCP szerver ugyanabban a Django processben
│   ├── printers/
│   ├── slicers/
│   └── files/
│
├── frontend/
│   ├── templates/
│   ├── static/
│   └── js/
│
├── workers/
│   ├── cad/
│   ├── slicing/
│   └── validation/
│
├── docker/
│   ├── django/
│   ├── openscad/
│   └── slicer/          # PrusaSlicer (MVP) + OrcaSlicer (később)
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
│
├── docs/
│
├── docker-compose.yml          # alap stack (Ollama nélkül)
├── docker-compose.ollama.yml   # belső Ollama override
├── .env.example
├── .gitignore
├── README.md
└── LICENSE
```

Minden Django app a nézetektől független `services.py`-t tartalmaz. A
view / DRF serializer / MCP tool mind ugyanezeket a service-eket hívja.

---

# 18. Docker Compose MVP

A stack **két üzemmódban** indítható, ugyanabból a kódbázisból. A különbség
csak az, hogy az Ollama belső konténerként vagy külső szolgáltatásként fut.

## 18.1 Belső Ollama (minden egy gépen)

```text
web (django)
worker (celery)
postgres (pgvector)   <- pgvector/pgvector:pg16
redis
ollama                <- belső szolgáltatás
openscad-worker
```

## 18.2 Külső Ollama (Ollama külön hoston / GPU gépen)

```text
web (django)
worker (celery)
postgres (pgvector)   <- pgvector/pgvector:pg16
redis
openscad-worker
```

Az Ollama itt nem konténer, hanem külső HTTP szolgáltatás:

```env
OLLAMA_BASE_URL=http://<host>:11434
```

## 18.3 Compose fájlok

```text
docker-compose.yml            # alap stack (Ollama NÉLKÜL)
docker-compose.ollama.yml     # override: hozzáadja az ollama service-t
```

Az override **csak** az `ollama` service-t adja hozzá, és felülírja a
`web`/`worker` `OLLAMA_BASE_URL`-jét `http://ollama:11434`-re.

Indítás:

```bash
# 1) Külső Ollama
docker compose up -d

# 2) Belső Ollama
docker compose -f docker-compose.yml -f docker-compose.ollama.yml up -d
```

Később:

```text
slicer-worker
printer-gateway
minio
```

## 18.4 TrueNAS SCALE 25.10 (Goldeye)

- Natív Docker + `docker compose`; GPU átadható az Ollamának
  (NVIDIA container toolkit + `deploy.resources.reservations.devices`).
- A SCALE rootfs immutábilis: **minden perzisztens adat pool-datasetre**
  kerüljön, pl.:

```text
/mnt/<pool>/apps/printforge/
├── pg/          # postgres data
├── redis/       # redis appendonly
├── media/       # MEDIA_ROOT (scad/stl/glb)
├── ollama/      # ollama modellek (csak belső módban)
└── .env         # a pool-on, NEM a repo-ban
```

- Csak a `web` port legyen publikálva; az `ollama`, `db`, `redis`,
  `openscad-worker` belső hálón maradjon. **A 11434 soha ne legyen kint.**
- Indítás SSH-ból `docker compose`-szal javasolt; a UI „Custom App”
  kevésbé kényelmes env override és hálózatkezelés miatt.
- A `web` és `worker` ugyanaz az image, csak más a command.
- Image-ek: `pgvector/pgvector:pg16` (DB, a pgvector extension már benne
  van), `redis:7-alpine`, a Django image pedig `uv sync --frozen`-nel épül
  a `uv.lock` alapján.
- pgvector aktiválás migrációban: `CREATE EXTENSION IF NOT EXISTS vector;`

---

# 19. Environment variables

Példa:

```env
DJANGO_DEBUG=true
DATABASE_URL=postgres://...
REDIS_URL=redis://redis:6379/0

# belső Ollama (override) esetén:
OLLAMA_BASE_URL=http://ollama:11434
# külső Ollama esetén pl.:
# OLLAMA_BASE_URL=http://192.168.1.50:11434

OLLAMA_MODEL=qwen3-coder:30b
# kisebb GPU/CPU géphez: OLLAMA_MODEL=qwen2.5-coder:7b

# Embedding (RAG)
EMBEDDING_MODEL=bge-m3
EMBEDDING_DIM=1024
# könnyű fallback: EMBEDDING_MODEL=nomic-embed-text / EMBEDDING_DIM=768
RAG_ENABLED=false        # MVP: infrastruktúra kész, használat Phase 4-től

STORAGE_BACKEND=local
MEDIA_ROOT=/data/media
```

Opcionális:

```env
OPENAI_API_KEY=
OPENROUTER_API_KEY=
```

Cloud LLM soha ne legyen kötelező.

---

# 20. Biztonság

Az LLM által generált kód nem megbízható.

Kötelező:

- OpenSCAD sandbox;
- resource limit;
- timeout (kemény, végtelen ciklus ellen);
- CPU limit;
- memory limit;
- filesystem isolation;
- network access tiltása az OpenSCAD workerben;
- shell command execution tiltása;
- user fájlok elkülönítése.

Konkrét konténer kapcsolók (openscad-worker):

```text
--network none
--read-only
--tmpfs /tmp
--cap-drop ALL
--security-opt no-new-privileges
--pids-limit 256
--memory 1g --cpus 1.0
```

Python oldalon a `subprocess` mindig **lista-argumentummal** hívódjon
(`shell=False`), soha ne string parancs. Az OpenSCAD `import()` /
`surface()` fájlt tud olvasni és DoS-t tud okozni, ezért ezeket
whitelistelni kell, és a futásra kemény timeout kell.

A printer control külön permission legyen.

```text
VIEWER
  -> model viewing

MEMBER
  -> generate/edit

PRINTER_OPERATOR
  -> print

ADMIN
  -> printer management

OWNER
  -> workspace management
```

---

# 21. Fejlesztési sorrend

## Phase 1 – Foundation

- [x] Repository
- [x] uv + pyproject.toml
- [x] Django
- [x] PostgreSQL + pgvector
- [x] Docker Compose
- [x] Teszt keretrendszer (pytest-django)
- [x] User authentication
- [x] Workspace
- [x] Project
- [x] Basic UI

## Phase 2 – CAD

- [x] Ollama integration
- [x] Qwen3-Coder adapter
- [x] Prompt endpoint
- [x] OpenSCAD generation
- [x] OpenSCAD worker
- [x] STL generation
- [x] File storage
- [x] Embedding szolgáltatás + pgvector extension (csak infrastruktúra)

## Phase 3 – Viewer

- [x] Three.js
- [x] STL/GLB loading
- [x] rotate
- [x] zoom
- [x] pan
- [x] model information
- [x] download

## Phase 4 – Agent

- [x] LangGraph
- [x] Planner
- [x] Research Agent
- [x] RAG retrieval a Research agenthez (pgvector + bge-m3)
- [x] Standard alkatrész tudásbázis ingest (csavarok, szabványok, méretek)
- [x] CAD Agent
- [x] Validator
- [x] Retry loop
- [x] AgentRun persistence
- [ ] Laya döntési modell kiértékelése (Planner/guardrail/triage)
- [x] Vision input: kép feltöltés és referencia-ként használat, ha a
      modell vision-képes (lásd 27. fejezet)

## Phase 5 – Slicing

- [x] PrusaSlicer worker
- [x] printer profiles
- [x] filament profiles
- [x] process profiles
- [ ] slicing preview
- [x] time/filament estimation
- [x] Build plate / multi-object slicing (később, lásd 28. fejezet)

## Phase 6 – Printer

- [x] Printer abstraction
- [x] K2 Pro adapter
- [x] CFS status
- [x] printer status
- [x] print upload
- [x] print start
- [x] print status
- [x] print cancellation

## Phase 7 – Multi-user

- [x] permissions
- [x] shared projects
- [x] shared printer queue
- [x] print history
- [x] notifications

## Phase 8 – Community

- [x] public models
- [x] model sharing
- [x] search
- [x] tags
- [x] ratings
- [x] downloads
- [x] model licenses
- [x] AI leírás/tag kitöltés („Description by AI" gomb, üres mezők,
      no-overwrite – lásd 29. fejezet)

---

# 22. Első konkrét milestone

A fejlesztés első célja legyen:

```text
Browser
   |
   | "Create a phone holder"
   v
Django
   |
   v
Ollama / Qwen3-Coder
   |
   v
model.scad
   |
   v
OpenSCAD
   |
   v
model.stl
   |
   v
Three.js
   |
   v
3D preview
```

Ha ez működik, az első technikai mérföldkő kész.

Ezután lehet hozzáadni a research agentet és a validációt.

---

# 23. Nem cél az első verzióban

Ne kerüljön az MVP-be:

- saját 3D engine;
- saját slicer;
- saját CAD kernel;
- mobilalkalmazás;
- cloud account;
- marketplace;
- komplex billing;
- 10+ printer támogatása;
- többféle CAD backend;
- komplex permission rendszer.

Az első cél egy stabil:

**Prompt → Parametric CAD → STL → Browser Preview**

pipeline.

---

# 24. Hosszú távú vízió

```text
                    AI 3D PRINT HUB
                           |
       +-------------------+-------------------+
       |                   |                   |
       v                   v                   v
    AI CAD             Workspace            Printers
       |                   |                   |
       v                   v                   v
   OpenSCAD            Projects             K2 Pro
   CadQuery            Versions             Bambu
   build123d           Sharing              Prusa
       |                   |                   |
       +-------------------+-------------------+
                           |
                           v
                     Print Queue
                           |
                           v
                      Print History
                           |
                           v
                    Community Library
```

A végső cél:

> Egy teljesen self-hosted, open-source platform, amely a természetes nyelvű ötlettől a parametrikus CAD-en és szeletelésen keresztül egészen a tényleges 3D nyomtatásig végigviszi a felhasználót.

---

# 25. MCP integráció

Az MCP szerver **ugyanabban a Django processben** fut, nem külön
szolgáltatás. Nem a frontendet és nem a REST nézeteket hívja, hanem
közvetlenül a `services.py`-okat.

## 25.1 Miért MCP-ready a szerkezet

- Minden üzleti logika `services.py`-ban van, a view-k csak hívják.
- A DRF JSON API (`/api/v1`) és az MCP toolok ugyanazt a service-t
  használják – nincs duplikált logika.
- A frontend `fetch`-csel a JSON API-ra épül (nincs HTMX), így az
  API szerződés stabil és tesztelhető.

## 25.2 Rétegek

```text
Alpine.js / fetch          MCP client (LLM)
        |                         |
        v                         v
   /api/v1 (DRF)            mcp/ tools
        \                        /
         \                      /
          v                    v
        services.py  (üzleti logika)
                  |
                  v
               models / ORM / Celery
```

## 25.3 MCP tool készlet

A registry (`mcp.tools`) az élő igazságforrás; a transzport a hivatalos SDK-val
fut (lásd 37. fejezet). A core toolok:

```text
create_workspace
create_project
list_projects
generate_model_from_prompt
get_model_version
export_model_stl
edit_model_from_annotations
publish_project / unpublish_project
search_public_projects
set_project_tags
rate_project
record_download
mark_project_printed
generate_project_description
create_build_plate / add_plate_item
enqueue_print_job
```

Ezek mind `services.py` hívások, nem shell és nem HTTP.

Fontos: az MCP toolok ugyanazoknak a permission-öknek és sandbox
szabályoknak legyenek alárendelve, mint a webes felhasználó (lásd
20. fejezet). A `allow_shell` továbbra is tilos.

---

# 26. Dependency- és tesztelési stratégia

## 26.1 uv

- `uv` kezeli a függőségeket és a virtuális környezetet.
- `pyproject.toml` a forrás, `uv.lock` a reprodukálható zárolás
  (commitolva).
- Docker image is uv-val telepít: `uv sync --frozen` a `uv.lock` alapján.
- Fejlesztés:

```bash
uv sync
uv run python manage.py migrate
uv run pytest
```

- Dev csoportok: `uv sync --group dev` (pytest, ruff stb.).

## 26.2 Tesztelés

Keretrendszer:

- `pytest` + `pytest-django`
- `factory_boy` a tesztadatokhoz
- DRF `APIClient` az API tesztekhez
- `ruff` lint + formázás, `mypy` opcionális

Szintek (`tests/`):

```text
tests/unit/          # services.py, pure logika, nincs DB
tests/integration/   # ORM, API, Celery taskok (DB-vel)
tests/e2e/           # teljes prompt -> STL folyamat (Ollama mockolva)
```

## 26.3 Amit kötelező tesztelni

- Permission-ök (workspace role-ok, printer permission).
- Az OpenSCAD sandbox: tiltott `import()`, timeout, resource limit.
- Structured specification Pydantic validáció (8. fejezet).
- A CAD pipeline: prompt → scad → stl (LLM mockkal, determinisztikusan).
- Storage backend: lokális írás/olvasás.
- MCP toolok: ugyanazt a service-t hívják, mint az API.

## 26.4 CI

GitHub Actions (a repo GitHubon van):

```text
- ruff check
- pytest (PostgreSQL + pgvector service container)
- docker compose config validáció
```

Az OpenSCAD-worker hívás tesztben stub/mock, hogy CI-ban ne kelljen
valódi OpenSCAD futás.

---

# 27. Vision / kép alapú prompt (később)

Cél: a felhasználó feltölthessen **fényképet** (létező tárgyról, hibás
alkatrészről, kézzel rajzolt vázlatról), és az AI azt is használja
referenciaként a modell megtervezéséhez – ha a kiválasztott LLM
**vision-képes**.

## 27.1 Provider képesség

A vision-támogatást nem hardcode-oljuk, hanem a `LLMProvider`
interfészén keresztül kérdezzük le:

```text
LLMProvider
├── supports_vision() -> bool
└── complete(messages, images=[...])
```

- Ollama: `llava`, `qwen2.5-vl`, `llama3.2-vision` stb. – a modell
  capability metaadatai döntik el, nem a név.
- OpenAICompatible / Anthropic: az API támogatja, de a választott
  modelltől függ.

Fallback: ha a modell nem vision-képes, a kép **nem** kerül elküldésre,
a felhasználó figyelmeztetést kap, és a generálás a szöveges prompt
alapján fut tovább (nem hibázik el).

## 27.2 Használat referenciaként

- A kép a structured specification része (`reference_image` mező + a
  feltöltött fájl azonosítója).
- A Planner/Research agent referenciaként kapja: forma, arány,
  funkció, a fotón olvasható szöveg/felirat.
- Abszolút méretet a kép önmagában nem ad, ezért a felhasználótól
  kérhető skála/mérték (pl. „mekkora ez valójában?”) vagy egy ismert
  referencia a fotón.
- A feltöltött kép a projektverzióhoz kapcsolódik (reprodukálhatóság),
  és a storage backendben tárolódik.

## 27.3 Nem cél

- Fotogrammetria / kép → mesh rekonstrukció nem cél.
- A kép nem helyettesíti a geometriai validációt; a kimenet továbbra
  is parametrikus OpenSCAD.

---

# 28. Build plate / multi-object slicing (később)

Cél: a felhasználó **több modellt tegyen ugyanarra a tálcára**, és egy
szeleteléssel (egy G-code-dal) nyomtassa ki őket – a szokásos
slicer-élményhez hasonlóan.

Az MVP-ben a `PrintJob` egyetlen `ModelVersion`-t szeletel
(`_read_model` egy STL-t olvas, a `SlicerBackend.slice()` egy mesh-t
kap). Ez a fejezet azt a bővítést írja le, amivel ez több objektumra
nyílik.

## 28.1 Domain

Új fogalom: **BuildPlate** (tálca) és **PlateItem** (egy objet a
tálcán).

```text
Project
└── BuildPlate            # egy tálca-elrendezés
    ├── PlateItem         # model_version + pozíció/rotáció/skála
    ├── PlateItem
    └── PlateItem
```

- `BuildPlate` a projekthez tartozik, és opcionálisan egy
  printer-profilt köt.
- `PlateItem`: `model_version` FK + `x`, `y`, `z` pozíció, `rotation`,
  `scale` (illetve `settings_json` a későbbi per-item override-okhoz).
- A `PrintJob` vagy egy `BuildPlate`-re mutat, vagy megmarad egy-objektumosnak,
  és a tálca a job előkészítő lépése.

## 28.2 Szeletelés

- A `SlicerBackend.slice()` kapjon **mesh-listát** (tálca), ne egyetlen
  `ModelInput`-ot. A visszafelé kompatibilitás érdekében az egy-mesh
  hívás maradhat egy egyelemű lista.
- Konkrét implementáció: a modelleket `--merge`-dzsel egy tálcává
  fűzni, vagy 3MF projectként átadni a PrusaSlicernek (ez utóbbi viszi
  a pozíciót/rotációt is, ezért ez a preferált út).
- `estimate`/metadata maradjon **tálca-szintű összesítés** (idő,
  filament), opcionálisan itemenkénti bontással.

## 28.3 Nyitott kérdések / kockázatok

- **Ütközés- és beférős-ellenőrzés**: a tálca méretét és az
  objektumok elhelyezését validálni kell (auto-arrange vagy legalább
  „nem lóg le / nem fedi egymást").
- **Per-item profilok**: eltérő filament/szín egy tálcán – csak akkor,
  ha a nyomtató (pl. CFS) támogatja.
- **Preview**: a viewernek a tálcát és az egyes itemek pozícióját is
  mutatnia kell.
- Az ellenőrzés továbbra is a slicer oldalán történik, saját geometriai
  motort nem építünk (lásd 23. fejezet).

---

# 29. AI leírás és tag kitöltés (később)

Cél: a modell leírása és tagjei ne maradjanak üresen. Ha üresek, az LLM
**javasoljon** tartalmat, és legyen egy **„Description by AI"** gomb,
amivel a felhasználó egy kattintással ki tudja tölteni a mezőket.

Alapszabály: **amit a felhasználó beírt, azt soha nem írjuk felül.**

## 29.1 Kitöltési szabályok

- Az automatikus kitöltés **csak üres mezőt** tölt (leírás és/vagy tag).
  Nem üres mezőt az automata kihagy.
- A gomb („Description by AI") is a fenti szabályt követi: kézzel írt
  tartalmat nem ír felül. Ha mégis felülírás kellene, ahhoz külön,
  explicit megerősítés szükséges.
- A kézzel írt érték **mindig erősebb** a generáltnál.
- A generált szöveg **javaslat**: a felhasználó szerkesztheti.

## 29.2 Provenance (miért fontos)

Ahhoz, hogy a „ne írjuk felül" szabálybetarthó legyen, nyilván kell
tartani, honnan jött az érték:

```text
description_source:  manual | ai | empty
tags_source:         manual | ai | empty
```

- Ha `manual` → az AI semmilyen esetben nem nyúl hozzá.
- Ha `ai` vagy `empty` → az AI/gomb feltöltheti.
- A gomb által írt érték `ai`-re vált, a user szerkesztése `manual`-ra.

## 29.3 Bemenet és generálás

- Bemenet: a project neve + verzió `prompt` + `specification_json` +
  `validation_json` (a tényleges geometriából). Vizuális jelzés (preview
  kép / vision, lásd 27. fejezet) opcionális plusz.
- Kimenet: rövid, tárgyilagos leírás + **strukturált tag-lista**
  (Pydantic séma, mint a structured specification).
- Ugyanazt a `LLMProvider`-t használja, mint a generálás; ha nincs
  elérhető modell, a funkció csendben kimarad (nem blokkol).

## 29.4 Hol jelenik meg

- **Automatika**: project létrehozásakor, és/vagy amikor egy verzió
  elkészül – ha a mező üres.
- **Gomb**: a project/verzió oldalon „Description by AI" – a felhasználó
  bármikor kérhet javaslatot.
- A tag mező a Phase 8 (Community) tag-funkciójára épül; addig csak a
  leírás él.

## 29.5 Nem cél

- Nem írunk felül meglévő, felhasználói tartalmat.
- Nem generálunk hosszú marketing-szöveget; rövid, technikai leírás a
  cél.

---

# 30. CAD primitívek (prompt-függő geometria)

A 8. fejezet structured specification-je eleinte egyetlen paraméteres
**telefontartó**-templátot tudott csak leírni, ezért bármely prompt
gyakorlatilag ugyanazt a modellt adta. A 30. fejezet ezt nyitja ki:
a specifikáció `primitives` listát kap, amiből az LLM (kód nélkül) valódi
geometriát ír le.

- Primitivek: `box`, `cylinder`, `sphere`, `cone`; `role: add | subtract`;
  `position` = a primitív középpontja mm-ben; `rotation` fokban.
- A CAD backend CSG-vel renderel:
  `difference() { union() { <add> } <subtract> }`.
- Üres `primitives` esetén a korábbi telefontartó-templát fut (backward
  compatible, bájt-azonos kimenet).
- A Planner/Editor a primitívekből állítja össze a tárgyat (a tálcán állva,
  min Z = 0); a `dimensions`/`wall_thickness`/`mounting` mezők
  kompatibilitásból megmaradnak.
- A vizuális annotációs `operations` (lásd a vizuális szerkesztés tervét)
  a primitív-bázisra rétegződik.

Részletek: [`docs/cad-primitives.md`](./docs/cad-primitives.md).

---

# 31. Vision önellenőrzés

A pipeline a validálás után **visszaellenőrzi saját magát**, ha a választott
LLM vision-képes:

```text
cad -> validate -> review
review --(nem egyezik, van kísérlet)--> cad (LLM reviserrel)
review --(egyezik / nincs vision / kimerült)--> END
```

- A `review` node az STL-ből előnézeti PNG-t renderel
  (`designs/cad/preview.py`, trimesh + Pillow, headless), és ha a provider
  `supports_vision()`, strukturált `ReviewResult`-ot kér tőle
  (`matches`, `issues`, `summary`).
- Ha nem egyezik és van kísérlet, a review hibái bekerülnek a
  `validation.errors`-be, és egy **LLM-alapú reviser** javítja a
  specifikációt; a CAD újrafut. A retry továbbra is korlátos
  (`AGENT_MAX_ATTEMPTS`).
- Ha nincs vision / nincs preview / a review meghiúsul: a run **nem bukik
  el**, csak figyelmeztetés kerül a history-ba.
- Az előnézeti kép a `ModelVersion.preview_image` mezőbe kerül, és a
  `GET /api/v1/versions/{id}/artifact/preview/` végponton letölthető
  (inline `image/png`).

Részletek: [`docs/vision-self-check.md`](./docs/vision-self-check.md).

---

# 32. Skill-ek (újrahasznosítható generálási receptek)

A **Skill** névvel ellátott, felhasználó által létrehozható recept egy
tárgy-osztály generálásához (pl. „süti kinyomó”, „telefontartó”). Alapelv
változatlan: a skill **strukturált adat + szöveges iránymutatás**, soha nem
OpenSCAD kód; az LLM továbbra is `ModelSpecification`-t ad, a geometriát a CAD
backend állítja elő.

- Két fajta, egy sémán: `guidance` (iránymutatás + defaultok + gépi
  ellenőrzések) és `template` (beégetett generátort nevez meg, pl. a meglévő
  telefontartó). A mai beégetett sablon így egy `template` skill.
- A `defaults_json` **javaslat** a promptnak; a `constraints_json`-t a
  **Validator** kényszeríti (pl. `min_wall_mm`, `must_rest_on_plate`,
  `require_primitives`) – a skill kikényszerített, nem „sugallt”.
- Kiválasztás determinisztikusan: **manuális** (projektnél tartósan vagy
  generálásnál egyszer) mindig elsőbbséget élvez; egyébként **auto** –
  először tag/`object_kind` egyezés, majd `RAG_ENABLED` esetén szemantikus
  illesztés.
- Kapcsolódás: `Project.skills` M2M (tartós) + per-run `skill_ids` (egyszeri).
  A használt skillek és a választás módja (`manual`/`auto`) a verzió
  provenance-ába (`validation_json`) kerül.
- REST: `skills` CRUD ViewSet; UI: `/skills/` szerkesztő. Seed skillek:
  `phone_holder` (template), `cookie_cutter` (guidance).

Részletek: [`docs/skills.md`](./docs/skills.md).

---

# 33. Planner visszakérdezés és feltételezések

A Planner eddig minden hiányzó értéket csendben printable defaulttal pótolt.
Mostantól a döntés auditálható:

- `Clarification` (`question`, `answer`, `kind`, `field`) a `PlannerPlan`
  része – **nem** a `ModelSpecification`-é, ezért soha nem jut el a CAD
  backendhez (a strict LLM↔CAD szerződés változatlan).
- `kind="assumed"`: a Planner tippelt (`answer` kitöltve), a run megy tovább.
  `kind="needs_user_input"`: nem tippel, `clarify_policy="ask"` esetén a run
  megáll `status="clarification"`-nel (nincs `ModelVersion`), a user válaszol,
  és **új run** indul a válaszokkal.
- Alapértelmezés `clarify_policy="assume"`: a generálás **sosem blokkol**
  magától. Az assumed tippek a verzió `validation_json`-jába kerülnek
  (`assumptions`, `review_required`), és a UI felülvizsgálatra jelöli őket.
- API: `POST /api/v1/runs/{id}/clarifications/`; a generálás payload
  opcionális `clarify` mezője.

Részletek: [`docs/planner-clarification.md`](./docs/planner-clarification.md).

---

# 34. Verzió-history: újragenerálás és szerkesztés

A verziók **immutable-ek** (11. fejezet): a szerkesztés sosem írja át a
meglévő verziót, hanem új, a forrásból származó verziót hoz létre.

- `ModelVersion` új mezői: `parent_version` (a származás) és `origin`
  (`generate` | `annotation` | `regenerate` | `manual`).
- `POST /api/v1/versions/{id}/regenerate/`:
  - üres body → tiszta újragenerálás (ugyanaz a prompt),
  - `prompt` → szerkesztett promptból újragenerálás,
  - `specification_json` → kézzel adott spec manuális renderje.
- Az újragenerált run ugyanazt a `run_agent_workflow`-t futtatja, ezért
  örökli a Planner visszakérdezést/feltételezéseket (33. fejezet).

Részletek: [`docs/version-history-controls.md`](./docs/version-history-controls.md).

---

# 35. Workspace-first navigáció

A főoldal a **workspace-ek listája**; az elemek (projektek) egy workspace-en
belül jönnek létre.

```text
/                        -> Workspace lista (főoldal)
/workspaces/{id}/        -> Elemek (projektek) listája az adott workspace-ben
/projects/{id}/          -> Elem részlete (viewer, verziók, history controls)
/community/...           -> Változatlan
```

A modellben, route-ban és API-ban a `Project` név marad; csak a UI mondhat
„elemet”. A projekt létrehozásakor a workspace a route-ból jön, nem
választólistából; az API `?workspace=<id>` szűrőt kap.

Részletek: [`docs/workspace-navigation.md`](./docs/workspace-navigation.md).

---

# 36. Web research (SearXNG + trafilatura)

A Research agent a lokális RAG mellett opcionálisan egy **SearXNG-kompatibilis
JSON API-t** kérdez:

```text
GET {SEARXNG_BASE_URL}/search?q=<query>&format=json
```

- Engedélyezés: `SEARCH_BACKEND=searxng` + `SEARXNG_BASE_URL` (a self-hosted
  instance a `docker-compose.search.yml` override-dal indítható, belső
  hálón, publikált port nélkül).
- A legfelső találatok oldalának fő szövegét a **`trafilatura`** nyeri ki
  (bounded: max ~3000 karakter találatonként).
- **Best-effort:** kikapcsolt funkció, hiányzó URL, elérhetetlen instance
  vagy használhatatlan JSON esetén üres találat + warning; a generálás nem
  áll meg és nem bukik el.
- Az oldalletöltés **SSRF-védett** (csak `http`/`https`, nyilvános IP-re
  oldódó host), és a worker processzben fut, nem az OpenSCAD sandboxban.

---

# 37. MCP transzport (hivatalos SDK)

A `mcp` app registryje (`mcp.tools`) az egyetlen igazságforrás; a
`mcp/server.py` ebből épít `MCPServer`-t, és minden tool hívása a
`mcp.tools.call` **egyetlen chokepointján** megy át – így az `authorize`
workspace-role kapu és a `allow_shell` tiltása ugyanúgy érvényes, mint a
webes felhasználónál.

- Futtatás: `manage.py mcp_server --transport stdio|streamable-http`
  (`--host` / `--port` / `--path`).
- MCP-nek nincs Django `request.user`-je: az identitás a
  `MCP_SERVICE_USER_ID` settingből / env-ből (illetve a
  `mcp_service_user_id` runtime override-ból) jön. A kliens által küldött
  `user_id`/`owner_id` paraméter eldobódik – nem lehet más felhasználót
  megszemélyesíteni.
- Tool készlet: `create_workspace`, `create_project`, `list_projects`,
  `generate_model_from_prompt`, `get_model_version`, `export_model_stl`,
  `edit_model_from_annotations`, `publish_project`, `unpublish_project`,
  `search_public_projects`, `set_project_tags`, `rate_project`,
  `record_download`, `mark_project_printed`, `generate_project_description`,
  `create_build_plate`, `add_plate_item`, `enqueue_print_job`.

Lásd még: 25. fejezet.

---

# 38. Creality K2 CFS a Moonraker `[box]` objektumból

A Creality K2 sorozat stock Moonraker alatt fut, és a Creality `[box]` Klipper
modul a standard object-query API-n keresztül adja a CFS állapotát:

```text
GET /printer/objects/query?box
```

- Ez az **elsődleges** CFS-forrás (dokumentált Klipper/Moonraker felület,
  nincs reverse-engineered framing).
- A `printers/k2_box.py` két, a vadon előforduló payload-alakot tolerál:
  először a community „flat” `box["slots"]` listát, fallbackként a stock
  Creality `T1..Tn` per-unit párhuzamos tömböket
  (`material_type` / `color_value` / `vender`).
- A parser szándékosan defenzív: parse-olhatatlan alak → `[]`, és a K2
  transzport a `printers/k2_websocket` readerre esik vissza. A „nincs CFS” és
  a „query hiba” megkülönböztethető.

Részletek: `backend/printers/k2_box.py`.

---

# 39. 2D `extrude` primitív

A 30. fejezet primitívjei (box/cylinder/sphere/cone) nem tudnak kifejezni
vékony falú, 2D körvonalból húzott tárgyat (pl. *süti kinyomó*). Ezt a
`extrude` primitív nyitja ki:

- `Primitive.type` bővül: `"extrude"`; új mezők: `profile: list[Vec2]` (2D
  körvonal pontok mm-ben, XZ sík), `wall_thickness`, `round_radius`, `height`.
- Renderelés: `linear_extrude(height) polygon(points)`; fal esetén
  `difference()` a belül eltolt (Pythonban számolt) körvonallal;
  `round_radius` → `offset(r=...)` / `minkowski` (kicsi, korlátozott).
- A `profile` **inline pontlista** – soha fájl, nincs `import()`/`surface()`
  (a sandbox szabály sérülne); a meglévő `validate_scad_source` ellenőrzi.
  Új bounds: pontszám (pl. ≤ 256), koordináta-tartomány a primitív boundokkal.
- A skill-ekkel együtt készült el (a *süti kinyomó* és hasonlók enélkül nem
  működnének), és a `docs/cad-primitives.md` „nem cél” listáját ez a pont
  felülírja.

Részletek: [`docs/skills.md`](./docs/skills.md) 5., valamint
[`docs/cad-primitives.md`](./docs/cad-primitives.md).

