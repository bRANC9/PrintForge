# CAD primitívek – prompt-függő geometria

A jelenlegi `OpenSCADBackend` egyetlen fix **telefontartó**-templátot renderel,
ezért bármely prompt gyakorlatilag ugyanazt a modellt adja. Ez a dokumentum a
specifikáció azon bővítését írja le, amellyel az LLM **valódi, primitív-alapú
geometriát** írhat le (kód nélkül), és a CAD backend azt CSG-vel rendereli.

Alapelv változatlan: az LLM csak strukturált adatot ad
(`ModelSpecification`), soha nem OpenSCAD kódot, és a CAD backend minden
számot kényszer-konvertál + korlátoz (sandbox-safe).

## 1. `agents/spec.py` bővítés (llm-provider)

```python
class Primitive(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    type: Literal["box", "cylinder", "sphere", "cone"]
    role: Literal["add", "subtract"] = "add"
    position: Vec3 = Vec3(0, 0, 0)      # a primitív KÖZÉPPONTJA, mm
    rotation: Vec3 = Vec3(0, 0, 0)      # fok, XYZ sorrend
    width: float | None = None          # box X méret
    depth: float | None = None          # box Y méret
    height: float | None = None         # box Z / cylinder|cone magasság
    diameter: float | None = None       # cylinder|sphere|cone átmérő
    label: str = ""                     # <= 120 karakter

class ModelSpecification(BaseModel):
    ...
    primitives: list[Primitive] = Field(default_factory=list)
```

Korlátok: méret `0.4 .. 1000`, pozíció/rotáció `-1000 .. 1000` (rotáció fokban,
`-360 .. 360`), max 64 primitív.

Jelentés:
- `box`: `width` x `depth` x `height` téglatest.
- `cylinder`: `diameter` átmérő, `height` magasság.
- `sphere`: `diameter` átmérő.
- `cone`: `diameter` alapprofátmérő, `height`, csúcsban 0.
- `role="add"` anyagot ad hozzá; `role="subtract"` kivon.

Konvenció: a `position` a primitív középpontja; a tárgy a tálcán álljon
(min Z = 0) – ezt a planner prompt kéri a modelltől.

## 2. `designs/cad/openscad.py` (cad-worker)

- `parse_primitives(specification) -> list[RenderPrimitive]` (dataclass),
  minden szám `_coerce_float`-tal, ismeretlen `type`/`role` → `SpecificationError`.
- Renderelés:
  - `box`: `translate(p) rotate(r) translate([-w/2,-d/2,-h/2]) cube([w,d,h])`
  - `cylinder`: `translate(p) rotate(r) cylinder(d=diameter, h=height, center=true)`
  - `sphere`: `translate(p) sphere(d=diameter)`
  - `cone`: `translate(p) rotate(r) cylinder(d1=diameter, d2=0, h=height, center=true)`
  - Alap: `difference() { union() { <add primitívek> } <subtract primitívek> }`.
- Ha `primitives` üres **vagy hiányzik** → a mai viselkedés (base telefontartó
  templát + `operations`), bájt-azonos kimenettel (backward compatible).
- Ha `primitives` nem üres: a bázis a primitív-CSG; a vizuális annotációs
  `operations` ezután ugyanúgy `union`/`difference` rétegként kerül rá.
- `validate()` a primitív-parse hibákat is blokkoló hibaként adja vissza.
- Továbbra sem generálható `import()`/`surface()`/`include`/`use`.

## 3. Planner/Editor prompt (agent-orchestrator)

- A planner/editor system prompt kapja meg a primitívek leírását és a
  középpont-konvenciót.
- Ha a kért tárgy lényegében telefontartó, a modell a beépített templátot
  használhatja (`primitives: []`); egyébként **primitívekből** állítsa össze.
- A `dimensions`/`angle`/`wall_thickness`/`mounting` mezők továbbra is
  kitöltendők (kompatibilitás), de primitívek esetén a geometriát a
  `primitives` adja.

## 4. Frontend (viewer-frontend)

- Prompt→agent generálásnál a UI **csak akkor** váltson a legutóbbi verzióra,
  ha valóban **új** verzió született (id > a beküldés előtti maximum), illetve
  ha az agent-run `done`. Ha a run `failed`, a kiválasztás maradjon a régi
  verzió, és a hiba jelenjen meg hangsúlyosan (ne tűnjön úgy, hogy „ugyanaz a
  modell jött ki”).

## 5. Nem cél

- Az LLM OpenSCAD kódot írjon.
- Freeform mesh editing.
- Tetszőleges CSG (extrude/lathe/hull) – egyelőre box/cylinder/sphere/cone.
