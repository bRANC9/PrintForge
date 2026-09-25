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
  node a `deps.review_provider or deps.provider`-t és a
  `deps.preview_renderer`-t kapja.
- `workflow.WorkflowDeps` új mezők:
  `preview_renderer: Callable = render_stl_preview` és
  `review_provider: LLMProvider | None = None`.
- `build_dependencies` (tasks.py): `reviser=make_llm_reviser(provider)`, és a
  review külön vision modellt kap, ha a `ollama_vision_model` runtime beállítás
  ki van töltve (`get_provider(model=vision_model)`); üresen a fő providerre esik
  vissza. Így a tervezés koder modellen, a self-check vision modellen futhat.

### 4.1 Külön vision modell (opcionális)

- Beállítás: `OLLAMA_VISION_MODEL` env, illetve a Beállítások oldalon a
  `ollama_vision_model` runtime override (DB → env → default `""`).
- Üres érték = a review a fő `OLLAMA_MODEL`-t használja; ha az nem vision-képes,
  a review kimarad (warning).
- 8 GB VRAM-on az Ollama a két 7B modellt egymás után tölti be (swap) — ez
  elvárt; a self-check csak a generálás végén fut.

### 4.2 Determinisztikus alak-guard (gyenge vision modell ellen)

- A `review` node a vision verdikt mellé egy determinisztikus ellenőrzést is
  futtat (`geometry_missing_issue`): ha a `specification`-ben **nincs
  `primitives`**, a CAD backend csak a beépített holder sablont vagy egy
  szintetizált dobozt tud renderelni, soha nem a kért alakot.
- Ha a kérés nem holder-jellegű (nincs `holder`/`tartó`/`stand`/`phone`/
  `telefon`/`tablet` token a promptban) és a spec primitív nélküli, a guard
  felülírja a `matches: true` verdiktet, és ugyanúgy `retry`-t indít, mint egy
  valódi mismatch. Így egy gyenge, mindent jóváhagyó vision modell (pl.
  `llava:7b`) sem engedi át a fallback alkatrészt.
- Holder-kérésnél a guard szándékosan nem szól, mert ott a beépített sablon a
  helyes. A guard csak egy korlátos retry-t kér, modellt sosem blokkol.

## 5. Perzisztálás (agent-orchestrator, `agents/tasks.py`)

- `_persist_version`: ha van `preview_image`, menti a
  `ModelVersion.preview_image` mezőbe (a storage backendbe), és a
  `validation_json`-ba bekerül a `vision_review` összegzése (issues/summary,
  **nem** a nyers bájtok).
- A nyers input/output is bekerül: `validation_json["vision_trace"]` =
  `{system, prompt, response, guard_override}` (`_vision_trace_summary`, a
  prompt `VISION_TRACE_PROMPT_CHARS` = 4000 karakterre vágva). Ez a
  `AgentRun.state_json`-ba is bekerül, így a UI-on a „Vision önellenőrzés"
  panelen látható, **mit kapott és mit válaszolt** a vision modell.
- `_summarise_state`: `vision_used`, `vision_review` (issues/summary), és
  `vision_trace` — bájtok nélkül.

## 6. API (api-dev)

- `designs/services.py`: `ARTIFACT_KINDS` bővítés `"preview"`-vel,
  `"preview" -> "preview_image"` mező, content type `image/png`, hogy a UI
  le tudja tölteni a `/api/v1/versions/{id}/artifact/preview/` végponton.

## 7. Nem cél

- GPU/OpenGL renderer; a preview egyszerű, determinisztikus szoftver-render.
- A vision review nem blokkolhatja a generálást; hiba → warning + kész modell.
