"""Planner agent (terv.md 6., 7. fejezet).

The Planner turns the raw user prompt into a validated
:class:`~agents.spec.ModelSpecification` **through
``LLMProvider.structured``**. It also decides whether the Research agent is
needed and, if so, what to look up.

Boundary (terv.md 7. fejezet): the Planner NEVER produces OpenSCAD source or
STL/mesh data -- it only produces structured data. The CAD source is generated
later by the CAD node through :class:`~designs.cad.base.CADBackend`, and the
mesh only exists after the Validator exports it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents.llm import LLMError, LLMProvider, LLMResponseError
from agents.spec import Clarification, ModelSpecification

from .skills import template_object_kind, with_skills
from .state import WorkflowState, append_history, failure_state
from .vision import reference_images, reference_prompt_for, structured_with_reference_image

logger = logging.getLogger(__name__)

__all__ = [
    "CLARIFICATION_PROMPT",
    "MAX_REPAIR_ERRORS",
    "MAX_REPAIR_HINT_CHARS",
    "OPERATION_DIMENSIONS_PROMPT",
    "PLANNER_SYSTEM_PROMPT",
    "PRIMITIVE_DIMENSIONS_PROMPT",
    "PlannerPlan",
    "clarification_state",
    "coerce_plan",
    "make_planner_node",
    "split_clarifications",
]

#: Shared instruction pinning the per-operation required fields. The Planner,
#: Editor and Reviser all emit ``ModelSpecification.operations``; a ``slot``
#: without its ``diameter`` (or any other missing required dimension) is a
#: fixable specification error the CAD backend rejects, so every prompt states
#: the contract explicitly and asks the LLM to fill the fields.
#:
#: Token budget: the *per-kind mapping itself* is no longer restated here. The
#: JSON schema that ships with every request says it on the exact field being
#: filled (``diameter``: "Required for hole, boss, slot; ignored otherwise"),
#: which is both cheaper and closer to the decision than a paragraph at the top
#: of the prompt. What stays here is the instruction the schema cannot state:
#: these are always required, and they must be concrete numbers, never nulls.
OPERATION_DIMENSIONS_PROMPT = (
    "Every 'operations' entry MUST carry 'depth', 'origin' and 'normal' plus the "
    "dimensions its kind requires, which the schema states on each field. Give "
    "every one of them a concrete number, never null. "
)

#: Shared instruction pinning the per-type required fields of every
#: ``ModelSpecification.primitives`` entry. A ``box`` emitted without ``depth``
#: or ``height`` (or a ``cylinder``/``cone`` without ``diameter``/``height``) is
#: a fixable specification error the CAD backend rejects, so the Planner,
#: Editor and Reviser all state the contract explicitly and ask the LLM for
#: concrete numbers instead of nulls.
#:
#: Token budget: for the same reason as above, the per-type sizes, the
#: millimetre unit and the "min Z = 0" build-plate rule are left to the field
#: descriptions in the schema, which carry them verbatim. What stays here is
#: the instruction the schema cannot state (concrete numbers, never nulls) plus
#: the two hard-won behavioural facts: an ``extrude`` needs a real ``profile``
#: *and* a ``height``, and the cookie-cutter example that pins the JSON shape.
PRIMITIVE_DIMENSIONS_PROMPT = (
    "Every 'primitives' entry MUST carry the sizes its type needs (each field's "
    "description says which): give every required size a concrete number and "
    "never leave it missing or null. 'extrude' also needs 'profile' (3+ ordered "
    "{'x','y'} points) and 'height'. 'wall_thickness' makes a hollow wall (e.g. "
    "a cookie cutter) and 'round_radius' rounds it. "
    "Example: "
    "{'type': 'extrude', 'role': 'add', 'position': {'x': 0, 'y': 0, 'z': 0}, "
    "'profile': [{'x': -30, 'y': -30}, {'x': 30, 'y': -30}, {'x': 30, 'y': 30}, "
    "{'x': -30, 'y': 30}], 'height': 25, 'wall_thickness': 1.2} - never null "
    "'height' for an 'extrude'. "
)

#: Shared instruction teaching the Planner/Editor how to record missing values
#: instead of silently inventing them (docs/planner-clarification.md 2.).
#: ``clarifications`` is Planner-only data: it is part of ``PlannerPlan`` and
#: therefore never reaches the CAD backend (the strict terv.md 8. contract is
#: unchanged).
#:
#: Two short few-shot examples pin the exact JSON shape: small local models
#: (e.g. qwen2.5-coder:7b, mistral-nemo:12b) otherwise skip ``clarifications``
#: entirely and silently assume a generic part for an ambiguous request such as
#: "Keszits egy telefontartot." The examples stay bounded (one entry each) and
#: keep every existing rule intact. The "at most 8 entries" bound and the
#: per-field meanings now live in the schema (``maxItems``/``description``).
CLARIFICATION_PROMPT = (
    "For a value the user did not give that could change the part's "
    "function, do one of two things: (a) choose a realistic, printable default "
    "and record it in 'clarifications' with 'kind': 'assumed'; or (b) when a "
    "wrong guess could break the part (a critical dimension, mounting or "
    "interface), record it with 'kind': 'needs_user_input' and 'answer': ''. "
    "Use an empty list when nothing was missing. 'clarifications' is "
    "Planner-only data: never put it inside the specification and never send it "
    "to the CAD backend. "
    'Example A ("Make a phone stand.") - do NOT guess, add one top-level '
    '"clarifications" entry: {"question": "Which phone width in mm should it '
    'fit?", "answer": "", "kind": "needs_user_input", "field": "dimensions"}. '
    'Example B ("Make a 120 mm cable clip for a 6 mm cable.") - assume a '
    'printable wall, add one entry: {"question": "No wall thickness given; '
    'assuming a printable 3 mm wall.", "answer": "3", "kind": "assumed", '
    '"field": "wall_thickness"}. '
)

#: The Planner system prompt. Two budgets fight here, and the resolution is the
#: same rule as in :mod:`agents.spec`: state a fact **once**, in the place the
#: LLM reads it. The per-field contract (units, required sizes, the ``min Z =
#: 0`` build-plate rule, the primitive type list) ships in the JSON Schema in
#: the same system message, so restating it in prose here is pure cost against
#: a 4096-token context. Every behavioural instruction stays, because the
#: schema cannot express "what to do", only "what a field means".
PLANNER_SYSTEM_PROMPT = (
    "You are the Planner agent of an OpenSCAD 3D-printing workflow. "
    "Read the request and return ONE JSON object with two jobs: "
    "(1) the full 'specification'; "
    "(2) 'needs_research' and 'research_query' - whether real-world product or "
    "standard-part data (exact phone dimensions, ISO screw sizes) must be "
    "looked up first, plus a concise query. "
    "Describe the real geometry with the 'primitives' list: one or more "
    "primitives, each placed by its 'position'. "
    "Use role 'add' for material and role 'subtract' for holes and cutouts. "
    "Keep dimensions, angle, wall_thickness and mounting filled, with printable "
    "values. "
    "Use 'primitives': [] ONLY when the user asked for the built-in phone "
    "holder (a phone or tablet stand); otherwise you MUST fill 'primitives' "
    "with the geometry that builds it and never fall back to that template. "
    "For a flat, 2D-shaped object (a stamp, cookie cutter, silhouette or "
    "ornament) use a single 'extrude' whose 'profile' lists the {'x','y'} "
    "outline points in order. "
    + OPERATION_DIMENSIONS_PROMPT
    + PRIMITIVE_DIMENSIONS_PROMPT
    + CLARIFICATION_PROMPT
    + "Never emit OpenSCAD code, G-code or STL data - only the structured plan."
)


#: Structured Planner output.
#:
#: The nested ``specification`` is a fully validated
#: :class:`~agents.spec.ModelSpecification`, so the LLM and the CAD backend
#: keep their strict contract (terv.md 8. fejezet); the extra flags only drive
#: graph routing and never reach the CAD backend. That is developer context, so
#: it lives in this comment: a class docstring here is shipped to the model on
#: every call as ``$defs``/root ``description`` (see the ``agents.spec`` module
#: docstring for the full rationale).
class PlannerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    specification: ModelSpecification
    needs_research: bool = Field(
        default=False,
        description="True when product/standard data must be looked up first.",
    )
    research_query: str | None = Field(
        default=None,
        max_length=512,
        description="Lookup query for the Research agent (when needs_research).",
    )
    clarifications: list[Clarification] = Field(
        default_factory=list,
        max_length=8,
        description="Missing values assumed, or to ask the user about.",
    )


def split_clarifications(plan: PlannerPlan) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a plan's clarifications into ``(assumptions, blocking)``.

    ``assumed`` entries continue the run (the Planner guessed a value); the
    ``needs_user_input`` entries are the blocking questions that, under the
    ``ask`` policy, stop the workflow before any specification is finalised
    (docs/planner-clarification.md 2.).
    """
    assumptions = [item.model_dump() for item in plan.clarifications if item.kind == "assumed"]
    blocking = [
        item.model_dump() for item in plan.clarifications if item.kind == "needs_user_input"
    ]
    return assumptions, blocking


def clarification_state(
    plan: PlannerPlan,
    state: WorkflowState,
    *,
    image_used: bool,
    image_warning: str | None,
    max_attempts: int,
    stage: str,
) -> dict[str, Any] | None:
    """Build the partial state for a blocking clarification, or ``None``.

    Returns ``None`` when the plan asks no question (or the policy is not
    ``ask``), so the caller continues to the normal planned branch. There is
    deliberately no ``specification`` in the returned update: a run that stops
    for clarification never finalises one and never reaches the CAD agent.
    """
    blocking = [
        item.model_dump() for item in plan.clarifications if item.kind == "needs_user_input"
    ]
    if not blocking or state.get("clarify_policy") != "ask":
        return None

    assumptions, _ = split_clarifications(plan)
    history = append_history(state, f"{stage}: {len(blocking)} question(s) for the user")
    if image_warning:
        history = [*history, f"{stage}: {image_warning}"]
    return {
        "clarifications": blocking,
        "assumptions": assumptions,
        "needs_research": bool(plan.needs_research),
        "research_query": plan.research_query,
        "reference_image_used": image_used,
        "reference_image_warning": image_warning,
        "attempt": 0,
        "max_attempts": int(max_attempts),
        "status": "clarification",
        "error": None,
        "history": history,
    }


def coerce_plan(raw: Any) -> PlannerPlan:
    """Normalise a provider payload into a :class:`PlannerPlan`.

    Providers that support the nested schema return ``{"specification": ...}``.
    A provider that answers with a bare specification object (the plain
    ``ModelSpecification`` shape) is also accepted, with research disabled --
    this keeps the node robust across backends and trivial to fake in tests.
    """
    payload = dict(raw) if isinstance(raw, dict) else raw
    if isinstance(payload, dict) and "specification" not in payload and "object" in payload:
        return PlannerPlan(specification=ModelSpecification.model_validate(payload))
    return PlannerPlan.model_validate(payload)


#: How many individual schema violations the repair hint may list.
MAX_REPAIR_ERRORS = 3

#: Hard character cap on the repair hint, *including* its preamble.
#:
#: Why this is bounded: the re-ask repeats the question **and** the complaint, so
#: the second attempt of an already near-the-limit request is the one that is
#: guaranteed to overflow a small context. A Pydantic ``ValidationError`` for
#: this schema prints every violation, each with a ``loc``, a message, a type
#: and the offending input value -- measured 600-2500 characters, i.e. 150-600
#: tokens on top of a request that was already ~112 tokens over a 4096
#: context. The first lines carry the field, the constraint and the received
#: value, which is exactly what lets a model repair itself, so the hint keeps
#: those and drops the rest.
MAX_REPAIR_HINT_CHARS = 400

#: Preamble of :func:`_repair_hint` (kept identical so it stays a substring).
_REPAIR_PREAMBLE = (
    "Your previous answer was rejected by the schema. Fix exactly this problem "
    "and return the complete object again:\n"
)

#: The single complaint used when a repairable failure has no field-level
#: detail to quote -- an answer that was not JSON, or was JSON but not an
#: object. There is no field to name, so the hint names the only thing wrong:
#: the shape of the whole answer. One bullet, same character cap, same
#: mechanism as every other hint.
_REPAIR_SHAPE_COMPLAINT = "return ONE JSON object matching the schema, and nothing else"


def _repair_hint(exc: ValidationError | LLMResponseError) -> str:
    """Return a bounded repair hint for a schema-invalid Planner answer.

    *exc* is the answer the loop could not accept, in either of the two shapes
    that mean the same thing: a :class:`~agents.llm.LLMResponseError` the
    provider raised after validating the model's own answer (the common case on
    a small local model -- the response did not fit the schema), or a
    :class:`~pydantic.ValidationError` from :func:`coerce_plan` on a payload the
    provider accepted (e.g. a bare ``ModelSpecification`` it was lenient
    about). Both re-ask through this one hint, so both are bounded the same
    way and neither grows a second mechanism.

    The hint carries the problem and the offending location -- the two things a
    model needs to fix itself -- and nothing else. It is bounded twice over, on
    purpose:

    * by :data:`MAX_REPAIR_ERRORS`, because a model can only act on a couple of
      concrete complaints at once, and a long list makes it re-emit the same
      broken structure;
    * by :data:`MAX_REPAIR_HINT_CHARS`, because the hint is appended to the
      user prompt of a request that is already close to the context ceiling (see
      the constant's docstring).

    The full, unbounded ``str(exc)`` is still reported through
    :func:`~agents.graph.state.failure_state` on the last attempt, so the
    operator-facing error keeps every violation.
    """
    if isinstance(exc, LLMResponseError):
        # No ``validation_error`` means the complaint is about the whole answer
        # (invalid JSON / not an object), so there are no field errors to list.
        if exc.validation_error is None:
            return f"{_REPAIR_PREAMBLE}- (response): {_REPAIR_SHAPE_COMPLAINT}"
        exc = exc.validation_error
    errors = exc.errors()
    lines = [
        f"- {'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
        for error in errors[:MAX_REPAIR_ERRORS]
    ]
    hidden = len(errors) - len(lines)
    if hidden > 0:
        lines.append(f"- (+{hidden} more schema error(s))")
    body = "\n".join(lines)
    if len(body) > MAX_REPAIR_HINT_CHARS:
        # Cut on a line boundary so the model never sees half a constraint.
        body = body[:MAX_REPAIR_HINT_CHARS].rsplit("\n", 1)[0] + "\n- (truncated)"
    return f"{_REPAIR_PREAMBLE}{body}"


def make_planner_node(
    *,
    provider: LLMProvider,
    max_attempts: int,
    planner_retries: int = 2,
) -> Callable[[WorkflowState], dict[str, Any]]:
    """Build the ``planner`` graph node bound to *provider*.

    The returned callable is a LangGraph node: it takes the running state and
    returns a partial state update. All LLM access goes through the injected
    ``LLMProvider``, so tests pass a fake and no live model is required.

    ``planner_retries`` bounds how often an unusable answer is re-asked before
    the run fails. Small local models regularly emit a slightly malformed
    structure (e.g. an ``extrude`` whose profile has 2 points instead of 3);
    re-asking usually yields a valid answer, which beats failing the whole
    generation. Two shapes of "unusable" are re-asked, because the answer
    arrived and did not fit: a :class:`~agents.llm.LLMResponseError` the
    provider raised while validating the model's own answer -- the most frequent
    non-timeout failure of the 13-model round, 5 of its 39 cells
    (granite4.2:8b twice, ornith-1.5:9b twice, llava:13b once;
    docs/model-matrix-test-round.md footnotes 1/3) -- and a
    :class:`~pydantic.ValidationError` from :func:`coerce_plan` on a payload the
    provider accepted.

    A plain :class:`~agents.llm.LLMError` (timeout / transport / empty
    response / caller mistake) is never retried here: the request did not
    complete, and repeating a timeout would only double the wait for the same
    timeout.
    """

    def planner_node(state: WorkflowState) -> dict[str, Any]:
        # The Planner only ever asks for structured data. It must not be able
        # to produce a mesh: there is no mesh concept here at all.
        prompt = reference_prompt_for(provider, state)
        system_prompt = with_skills(PLANNER_SYSTEM_PROMPT, state.get("skills"))
        attempts = max(int(planner_retries), 1)
        repair_hint = ""
        for attempt in range(1, attempts + 1):
            # The re-ask repeats the *question* plus the concrete complaint, so
            # the model can actually repair itself. A blind repeat returns the
            # same malformed structure: measured, granite4.2 / deepseek-r1 always
            # emit an 'extrude' with an empty profile. Measured on the models
            # that made this loop reachable: the complaint repairs a violated
            # bound (qwen2.5-coder:7b took wall_thickness 0.2 -> 0.4) but not a
            # structurally missing field (granite4.2:8b and ornith-1.5:9b both
            # re-emitted a 0-point extrude profile), so expect the gain on
            # constraints, not on an absent profile.
            ask = f"{prompt}\n\n{repair_hint}" if repair_hint else prompt
            try:
                # The optional reference image (terv.md 27.) is attached here; the
                # helper falls back to a text-only call and never fails the run
                # because of vision.
                raw, image_used, image_warning = structured_with_reference_image(
                    provider,
                    ask,
                    PlannerPlan,
                    system=system_prompt,
                    images=reference_images(state),
                )
                plan = coerce_plan(raw)
                break
            except (LLMResponseError, ValidationError) as exc:
                # The answer arrived and did not fit, so asking again with the
                # concrete complaint is worth one more round trip. This is the
                # measured common case: without it the loop was unreachable for
                # a provider-raised schema mismatch, which is how granite4.2 /
                # llava actually failed, and the run died on the first answer.
                if attempt >= attempts:
                    return failure_state(
                        state,
                        stage="planner",
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                # Unusable structured output: re-ask with a *bounded* complaint.
                repair_hint = _repair_hint(exc)
                logger.warning(
                    "planner returned an invalid plan (attempt %d/%d): %s",
                    attempt,
                    attempts,
                    exc,
                )
            except LLMError as exc:
                # The request did not complete, so there is nothing to repair:
                # a timeout would only double the wait for the same timeout.
                return failure_state(
                    state,
                    stage="planner",
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
        else:  # pragma: no cover - the loop always breaks or returns
            return failure_state(
                state,
                stage="planner",
                error_type="ValidationError",
                message="planner returned no plan",
            )

        # Record the exchange so the UI can show what the planner was told and
        # what it answered (docs/vision-self-check.md 5.).
        llm_trace = [
            *(state.get("llm_trace") or []),
            {
                "agent": "planner",
                "attempt": 0,
                "system": system_prompt,
                "prompt": prompt,
                "response": plan.specification.model_dump() if isinstance(raw, dict) else {},
                "image_used": bool(image_used),
            },
        ]

        # Blocking questions under the "ask" policy stop the run before a
        # specification is finalised: no CAD, no STL (docs/planner-clarification.md 2.).
        clarification = clarification_state(
            plan,
            state,
            image_used=image_used,
            image_warning=image_warning,
            max_attempts=max_attempts,
            stage="planner",
        )
        if clarification is not None:
            return {**clarification, "llm_trace": llm_trace}

        assumptions, _blocking = split_clarifications(plan)
        specification = plan.specification.model_dump()
        # A "template" skill selects a built-in CAD generator (docs/skills.md 4.).
        object_kind = template_object_kind(state.get("skills"))
        if object_kind:
            specification["object"] = object_kind

        history = append_history(state, "planner: specification ready")
        if assumptions:
            history = [
                *history,
                f"planner: {len(assumptions)} feltételezés (review required)",
            ]
        if image_warning:
            history = [*history, f"planner: {image_warning}"]
        return {
            "specification": specification,
            "needs_research": bool(plan.needs_research),
            "research_query": plan.research_query,
            "research_sources": [],
            "research_used": False,
            "research_web_used": False,
            "clarifications": [],
            "assumptions": assumptions,
            "reference_image_used": image_used,
            "reference_image_warning": image_warning,
            "llm_trace": llm_trace,
            "attempt": 0,
            "max_attempts": int(max_attempts),
            "status": "planned",
            "error": None,
            "history": history,
        }

    return planner_node
