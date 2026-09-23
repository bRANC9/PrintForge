# Skill-ek (újrahasznosítható generálási receptek)

A rendszer jelenleg egyetlen beégetett sablont ismer (a telefontartó), és a
Planner minden promptot nulláról értelmez. Ez a dokumentum a **Skill** fogalmat
vezeti be: névvel ellátott, **felhasználó által létrehozható** recept, amely egy
tárgy-osztály generálását segíti (pl. „süti kinyomó”, „telefontartó”,
„falikonzol”). A skill **mankó** a rendszernek: vagy a rendszer választja ki, vagy
a felhasználó adja hozzá előre.

Alapelv változatlan: a skill **strukturált adat + szöveges iránymutatás**, soha
nem OpenSCAD kód. Az LLM továbbra is `ModelSpecification`-t ad, a geometriát a
CAD backend állítja elő a sandboxban (terv.md 7., 8., 20. fejezet).

## 1. Mi egy skill

Két fajta, egy sémán:

- **`guidance` skill** – szöveges iránymutatás + strukturált default-ok +
  gépi ellenőrzések egy tárgy-osztályra. Pl. a *süti kinyomó*: „vékony fal
  (1.2 mm), 2D körvonal, 20–30 mm fal­magasság, lekerekített felső él, opcionális
  fogantyú a tetején”.
- **`template` skill** – egy beégetett CAD-generátort nevez meg
  (`template_key`), pl. a meglévő telefontartó. Így a mai „beépített sablon”
  **egy skill lesz**, nem külön kódút.

Ez egységesíti a mai beégetett telefontartót és az új recepteket.

## 2. Adatmodell (új `skills` app, core-model)

```python
class Skill(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    description = models.TextField(blank=True)

    kind = models.CharField(max_length=16, choices=[("guidance","Guidance"),("template","Template")],
                            default="guidance")
    template_key = models.CharField(max_length=64, blank=True, default="")  # pl. "phone_holder"
    object_kind = models.CharField(max_length=64, blank=True, default="")   # pl. "cookie_cutter"

    # Guidance: a Planner/Editor promptba kerülő iránymutatás (bounded).
    guidance = models.TextField(blank=True)
    # Strukturált default-ok (a ModelSpecification egy részhalmaza, validálva).
    defaults_json = models.JSONField(default=dict, blank=True)
    # Gépi ellenőrzések, amiket a Validator kényszerít (nem csak prompt).
    constraints_json = models.JSONField(default=dict, blank=True)
    # Kulcsszavak/tagek az automatikus illesztéshez.
    tags = models.ManyToManyField("projects.Tag", blank=True, related_name="skills")

    workspace = models.ForeignKey("workspaces.Workspace", null=True, blank=True,
                                  on_delete=models.CASCADE, related_name="skills")
    is_builtin = models.BooleanField(default=False)   # seed, read-only
    is_public = models.BooleanField(default=False)     # közösségi (Phase 8 minta)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="created_skills")
    created_at / updated_at
```

- `workspace=None` + `is_builtin=True` → globális seed skill.
- `workspace=<ws>` → workspace-szintű, privát.
- Kapcsolódás projekthez: `Project.skills = M2M(Skill, blank=True)` (tartós
  hozzárendelés), illetve per-run `skill_ids` (egyszeri).

Seed skillek (migráció/data migration): `phone_holder` (template), `cookie_cutter`
(guidance), esetleg `wall_bracket`.

## 3. Kiválasztás

Két út, determinisztikus sorrendben:

1. **Manuális** – a user a projektnél (tartósan) vagy a generálásnál (egyszer)
   bejelöli a skilleket. Ez mindig elsőbbséget élvez.
2. **Auto** – ha nincs manuális, a `skills.services.select_skills(prompt, object_kind)`:
   - először **tag/`object_kind` egyezés** (magyarázható, olcsó),
   - ha nincs találat és `RAG_ENABLED`, **szemantikus** illesztés a
     skill-leírások embeddingjeivel (a meglévő pgvector infrastruktúra).

A kiválasztott skillek listája + a választás módja (`manual`/`auto`) bekerül a
state-be és a verzió provenance-ába.

## 4. Injektálás (llm-provider + agent-orchestrator)

- A Planner/Editor prompt kap egy **„Aktív skillek”** blokkot: név, leírás,
  `guidance`, `defaults_json` (mint javasolt értékek). Az LLM **továbbra is**
  `ModelSpecification`-t ad vissza.
- A `constraints_json`-t **nem** csak promptoljuk: a **Validator** ellenőrzi
  (pl. `min_wall_mm`, `must_rest_on_plate`, `require_primitives`,
  `forbid_operations`), és strukturált hibát ad, ha sérül — így a skill
  kikényszerített, nem „sugallt”.
- `template` skill esetén a Planner a `template_key`-t állítja be a
  specifikációban (pl. `object="phone_holder"`), és a CAD backend a beégetett
  generátort futtatja (a mai `primitives: []` viselkedés általánosítása).

## 5. CAD következmény: 2D `extrude` primitív (cad-worker)

A *süti kinyomó* (vékony falú, 2D körvonal, magas fal) a jelenlegi
box/cylinder/sphere/cone primitívekkel **nem fejezhető ki**. Ezért a skill-ek
egy CAD-bővítést igényelnek:

```python
class Primitive(BaseModel):
    type: Literal["box", "cylinder", "sphere", "cone", "extrude"]
    profile: list[Vec2] = []      # 2D körvonal pontok (mm), XZ sík
    wall_thickness: float | None  # >0 -> üreges fal (külső - belső)
    round_radius: float | None    # opcionális felső él lekerekítés
    height: float | None          # extrusion magasság
    ...
```

- Renderelés: `linear_extrude(height) polygon(points)`; fal esetén
  `difference()` a belül eltolt (Pythonban számolt) körvonallal; `round_radius`
  → `offset(r=...)`/`minkowski` (kicsi, korlátozott).
- A `profile` **inline pontlista** — soha fájl, nincs `import()`/`surface()`
  (a sandbox szabály sérülne). A meglévő `validate_scad_source` ezt ellenőrzi.
- Új bounds: pontszám (pl. ≤ 256), koordináta-tartomány a primitív boundokkal.

**Döntés:** az `extrude`/`profile` primitív **a skill-ekkel együtt** készül el
(a *süti kinyomó* és hasonló skillek enélkül nem működnek). Ez a természetes
következő CAD-lépés a mostani primitívek után; a `docs/cad-primitives.md`
„nem cél” listáját ez a pont felülírja.

## 6. Szerzői UI + jogosultságok (viewer-frontend, api-dev)

- **Skill lista** (`/skills/`): szűrés workspace/global/public, keresés.
- **Skill szerkesztő**: név, leírás, `guidance`, `defaults`, `constraints`,
  tagek, típus. A `defaults`/`constraints` mezőket űrlap + JSON nézet.
- Bárki létrehozhat workspace-skillt; a built-in seedek read-only-k (ADMIN
  kezeli). Közösségi megosztás a Phase 8 mintát követi (`is_public`, licenc).
- A generálás/új elem űrlapon: „Skillek” választó (manuális), és egy
  „Automatikus skill-választás” kapcsoló.
- REST: `skills` CRUD ViewSet + `POST /api/v1/projects/{id}/generate/`
  kiegészítés `skill_ids`-szal.

## 7. Provenance

- A verzió `validation_json`-jába bekerül:
  `{"skills": [{"slug": "...", "selection": "manual|auto"}], "skill_warnings": [...]}`.
- A verzió oldal mutatja: „Használt skillek: Süti kinyomó (auto)”.

## 8. Döntések

- **Skill reprezentáció**: egységes `guidance` + `template` egy sémán; a
  beégetett telefontartó is `template` skill lesz.
- **Auto-választás**: tag/`object_kind` egyezés elsőként, embedding fallback
  csak ha nincs találat (magyarázható, olcsó).
- **`defaults` ereje**: prompt-javaslat; a `constraints`-t a Validator
  kényszeríti. (A `ModelSpecification` mezői kötelezőek, ezért strukturált
  default-merge-re nincs szükség.)
- **CAD `extrude`**: a skill-ekkel együtt, egy feladatként (5. pont).
- **Elnevezés**: a modell/API `Project` marad; a UI „Elem”-et mondhat.

## 9. Érintett fájlok sávonként

| Sáv | Fájlok |
| --- | --- |
| core-model | `backend/skills/models.py`, `backend/skills/migrations/**`, seed data migration, `projects/models.py` (`Project.skills` M2M) |
| api-dev | `backend/skills/services.py`, `backend/api/views.py`, `backend/api/serializers.py` |
| llm-provider | `backend/agents/spec.py` (`extrude` primitív), `backend/agents/llm/tests/**` |
| agent-orchestrator | `backend/agents/graph/planner.py`, `editor.py`, `validator.py`, `state.py` (skill-injektálás + constraint-ellenőrzés) |
| cad-worker | `backend/designs/cad/openscad.py` (`extrude`/`profile` renderelés), `backend/designs/cad/tests/**` |
| viewer-frontend | `frontend/templates/skills/**`, `frontend/static/js/skills.js`, generálás/új-elem űrlap |
| qa-tests | `tests/**` |

## 10. Nem cél

- Skill = kód. A skill soha nem OpenSCAD/Python kód, csak adat.
- Freeform mesh / tetszőleges CSG a skilltől.
- Skill-piactér/verziózás az első körben (közösségi megosztás később).
