# Planner visszakérdezés és feltételezések (user-in-the-loop)

A Planner eddig minden hiányzó értéket csendben, printable defaulttal pótolt.
Ez a dokumentum két, egy mechanizmuson alapuló viselkedést vezet be:

1. **Visszakérdezés** – ha a kérés kétértelmű és a tipp kockázatos, a run
   megáll, és a kérdéseket felteszi a felhasználónak.
2. **Feltételezés** – ha a tipp biztonságos, a run továbbmegy, de a Planner
   **feljegyzi, hogy ez egy saját maga megválaszolt kérdés volt**, és
   felülvizsgálatra jelöli.

Alapelv: a generálás **sosem blokkol**, csak ha a felhasználó kifejezetten
kéri („Kérdezz vissza, ha bizonytalan” / `clarify_policy="ask"`). A feltételezés
nem hiba, hanem auditálható provenance.

## 1. Adatmodell (`agents/spec.py`, llm-provider)

```python
class Clarification(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(max_length=300)
    answer: str = Field(default="", max_length=600)   # a tipp; üres, ha a user válaszol
    kind: Literal["assumed", "needs_user_input"] = "assumed"
    field: str = ""                                    # pl. "dimensions.width"

class PlannerPlan(BaseModel):
    ...
    clarifications: list[Clarification] = Field(default_factory=list, max_length=8)
```

Jelentés:
- `kind="assumed"`: a Planner tippelt (`answer` kitöltve), a run megy tovább.
- `kind="needs_user_input"`: a Planner **nem** tippel (`answer` üres); `ask`
  policy esetén a run megáll.

A `clarifications` a `PlannerPlan` része, nem a `ModelSpecification`-é, ezért
soha nem jut el a CAD backendhez (a strict LLM↔CAD szerződés változatlan,
terv.md 8. fejezet).

## 2. Planner node (`agents/graph/planner.py`)

- A `PLANNER_SYSTEM_PROMPT` kiegészül: ha egy érték hiányzik,
  - **vagy** adjon életszerű, printable defaultot **és** vegye fel
    `kind="assumed"`-ként (a tipp az `answer`-ben, indoklás a `question`-ben);
  - **vagy**, ha a tipp a funkciót eltörhetné (kritikus méret, csatlakozás),
    `kind="needs_user_input"`-et adjon `answer=""`-rel.
- A node a `PlannerPlan`-ből kiszámolja:
  - `assumptions = [c for c in clarifications if c.kind == "assumed"]`
  - `blocking = [c for c in clarifications if c.kind == "needs_user_input"]`
- Ha `blocking` **és** `state["clarify_policy"] == "ask"`:
  `status="clarification"`, `clarifications=blocking`, nincs `specification`
  véglegesítés, nincs CAD.
- Egyébként: `status="planned"`, `assumptions` bekerül a state-be, a
  history kap egy `planner: N feltételezés (review required)` bejegyzést.

## 3. Workflow (`agents/graph/state.py`, `workflow.py`)

- `WorkflowState` új mezői:
  - `clarify_policy: Literal["ask", "assume"]`
  - `clarifications: list[dict[str, Any]]`
  - `assumptions: list[dict[str, Any]]`
- `route_after_planner`: `if status in {"failed", "clarification"}: return END`.
- `run_workflow(...)` új opcionális kwarg: `clarify_policy: str = "assume"`.
- Az `editor` node ugyanezt a sémát használja (a `PlannerPlan`-t adja vissza),
  ezért a visszakérdezés vizuális szerkesztésnél is működik.

Ez **nem** LangGraph checkpoint/interrupt: a run lezár `status="clarification"`-nel,
a user válaszol, és **új run** indul a válaszokkal (a meglévő async, pollozós
modellbe illik, nincs szükség checkpointerre).

## 4. Perzisztálás (`agents/tasks.py`)

- `run_agent_workflow` terminális ágai:
  - `status="clarification"` → **nem** készül `ModelVersion`. A kérdések a
    `AgentRun.state_json`-ba kerülnek; a run `done`, de verzió nélkül.
  - egyébként változatlan.
- `_summarise_state` rögzítse: `clarification_count`, `clarifications`
    (kérdés + `field`, `answer` nélkül a blockingnál), `assumption_count`,
    `assumptions` (kérdés + tipp + `field`).
- `_persist_version`: ha `assumptions` nem üres, a verzió
  `validation_json`-jába bekerül:

  ```json
  { "assumptions": [ { "field": "...", "question": "...", "answer": "..." } ],
    "review_required": true }
  ```

## 5. API (`api/`)

- Generálás payload (`VersionCreateSerializer`) új opcionális mező:
  `clarify` (`"ask"` | `"assume"`, default `"assume"`).
- Új action a `ModelVersionViewSet`-en / projekt-szintű run-kezelés:
  `POST /api/v1/runs/{id}/clarifications/` body
  `{"answers": [{"field": "...", "answer": "..."}]}` →
  `designs.services.answer_clarifications(run, answers, created_by)`:
  - a run `state_json`-ából kiolvassa az eredeti promptot,
  - `run_agent_workflow.delay(prompt + answers, ...)` új runként,
  - válasz `202 {"queued": true, "parent_run": <id>}`.
- `AgentRunSerializer` read-only: `clarifications`, `assumptions`.
- `ModelVersionSerializer` read-only: `assumptions` (a `validation_json`-ból).

## 6. Frontend (`project.js`, `viewer.html`)

- Ha a legutóbbi run `clarifications`-t tartalmaz (és `status` nem `done`
  verzióval): form a kérdésekkel (mezőnként input), „Válaszok elküldése” →
  `POST .../clarifications/`, majd a verziólista pollozása.
- Ha a kiválasztott verzió `assumptions`-t tartalmaz: borostyán panel
  *„AI feltételezések – érdemes felülvizsgálni”*, mezőnként a tippelt értékkel.
- Generálásnál opcionális checkbox: „Kérdezz vissza, ha bizonytalan”
  (`clarify=ask`).

## 7. Nem cél

- Nem blokkolunk automatikusan; a `ask` policy explicit kérés.
- Nem LangGraph interrupt/checkpoint (nincs félbehagyott, perzisztált gráf).
- A feltételezés nem írja felül a user által megadott értéket (terv.md 29.).
