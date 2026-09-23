"""Research agent + web search wiring (terv.md 7. fejezet).

No network: both the RAG ``retrieve`` callable and the web ``search_fn`` are
injected fakes. The tests pin that a web hit augments the context, that its
provenance is marked ``kind="web"`` next to the RAG hits, and that every search
failure stays best-effort.
"""

from __future__ import annotations

from typing import Any

from agents.graph import WorkflowDeps, run_workflow
from agents.graph.tests.fakes import DEFAULT_SPEC, ENRICHED_SPEC, FakeCADBackend, FakeProvider

RAG_DOC = {
    "chunk_id": 7,
    "document_id": 3,
    "title": "Samsung Galaxy S24 dimensions",
    "source_url": "https://example.test/s24",
    "content": "70.6 mm x 147.0 mm x 7.6 mm",
    "score": 0.91,
}
WEB_RESULT = {
    "title": "GSMArena S24",
    "url": "https://web.test/gsmarena",
    "snippet": "147.0 x 70.6 x 7.6 mm",
    "content": "Full extracted specification text.",
}


def _provider() -> FakeProvider:
    return FakeProvider(
        plan={
            "specification": DEFAULT_SPEC,
            "needs_research": True,
            "research_query": "Galaxy S24 dimensions",
        },
        enriched=ENRICHED_SPEC,
    )


def _deps(
    provider: FakeProvider,
    cad: FakeCADBackend,
    *,
    retrieve_fn: Any,
    search_fn: Any = None,
    web_limit: int = 5,
) -> WorkflowDeps:
    return WorkflowDeps(
        provider=provider,
        cad_backend=cad,
        retrieve_fn=retrieve_fn,
        search_fn=search_fn,
        web_limit=web_limit,
        max_attempts=3,
    )


def test_web_results_augment_the_context_and_are_tagged():
    rag_calls: list[Any] = []
    web_calls: list[tuple[str, int]] = []

    def retrieve(query: str, limit: int = 8, company_id: str | None = None) -> list[dict]:
        rag_calls.append((query, limit, company_id))
        return [RAG_DOC]

    def search(query: str, *, limit: int = 5) -> list[dict]:
        web_calls.append((query, limit))
        return [WEB_RESULT]

    provider = _provider()
    cad = FakeCADBackend()

    state = run_workflow(
        "phone holder",
        deps=_deps(provider, cad, retrieve_fn=retrieve, search_fn=search, web_limit=3),
    )

    assert state["status"] == "done"
    assert web_calls == [("Galaxy S24 dimensions", 3)]
    assert state["research_web_used"] is True
    assert state["research_used"] is True

    kinds = [source["kind"] for source in state["research_sources"]]
    assert kinds == ["rag", "web"]
    web_source = state["research_sources"][1]
    assert web_source["source_url"] == "https://web.test/gsmarena"
    assert web_source["title"] == "GSMArena S24"
    assert web_source["snippet"] == "147.0 x 70.6 x 7.6 mm"

    # Both contexts reached the enrichment prompt.
    prompt = provider.last_prompt
    assert "70.6 mm x 147.0 mm x 7.6 mm" in prompt
    assert "Full extracted specification text." in prompt


def test_search_failure_is_best_effort_and_rag_still_enriches():
    def retrieve(*args: Any, **kwargs: Any) -> list[dict]:
        return [RAG_DOC]

    def search(*args: Any, **kwargs: Any) -> list[dict]:
        raise RuntimeError("searxng down")

    provider = _provider()
    cad = FakeCADBackend()

    state = run_workflow(
        "x",
        deps=_deps(provider, cad, retrieve_fn=retrieve, search_fn=search),
    )

    assert state["status"] == "done"
    assert state["research_used"] is True
    assert state["research_web_used"] is False
    assert [source["kind"] for source in state["research_sources"]] == ["rag"]


def test_web_only_results_still_enrich_the_specification():
    def retrieve(*args: Any, **kwargs: Any) -> list[dict]:
        return []  # RAG disabled/empty

    def search(*args: Any, **kwargs: Any) -> list[dict]:
        return [WEB_RESULT]

    provider = _provider()
    cad = FakeCADBackend()

    state = run_workflow(
        "x",
        deps=_deps(provider, cad, retrieve_fn=retrieve, search_fn=search),
    )

    assert state["status"] == "done"
    assert state["research_used"] is True
    assert state["research_web_used"] is True
    assert state["specification"]["angle"] == 20.0  # the enrichment was applied
    assert provider.calls == ["PlannerPlan", "ModelSpecification"]


def test_no_search_fn_keeps_the_rag_only_behaviour():
    def retrieve(*args: Any, **kwargs: Any) -> list[dict]:
        return [RAG_DOC]

    provider = _provider()
    cad = FakeCADBackend()

    state = run_workflow("x", deps=_deps(provider, cad, retrieve_fn=retrieve))

    assert state["research_web_used"] is False
    assert [source["kind"] for source in state["research_sources"]] == ["rag"]
