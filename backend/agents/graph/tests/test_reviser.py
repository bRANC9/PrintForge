"""Unit tests for the LLM specification reviser (docs/vision-self-check.md 4.).

No network, no database: the provider is a fake and the reviser is called
directly. The reviser must be best-effort -- a failure returns ``None`` so the
CAD node retries the unchanged specification.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from agents.graph.reviser import (
    REVISER_SYSTEM_PROMPT,
    make_llm_reviser,
    revision_regressions,
)
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


# ---------------------------------------------------------------------------
# Regression guard: a revision may not shrink the part to satisfy a bound
# ---------------------------------------------------------------------------


def _spec(width, height, thickness, primitives=None):
    return {
        "object": "cookie_cutter",
        "dimensions": {"width": width, "height": height, "thickness": thickness},
        "angle": 0.0,
        "wall_thickness": 2.0,
        "mounting": {"type": "none", "count": 0},
        "material": "PLA",
        "primitives": primitives if primitives is not None else [dict(_BOX)],
    }


_BOX = {"type": "box", "role": "add", "width": 150.0, "depth": 30.0, "height": 5.0}
_EXTRUDE = {
    "type": "extrude",
    "role": "add",
    "height": 30.0,
    "wall_thickness": 2.0,
    "profile": [{"x": 0.0, "y": 0.0}, {"x": 50.0, "y": 0.0}, {"x": 25.0, "y": 30.0}],
}


def test_shrinking_the_part_to_satisfy_a_bound_is_a_regression():
    """The measured case: 150x30x5 mm became 10x5x0.5 mm to pass validation."""
    previous = _spec(150.0, 30.0, 5.0)
    revised = _spec(10.0, 5.0, 0.5)
    errors = ["Field 'dimensions.height' must be >= 5.0 (got 2.0)"]

    reasons = revision_regressions(previous, revised, errors)

    assert any("dimensions.width shrank" in reason for reason in reasons)
    assert any("dimensions.thickness shrank" in reason for reason in reasons)


def test_a_complaint_about_the_size_allows_that_one_growth():
    """Raising the offending field (and only that) is the legitimate fix."""
    previous = _spec(150.0, 30.0, 2.0)
    revised = _spec(150.0, 30.0, 5.0)
    errors = ["Field 'dimensions.thickness' must be >= 5.0 (got 2.0)"]

    assert revision_regressions(previous, revised, errors) == []


def test_a_smaller_part_the_user_asked_for_is_accepted() -> None:
    """When the complaint is about the size, shrinking is legitimate."""
    previous = _spec(150.0, 30.0, 5.0)
    revised = _spec(80.0, 30.0, 5.0)

    assert revision_regressions(previous, revised, ["dimensions must fit the bed"]) == []


def test_dropping_every_primitive_or_the_outline_is_a_regression() -> None:
    previous = _spec(150.0, 30.0, 5.0, primitives=[dict(_EXTRUDE)])
    emptied = _spec(150.0, 30.0, 5.0, primitives=[])

    assert any(
        "every primitive" in reason for reason in revision_regressions(previous, emptied, [])
    )

    downgraded = _spec(150.0, 30.0, 5.0, primitives=[dict(_BOX)])
    assert any("extrude" in reason for reason in revision_regressions(previous, downgraded, []))


def test_reviser_refuses_a_regressing_revision() -> None:
    """The reviser returns ``None`` so the unchanged spec is retried."""
    provider = FakeProvider(enriched=_spec(10.0, 5.0, 0.5))
    reviser = make_llm_reviser(provider)
    previous = _spec(150.0, 30.0, 5.0)
    errors = ["Field 'dimensions.height' must be >= 5.0 (got 2.0)"]

    assert reviser(previous, errors, 2) is None
    assert reviser.last_exchange is not None
    assert reviser.last_exchange["rejected"]


def test_a_healthy_revision_is_still_applied() -> None:
    provider = FakeProvider(enriched=_spec(150.0, 30.0, 5.0, primitives=[dict(_EXTRUDE)]))
    reviser = make_llm_reviser(provider)
    previous = _spec(150.0, 30.0, 2.0, primitives=[dict(_BOX)])

    corrected = reviser(previous, ["dimensions.thickness must be >= 5.0"], 2)

    assert corrected is not None
    assert "rejected" not in (reviser.last_exchange or {})
