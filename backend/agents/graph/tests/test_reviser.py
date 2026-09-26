"""Unit tests for the LLM specification reviser (docs/vision-self-check.md 4.).

No network, no database: the provider is a fake and the reviser is called
directly. The reviser must be best-effort -- a failure returns ``None`` so the
CAD node retries the unchanged specification.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from agents.graph.reviser import REVISER_SYSTEM_PROMPT, make_llm_reviser
from agents.graph.tests.fakes import DEFAULT_SPEC, ENRICHED_SPEC, FakeProvider
from agents.llm import LLMError


class RecordingProvider(FakeProvider):
    """Fake provider that records the reviser prompts it received."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.systems.append(str(kwargs.get("system", "")))
        return super().structured(prompt, schema, **kwargs)


def test_reviser_returns_the_corrected_specification():
    provider = RecordingProvider(enriched=ENRICHED_SPEC)
    reviser = make_llm_reviser(provider)

    result = reviser(DEFAULT_SPEC, ["wall thickness is not printable"], 2)

    assert result == ENRICHED_SPEC
    assert provider.calls == ["ModelSpecification"]
    assert provider.systems == [REVISER_SYSTEM_PROMPT]
    # The prompt carries the current spec and the concrete errors.
    prompt = provider.prompts[0]
    assert DEFAULT_SPEC["object"] in prompt
    assert "wall thickness is not printable" in prompt
    assert "Attempt 2" in prompt


def test_reviser_returns_none_on_llm_error():
    class BrokenProvider(FakeProvider):
        def structured(self, prompt: str, schema: Any, **kwargs: Any) -> dict[str, Any]:
            raise LLMError("reviser offline")

    reviser = make_llm_reviser(BrokenProvider())

    assert reviser(DEFAULT_SPEC, ["boom"], 1) is None


def test_reviser_returns_none_on_invalid_specification():
    # A payload that does not validate against ModelSpecification must be a
    # no-op retry, never a crash.
    provider = FakeProvider(enriched={"object": ""})
    reviser = make_llm_reviser(provider)

    assert reviser(DEFAULT_SPEC, ["boom"], 1) is None


def test_reviser_records_its_exchange_for_the_trace():
    """The reviser keeps the prompt + answer so the UI can show the run."""
    provider = RecordingProvider(enriched=ENRICHED_SPEC)
    reviser = make_llm_reviser(provider)

    result = reviser(DEFAULT_SPEC, ["wall thickness is not printable"], 2)

    assert result == ENRICHED_SPEC
    exchange = reviser.last_exchange
    assert exchange["agent"] == "reviser"
    assert exchange["attempt"] == 2
    assert exchange["system"] == REVISER_SYSTEM_PROMPT
    assert "wall thickness is not printable" in exchange["prompt"]
    assert exchange["response"] == ENRICHED_SPEC


def test_reviser_clears_the_exchange_on_failure():
    """A failed call must not leave a stale exchange behind."""

    class BrokenProvider(FakeProvider):
        def structured(self, prompt: str, schema: Any, **kwargs: Any) -> dict[str, Any]:
            raise LLMError("reviser offline")

    reviser = make_llm_reviser(BrokenProvider())

    assert reviser(DEFAULT_SPEC, ["boom"], 1) is None
    assert reviser.last_exchange is None
