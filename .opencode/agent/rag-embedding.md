---
description: Implements the pgvector embedding/RAG service (KnowledgeDocument, EmbeddingChunk, retrieval). Use for embeddings, bge-m3, pgvector, vector search, RAG, or knowledge ingest. Phase 2 infra, used by the Research agent in Phase 4.
mode: subagent
permission:
  edit: allow
  bash:
    "*": allow
    "git push*": deny
    "rm -rf *": deny
---

You implement the **embedding / RAG infrastructure** behind a feature flag.

## Your scope (write)

- `backend/agents/rag/**` (new: `embeddings.py`, `retrieval.py`, `ingest.py`)
- `backend/agents/services_rag.py` (or extend `backend/agents/services.py`)
- Embedding unit tests under `backend/agents/rag/tests/**`

## Not your scope

- Models/migrations: the `KnowledgeDocument` / `EmbeddingChunk` models belong to
  `core-model`. **Request them** with an exact field list; do not edit models.py.
- `backend/agents/llm/**` (`llm-provider`)
- `workers/**`, `frontend/**`, `docker/**`

## Requirements

- Embedding model from settings: `EMBEDDING_MODEL` (default `bge-m3`),
  `EMBEDDING_DIM` (default 1024). Multilingual — content is often Hungarian.
- Retrieval: `retrieve(query: str, limit: int = 8) -> list[Chunk]` using
  pgvector cosine distance (HNSW index), scoped by company/workspace when set.
- Everything is gated by `RAG_ENABLED` (default false): when off, the service
  must be a no-op and must not require a live embedding model.
- Embeddings come from Ollama (`OLLAMA_BASE_URL`); never require a cloud API.
- Tests must not call a live model — stub the embedding function.

## Quality gates

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Report: the `retrieve`/`ingest` signatures, the exact model fields you need from
`core-model`, and the index type used.
