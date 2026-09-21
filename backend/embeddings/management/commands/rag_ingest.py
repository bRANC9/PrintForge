"""``manage.py rag_ingest`` -- populate the pgvector RAG knowledge base.

Without an ingest path the Research agent would have nothing to retrieve, so
this command is the operational entry point for the embedding infrastructure
(terv.md 5., 7. fejezet).

Examples::

    manage.py rag_ingest --seed
    manage.py rag_ingest --file docs/screws.json --source-type datasheet
    manage.py rag_ingest --dir knowledge/ --company-id acme
    manage.py rag_ingest --seed --dry-run
    manage.py rag_ingest --dir knowledge/ --clear

The command is a safe no-op with a clear message when the runtime
``rag_enabled`` setting is false (see ``configuration.services``) or the default
database is not PostgreSQL (the ``embeddings`` app is not installed on sqlite).
``--dry-run`` parses and reports without touching the embedding model or the
database.

Input formats (see :mod:`embeddings.loaders`):

* JSON: one object or a list of ``{title, content, source_url?, source_type?,
  company_id?}``.
* Markdown / text: the first ``#`` heading is the title (fallback: file name),
  the rest is the content.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from embeddings.loaders import (
    SOURCE_TYPES,
    LoaderError,
    load_directory,
    load_file,
)
from embeddings.seed import seed_documents
from embeddings.services import (
    RagError,
    chunk_text,
    ingest_document,
    postgres_available,
    rag_enabled,
)

__all__ = ["Command"]


class Command(BaseCommand):
    help = "Ingest knowledge documents into the pgvector RAG knowledge base."

    def add_arguments(self, parser) -> None:
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument(
            "--file",
            dest="file",
            metavar="PATH",
            help="Ingest a single .json, .md or .txt file.",
        )
        source.add_argument(
            "--dir",
            dest="directory",
            metavar="PATH",
            help="Recursively ingest every supported file under PATH.",
        )
        source.add_argument(
            "--seed",
            action="store_true",
            help="Ingest the built-in, sourced reference dataset.",
        )
        parser.add_argument(
            "--source-type",
            dest="source_type",
            choices=SOURCE_TYPES,
            default=None,
            help="Force the document source type (default: per document, else 'manual').",
        )
        parser.add_argument(
            "--company-id",
            dest="company_id",
            default="",
            help="Company/workspace scope stored on the documents.",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete existing documents before ingesting.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report only; never call the embedding model or write.",
        )

    # -- entry point --------------------------------------------------------

    def handle(self, *args: Any, **options: Any) -> None:
        if not rag_enabled():
            self.stdout.write(
                self.style.WARNING(
                    "RAG is disabled (rag_enabled=false); nothing to ingest. "
                    "Enable it in the runtime settings."
                )
            )
            return
        if not postgres_available():
            self.stdout.write(
                self.style.WARNING(
                    "The RAG store requires PostgreSQL + pgvector, but the default "
                    "database is not PostgreSQL; nothing to ingest."
                )
            )
            return

        try:
            documents = self._collect(options)
        except LoaderError as exc:
            raise CommandError(str(exc)) from exc

        if not documents:
            self.stdout.write(self.style.WARNING("No documents found; nothing to ingest."))
            return

        chunk_counts = [len(chunk_text(document["content"])) for document in documents]
        total_chunks = sum(chunk_counts)
        company_id = str(options.get("company_id") or "").strip()
        dry_run = bool(options.get("dry_run"))

        if options.get("clear"):
            if dry_run:
                self.stdout.write(
                    f"Dry run: would delete {self._existing_count(company_id)} existing document(s)."
                )
            else:
                self.stdout.write(f"Deleted {self._clear(company_id)} existing document(s).")

        if dry_run:
            for document, chunk_count in zip(documents, chunk_counts, strict=True):
                self.stdout.write(
                    f"  - [{document['source_type']}] {document['title']} ({chunk_count} chunk(s))"
                )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dry run: would ingest {len(documents)} document(s), {total_chunks} chunk(s)."
                )
            )
            return

        try:
            ingested = self._ingest(documents)
        except RagError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(f"Ingested {ingested} document(s), {total_chunks} chunk(s).")
        )

    # -- helpers ------------------------------------------------------------

    def _collect(self, options: dict[str, Any]) -> list[dict[str, Any]]:
        source_type = options.get("source_type")
        company_id = str(options.get("company_id") or "")

        if options.get("seed"):
            documents = seed_documents()
        elif options.get("file"):
            documents = load_file(
                Path(options["file"]),
                default_source_type=source_type,
                default_company_id=company_id,
            )
        else:
            documents = load_directory(
                Path(options["directory"]),
                default_source_type=source_type,
                default_company_id=company_id,
            )

        # Explicit CLI values win over the per-document metadata.
        if source_type:
            for document in documents:
                document["source_type"] = source_type
        if company_id:
            for document in documents:
                document["company_id"] = company_id
        return documents

    def _existing_count(self, company_id: str = "") -> int:
        from embeddings.models import KnowledgeDocument

        queryset = KnowledgeDocument.objects.all()
        if company_id:
            queryset = queryset.filter(company_id=company_id)
        return queryset.count()

    def _clear(self, company_id: str = "") -> int:
        from embeddings.models import KnowledgeDocument

        queryset = KnowledgeDocument.objects.all()
        if company_id:
            queryset = queryset.filter(company_id=company_id)
        count = queryset.count()
        queryset.delete()
        return count

    def _ingest(self, documents: list[dict[str, Any]]) -> int:
        for document in documents:
            ingest_document(**document)
        return len(documents)
