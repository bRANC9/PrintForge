"""Skill injection + enforced skill constraints (docs/skills.md 3-4).

No database, no network: the skill selector is a fake injected through
:class:`~agents.graph.WorkflowDeps` and the skills themselves are plain dicts.
These tests pin the two halves of the feature -- the "Aktív skillek" prompt
block and the Validator's *mechanical* constraint checks -- plus the
``template`` skill -> ``specification["object"]`` convention.
"""

from __future__ import annotations

from typing import Any

from agents.graph import WorkflowDeps, run_workflow
from agents.graph.skills import (
    ACTIVE_SKILLS_HEADER,
    build_skills_block,
    skill_constraint_errors,
    template_object_kind,
    with_skills,
)
from agents.graph.tests.fakes import DEFAULT_SPEC, FakeCADBackend, FakeProvider

CUTTER = {
    "id": 1,
    "slug": "cookie-cutter",
    "name": "Süti kinyomó",
    "kind": "guidance",
    "template_key": "",
    "object_kind": "cookie_cutter",
    "description": "Vékony falú süti kinyomó.",
    "guidance": "1.2 mm fal, 20-30 mm magasság.",
    "defaults_json": {"wall_thickness": 1.2, "material": "PLA"},
    "constraints_json": {"min_wall_mm": 1.2, "must_rest_on_plate": True},
}
TEMPLATE = {
    "id": 2,
    "slug": "phone-holder",
    "name": "Telefontartó",
    "kind": "template",
    "template_key": "phone_holder",
    "object_kind": "phone_holder",
    "description": "Beépített sablon.",
    "guidance": "",
    "defaults_json": {},
    "constraints_json": {},
}


def _never_retrieve(*args: Any, **kwargs: Any) -> list[dict]:
    raise AssertionError("retrieve() must not be called when research is not needed")


def _deps(provider: FakeProvider, cad: FakeCADBackend, **kwargs: Any) -> WorkflowDeps:
    kwargs.setdefault("retrieve_fn", _never_retrieve)
    kwargs.setdefault("max_attempts", 3)
    return WorkflowDeps(provider=provider, cad_backend=cad, **kwargs)


def _selecting(skills: list[dict[str, Any]], *, expect_manual: bool | None = None):
    """Return a fake ``select_skills`` callable recording its call arguments."""

    calls: list[dict[str, Any]] = []

    def select(*, prompt: str, object_kind: str = "", workspace: Any = None, manual_ids=None):
        calls.append(
            {
                "prompt": prompt,
                "object_kind": object_kind,
                "workspace": workspace,
                "manual_ids": list(manual_ids or []),
            }
        )
        if expect_manual is not None:
            assert bool(manual_ids) is expect_manual
        return list(skills)

    select.calls = calls  # type: ignore[attr-defined]
    return select


# ---------------------------------------------------------------------------
# Prompt block
# ---------------------------------------------------------------------------


def test_build_skills_block_renders_name_description_guidance_defaults():
    block = build_skills_block([CUTTER])

    assert ACTIVE_SKILLS_HEADER in block
    assert CUTTER["name"] in block
    assert CUTTER["description"] in block
    assert CUTTER["guidance"] in block
    assert '"wall_thickness": 1.2' in block
    assert '"material": "PLA"' in block


def test_build_skills_block_is_empty_without_skills():
    assert build_skills_block([]) == ""
    assert build_skills_block(None) == ""
    assert with_skills("BASE", []) == "BASE"


def test_template_object_kind_prefers_template_key():
    assert template_object_kind([CUTTER]) == ""
    assert template_object_kind([CUTTER, TEMPLATE]) == "phone_holder"
    assert template_object_kind([]) == ""


# ---------------------------------------------------------------------------
# Selection wiring + provenance
# ---------------------------------------------------------------------------


def test_manual_skill_ids_win_and_reach_the_prompt():
    provider = FakeProvider()
    cad = FakeCADBackend()
    select = _selecting([CUTTER], expect_manual=True)

    state = run_workflow(
        "make a cookie cutter",
        deps=_deps(provider, cad, select_skills_fn=select),
        skill_ids=[1],
    )

    assert state["status"] == "done"
    assert state["skill_selection"] == "manual"
    assert [skill["slug"] for skill in state["skills"]] == ["cookie-cutter"]
    assert select.calls[0]["manual_ids"] == [1]
    # The prompt actually carried the block (the fake records the system prompt).
    assert ACTIVE_SKILLS_HEADER in provider.last_system


def test_auto_selection_runs_without_explicit_ids():
    provider = FakeProvider()
    cad = FakeCADBackend()
    select = _selecting([CUTTER], expect_manual=False)

    state = run_workflow(
        "make a cookie cutter",
        deps=_deps(provider, cad, select_skills_fn=select),
        auto_skill_selection=True,
    )

    assert state["skill_selection"] == "auto"
    assert [skill["slug"] for skill in state["skills"]] == ["cookie-cutter"]
    assert select.calls[0]["prompt"] == "make a cookie cutter"


def test_no_selection_skips_the_selector_entirely():
    provider = FakeProvider()
    cad = FakeCADBackend()
    select = _selecting([CUTTER])

    state = run_workflow("x", deps=_deps(provider, cad, select_skills_fn=select))

    assert state["skills"] == []
    assert state["skill_selection"] == ""
    assert select.calls == []
    assert ACTIVE_SKILLS_HEADER not in provider.last_system


def test_broken_selector_never_fails_the_run():
    def boom(**kwargs: Any):
        raise RuntimeError("skills table missing")

    provider = FakeProvider()
    cad = FakeCADBackend()

    state = run_workflow(
        "x",
        deps=_deps(provider, cad, select_skills_fn=boom),
        auto_skill_selection=True,
    )

    assert state["status"] == "done"
    assert state["skills"] == []
    assert state["skill_selection"] == ""


def test_template_skill_sets_the_specification_object():
    provider = FakeProvider()
    cad = FakeCADBackend()

    state = run_workflow(
        "holder for a phone",
        deps=_deps(provider, cad, select_skills_fn=_selecting([TEMPLATE])),
        auto_skill_selection=True,
    )

    assert state["specification"]["object"] == "phone_holder"
    # The built-in generator convention: the object kind reaches the CAD agent.
    assert cad.validated_models[0].specification["object"] == "phone_holder"


# ---------------------------------------------------------------------------
# Enforced constraints (Validator)
# ---------------------------------------------------------------------------


def _cutter_spec(**overrides: Any) -> dict[str, Any]:
    spec = {
        **DEFAULT_SPEC,
        "object": "cookie_cutter",
        "wall_thickness": 1.2,
        "primitives": [
            {
                "type": "box",
                "role": "add",
                "position": {"x": 0.0, "y": 0.0, "z": 10.0},
                "width": 60.0,
                "depth": 60.0,
                "height": 20.0,
            }
        ],
    }
    spec.update(overrides)
    return spec


def test_skill_constraint_errors_accepts_a_compliant_specification():
    problems, warnings = skill_constraint_errors(_cutter_spec(), [CUTTER])

    assert problems == []
    assert warnings == []


def test_skill_constraint_errors_enforces_min_wall():
    problems, _ = skill_constraint_errors(_cutter_spec(wall_thickness=0.8), [CUTTER])

    assert len(problems) == 1
    assert "min_wall_mm 1.2" in problems[0]


def test_skill_constraint_errors_enforces_rest_on_plate():
    floating = _cutter_spec()
    floating["primitives"][0]["position"]["z"] = 30.0

    problems, _ = skill_constraint_errors(floating, [CUTTER])

    assert len(problems) == 1
    assert "does not rest on the plate" in problems[0]


def test_skill_constraint_errors_ignores_subtractive_primitives():
    spec = _cutter_spec()
    spec["primitives"].append(
        {
            "type": "cylinder",
            "role": "subtract",
            "position": {"x": 0.0, "y": 0.0, "z": 50.0},
            "diameter": 4.0,
            "height": 10.0,
        }
    )

    problems, _ = skill_constraint_errors(spec, [CUTTER])

    assert problems == []


def test_skill_constraint_errors_require_primitives_by_type():
    skill = {**CUTTER, "constraints_json": {"require_primitives": ["extrude"]}}

    problems, _ = skill_constraint_errors(_cutter_spec(), [skill])

    assert len(problems) == 1
    assert "missing required primitive type(s): extrude" in problems[0]


def test_skill_constraint_errors_forbid_operations():
    skill = {**CUTTER, "constraints_json": {"forbid_operations": ["cut", "pocket"]}}
    spec = _cutter_spec(
        operations=[
            {
                "kind": "cut",
                "origin": {"x": 0.0, "y": 0.0, "z": 5.0},
                "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
                "depth": 5.0,
                "width": 10.0,
                "height": 10.0,
            }
        ]
    )

    problems, _ = skill_constraint_errors(spec, [skill])

    assert len(problems) == 1
    assert "forbidden operation kind(s) used: cut" in problems[0]


def test_skill_constraint_errors_warns_on_unknown_keys():
    skill = {**CUTTER, "constraints_json": {"min_wall_mm": 1.2, "future_check": True}}

    problems, warnings = skill_constraint_errors(_cutter_spec(), [skill])

    assert problems == []
    assert any("future_check" in warning for warning in warnings)


def test_validator_enforces_the_skill_constraint_end_to_end():
    skill = {**CUTTER, "constraints_json": {"min_wall_mm": 5.0}}
    provider = FakeProvider(
        plan={"specification": DEFAULT_SPEC, "needs_research": False, "research_query": None}
    )
    cad = FakeCADBackend()

    state = run_workflow(
        "x",
        deps=_deps(provider, cad, select_skills_fn=_selecting([skill]), max_attempts=2),
        auto_skill_selection=True,
    )

    assert state["status"] == "failed"
    # The constraint, not the backend, blocked every attempt (and never exported).
    assert cad.export_calls == 0
    assert cad.generate_calls == 2
    assert any("min_wall_mm 5.0" in error for error in state["validation"]["errors"])
    assert "min_wall_mm 5.0" in state["error"]


def test_validator_passes_skill_constraints_when_satisfied():
    skill = {**CUTTER, "constraints_json": {"min_wall_mm": 0.4}}
    provider = FakeProvider()
    cad = FakeCADBackend()

    state = run_workflow(
        "x",
        deps=_deps(provider, cad, select_skills_fn=_selecting([skill])),
        auto_skill_selection=True,
    )

    assert state["status"] == "done"
    assert state["validation"]["errors"] == []
    assert cad.export_calls == 1
