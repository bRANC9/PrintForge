"""End-to-end: an ``extrude`` specification -> SCAD -> STL through the pipeline.

``backend/designs/cad/tests/test_extrude.py`` already pins the parser/renderer in
isolation. This module covers the *pipeline* seam (docs/skills.md 5.): a stored
specification carrying an ``extrude`` primitive flows through
``render_model_stl`` / the agent task with the **real** ``OpenSCADBackend`` and
only the subprocess boundary stubbed, so the generated source is read back from
the storage backend and must be a safe ``linear_extrude``/``polygon`` -- never a
forbidden ``import()``/``surface()``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from factories import ModelVersionFactory, ProjectFactory

from agents.graph import WorkflowDeps
from agents.graph.tests.fakes import FakeProvider
from agents.spec import ModelSpecification
from agents.tasks import run_agent_workflow
from designs.cad.openscad import OpenSCADBackend
from designs.services import create_next_version
from designs.tasks import render_model_stl
from files.services import LocalStorage

pytestmark = pytest.mark.django_db

FAKE_STL = b"solid printforge\nfacet normal 0 0 1\nendfacet\nendsolid printforge\n"

#: A hollow extruded cookie-cutter outline: a square with a thin wall.
EXTRUDE_PRIMITIVE = {
    "type": "extrude",
    "role": "add",
    "position": {"x": 0.0, "y": 0.0, "z": 0.0},
    "profile": [
        {"x": 0.0, "y": 0.0},
        {"x": 40.0, "y": 0.0},
        {"x": 40.0, "y": 40.0},
        {"x": 0.0, "y": 40.0},
    ],
    "height": 20.0,
    "wall_thickness": 1.2,
}
EXTRUDE_SPEC: dict = {
    **ModelSpecification.example(),
    "object": "cookie_cutter",
    "wall_thickness": 1.2,
    "primitives": [EXTRUDE_PRIMITIVE],
}


def _stub_openscad(monkeypatch, data: bytes = FAKE_STL) -> None:
    """Replace the OpenSCAD CLI; the backend still writes the real SCAD source."""

    def fake_run(args, **kwargs):  # noqa: ANN001
        Path(args[2]).write_bytes(data)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_stored_extrude_specification_renders_safe_scad_through_the_pipeline(monkeypatch, tmp_path):
    project = ProjectFactory()
    version = create_next_version(
        project=project,
        prompt="make a cookie cutter",
        created_by=project.created_by,
        specification=EXTRUDE_SPEC,
    )
    _stub_openscad(monkeypatch)
    storage = LocalStorage(root=tmp_path)
    monkeypatch.setattr("designs.cad.pipeline.get_backend", lambda: OpenSCADBackend(mode="local"))
    monkeypatch.setattr("designs.cad.pipeline.get_storage", lambda: storage)

    result = render_model_stl(version.pk)

    version.refresh_from_db()
    assert result["status"] == "done"
    assert version.validation_json["status"] == "done"
    assert version.validation_json["errors"] == []

    scad = storage.read_bytes(version.scad_file.name).decode("utf-8")
    assert "linear_extrude" in scad
    assert "polygon(" in scad
    assert "import(" not in scad
    assert "surface(" not in scad
    assert "include <" not in scad
    assert "use <" not in scad
    assert storage.read_bytes(version.stl_file.name) == FAKE_STL


def test_agent_task_renders_the_extrude_specification_to_scad(monkeypatch, tmp_path):
    """The full agent pipeline (fake LLM, real CAD renderer, stubbed CLI)."""
    project = ProjectFactory()
    provider = FakeProvider(
        plan={
            "specification": EXTRUDE_SPEC,
            "needs_research": False,
            "research_query": None,
        }
    )
    deps = WorkflowDeps(
        provider=provider,
        cad_backend=OpenSCADBackend(mode="local"),
        retrieve_fn=lambda *args, **kwargs: [],
        preview_renderer=lambda _stl: b"",
        max_attempts=3,
    )
    monkeypatch.setattr("agents.tasks.build_dependencies", lambda: deps)
    storage = LocalStorage(root=tmp_path)
    monkeypatch.setattr("agents.tasks.get_storage", lambda: storage)
    _stub_openscad(monkeypatch)

    run_id = run_agent_workflow(project.pk, "make a cookie cutter", project.created_by_id)

    version = project.versions.get()
    assert version.validation_json["agent_run_id"] == run_id
    assert version.specification_json["primitives"][0]["type"] == "extrude"

    scad = storage.read_bytes(version.scad_file.name).decode("utf-8")
    assert "linear_extrude(height=20.0)" in scad
    assert "polygon(points=[[0.0, 0.0], [40.0, 0.0], [40.0, 40.0], [0.0, 40.0]])" in scad
    assert "import(" not in scad
    assert "surface(" not in scad
    assert storage.read_bytes(version.stl_file.name) == FAKE_STL


def test_extrude_specification_survives_serialisation_to_a_version():
    version = ModelVersionFactory(project=ProjectFactory(), specification_json=EXTRUDE_SPEC)

    version.refresh_from_db()
    assert version.specification_json["primitives"] == [EXTRUDE_PRIMITIVE]
