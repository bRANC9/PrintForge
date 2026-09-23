"""Deterministic fakes for the agent workflow tests.

No network, no database, no OpenSCAD: the tests inject these through
:class:`~agents.graph.WorkflowDeps` and verify the graph's own logic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from agents.llm import LLMProvider
from agents.spec import ModelSpecification
from designs.cad.base import CADBackend, GeneratedModel, SpecificationError

__all__ = [
    "DEFAULT_SPEC",
    "ENRICHED_SPEC",
    "FakeCADBackend",
    "FakeProvider",
    "FakeVisionProvider",
    "SpecFixingCADBackend",
]

DEFAULT_SPEC: dict[str, Any] = ModelSpecification.example()

_ENRICHED = ModelSpecification.example()
_ENRICHED["angle"] = 20.0
_ENRICHED["mounting"] = {"type": "M5", "count": 4}
ENRICHED_SPEC: dict[str, Any] = _ENRICHED


class FakeProvider(LLMProvider):
    """In-memory provider: returns canned structured payloads per schema name."""

    name = "fake"

    def __init__(
        self,
        *,
        plan: dict[str, Any] | None = None,
        enriched: dict[str, Any] | None = None,
        planner_error: Exception | None = None,
    ) -> None:
        self.plan = plan or {
            "specification": DEFAULT_SPEC,
            "needs_research": False,
            "research_query": None,
        }
        self.enriched = enriched
        self.planner_error = planner_error
        self.calls: list[str] = []
        #: System prompt of the most recent ``structured`` call, so tests can
        #: assert what was actually injected (e.g. the skills block).
        self.last_system: str = ""
        #: User prompt of the most recent ``structured`` call.
        self.last_prompt: str = ""

    def generate(self, prompt: str, **kwargs: Any) -> str:
        return "fake generation (never used by the workflow)"

    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        name = getattr(schema, "__name__", str(schema))
        self.calls.append(name)
        self.last_system = str(kwargs.get("system", ""))
        self.last_prompt = str(prompt)
        if name == "PlannerPlan":
            if self.planner_error is not None:
                raise self.planner_error
            return dict(self.plan)
        if name == "ModelSpecification":
            if self.enriched is None:
                raise AssertionError("unexpected research enrichment call")
            return dict(self.enriched)
        raise AssertionError(f"unexpected schema {name!r}")


class FakeVisionProvider(FakeProvider):
    """Vision-capable fake: returns canned ``ReviewResult`` payloads in order.

    Everything else is inherited from :class:`FakeProvider`, so the Planner and
    Research paths keep working. The review calls are recorded separately
    (``review_prompts``/``review_images``/``review_systems``) and also appended
    to ``calls`` as ``"ReviewResult"``.
    """

    name = "fake-vision"

    def __init__(
        self,
        *,
        reviews: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.reviews = [dict(review) for review in (reviews or [])]
        self.review_prompts: list[str] = []
        self.review_images: list[list[bytes]] = []
        self.review_systems: list[str] = []

    def supports_vision(self) -> bool:
        return True

    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        name = getattr(schema, "__name__", str(schema))
        if name == "ReviewResult":
            self.calls.append(name)
            self.review_prompts.append(prompt)
            self.review_images.append(list(kwargs.get("images") or []))
            self.review_systems.append(str(kwargs.get("system", "")))
            if not self.reviews:
                raise AssertionError("unexpected ReviewResult call")
            return dict(self.reviews.pop(0))
        return super().structured(prompt, schema, **kwargs)


class FakeCADBackend(CADBackend):
    """Deterministic backend whose validator fails the first *failures* attempts."""

    name = "fake"

    def __init__(
        self,
        *,
        failures: int = 0,
        scad_source: str = "// fake scad\ncube([1, 1, 1]);\n",
        stl_bytes: bytes = b"solid fake\nendsolid fake\n",
        generate_error: Exception | None = None,
        export_error: Exception | None = None,
    ) -> None:
        self.failures = failures
        self.scad_source = scad_source
        self.stl_bytes = stl_bytes
        self.generate_error = generate_error
        self.export_error = export_error
        self.generate_calls = 0
        self.validate_calls = 0
        self.export_calls = 0
        self.validated_models: list[GeneratedModel] = []

    def generate(self, specification: dict[str, Any]) -> str:
        self.generate_calls += 1
        if self.generate_error is not None:
            raise self.generate_error
        return self.scad_source

    def validate(self, model: GeneratedModel) -> list[str]:
        self.validate_calls += 1
        self.validated_models.append(model)
        if self.validate_calls <= self.failures:
            return [f"blocking problem #{self.validate_calls}"]
        return []

    def export(self, model: GeneratedModel, format: str) -> bytes:
        self.export_calls += 1
        if self.export_error is not None:
            raise self.export_error
        return self.stl_bytes


class SpecFixingCADBackend(CADBackend):
    """Fails ``generate`` until every slot operation has diameter + length.

    Models the real, *fixable* specification error the LLM can produce: a
    ``slot`` operation missing its required ``diameter`` makes the OpenSCAD
    backend raise :class:`~designs.cad.base.SpecificationError`. Generation only
    succeeds once the reviser has filled the missing numeric fields, so the CAD
    retry loop can be exercised end-to-end without OpenSCAD.
    """

    name = "spec-fixing"

    def __init__(
        self,
        *,
        scad_source: str = "// fixed scad\ncube([1, 1, 1]);\n",
        stl_bytes: bytes = b"solid fixed\nendsolid fixed\n",
    ) -> None:
        self.scad_source = scad_source
        self.stl_bytes = stl_bytes
        self.generate_calls = 0
        self.validate_calls = 0
        self.export_calls = 0
        self.seen_specifications: list[dict[str, Any]] = []

    def generate(self, specification: dict[str, Any]) -> str:
        self.generate_calls += 1
        self.seen_specifications.append(specification)
        for index, operation in enumerate(specification.get("operations") or []):
            if not isinstance(operation, dict) or operation.get("kind") != "slot":
                continue
            if operation.get("diameter") is None:
                raise SpecificationError(
                    f"operations[{index}].diameter is required for a 'slot' operation"
                )
            if operation.get("length") is None:
                raise SpecificationError(
                    f"operations[{index}].length is required for a 'slot' operation"
                )
        return self.scad_source

    def validate(self, model: GeneratedModel) -> list[str]:
        self.validate_calls += 1
        return []

    def export(self, model: GeneratedModel, format: str) -> bytes:
        self.export_calls += 1
        return self.stl_bytes
