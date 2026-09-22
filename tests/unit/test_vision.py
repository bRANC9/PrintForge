"""Reference-image (vision) handling in the agent workflow (terv.md 27.).

All providers are fakes: the point is the policy in
:mod:`agents.graph.vision` and the Planner node -- query capability, never send
an image to a provider that cannot see, and never fail a run because of vision.
No live Ollama is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.graph.planner import make_planner_node
from agents.graph.vision import (
    VISION_FALLBACK_WARNING,
    VISION_UNSUPPORTED_WARNING,
    reference_images,
    reference_prompt,
    reference_prompt_for,
    structured_with_reference_image,
)
from agents.llm import LLMError, LLMProvider
from agents.spec import ModelSpecification

IMAGE = b"\x89PNG\r\n\x1a\nfake-image-bytes"


class RecordingProvider(LLMProvider):
    """Fake provider recording whether images were attached."""

    name = "recording"

    def __init__(
        self,
        *,
        vision: bool = False,
        payload: dict[str, Any] | None = None,
        reject_image: bool = False,
        fail_always: bool = False,
    ) -> None:
        self._vision = vision
        self.payload = payload if payload is not None else {"ok": True}
        self.reject_image = reject_image
        self.fail_always = fail_always
        self.image_calls: list[list[bytes]] = []
        self.text_calls = 0

    def supports_vision(self) -> bool:
        return self._vision

    def generate(self, prompt: str, *, images=None, **kwargs: Any) -> str:
        return "text"

    def structured(self, prompt, schema, *, images=None, **kwargs: Any) -> dict[str, Any]:
        if images:
            self.image_calls.append(list(images))
            if self.reject_image or self.fail_always:
                raise LLMError("image rejected")
            return dict(self.payload)
        self.text_calls += 1
        if self.fail_always:
            raise LLMError("text failed")
        return dict(self.payload)


class ExplodingVisionProvider(RecordingProvider):
    """Fails the test if ``supports_vision`` is queried at all."""

    def supports_vision(self) -> bool:
        raise AssertionError("supports_vision must not be queried without an image")


# ---------------------------------------------------------------------------
# reference_images / reference_prompt helpers
# ---------------------------------------------------------------------------


def test_reference_images_absent_is_empty():
    assert reference_images({}) == []
    assert reference_images({"reference_image": None}) == []


def test_reference_images_normalises_a_single_image_to_a_list():
    assert reference_images({"reference_image": IMAGE}) == [IMAGE]
    assert reference_images({"reference_image": bytearray(b"xy")}) == [b"xy"]


def test_reference_prompt_without_a_reference_is_unchanged():
    state = {"prompt": "make a phone holder"}

    assert reference_prompt(state) == "make a phone holder"
    assert reference_prompt(state, vision=True) == "make a phone holder"


def test_reference_prompt_appends_the_note_and_photo_hint():
    state = {
        "prompt": "make it",
        "reference_image_note": "  70 mm wide  ",
        "reference_image": IMAGE,
    }

    with_hint = reference_prompt(state, vision=True)
    without_hint = reference_prompt(state, vision=False)

    assert "Reference note: 70 mm wide" in with_hint
    assert "reference photo is attached" in with_hint.lower()
    assert "Reference note: 70 mm wide" in without_hint
    assert "reference photo is attached" not in without_hint.lower()


def test_reference_prompt_for_skips_the_capability_query_without_an_image():
    provider = ExplodingVisionProvider(vision=True)

    assert reference_prompt_for(provider, {"prompt": "just text"}) == "just text"


# ---------------------------------------------------------------------------
# structured_with_reference_image policy
# ---------------------------------------------------------------------------


def test_no_image_is_a_plain_text_call():
    provider = RecordingProvider()

    payload, used, warning = structured_with_reference_image(provider, "prompt", dict, system="sys")

    assert payload == {"ok": True}
    assert used is False
    assert warning is None
    assert provider.image_calls == []
    assert provider.text_calls == 1


def test_unsupported_provider_never_receives_the_image():
    provider = RecordingProvider(vision=False)

    payload, used, warning = structured_with_reference_image(
        provider, "prompt", dict, system="sys", images=[IMAGE]
    )

    assert payload == {"ok": True}
    assert used is False
    assert warning == VISION_UNSUPPORTED_WARNING
    assert provider.image_calls == []
    assert provider.text_calls == 1


def test_supported_provider_receives_the_image():
    provider = RecordingProvider(vision=True, payload={"seen": True})

    payload, used, warning = structured_with_reference_image(
        provider, "prompt", dict, system="sys", images=[IMAGE]
    )

    assert payload == {"seen": True}
    assert used is True
    assert warning is None
    assert provider.image_calls == [[IMAGE]]
    assert provider.text_calls == 0


def test_image_rejection_falls_back_to_text_only_with_a_warning():
    provider = RecordingProvider(vision=True, reject_image=True, payload={"fallback": True})

    payload, used, warning = structured_with_reference_image(
        provider, "prompt", dict, system="sys", images=[IMAGE]
    )

    assert payload == {"fallback": True}
    assert used is False
    assert warning == VISION_FALLBACK_WARNING
    assert provider.image_calls == [[IMAGE]]
    assert provider.text_calls == 1


def test_text_only_failure_propagates():
    provider = RecordingProvider(fail_always=True)

    with pytest.raises(LLMError):
        structured_with_reference_image(provider, "prompt", dict, system="sys")


def test_image_retry_failure_still_propagates():
    provider = RecordingProvider(vision=True, fail_always=True)

    with pytest.raises(LLMError):
        structured_with_reference_image(provider, "prompt", dict, system="sys", images=[IMAGE])


# ---------------------------------------------------------------------------
# Planner node integration
# ---------------------------------------------------------------------------


def _plan_payload() -> dict[str, Any]:
    return {"specification": ModelSpecification.example(), "needs_research": False}


def _run_planner(provider: RecordingProvider, *, image: bytes | None = IMAGE) -> dict[str, Any]:
    node = make_planner_node(provider=provider, max_attempts=3)
    return node({"prompt": "make a phone holder", "reference_image": image, "history": []})


def test_planner_marks_the_image_used_when_supported():
    provider = RecordingProvider(vision=True, payload=_plan_payload())

    update = _run_planner(provider)

    assert update["status"] == "planned"
    assert update["reference_image_used"] is True
    assert update["reference_image_warning"] is None
    assert provider.image_calls == [[IMAGE]]


def test_planner_records_a_warning_when_vision_is_unsupported():
    provider = RecordingProvider(vision=False, payload=_plan_payload())

    update = _run_planner(provider)

    assert update["status"] == "planned"
    assert update["reference_image_used"] is False
    assert update["reference_image_warning"] == VISION_UNSUPPORTED_WARNING
    assert provider.image_calls == []
    assert provider.text_calls == 1


def test_planner_falls_back_when_the_model_rejects_the_image():
    provider = RecordingProvider(vision=True, reject_image=True, payload=_plan_payload())

    update = _run_planner(provider)

    assert update["status"] == "planned"
    assert update["reference_image_used"] is False
    assert update["reference_image_warning"] == VISION_FALLBACK_WARNING
    assert provider.image_calls == [[IMAGE]]
    assert provider.text_calls == 1


def test_planner_without_an_image_is_fully_text_only():
    provider = ExplodingVisionProvider(payload=_plan_payload())

    update = _run_planner(provider, image=None)

    assert update["status"] == "planned"
    assert update["reference_image_used"] is False
    assert update["reference_image_warning"] is None
    assert provider.image_calls == []
    assert provider.text_calls == 1
