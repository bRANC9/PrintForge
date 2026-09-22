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
├── OpenAICompatibleProvider
└── AnthropicProvider (később)
```

Így az alkalmazás ne legyen Ollama-specifikus.

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

MVP:

- lokális filesystem vagy Docker volume

Később:

- S3
- MinIO (self-hosted, S3-kompatibilis objektumtár)

Megjegyzés: a MinIO **nem adatbázis és nem vektoros tár**. Fájlok
(STL/GLB/SCAD, preview képek) tárolására való, ugyanazzal az S3 API-val,
amit a felhő is használ. Az MVP lokális volume-mal indul; a storage
backend interface mögött van, így az átállás kódváltás nélkül megy.

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

- [ ] Repository
- [ ] uv + pyproject.toml
- [ ] Django
- [ ] PostgreSQL + pgvector
- [ ] Docker Compose
- [ ] Teszt keretrendszer (pytest-django)
- [ ] User authentication
- [ ] Workspace
- [ ] Project
- [ ] Basic UI

## Phase 2 – CAD

- [ ] Ollama integration
- [ ] Qwen3-Coder adapter
- [ ] Prompt endpoint
- [ ] OpenSCAD generation
- [ ] OpenSCAD worker
- [ ] STL generation
- [ ] File storage
- [ ] Embedding szolgáltatás + pgvector extension (csak infrastruktúra)

## Phase 3 – Viewer

- [ ] Three.js
- [ ] STL/GLB loading
- [ ] rotate
- [ ] zoom
- [ ] pan
- [ ] model information
- [ ] download

## Phase 4 – Agent

- [ ] LangGraph
- [ ] Planner
- [ ] Research Agent
- [ ] RAG retrieval a Research agenthez (pgvector + bge-m3)
- [ ] Standard alkatrész tudásbázis ingest (csavarok, szabványok, méretek)
- [ ] CAD Agent
- [ ] Validator
- [ ] Retry loop
- [ ] AgentRun persistence
- [ ] Laya döntési modell kiértékelése (Planner/guardrail/triage)
- [ ] Vision input: kép feltöltés és referencia-ként használat, ha a
      modell vision-képes (lásd 27. fejezet)

## Phase 5 – Slicing

- [ ] PrusaSlicer worker
- [ ] printer profiles
- [ ] filament profiles
- [ ] process profiles
- [ ] slicing preview
- [ ] time/filament estimation
- [ ] Build plate / multi-object slicing (később, lásd 28. fejezet)

## Phase 6 – Printer

- [ ] Printer abstraction
- [ ] K2 Pro adapter
- [ ] CFS status
- [ ] printer status
- [ ] print upload
- [ ] print start
- [ ] print status
- [ ] print cancellation

## Phase 7 – Multi-user

- [ ] permissions
- [ ] shared projects
- [ ] shared printer queue
- [ ] print history
- [ ] notifications

## Phase 8 – Community

- [ ] public models
- [ ] model sharing
- [ ] search
- [ ] tags
- [ ] ratings
- [ ] downloads
- [ ] model licenses
- [ ] AI leírás/tag kitöltés („Description by AI" gomb, üres mezők,
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

# 25. MCP integráció (később)

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

## 25.3 MCP tool készlet (terv)

```text
create_project
generate_model_from_prompt
get_model_version
list_projects
export_model_stl
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

