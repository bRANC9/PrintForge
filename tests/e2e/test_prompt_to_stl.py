"""End-to-end: prompt -> validated spec -> OpenSCAD -> STL -> storage.

Both the LLM and the OpenSCAD CLI are stubbed, so the whole flow is
deterministic and needs neither network nor a CAD toolchain (terv.md 26.2,
26.3). The real Celery task body runs synchronously.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from factories import ProjectFactory
from pydantic import BaseModel, ValidationError

from agents.llm.base import LLMProvider
from agents.spec import ModelSpecification
from designs.cad.openscad import OpenSCADBackend, OpenSCADError
from designs.services import create_next_version
from designs.tasks import render_model_stl
from files.services import LocalStorage

pytestmark = pytest.mark.django_db

FAKE_STL = b"solid printforge\nfacet normal 0 0 1\nendfacet\nendsolid printforge\n"


class FakeLLM(LLMProvider):
    """A deterministic provider: it returns the given structured payload."""

    name = "fake"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def generate(self, prompt: str, **kwargs: Any) -> str:
        self.prompts.append(prompt)
        return "ok"

    def structured(
        self,
        prompt: str,
        schema: type[BaseModel] | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        return dict(self.payload)


def _stub_openscad(monkeypatch, data: bytes = FAKE_STL) -> None:
    """Replace the OpenSCAD CLI with a stub that writes ``data`` to the output."""

    def fake_run(args, **kwargs):  # noqa: ANN001
        Path(args[2]).write_bytes(data)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_prompt_to_stl_end_to_end(monkeypatch, tmp_path):
    prompt = "make me a phone holder for a 70 mm phone"

    # 1. The (mocked) LLM turns the prompt into the terv.md 8. example.
    provider = FakeLLM(ModelSpecification.example())
    specification = ModelSpecification.model_validate(
        provider.structured(prompt, ModelSpecification)
    )

    # 2. Persist v1 with the validated specification.
    project = ProjectFactory()
    version = create_next_version(
        project=project,
        prompt=prompt,
        created_by=project.created_by,
        specification=specification.model_dump(),
    )

    # 3. Stub the CAD binary and the storage root.
    _stub_openscad(monkeypatch)
    storage = LocalStorage(root=tmp_path)
    monkeypatch.setattr("designs.cad.pipeline.get_backend", lambda: OpenSCADBackend(mode="local"))
    monkeypatch.setattr("designs.cad.pipeline.get_storage", lambda: storage)

    # 4. Run the real task body synchronously (no broker, no worker).
    result = render_model_stl(version.pk)

    version.refresh_from_db()
    assert result["status"] == "done"
    assert version.validation_json["status"] == "done"
    assert version.validation_json["errors"] == []
    assert version.validation_json["stl_bytes"] == len(FAKE_STL)

    scad = storage.read_bytes(version.scad_file.name).decode("utf-8")
    stl = storage.read_bytes(version.stl_file.name)
    assert "phone_holder" in scad
    assert "import(" not in scad
    assert stl == FAKE_STL
    assert provider.prompts == [prompt]


def test_pipeline_failure_is_persisted_and_leaves_no_stl(monkeypatch, tmp_path):
    project = ProjectFactory()
    version = create_next_version(
        project=project,
        prompt="boom",
        created_by=project.created_by,
        specification=ModelSpecification.example(),
    )

    def failing_run(args, **kwargs):  # noqa: ANN001
        return subprocess.CompletedProcess(args, 1, "", "syntax error")

    monkeypatch.setattr(subprocess, "run", failing_run)
    storage = LocalStorage(root=tmp_path)
    monkeypatch.setattr("designs.cad.pipeline.get_backend", lambda: OpenSCADBackend(mode="local"))
    monkeypatch.setattr("designs.cad.pipeline.get_storage", lambda: storage)

    with pytest.raises(OpenSCADError):
        render_model_stl(version.pk)

    version.refresh_from_db()
    assert version.validation_json["status"] == "failed"
    assert version.validation_json["errors"]
    assert version.stl_file.name == ""
    assert not storage.exists(f"projects/{project.id}/v{version.version}/model.stl")


def test_invalid_llm_output_never_reaches_the_cad_pipeline():
    provider = FakeLLM({"object": "phone_holder"})  # missing dimensions/mounting

    with pytest.raises(ValidationError):
        ModelSpecification.model_validate(provider.structured("anything", ModelSpecification))
