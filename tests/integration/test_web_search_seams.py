"""Web search wired into the Research agent and persisted (terv.md 7. fejezet).

``backend/agents/tests/test_search.py`` pins ``WebSearchBackend`` in isolation and
``test_research_web.py`` pins the node with an injected ``search_fn``. This module
covers the seam in between: the **real** backend (fake HTTP only) is handed to
the real task, the web hits are tagged ``kind="web"`` next to RAG, and a disabled
backend contributes nothing while still leaving the run healthy.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from factories import ProjectFactory

from agents.graph import WorkflowDeps
from agents.graph.tests.fakes import DEFAULT_SPEC, ENRICHED_SPEC, FakeCADBackend, FakeProvider
from agents.models import AgentRun, AgentRunStatus
from agents.search import WebSearchBackend
from agents.tasks import run_agent_workflow
from files.services import LocalStorage

pytestmark = pytest.mark.django_db

RAG_DOC = {
    "chunk_id": 7,
    "document_id": 3,
    "title": "Samsung Galaxy S24 dimensions",
    "source_url": "https://example.test/s24",
    "content": "70.6 mm x 147.0 mm x 7.6 mm",
    "score": 0.91,
}
SEARXNG_PAYLOAD = {
    "results": [
        {
            "title": "GSMArena S24",
            "url": "https://web.test/gsmarena",
            "content": "147.0 x 70.6 x 7.6 mm",
        }
    ]
}


class _FakeHttp:
    """Records requested URLs and returns canned bodies (or raises)."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def __call__(self, url: str, *, timeout: float = 0.0) -> bytes:
        self.urls.append(url)
        value = self.responses.get(url)
        if value is None:
            for prefix, candidate in self.responses.items():
                if prefix in url:
                    value = candidate
                    break
        if value is None:
            raise AssertionError(f"unexpected URL {url}")
        if isinstance(value, Exception):
            raise value
        return value.encode("utf-8") if isinstance(value, str) else value


def _patch_settings(monkeypatch, *, backend: str, base_url: str) -> None:
    """Resolve the search settings from a plain dict instead of the DB/env."""
    monkeypatch.setattr(
        "agents.search._get_setting",
        lambda name, default="": {"search_backend": backend, "searxng_base_url": base_url}.get(
            name, default
        ),
    )


def _deps(provider: FakeProvider, *, search_fn) -> WorkflowDeps:
    return WorkflowDeps(
        provider=provider,
        cad_backend=FakeCADBackend(),
        retrieve_fn=lambda *args, **kwargs: [RAG_DOC],
        search_fn=search_fn,
        preview_renderer=lambda _stl: b"",
        max_attempts=3,
    )


def _install(monkeypatch, tmp_path, deps: WorkflowDeps) -> None:
    monkeypatch.setattr("agents.tasks.build_dependencies", lambda: deps)
    monkeypatch.setattr("agents.tasks.get_storage", lambda: LocalStorage(root=tmp_path))


def _research_provider() -> FakeProvider:
    return FakeProvider(
        plan={
            "specification": DEFAULT_SPEC,
            "needs_research": True,
            "research_query": "Galaxy S24 dimensions",
        },
        enriched=ENRICHED_SPEC,
    )


def test_web_results_reach_the_task_and_are_tagged_web(monkeypatch, tmp_path):
    search_url = (
        "http://searxng:8080/search?q=Galaxy+S24+dimensions&format=json&safesearch=1&language=all"
    )
    http = _FakeHttp({search_url: json.dumps(SEARXNG_PAYLOAD)})
    _patch_settings(monkeypatch, backend="searxng", base_url="http://searxng:8080")
    backend = WebSearchBackend(base_url="http://searxng:8080", http_get=http, fetch_results=0)

    project = ProjectFactory()
    provider = _research_provider()
    _install(monkeypatch, tmp_path, _deps(provider, search_fn=backend.search))

    run_id = run_agent_workflow(project.pk, "phone holder", project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert run.state_json["research_web_used"] is True
    kinds = [source["kind"] for source in run.state_json["research_sources"]]
    assert kinds == ["rag", "web"]
    web_source = run.state_json["research_sources"][1]
    assert web_source["source_url"] == "https://web.test/gsmarena"
    assert web_source["title"] == "GSMArena S24"
    # The web context reached the enrichment prompt (the provider saw it).
    assert "147.0 x 70.6 x 7.6 mm" in provider.last_prompt
    assert http.urls == [search_url]


def test_disabled_backend_contributes_nothing_and_keeps_the_run_healthy(monkeypatch, tmp_path):
    http = _FakeHttp({})  # must never be called
    _patch_settings(monkeypatch, backend="", base_url="http://searxng:8080")
    backend = WebSearchBackend(base_url="http://searxng:8080", http_get=http, fetch_results=0)

    assert backend.enabled is False
    assert backend.search("anything") == []
    assert http.urls == []

    project = ProjectFactory()
    provider = _research_provider()
    _install(monkeypatch, tmp_path, _deps(provider, search_fn=backend.search))

    run_id = run_agent_workflow(project.pk, "phone holder", project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert run.state_json["research_web_used"] is False
    assert [source["kind"] for source in run.state_json["research_sources"]] == ["rag"]


def test_transport_failure_is_best_effort_for_the_task(monkeypatch, tmp_path):
    http = _FakeHttp({"http://searxng:8080/search": OSError("connection refused")})
    _patch_settings(monkeypatch, backend="searxng", base_url="http://searxng:8080")
    backend = WebSearchBackend(base_url="http://searxng:8080", http_get=http, fetch_results=0)

    project = ProjectFactory()
    provider = _research_provider()
    _install(monkeypatch, tmp_path, _deps(provider, search_fn=backend.search))

    run_id = run_agent_workflow(project.pk, "phone holder", project.created_by_id)

    run = AgentRun.objects.get(pk=run_id)
    assert run.status == AgentRunStatus.DONE
    assert run.state_json["research_web_used"] is False
    assert [source["kind"] for source in run.state_json["research_sources"]] == ["rag"]
