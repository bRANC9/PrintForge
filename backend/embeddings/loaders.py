"""Input loaders for ``manage.py rag_ingest``.

Pure, DB-free helpers so they can be unit-tested on sqlite (the ``embeddings``
app itself is only installed on PostgreSQL + pgvector). A loader turns a file
into a list of document dicts shaped like :func:`embeddings.services.ingest_document`:

``{title, content, source_url, source_type, company_id}``

Supported inputs:

* ``.json`` -- a single object or a list of objects with ``title``, ``content``
  and the optional ``source_url`` / ``source_type`` / ``company_id`` keys.
* ``.md`` / ``.markdown`` / ``.txt`` -- the first Markdown heading becomes the
  title (fallback: the file name without extension), the rest is the content.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: Valid ``KnowledgeDocument.source_type`` values (mirrors the model choices).
SOURCE_TYPES: tuple[str, ...] = ("standard", "datasheet", "web", "manual")

SUPPORTED_SUFFIXES: tuple[str, ...] = (".json", ".md", ".markdown", ".txt")

_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*\S)\s*$")


class LoaderError(ValueError):
    """Raised for malformed or unsupported input files."""


def normalize_document(
    raw: Any,
    *,
    default_source_type: str | None = None,
    default_company_id: str = "",
    origin: str = "<input>",
) -> dict[str, Any]:
    """Validate one raw document dict and normalise its optional fields."""
    if not isinstance(raw, dict):
        raise LoaderError(f"{origin}: expected a JSON object, got {type(raw).__name__}")

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise LoaderError(f"{origin}: 'title' must be a non-empty string")

    content = raw.get("content")
    if not isinstance(content, str):
        raise LoaderError(f"{origin}: 'content' must be a string")

    source_type = raw.get("source_type") or default_source_type or "manual"
    if source_type not in SOURCE_TYPES:
        raise LoaderError(
            f"{origin}: invalid source_type {source_type!r} "
            f"(expected one of {', '.join(SOURCE_TYPES)})"
        )

    return {
        "title": title.strip(),
        "content": content,
        "source_url": str(raw.get("source_url") or ""),
        "source_type": source_type,
        "company_id": str(raw.get("company_id") or default_company_id or ""),
    }


def parse_json_payload(
    payload: Any,
    *,
    default_source_type: str | None = None,
    default_company_id: str = "",
    origin: str = "<json>",
) -> list[dict[str, Any]]:
    """Turn a decoded JSON payload (object or list) into document dicts."""
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = [payload]
    else:
        raise LoaderError(f"{origin}: expected a JSON object or list of objects")

    return [
        normalize_document(
            item,
            default_source_type=default_source_type,
            default_company_id=default_company_id,
            origin=f"{origin}[{index}]",
        )
        for index, item in enumerate(items)
    ]


def parse_markdown(text: str, *, fallback_title: str) -> dict[str, Any]:
    """Extract the first heading as the title and the rest as the content."""
    title: str | None = None
    body: list[str] = []
    for line in (text or "").splitlines():
        if title is None:
            match = _HEADING_RE.match(line)
            if match:
                title = match.group(1).strip()
                continue
        body.append(line)
    return {"title": title or fallback_title, "content": "\n".join(body).strip()}


def load_file(
    path: str | Path,
    *,
    default_source_type: str | None = None,
    default_company_id: str = "",
) -> list[dict[str, Any]]:
    """Load one supported file into document dicts."""
    file_path = Path(path)
    if not file_path.is_file():
        raise LoaderError(f"{file_path}: no such file")

    suffix = file_path.suffix.lower()
    text = file_path.read_text(encoding="utf-8")

    if suffix == ".json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LoaderError(f"{file_path}: invalid JSON: {exc}") from exc
        return parse_json_payload(
            payload,
            default_source_type=default_source_type,
            default_company_id=default_company_id,
            origin=str(file_path),
        )

    if suffix in (".md", ".markdown", ".txt"):
        document = parse_markdown(text, fallback_title=file_path.stem)
        return [
            normalize_document(
                document,
                default_source_type=default_source_type,
                default_company_id=default_company_id,
                origin=str(file_path),
            )
        ]

    raise LoaderError(f"{file_path}: unsupported file type '{suffix}' (use .json, .md or .txt)")


def load_directory(
    path: str | Path,
    *,
    default_source_type: str | None = None,
    default_company_id: str = "",
) -> list[dict[str, Any]]:
    """Recursively load every supported file under *path* (sorted, deterministic)."""
    root = Path(path)
    if not root.is_dir():
        raise LoaderError(f"{root}: no such directory")

    files = sorted(
        (
            file
            for file in root.rglob("*")
            if file.is_file() and file.suffix.lower() in SUPPORTED_SUFFIXES
        ),
        key=str,
    )
    documents: list[dict[str, Any]] = []
    for file_path in files:
        documents.extend(
            load_file(
                file_path,
                default_source_type=default_source_type,
                default_company_id=default_company_id,
            )
        )
    return documents
