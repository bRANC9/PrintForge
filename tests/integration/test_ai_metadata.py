"""AI description/tag autofill guarantees (terv.md 29. fejezet).

The core promise is provenance: a field whose ``*_source == manual`` is never
overwritten unless ``force=True``, and any LLM failure is swallowed into an
``applied=False`` result instead of propagating. The LLM is always a fake here,
so no live Ollama/model is required.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from factories import ProjectFactory

from agents.llm import LLMError
from projects.models import ContentSource
from projects.services import (
    generate_project_description,
    maybe_autofill_project_metadata,
    set_project_tags,
)

pytestmark = pytest.mark.django_db


class FakeProvider:
    """Minimal ``structured``-only provider returning a canned suggestion."""

    name = "fake"

    def __init__(self, summary="", tags=None):
        self.result = {"summary": summary, "tags": list(tags or [])}
        self.calls = 0

    def structured(self, prompt, schema, **kwargs):
        self.calls += 1
        return dict(self.result)


class BoomProvider:
    """Provider that raises when called; used to assert "never called"."""

    name = "boom"

    def __init__(self, error=None):
        self.error = error or AssertionError("the LLM must not be called")
        self.calls = 0

    def structured(self, prompt, schema, **kwargs):
        self.calls += 1
        raise self.error


# ---------------------------------------------------------------------------
# No-overwrite guarantee
# ---------------------------------------------------------------------------


def test_manual_description_is_preserved_while_tags_are_filled():
    project = ProjectFactory()
    project.description = "Hand written"
    project.description_source = ContentSource.MANUAL
    project.save(update_fields=["description", "description_source"])

    result = generate_project_description(project, provider=FakeProvider("AI text", ["ai-tag"]))

    project.refresh_from_db()
    assert result["applied"] is True
    assert project.description == "Hand written"
    assert project.description_source == ContentSource.MANUAL
    assert project.tags_source == ContentSource.AI
    assert result["description"] is None
    assert list(project.tags.values_list("name", flat=True)) == ["ai-tag"]


def test_manual_tags_are_preserved_while_description_is_filled():
    project = ProjectFactory()
    project.tags_source = ContentSource.MANUAL
    project.save(update_fields=["tags_source"])
    set_project_tags(project, ["hand-picked"])

    result = generate_project_description(project, provider=FakeProvider("AI text", ["ai-tag"]))

    project.refresh_from_db()
    assert result["applied"] is True
    assert project.description == "AI text"
    assert project.description_source == ContentSource.AI
    assert project.tags_source == ContentSource.MANUAL
    assert result["tags"] == []
    assert list(project.tags.values_list("name", flat=True)) == ["hand-picked"]


def test_both_manual_skips_the_llm_entirely():
    project = ProjectFactory()
    project.description_source = ContentSource.MANUAL
    project.tags_source = ContentSource.MANUAL
    project.save(update_fields=["description_source", "tags_source"])

    provider = BoomProvider()
    result = generate_project_description(project, provider=provider)

    assert result["applied"] is False
    assert result["reason"] == "manual"
    assert provider.calls == 0


def test_force_overwrites_manual_fields():
    project = ProjectFactory()
    project.description = "Hand written"
    project.description_source = ContentSource.MANUAL
    project.tags_source = ContentSource.MANUAL
    project.save(update_fields=["description", "description_source", "tags_source"])

    result = generate_project_description(
        project, provider=FakeProvider("AI text", ["ai-tag"]), force=True
    )

    project.refresh_from_db()
    assert result["applied"] is True
    assert project.description == "AI text"
    assert project.description_source == ContentSource.AI
    assert project.tags_source == ContentSource.AI
    assert list(project.tags.values_list("name", flat=True)) == ["ai-tag"]


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_llm_error_is_swallowed():
    project = ProjectFactory()

    result = generate_project_description(project, provider=BoomProvider(LLMError("ollama down")))

    assert result == {
        "applied": False,
        "reason": "llm_error",
        "error": "ollama down",
        "description": None,
        "tags": [],
    }


def test_unexpected_provider_error_is_swallowed_with_its_type():
    project = ProjectFactory()

    result = generate_project_description(project, provider=BoomProvider(RuntimeError("boom")))

    assert result["applied"] is False
    assert result["reason"] == "error"
    assert "RuntimeError" in result["error"]
    assert "boom" in result["error"]


def test_empty_suggestion_reports_nothing_to_fill():
    project = ProjectFactory()

    result = generate_project_description(project, provider=FakeProvider("", []))

    assert result["applied"] is False
    assert result["reason"] == "nothing_to_fill"
    project.refresh_from_db()
    assert project.description == ""
    assert project.description_source == ContentSource.EMPTY


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------


def test_named_provider_string_is_resolved_through_get_provider():
    project = ProjectFactory()
    resolved = FakeProvider("AI text", ["ai-tag"])

    with patch("agents.llm.get_provider", return_value=resolved) as getter:
        generate_project_description(project, provider="ollama")

    getter.assert_called_once_with("ollama")
    assert resolved.calls == 1


def test_default_provider_is_resolved_when_none_is_given():
    project = ProjectFactory()
    resolved = FakeProvider("AI text", ["ai-tag"])

    with patch("agents.llm.get_provider", return_value=resolved) as getter:
        generate_project_description(project)

    getter.assert_called_once_with()
    assert resolved.calls == 1


# ---------------------------------------------------------------------------
# maybe_autofill wrapper
# ---------------------------------------------------------------------------


def test_maybe_autofill_skips_when_description_and_tags_exist():
    project = ProjectFactory(description="already there")
    set_project_tags(project, ["existing"])

    with patch("agents.llm.get_provider", side_effect=AssertionError("must not resolve")):
        result = maybe_autofill_project_metadata(project)

    assert result["applied"] is False
    assert result["reason"] == "already_filled"


def test_maybe_autofill_fills_an_empty_project():
    project = ProjectFactory()
    resolved = FakeProvider("AI text", ["ai-tag"])

    with patch("agents.llm.get_provider", return_value=resolved):
        result = maybe_autofill_project_metadata(project)

    project.refresh_from_db()
    assert result["applied"] is True
    assert project.description == "AI text"
    assert list(project.tags.values_list("name", flat=True)) == ["ai-tag"]


def test_maybe_autofill_never_raises_on_a_broken_provider():
    project = ProjectFactory()

    with patch("agents.llm.get_provider", side_effect=RuntimeError("no provider")):
        result = maybe_autofill_project_metadata(project)

    assert result["applied"] is False
    assert result["reason"] == "error"
