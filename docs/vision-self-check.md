# Vision önellenőrzés (self-check)

A primitív-alapú CAD után a pipeline képes **visszaellenőrizni magát**: a
legenerált STL-ből előnézeti képet renderel, és ha a kiválasztott LLM
**vision-képes**, megkéri, hogy hasonlítsa össze a képet az eredeti kéréssel.
Ha nem egyezik és van még kísérlet, egy LLM-alapú reviser javítja a
specifikációt, és a CAD újrafut.

Ez a `terv.md` 27. (vision) és 6. (retry loop) fejezetére épül; a vision
továbbra is **opcionális és lekérdezett**, sosem kötelező.

## 1. Pipeline

```text
... -> cad -> validate
validate --(valid)--> review
validate --(retry)--> cad
validate --(failed)--> END
review --(retry, van kísérlet)--> cad
review --(END)--> END
```

- `review` node: renderel egy preview PNG-t az `stl_bytes`-ből, majd ha a
  provider `supports_vision()`, elküldi a vision modellnek.
- Ha nincs vision / nincs STL / a review meghiúsul: **kihagyja** (warning a
  history-ban), a run `done` marad.
- Ha vision szerint nem egyezik és `attempt < max_attempts`: a review
  hibáit beírja a `validation.errors`-be, `status="retry"`, és a CAD node
  LLM-reviserrel javít.

## 2. Preview renderer (`designs/cad/preview.py`, cad-worker)

```python
class PreviewRenderError(ValueError): ...

def render_stl_preview(
    stl_bytes: bytes,
    *,
    size: int = 512,
    views: tuple[str, ...] = ("iso", "front", "top"),
) -> bytes:  # PNG
```

- trimesh (már függőség) az STL parse-hoz, Pillow (már függőség) a
  raszterizáláshoz; painter-algoritmus + flat shading; nincs shell/network.
- Üres/hibás STL → `PreviewRenderError`.
- Lazy import a függvényen belül, hogy a CAD backend importja ne lassuljon.

## 3. Structured review (`agents/spec.py`, llm-provider)

```python
class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    matches: bool
    issues: list[str] = Field(default_factory=list, max_length=20)
    summary: str = Field(default="", max_length=500)
```

## 4. Review node + reviser (agent-orchestrator)

- `WorkflowState` új mezői: `preview_image: bytes` (in-process only),
  `vision_review: dict[str, Any]`, `vision_used: bool`.
- `agents/graph/review.py`:
  `make_review_node(provider, *, render_preview=render_stl_preview)`.
  - Nincs `stl_bytes` → skip.
  - `render_preview` hibája → warning, skip.
  - `not provider.supports_vision()` → `vision_review={"skipped": True,
    "reason": "no_vision"}`, `vision_used=False`, `status` marad `"done"`.
  - Egyébként `structured(prompt, ReviewResult, images=[preview],
    system=REVIEW_SYSTEM_PROMPT)`; `LLMError` esetén warning + skip (nem
    bukik el a run).
- `route_after_review(state)`: `"cad"` ha a review szerint nem egyezik ÉS
  `attempt < max_attempts`, egyébként `END`.
- `agents/graph/reviser.py`: `make_llm_reviser(provider)` → a CAD node
  `SpecReviser`-je; a hibákból + aktuális specből strukturáltan javított
  `ModelSpecification`-t ad (`structured(prompt, ModelSpecification)`);
  hiba esetén `None` (a CAD változatlan specet próbál).
- `workflow.build_workflow`: `validate` valid éle `review`-ra vált; a review
  node a `deps.provider`-t és a `deps.preview_renderer`-t kapja.
- `workflow.WorkflowDeps` új mező: `preview_renderer: Callable = render_stl_preview`.
- `build_dependencies` (tasks.py): `reviser=make_llm_reviser(provider)`.

## 5. Perzisztálás (agent-orchestrator, `agents/tasks.py`)

- `_persist_version`: ha van `preview_image`, menti a
  `ModelVersion.preview_image` mezőbe (a storage backendbe), és a
  `validation_json`-ba bekerül a `vision_review` összegzése (issues/summary,
  **nem** a nyers bájtok).
- `_summarise_state`: `vision_used`, `vision_review` (issues/summary) — bájtok
  nélkül.

## 6. API (api-dev)

- `designs/services.py`: `ARTIFACT_KINDS` bővítés `"preview"`-vel,
  `"preview" -> "preview_image"` mező, content type `image/png`, hogy a UI
  le tudja tölteni a `/api/v1/versions/{id}/artifact/preview/` végponton.

## 7. Nem cél

- GPU/OpenGL renderer; a preview egyszerű, determinisztikus szoftver-render.
- A vision review nem blokkolhatja a generálást; hiba → warning + kész modell.
