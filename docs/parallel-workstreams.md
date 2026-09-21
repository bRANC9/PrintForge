# Párhuzamos workstreamek (opencode subagentek)

A maradék munka (Phase 2–8) párhuzamosítható sávokra bontva. Minden sáv egy
opencode subagent (`.opencode/agent/<name>.md`), **diszjunkt írási scope-pal**,
hogy egyszerre futhassanak konfliktus nélkül.

## Sávok

| Agent | Fázis | Írási scope | Fő deliverable |
| --- | --- | --- | --- |
| `core-model` | 1–8 | `backend/config/**`, `*/models.py`, `*/migrations/**`, `*/admin.py` | séma, migrációk (serializált) |
| `api-dev` | 1–7 | `backend/api/**`, `*/services.py` | DRF JSON API + service réteg |
| `mcp-tools` | 1, 4+ | `backend/mcp/**` | in-process MCP toolok |
| `llm-provider` | 2 | `backend/agents/llm/**`, `backend/agents/spec.py` | `LLMProvider` + Pydantic spec |
| `cad-worker` | 2 | `backend/designs/cad/**`, `backend/designs/tasks.py`, `workers/cad/**`, `docker/openscad/**` | spec → SCAD → STL sandbox |
| `rag-embedding` | 2, 4 | `backend/agents/rag/**` | pgvector embedding + retrieval |
| `viewer-frontend` | 1, 3 | `frontend/**` | UI + Three.js STL viewer |
| `agent-orchestrator` | 4 | `backend/agents/graph/**`, `backend/agents/tasks.py` | Planner/Research/CAD/Validator loop |
| `slicer-worker` | 5 | `backend/slicers/**`, `workers/slicing/**`, `docker/slicer/**` | PrusaSlicer backend + profilok |
| `printer-integration` | 6 | `backend/printers/**` | `PrinterBackend` + K2/CFS adapter |
| `infra-devops` | 1–8 | `docker/django/**`, `docker/db/**`, compose, `.github/**`, `.env.example` | image, CI, TrueNAS |
| `qa-tests` | 1–8 | `tests/**` | unit/integration/e2e tesztek |

## Függőségek (mi blokkol mit)

```text
core-model ──► mindenki (séma nélkül nincs mit építeni)

llm-provider ──► agent-orchestrator, cad-worker (spec)
rag-embedding ◄── core-model (KnowledgeDocument, EmbeddingChunk modellek)
cad-worker ──► agent-orchestrator (CADBackend), api-dev (státusz endpoint)
slicer-worker ──► printer-integration (gcode bemenet)
api-dev ──► viewer-frontend, mcp-tools (endpoint szerződés)
infra-devops, qa-tests ──► bármikor párhuzamosan
```

## Ajánlott indítási sorrend

1. **Hullám 1 (párhuzamos):** `core-model` (elsőként, gyorsan zárja a sémát),
   `infra-devops`, `viewer-frontend` (statikus UI), `qa-tests` (teszt infra).
2. **Hullám 2 (párhuzamos):** `llm-provider`, `cad-worker`, `rag-embedding`,
   `api-dev`, `mcp-tools`.
3. **Hullám 3:** `agent-orchestrator` (a 2. hullám interfészeire épül).
4. **Hullám 4:** `slicer-worker`, `printer-integration`.

## Szabályok minden agentnek

- Csak a saját scope-jában ír. Más sáv fájlját nem szerkeszti — kérést ad át.
- A modell/migráció változás `core-model` dolga; a feature agent pontos
  mezőlistát kér tőle.
- Üzleti logika `services.py`-ban, hogy a DRF és az MCP is hívhassa.
- Kapuk: `ruff check`, `ruff format --check`, `djlint --check` (template),
  `pytest`, `manage.py check`. Push tilos agentből.

## Megjegyzés

A subagent-definíciók **konfigurációs fájlok**: az opencode a induláskor tölti
be őket. Módosítás után **újra kell indítani az opencode-ot**.
