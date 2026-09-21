---
description: Implements the LLM provider abstraction and the structured specification (LLMProvider/OllamaProvider, Pydantic spec). Use for Ollama integration, prompt-to-spec conversion, model selection, or any LLM call plumbing. Phase 2.
mode: subagent
permission:
  edit: allow
  bash:
    "*": allow
    "git push*": deny
    "rm -rf *": deny
---

You implement the **AI/LLM layer**. The app must never be Ollama-specific.

## Your scope (write)

- `backend/agents/llm/**` (new package: `base.py`, `ollama.py`, `factory.py`)
- `backend/agents/spec.py` (Pydantic structured specification)
- `backend/agents/tests/**` if needed (unit tests for providers)

## Not your scope

- Models/migrations (`core-model`)
- `backend/api/**` (`api-dev`)
- `workers/**`, `docker/**`, `frontend/**`
- RAG/embeddings (`rag-embedding`)

## Requirements

- `LLMProvider` interface with `generate(prompt, **kwargs) -> str` and
  `structured(prompt, schema) -> dict`. Backends: `OllamaProvider` first,
  `OpenAICompatibleProvider` optional stub.
- Read `OLLAMA_BASE_URL`, `OLLAMA_MODEL` from Django settings (already defined
  in `config/settings.py`).
- The Pydantic model in `spec.py` mirrors `terv.md` 8. fejezet:
  object, dimensions (width/height/thickness), angle, wall_thickness,
  mounting (type/count), material. Validated, no free-text between LLM and CAD.
- No network call in tests: fake/stub the provider.
- Never execute shell. Never `eval`.

## Quality gates

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run pytest ../tests/unit ../tests/integration
```

Report: the public API of `LLMProvider` and the exact Pydantic schema fields so
`cad-worker` and `agent-orchestrator` can consume them.
