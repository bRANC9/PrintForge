# 3D annotáció-alapú modell szerkesztés (Visual Prompts)

Ez a dokumentum a **felületen jelölt vizuális parancsok** (annotation + több
polygon kijelölése) alapján történő modellszerkesztés tervét és a sávok közötti
**fagyasztott interfészeket** írja le. A cél, hogy a felhasználó a 3D nézetben
rákattintson a modell felületére (vagy kijelöljön több polygon-t), szöveges
utasítást adjon hozzá, és az AI abból **validált parametrikus feature-öket**
készítsen, amit az OpenSCAD backend renderel.

> Alapelv változatlan: az LLM **soha nem ír OpenSCAD kódot és nem generál
> mesht**. Az LLM strukturált adatot ad (`ModelSpecification`), a geometriát az
> `OpenSCADBackend` állítja elő a sandboxban (lásd `terv.md` 7., 8., 20.).
> A "freeform mesh editing" (tetszőleges push/pull/extrude) **nem cél**.

## 1. Adatfolyam

```text
Felhasználó a viewerben:
  - annotációs mód be
  - felületre kattint -> pont + felületi normál (eredeti STL koordinátában)
  - több polygon kijelölése -> centroid + átlag-normál + méret
  - utasítás beírása annotációnként
        |
        v
POST /api/v1/versions/{base_version_id}/annotations/
  { prompt, annotations: [ ... ] }
        |
        v
designs.services.create_annotation_edit(...)
  -> agents.tasks.run_agent_workflow.delay(
         project_id, prompt, user_id,
         base_version_id=..., annotations=[...])
        |
        v
LangGraph:  START -> editor -> [research] -> cad -> validate -> END
  editor:  base_specification + annotations + prompt
           -> LLM (structured, PlannerPlan) -> UJ ModelSpecification
              (base dims megtartva + `operations` lista)
  cad:     CADBackend.generate(spec) -> OpenSCAD (base + operations)
  validate: biztonsági + geometriai ellenőrzés -> STL
        |
        v
új ModelVersion (parent_version = base_version,
                 annotations_json = annotations)
        |
        v
UI a projekt verziólistáját pollozza, megjeleníti az új verziót
```

## 2. Annotáció payload (frontend -> API)

Koordináták **mm-ben, az eredeti STL / OpenSCAD világkoordinátarendszerében**
(a viewer a megjelenítéshez eltolja a geometriát; a kliens ezt visszainvertálja,
lásd 6. pont).

```json
{
  "prompt": "A kijelölt peremre tegyél egy 4 mm-es lyukat, és kerekítsd le a sarkot.",
  "annotations": [
    {
      "id": "c1",
      "kind": "point",
      "point": [35.0, 12.5, 8.0],
      "normal": [0.0, 0.0, 1.0],
      "faces": [],
      "instruction": "4 mm-es átmenő lyuk"
    },
    {
      "id": "c2",
      "kind": "region",
      "point": [10.0, 5.0, 20.0],
      "normal": [1.0, 0.0, 0.0],
      "faces": [120, 121, 122, 340, 341],
      "region": {
        "centroid": [9.8, 5.1, 20.0],
        "normal": [1.0, 0.0, 0.0],
        "size": [12.0, 4.0, 0.0],
        "count": 5
      },
      "instruction": "3 mm mély zseb a kijelölt területen"
    }
  ]
}
```

- `faces`: kiválasztott háromszög-indexek (STL nem-indexelt triangle soup;
  opcionális, max ~5000).
- `region`: a kijelölés összegzése; a szerver/LLM ezt használja, nem a nyers
  triangle listát.
- `instruction`: annotációnkénti parancs. A `prompt` a teljes szerkesztés.

## 3. Fagyasztott szerződések

### 3.1 `agents/spec.py` (llm-provider)

Új Pydantic modellek, majd a `ModelSpecification` új mezője:

```python
class Vec3(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float
    z: float

class EditOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["hole", "pocket", "boss", "slot", "cut", "add"]
    origin: Vec3          # mm, modell-koordináta
    normal: Vec3          # a művelet tengelye (nem kell egységvektor; a CAD normalizál)
    depth: float          # >= 0.4, <= 200  (vágásmélység / boss magasság)
    width: float | None = None
    height: float | None = None
    diameter: float | None = None
    length: float | None = None
    label: str = ""       # rövid emberi címke, opcionális

class ModelSpecification(BaseModel):
    ...
    operations: list[EditOperation] = Field(default_factory=list)
```

Jelentés:
- `hole`: henger (d = `diameter`) kivonása a normál irányában, `depth` mélyen.
- `pocket` / `cut`: téglatest (`width` x `height` x `depth`) kivonása.
- `boss`: henger (`diameter`) hozzáadása `depth` magasan.
- `slot`: kapszula (`length` hossz, `diameter` átmérő) kivonása.
- `add`: téglatest (`width` x `height` x `depth`) hozzáadása.

A CAD backend minden értéket újra kényszer-konvertál és korlátoz (lásd 3.3),
ezért az LLM által adott érték **nem megbízható**.

### 3.2 `designs/models.py` (core-model)

A `ModelVersion` két új mezője:

```python
parent_version = models.ForeignKey(
    "self", null=True, blank=True, on_delete=models.SET_NULL,
    related_name="derived_versions",
)
annotations_json = models.JSONField(default=list, blank=True)
```

- `parent_version`: melyik verziót szerkesztette (szerkesztési lánc).
- `annotations_json`: a szerkesztés bemenete (reprodukálhatóság, provenance).

Új migráció: `designs/0003_...`.

### 3.3 `designs/cad/openscad.py` (cad-worker)

A `render_scad`/`OpenSCADBackend.generate` a base holder mellé rendereli a
`specification["operations"]` listát:

- Minden művelethez normalizált lokális bázis (`u`, `v`, `n`) Pythonban
  számolva, `multmatrix`-szal pozicionálva (OpenSCAD, sor-major, a 4. oszlop a
  transzláció).
- `difference() { union() { base; add/boss } hole/pocket/cut/slot }`.
- Új, korlátozott parse: `parse_operations(specification) -> list[RenderOperation]`,
  minden szám `_coerce_float`-tal, a `normal` nem lehet nulla.
- `validate()` a base paraméterek mellett az `operations` hibáit is adja vissza.
- A generált SCAD továbbra is **önálló** (nincs `import`/`include`) és
  sandbox-safe.

### 3.4 `api/` (api-dev)

- Új action a `ModelVersionViewSet`-en:
  `POST /api/v1/versions/{id}/annotations/` (MEMBER+ a workspace-ben).
- `AnnotationEditSerializer` (validálja a `prompt` + `annotations` shape-et).
- `ModelVersionSerializer` új, read-only mezői: `parent_version`,
  `annotations_json`.
- `designs.services.create_annotation_edit(*, base_version, prompt,
  annotations, created_by)`:
  - **nem** hoz létre verziót (azt az agent task teszi, elkerülve a duplázást);
  - lazy importtal enqueue-olja a `agents.tasks.run_agent_workflow`-ot;
  - broker hiba -> `RenderEnqueueError` (503).
- Válasz: `202 {"queued": true, "parent_version": <id>}`; a UI a
  `/projects/{id}/versions/` listát pollozza, és a `parent_version == base`
  verziót várja.

### 3.5 `agents/graph/**` + `agents/tasks.py` (agent-orchestrator)

- `WorkflowState` új mezői: `edit_mode: bool`, `base_specification:
  dict[str, Any]`, `annotations: list[dict[str, Any]]`.
- Új node: `make_editor_node(provider, max_attempts)` a
  `agents/graph/editor.py`-ban. A `PlannerPlan` sémát és a
  `structured_with_reference_image` helpert használja; a system prompt
  elmagyarázza az annotáció- és művelet-sémát. Visszaad: `specification`,
  `needs_research`, `research_query`, `status="planned"`, stb.
- `workflow.build_workflow`: `START` feltételesen `editor` vagy `planner`
  (`route_entry`), a többi él változatlan.
- `run_workflow(...)` új, opcionális kwargs: `base_specification`,
  `annotations`; ezek töltik a state-et (`edit_mode = bool(annotations)`).
- `agents/tasks.run_agent_workflow(..., base_version_id=None,
  annotations=None)`: a base verziót betölti (project-hez tartozik), a
  `specification_json`-ját `base_specification`-ként adja át, és a mentett új
  verzióra beírja a `parent_version` + `annotations_json` mezőket.
- A `_summarise_state` rögzítse az `annotation_count`-ot (raw bytes/JSON nélkül).

### 3.6 Frontend (viewer-frontend)

- Viewer API bővítés (`window.PrintForgeViewer`): annotációs mód,
  `getAnnotations()`, `clearAnnotations()`; a konténeren `viewer-annotation-*`
  CustomEvent-ek (buborékolnak, mint a `viewer-state`).
- Raycast a meshre; a találati pont visszainvertálása az eredeti STL
  koordinátába (`point - viewerOffset`), a normál változatlan.
- Több polygon: kattintás hozzáad, shift+kattintás hozzáad, üresre kattintás
  töröl; a kijelölt háromszögek kiemelése overlay mesh/wireframe-tel.
- Annotációs pin + normál nyíl megjelenítése.
- `project.js`: `annotations` állapot, lista panel (utasítás mezőnként,
  törlés), "Küldés AI-nak" gomb -> `POST .../annotations/`, majd pollozás.
- `viewer.html`: annotációs toolbar + panel; az `x-stl-viewer` config kapja meg
  az `annotationMode` flaget.
- `api-extras.js`: `endpoints.annotationEdit(versionId)`.

## 4. Érintett fájlok sávonként

| Sáv | Fájlok |
| --- | --- |
| core-model | `backend/designs/models.py`, `backend/designs/migrations/0003_*` |
| llm-provider | `backend/agents/spec.py` (+ `agents/llm/tests/test_spec.py`) |
| cad-worker | `backend/designs/cad/openscad.py`, `backend/designs/cad/tests/**` |
| api-dev | `backend/api/views.py`, `backend/api/serializers.py`, `backend/designs/services.py` |
| agent-orchestrator | `backend/agents/graph/editor.py`, `state.py`, `workflow.py`, `agents/tasks.py`, `agents/graph/tests/**` |
| viewer-frontend | `frontend/static/js/viewer.js`, `project.js`, `api-extras.js`, `frontend/templates/designs/viewer.html` |
| qa-tests | `tests/**` |

## 5. Biztonság

- Az LLM továbbra sem ad kódot; csak `ModelSpecification` + `operations`.
- Minden műveleti szám kényszer-konvertált és korlátozott a CAD backendben.
- A `normal` nem lehet nulla; a bázis ortonormált.
- A generált SCAD nem tartalmazhat `import()`/`surface()`/`include`/`use`
  konstrukciót (a meglévő `validate_scad_source` ezt ellenőrzi).
- Az annotáció a workspace permission alá esik (ugyanaz, mint a verzióké).

## 6. Koordináta-mapping (viewer)

A viewer a geometriát megjelenítéskor eltolja. A `setGeometry`-ben alkalmazott
transzláció:

```text
offset = (-center.x, -center.y + rawSize.y / 2, -center.z)
```

A raycast világkoordinátája a már eltolt geometrián van, ezért:

```text
modelPoint = hit.point - offset      // eredeti STL / OpenSCAD koordináta
modelNormal = hit.face.normal        // a normálok eltolás-invariánsak
```

Ez a transzformáció a viewer belső felelőssége; a backend már a modell
koordinátáit kapja.

## 7. Nem cél

- Freeform mesh editing / push-pull / tetszőleges polygon extrude.
- Kép -> mesh rekonstrukció.
- Új CAD kernel.
- Az annotáció mint geometriai igazság: ez **szándék**, amit az LLM strukturált
  műveletté fordít, és a CAD backend validál.
