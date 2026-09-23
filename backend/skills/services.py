"""Business logic for skills (docs/skills.md).

A skill is structured data plus textual guidance, never code. These functions
are HTTP-free so the JSON API, the MCP tools and the agent workflow can all call
them with plain arguments (model instances / ids).

Two responsibilities live here:

* **CRUD** -- the API view stays thin and delegates tag handling and the
  built-in read-only guard to :func:`create_skill` / :func:`update_skill` /
  :func:`delete_skill`.
* **Selection** -- :func:`select_skills` picks the skills for a generation run:
  manual ids first, then a cheap ``object_kind``/tag match, and only as a last
  resort a semantic (embedding) fallback. The embedding path is a lazily
  imported, injectable seam and never raises.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from django.db import transaction
from django.db.models import Q, QuerySet

from projects.models import Tag
from workspaces.models import Workspace

from .models import Skill, SkillKind

logger = logging.getLogger(__name__)

__all__ = [
    "create_skill",
    "delete_skill",
    "select_skills",
    "set_skill_tags",
    "skills_visible_to",
    "update_skill",
]

#: Scalar fields a caller may set/update through the service layer.
_UPDATABLE_FIELDS = frozenset(
    {
        "name",
        "description",
        "kind",
        "template_key",
        "object_kind",
        "guidance",
        "defaults_json",
        "constraints_json",
        "workspace",
        "is_public",
    }
)

#: Prompt tokeniser for the deterministic tag match (lowercase word-ish runs).
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def skills_visible_to(
    user: Any,
    *,
    workspace: Workspace | None = None,
    kind: str = "",
    q: str = "",
) -> QuerySet[Skill]:
    """Skills the caller may see: own workspaces + built-ins + public.

    Anonymous callers only get built-ins and public skills. The optional
    ``workspace`` narrows to that workspace's skills (plus the global ones);
    ``kind``/``q`` are the list-view filters.
    """
    if user is not None and getattr(user, "is_authenticated", False):
        queryset = Skill.objects.filter(
            Q(is_builtin=True) | Q(is_public=True) | Q(workspace__members__user=user)
        )
    else:
        queryset = Skill.objects.filter(Q(is_builtin=True) | Q(is_public=True))

    if workspace is not None:
        queryset = queryset.filter(Q(workspace=workspace) | Q(is_builtin=True) | Q(is_public=True))
    if kind:
        queryset = queryset.filter(kind=kind)
    if q:
        queryset = queryset.filter(
            Q(name__icontains=q)
            | Q(slug__icontains=q)
            | Q(description__icontains=q)
            | Q(object_kind__icontains=q)
        )
    return queryset.select_related("workspace", "created_by").prefetch_related("tags").distinct()


@transaction.atomic
def set_skill_tags(skill: Skill, names: Iterable[str] | None) -> Skill:
    """Replace ``skill``'s tags with ``names``, creating missing ``Tag`` rows."""
    tags: list[Tag] = []
    for raw in names or []:
        name = (raw or "").strip()
        if not name:
            continue
        tag, _created = Tag.objects.get_or_create(name=name)
        tags.append(tag)
    skill.tags.set(tags)
    return skill


@transaction.atomic
def create_skill(
    *,
    name: str,
    description: str = "",
    kind: str = SkillKind.GUIDANCE,
    template_key: str = "",
    object_kind: str = "",
    guidance: str = "",
    defaults_json: dict | None = None,
    constraints_json: dict | None = None,
    workspace: Workspace | None = None,
    is_public: bool = False,
    created_by: Any = None,
    tags: Iterable[str] | None = None,
) -> Skill:
    """Create a skill and attach its tags (docs/skills.md 2.)."""
    skill = Skill.objects.create(
        name=name,
        description=description or "",
        kind=kind,
        template_key=template_key or "",
        object_kind=object_kind or "",
        guidance=guidance or "",
        defaults_json=defaults_json or {},
        constraints_json=constraints_json or {},
        workspace=workspace,
        is_public=is_public,
        created_by=created_by,
    )
    if tags is not None:
        set_skill_tags(skill, tags)
    return skill


@transaction.atomic
def update_skill(skill: Skill, *, tags: Iterable[str] | None = None, **fields: Any) -> Skill:
    """Update a skill's scalar fields and (optionally) its tags.

    Built-in seed skills are read-only (docs/skills.md 6.); this is enforced by
    :class:`api.permissions.BuiltinReadOnly` at the edge and repeated here as a
    defence in depth for non-HTTP callers.
    """
    if getattr(skill, "is_builtin", False):
        raise ValueError("Built-in skills are read-only.")

    changed = False
    for name, value in fields.items():
        if name in _UPDATABLE_FIELDS:
            setattr(skill, name, value)
            changed = True
    if changed:
        skill.save()
    if tags is not None:
        set_skill_tags(skill, tags)
    return skill


def delete_skill(skill: Skill) -> None:
    """Delete a skill; built-in seed skills are read-only."""
    if getattr(skill, "is_builtin", False):
        raise ValueError("Built-in skills are read-only.")
    skill.delete()


# ---------------------------------------------------------------------------
# Selection (docs/skills.md 3.)
# ---------------------------------------------------------------------------


def _candidate_skills(workspace: Workspace | None) -> list[Skill]:
    """The skills eligible for a run: workspace-local + built-ins + public."""
    queryset = Skill.objects.prefetch_related("tags")
    if workspace is None:
        return list(queryset.filter(Q(is_builtin=True) | Q(is_public=True)))
    return list(
        queryset.filter(Q(workspace=workspace) | Q(is_builtin=True) | Q(is_public=True)).distinct()
    )


def _prompt_tokens(prompt: str) -> set[str]:
    return set(_TOKEN_RE.findall((prompt or "").lower()))


def _match_by_kind_and_tags(
    candidates: Sequence[Skill],
    *,
    prompt: str,
    object_kind: str,
) -> list[Skill]:
    """Deterministic, explainable match: exact ``object_kind`` then tags/slug."""
    if object_kind:
        exact = [skill for skill in candidates if skill.object_kind == object_kind]
        if exact:
            return exact

    tokens = _prompt_tokens(prompt)
    if not tokens:
        return []

    matched: list[Skill] = []
    for skill in candidates:
        if skill.object_kind and skill.object_kind.lower() in tokens:
            matched.append(skill)
            continue
        tag_terms = {tag.slug.lower() for tag in skill.tags.all()}
        tag_terms |= {tag.name.lower() for tag in skill.tags.all()}
        if skill.slug and skill.slug.lower() in tokens:
            matched.append(skill)
        elif tokens & tag_terms:
            matched.append(skill)
    return matched


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def _default_embedding_ranker(
    *,
    prompt: str,
    candidates: Sequence[Skill],
    limit: int = 3,
) -> list[int]:
    """Best-effort semantic rank via the shared embeddings service.

    Imported lazily so the sqlite fallback (no pgvector) never breaks the
    import, and any failure is swallowed by :func:`_select_by_embedding`.
    Returns the matching skill primary keys, best first.
    """
    from embeddings import services as embeddings  # lazy: pgvector is optional

    query = embeddings.embed_text(prompt)
    scored: list[tuple[float, int]] = []
    for skill in candidates:
        text = " ".join(filter(None, [skill.name, skill.description, skill.guidance]))
        if not text.strip():
            continue
        score = _cosine(query, embeddings.embed_text(text))
        if score > 0:
            scored.append((score, skill.pk))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [pk for _score, pk in scored[:limit]]


def _select_by_embedding(
    candidates: Sequence[Skill],
    *,
    prompt: str,
    ranker: Callable[..., Sequence[int]] | None,
    limit: int = 3,
) -> list[Skill]:
    """Run the (injected or default) ranker; never raise on embedding failure."""
    if not prompt or not candidates:
        return []
    selected = ranker or _default_embedding_ranker
    try:
        ranked_ids = list(selected(prompt=prompt, candidates=list(candidates), limit=limit))
    except Exception:  # noqa: BLE001 - selection must never fail a generation
        logger.warning("Skill embedding selection failed", exc_info=True)
        return []

    by_id = {skill.pk: skill for skill in candidates}
    result: list[Skill] = []
    for raw_id in ranked_ids:
        try:
            skill_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        skill = by_id.get(skill_id)
        if skill is not None and skill not in result:
            result.append(skill)
    return result


def select_skills(
    *,
    prompt: str,
    object_kind: str = "",
    workspace: Workspace | None = None,
    manual_ids: Iterable[int] | None = None,
    ranker: Callable[..., Sequence[int]] | None = None,
) -> list[Skill]:
    """Choose the skills for a generation run (docs/skills.md 3.).

    Deterministic order:

    1. ``manual_ids`` (explicit user choice) win; only ids visible to the run
       are returned, so a foreign/private id cannot leak in.
    2. Otherwise an ``object_kind`` / tag / slug match against the prompt.
    3. Only when that yields nothing does the embedding fallback run, through
       the injectable ``ranker`` seam (defaulting to the shared embeddings
       service). Embedding failures degrade to no selection and never raise.
    """
    candidates = _candidate_skills(workspace)

    if manual_ids:
        wanted = [int(pk) for pk in manual_ids]
        wanted_set = set(wanted)
        by_id = {skill.pk: skill for skill in candidates if skill.pk in wanted_set}
        return [by_id[pk] for pk in wanted if pk in by_id]

    matched = _match_by_kind_and_tags(candidates, prompt=prompt, object_kind=object_kind)
    if matched:
        return matched

    return _select_by_embedding(candidates, prompt=prompt, ranker=ranker)
