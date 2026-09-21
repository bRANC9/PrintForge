"""Tests for ``manage.py rag_ingest`` and its input loaders.

The loaders are pure and run on sqlite. The command's guard / dry-run paths are
exercised by invoking the ``Command`` class directly (the ``embeddings`` app is
not installed on sqlite, so Django would not discover the command there). The
full ingest round-trip is PostgreSQL-only and skipped on sqlite.
"""

from __future__ import annotations

import io
import json

import pytest
from django.conf import settings
from django.core.management import call_command

from embeddings import loaders, services
from embeddings.loaders import LoaderError, load_directory, load_file, parse_markdown
from embeddings.management.commands import rag_ingest
from embeddings.seed import seed_documents


def _options(**overrides):
    options = {
        "file": None,
        "directory": None,
        "seed": False,
        "source_type": None,
        "company_id": "",
        "clear": False,
        "dry_run": False,
    }
    options.update(overrides)
    return options


def _run(options) -> str:
    buffer = io.StringIO()
    command = rag_ingest.Command(stdout=buffer, no_color=True)
    command.handle(**options)
    return buffer.getvalue()


def _fail_ingest(**_kwargs):
    pytest.fail("the embedding model must not be called on this path")


# ---------------------------------------------------------------------------
# Feature-flag / availability guards
# ---------------------------------------------------------------------------


def test_noop_when_rag_disabled(monkeypatch):
    monkeypatch.setattr(rag_ingest, "rag_enabled", lambda: False)
    monkeypatch.setattr(rag_ingest, "ingest_document", _fail_ingest)
    output = _run(_options(seed=True))
    assert "RAG is disabled" in output


def test_noop_when_not_postgres(monkeypatch):
    monkeypatch.setattr(rag_ingest, "rag_enabled", lambda: True)
    monkeypatch.setattr(rag_ingest, "postgres_available", lambda: False)
    monkeypatch.setattr(rag_ingest, "ingest_document", _fail_ingest)
    output = _run(_options(seed=True))
    assert "requires PostgreSQL" in output


# ---------------------------------------------------------------------------
# Dry run (no model, no database)
# ---------------------------------------------------------------------------


def test_dry_run_reports_without_ingesting(monkeypatch, tmp_path):
    monkeypatch.setattr(rag_ingest, "rag_enabled", lambda: True)
    monkeypatch.setattr(rag_ingest, "postgres_available", lambda: True)
    monkeypatch.setattr(rag_ingest, "ingest_document", _fail_ingest)

    path = tmp_path / "guide.md"
    path.write_text("# M5 screw guide\n\nUse ISO 4762 M5 screws.\n", encoding="utf-8")

    output = _run(_options(file=str(path), dry_run=True))

    assert "M5 screw guide" in output
    assert "would ingest 1 document(s)" in output
    assert "1 chunk(s)" in output


def test_dry_run_seed_reports_all_documents(monkeypatch):
    monkeypatch.setattr(rag_ingest, "rag_enabled", lambda: True)
    monkeypatch.setattr(rag_ingest, "postgres_available", lambda: True)
    monkeypatch.setattr(rag_ingest, "ingest_document", _fail_ingest)

    output = _run(_options(seed=True, dry_run=True))

    assert f"would ingest {len(seed_documents())} document(s)" in output


def test_clear_dry_run_reports_count_without_deleting(monkeypatch):
    monkeypatch.setattr(rag_ingest, "rag_enabled", lambda: True)
    monkeypatch.setattr(rag_ingest, "postgres_available", lambda: True)
    monkeypatch.setattr(rag_ingest.Command, "_existing_count", lambda self, company_id="": 3)

    def _fail_clear(self, company_id=""):
        pytest.fail("--clear must not delete during a dry run")

    monkeypatch.setattr(rag_ingest.Command, "_clear", _fail_clear)

    output = _run(_options(seed=True, clear=True, dry_run=True))
    assert "would delete 3 existing document(s)" in output


def test_cli_source_type_overrides_seed_metadata(monkeypatch):
    monkeypatch.setattr(rag_ingest, "rag_enabled", lambda: True)
    monkeypatch.setattr(rag_ingest, "postgres_available", lambda: True)

    captured: list[dict] = []

    def _capture(**document):
        captured.append(document)

    monkeypatch.setattr(rag_ingest, "ingest_document", _capture)
    _run(_options(seed=True, source_type="web", company_id="acme"))

    assert captured
    assert {document["source_type"] for document in captured} == {"web"}
    assert {document["company_id"] for document in captured} == {"acme"}


def test_command_declares_the_documented_options():
    parser = rag_ingest.Command().create_parser("manage.py", "rag_ingest")
    destinations = {action.dest for action in parser._actions}
    assert {
        "file",
        "directory",
        "seed",
        "source_type",
        "company_id",
        "clear",
        "dry_run",
    } <= destinations


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def test_load_json_list_applies_metadata(tmp_path):
    path = tmp_path / "docs.json"
    path.write_text(
        json.dumps(
            [
                {"title": "A", "content": "aaa"},
                {
                    "title": "B",
                    "content": "bbb",
                    "source_type": "web",
                    "source_url": "https://example.test/b",
                    "company_id": "acme",
                },
            ]
        ),
        encoding="utf-8",
    )

    documents = load_file(path)

    assert [document["title"] for document in documents] == ["A", "B"]
    assert documents[0]["source_type"] == "manual"
    assert documents[1]["source_type"] == "web"
    assert documents[1]["company_id"] == "acme"


def test_load_json_single_object(tmp_path):
    path = tmp_path / "one.json"
    path.write_text(json.dumps({"title": "Only", "content": "x"}), encoding="utf-8")
    assert load_file(path)[0]["title"] == "Only"


def test_load_markdown_uses_first_heading(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("intro\n\n# Title here\n\nbody text\n", encoding="utf-8")

    document = load_file(path)[0]

    assert document["title"] == "Title here"
    assert "intro" in document["content"]
    assert "body text" in document["content"]
    assert "# Title here" not in document["content"]


def test_load_text_falls_back_to_filename(tmp_path):
    path = tmp_path / "no-heading.txt"
    path.write_text("just some text", encoding="utf-8")

    document = load_file(path)[0]

    assert document["title"] == "no-heading"
    assert document["content"] == "just some text"


def test_load_directory_is_recursive(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "sub" / "a.md").write_text("# A\n\na", encoding="utf-8")

    documents = load_directory(tmp_path)

    assert [document["title"] for document in documents] == ["b", "A"]


def test_unsupported_extension_raises(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("a,b", encoding="utf-8")
    with pytest.raises(LoaderError):
        load_file(path)


def test_invalid_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LoaderError):
        load_file(path)


def test_missing_title_raises():
    with pytest.raises(LoaderError):
        loaders.normalize_document({"content": "x"})


def test_missing_content_raises():
    with pytest.raises(LoaderError):
        loaders.normalize_document({"title": "t"})


def test_invalid_source_type_raises():
    with pytest.raises(LoaderError):
        loaders.normalize_document({"title": "t", "content": "c", "source_type": "bogus"})


def test_parse_markdown_without_heading_uses_fallback():
    assert parse_markdown("body", fallback_title="fb") == {"title": "fb", "content": "body"}


# ---------------------------------------------------------------------------
# Full round-trip (PostgreSQL + pgvector only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not settings.DB_IS_POSTGRES,
    reason="embeddings needs PostgreSQL + pgvector (skipped on sqlite)",
)
@pytest.mark.django_db
def test_seed_ingest_round_trip_via_call_command(monkeypatch, settings):
    from embeddings.models import EmbeddingChunk, KnowledgeDocument

    runtime = {"rag_enabled": True, "embedding_model": "bge-m3"}
    monkeypatch.setattr(services, "get_setting", lambda name: runtime.get(name))
    dimension = settings.EMBEDDING_DIM
    monkeypatch.setattr(
        services, "embed_text", lambda text: [1.0 + (i % 3) for i in range(dimension)]
    )

    buffer = io.StringIO()
    call_command("rag_ingest", seed=True, stdout=buffer, no_color=True)

    assert KnowledgeDocument.objects.count() == len(seed_documents())
    assert EmbeddingChunk.objects.count() >= len(seed_documents())
    assert "Ingested" in buffer.getvalue()
