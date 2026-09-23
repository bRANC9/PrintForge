"""Tests for the skills service layer (docs/skills.md).

CRUD/tag handling, visibility scoping and the deterministic
``select_skills`` order (manual -> object_kind/tag -> injectable embedding
fallback), including the "embedding failure never raises" guarantee.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from skills.models import Skill, SkillKind
from skills.services import (
    create_skill,
    delete_skill,
    select_skills,
    set_skill_tags,
    skills_visible_to,
    update_skill,
)
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture(autouse=True)
def _clear_seeded_skills():
    """Drop the seeded built-ins so visibility assertions are exact."""
    Skill.objects.all().delete()


@pytest.fixture
def owner():
    return User.objects.create_user(username="owner", email="owner@example.com", password="pw")


@pytest.fixture
def outsider():
    return User.objects.create_user(username="outsider", email="out@example.com", password="pw")


@pytest.fixture
def workspace(owner):
    return create_workspace(name="Lab", owner=owner)


def make_builtin(slug: str, **extra) -> Skill:
    return Skill.objects.create(
        name=slug.replace("-", " ").title(), slug=slug, is_builtin=True, **extra
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_skill_derives_slug_and_attaches_tags(owner):
    skill = create_skill(
        name="Cookie cutter",
        description="thin wall",
        guidance="1.2 mm wall",
        tags=["cookie", "kitchen"],
        created_by=owner,
    )

    assert skill.slug == "cookie-cutter"
    assert skill.kind == SkillKind.GUIDANCE
    assert set(skill.tags.values_list("name", flat=True)) == {"cookie", "kitchen"}
    assert skill.created_by == owner


def test_create_skill_reuses_existing_tag_rows(owner):
    first = create_skill(name="A", tags=["shared"], created_by=owner)
    second = create_skill(name="B", tags=["shared"], created_by=owner)

    assert first.tags.get().pk == second.tags.get().pk


def test_set_skill_tags_replaces_the_set(owner):
    skill = create_skill(name="A", tags=["one", "two"], created_by=owner)

    set_skill_tags(skill, ["two", "three"])

    assert set(skill.tags.values_list("name", flat=True)) == {"two", "three"}


def test_update_skill_applies_scalars_and_tags(owner):
    skill = create_skill(name="A", created_by=owner)

    update_skill(skill, name="B", object_kind="bracket", tags=["metal"])

    skill.refresh_from_db()
    assert skill.name == "B"
    assert skill.object_kind == "bracket"
    assert [tag.name for tag in skill.tags.all()] == ["metal"]


def test_update_and_delete_reject_builtin(owner):
    builtin = make_builtin("seed")

    with pytest.raises(ValueError, match="read-only"):
        update_skill(builtin, name="Nope")
    with pytest.raises(ValueError, match="read-only"):
        delete_skill(builtin)
    assert Skill.objects.filter(pk=builtin.pk).exists()


def test_delete_removes_a_custom_skill(owner):
    skill = create_skill(name="A", created_by=owner)

    delete_skill(skill)

    assert not Skill.objects.filter(pk=skill.pk).exists()


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def test_visibility_includes_builtins_and_public(owner, outsider, workspace):
    make_builtin("seed")
    public = Skill.objects.create(name="Public", slug="public", is_public=True)
    private = create_skill(name="Private", workspace=workspace, created_by=owner)
    foreign = create_skill(name="Foreign", created_by=outsider)

    visible = set(skills_visible_to(owner).values_list("pk", flat=True))

    assert visible == {public.pk, private.pk, Skill.objects.get(slug="seed").pk}
    assert foreign.pk not in visible


def test_visibility_for_anonymous_is_builtins_and_public(owner, workspace):
    make_builtin("seed")
    public = Skill.objects.create(name="Public", slug="public", is_public=True)
    create_skill(name="Private", workspace=workspace, created_by=owner)

    visible = set(skills_visible_to(None).values_list("pk", flat=True))

    assert visible == {public.pk, Skill.objects.get(slug="seed").pk}


def test_visibility_filters_by_kind_and_query(owner, workspace):
    create_skill(
        name="Cookie cutter", kind=SkillKind.GUIDANCE, workspace=workspace, created_by=owner
    )
    create_skill(
        name="Phone holder", kind=SkillKind.TEMPLATE, workspace=workspace, created_by=owner
    )

    assert [s.name for s in skills_visible_to(owner, kind="template")] == ["Phone holder"]
    assert [s.name for s in skills_visible_to(owner, q="cookie")] == ["Cookie cutter"]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_select_skills_manual_ids_win_and_preserve_order(owner, workspace):
    a = create_skill(name="A", object_kind="a", workspace=workspace, created_by=owner)
    b = create_skill(name="B", object_kind="b", workspace=workspace, created_by=owner)

    selected = select_skills(
        prompt="",
        workspace=workspace,
        manual_ids=[b.pk, a.pk],
        ranker=lambda **_kwargs: pytest.fail("ranker must not run for manual selection"),
    )

    assert [skill.pk for skill in selected] == [b.pk, a.pk]


def test_select_skills_manual_ids_are_scoped_to_visible(owner, outsider, workspace):
    mine = create_skill(name="Mine", workspace=workspace, created_by=owner)
    foreign = create_skill(name="Foreign", created_by=outsider)

    selected = select_skills(prompt="", workspace=workspace, manual_ids=[foreign.pk, mine.pk])

    assert [skill.pk for skill in selected] == [mine.pk]


def test_select_skills_matches_object_kind(owner, workspace):
    create_skill(name="Cookie", object_kind="cookie_cutter", workspace=workspace, created_by=owner)
    other = create_skill(name="Other", object_kind="bracket", workspace=workspace, created_by=owner)

    selected = select_skills(prompt="", object_kind="cookie_cutter", workspace=workspace)

    assert [skill.pk for skill in selected] == [Skill.objects.get(object_kind="cookie_cutter").pk]
    assert other.pk not in {skill.pk for skill in selected}


def test_select_skills_matches_tags_in_the_prompt(owner, workspace):
    tagged = create_skill(
        name="Kitchen helper", tags=["cookie"], workspace=workspace, created_by=owner
    )
    create_skill(name="Unrelated", workspace=workspace, created_by=owner)

    selected = select_skills(prompt="please make a cookie cutter", workspace=workspace)

    assert [skill.pk for skill in selected] == [tagged.pk]


def test_select_skills_falls_back_to_injected_ranker(owner, workspace):
    first = create_skill(name="First", workspace=workspace, created_by=owner)
    second = create_skill(name="Second", workspace=workspace, created_by=owner)

    def ranker(*, prompt, candidates, limit):
        assert prompt == "a totally unrelated wish"
        return [second.pk, first.pk]

    selected = select_skills(prompt="a totally unrelated wish", workspace=workspace, ranker=ranker)

    assert [skill.pk for skill in selected] == [second.pk, first.pk]


def test_select_skills_swallows_ranker_failure(owner, workspace):
    create_skill(name="Only", workspace=workspace, created_by=owner)

    def boom(**_kwargs):
        raise RuntimeError("ranker exploded")

    assert select_skills(prompt="unmatched wish", workspace=workspace, ranker=boom) == []


def test_select_skills_swallows_embedding_failure(monkeypatch, owner, workspace):
    create_skill(name="Gadget", object_kind="gadget", workspace=workspace, created_by=owner)

    def boom(_text):
        raise RuntimeError("ollama is down")

    monkeypatch.setattr("embeddings.services.embed_text", boom)

    # No deterministic match -> the default ranker runs and fails gracefully.
    assert select_skills(prompt="something completely different", workspace=workspace) == []
