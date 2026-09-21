"""Research agent (terv.md 6., 7. fejezet).

The Research node runs **only** when the Planner set ``needs_research``. It
queries the RAG knowledge base through the injected ``retrieve()`` callable
(``embeddings.services.retrieve`` / the ``agents.rag`` facade), keeps the
retrieved *sources* for provenance, and asks the LLM to enrich the
specification with the retrieved real-world data.

The retrieval itself is best-effort: when RAG is disabled (``RAG_ENABLED=false``
returns ``[]``), unreachable, or empty, the Planner's specification is kept
unchanged and the workflow continues. The Research agent never writes OpenSCAD
or STL data either -- it only returns structured data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from agents.llm import LLMError, LLMProvider
from agents.spec import ModelSpecification

from .state import WorkflowState, append_history

__all__ = ["RESEARCH_SYSTEM_PROMPT", "make_research_node"]

logger = logging.getLogger(__name__)

RESEARCH_SYSTEM_PROMPT = (
    "You are the Research agent of an OpenSCAD 3D-printing workflow. "
    "You are given the user's request, the Planner's structured specification "
    "and retrieved reference context. Use ONLY the reference context to fill in "
    "real-world product dimensions and standard part data (e.g. ISO screw "
    "sizes). Return a single JSON ModelSpecification that matches the schema; "
    "keep every value printable. Never emit OpenSCAD code, G-code or STL data."
)


def _source_of(document: dict[str, Any]) -> dict[str, Any]:
    """Reduce a retrieval hit to the provenance fields the workflow keeps."""
    return {
        "title": document.get("title") or "",
        "source_url": document.get("source_url") or "",
        "chunk_id": document.get("chunk_id"),
        "document_id": document.get("document_id"),
        "score": document.get("score"),
    }


def _format_context(documents: list[dict[str, Any]]) -> str:
    """Render retrieved chunks as numbered reference blocks for the LLM."""
    blocks: list[str] = []
    for index, document in enumerate(documents, start=1):
        title = document.get("title") or f"document {document.get('document_id', index)}"
        url = document.get("source_url") or ""
        content = str(document.get("content") or "").strip()
        header = f"[{index}] {title}" + (f" ({url})" if url else "")
        blocks.append(f"{header}\n{content}")
    return "\n\n".join(blocks)


def _research_prompt(user_prompt: str, specification: dict[str, Any], context: str) -> str:
    return (
        f"User request:\n{user_prompt}\n\n"
        f"Planner specification (JSON):\n{ModelSpecification.model_validate(specification).model_dump_json()}\n\n"
        f"Retrieved reference context:\n{context}\n\n"
        "Return the specification updated with any real dimensions/standard data "
        "found above. Keep the Planner values when the context does not help."
    )


def make_research_node(
    *,
    provider: LLMProvider,
    retrieve_fn: Callable[..., list[dict[str, Any]]],
    limit: int = 5,
) -> Callable[[WorkflowState], dict[str, Any]]:
    """Build the ``research`` graph node.

    Args:
        provider: LLM used to enrich the specification from the retrieved text.
        retrieve_fn: RAG entry point with the ``retrieve(query, limit, company_id)``
            signature. Injected so tests never touch pgvector or Ollama.
        limit: Maximum number of chunks to retrieve.
    """

    def research_node(state: WorkflowState) -> dict[str, Any]:
        query = (state.get("research_query") or state.get("prompt") or "").strip()
        documents: list[dict[str, Any]] = []

        if query:
            try:
                documents = [
                    document
                    for document in retrieve_fn(
                        query,
                        limit=limit,
                        company_id=state.get("company_id"),
                    )
                    if isinstance(document, dict)
                ]
            except Exception as exc:  # noqa: BLE001 - RAG is best-effort
                logger.warning("Research retrieval failed: %s", exc)
                documents = []

        sources = [_source_of(document) for document in documents]
        specification = dict(state.get("specification") or {})
        used = False

        if documents:
            try:
                enriched = provider.structured(
                    _research_prompt(
                        state.get("prompt", ""), specification, _format_context(documents)
                    ),
                    ModelSpecification,
                    system=RESEARCH_SYSTEM_PROMPT,
                )
                specification = ModelSpecification.model_validate(enriched).model_dump()
                used = True
            except (LLMError, ValidationError) as exc:
                # Enrichment is optional: fall back to the Planner spec.
                logger.warning("Research enrichment failed, keeping Planner spec: %s", exc)

        entry = f"research: {len(sources)} source(s), enriched={used}"
        return {
            "specification": specification,
            "research_sources": sources,
            "research_used": used,
            "status": "researched",
            "history": append_history(state, entry),
        }

    return research_node
