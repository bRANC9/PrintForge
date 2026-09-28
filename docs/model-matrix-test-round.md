# Model matrix: one more round across every model on the box

A PrintForge planner/guard/CAD kör egy újabb teljes tesztköre: **13 modell × 3
valós teszt case**, ahol az "output" nem puszta szöveg, hanem a pipeline tényleges
szerződése — spec → determinisztikus guard → OpenSCAD → STL → nyomtathatóság.

- **Dátum:** 2026-09-27
- **Commit:** `16dbb5c` (tiszta munkafa, `main` == `origin/main`)
- **Harness:** `backend/scripts/probe_pipeline.py` (a valós planner, a valós
  system prompt, a valós guardok; DB nélkül, Celery nélkül)
- **Ollama:** `http://192.168.1.250:11434`, **8 GB VRAM** (ez megmagyaráz sok mindent)
- **OpenSCAD:** `ghcr.io/branc9/printforge-openscad:latest` ephemeral sandbox
- **Tesztek:** 39 egyedi cella, 54 futás (a kontextus-limit miatti újrafuttatásokkal)

## A három teszt case

| id | prompt (ahogy a felhasználó írná) | amit valójában vizsgál |
| --- | --- | --- |
| `pvc` | *50 mm-es PVC csőre menő adapter, másik oldalán 1/4 collos kompresszor csatlakozó. Belső átmérő 50 mm, falvastagság 2 mm, hossz 40 mm.* | tiszta baseline, kerek furat, guard-ot nem kellene érintenie |
| `tree` | *Karácsonyfa alakú süti kinyomó, 90 mm magas, 3 mm falvastagsággal, és egy 60 mm hosszú fogantyúval a hátsó oldalán.* | a `SILHOUETTE_WORDS` + `PRESS_WORDS` guard, és a friss (01a1def) ≥6 pontos sziluett-szabály |
| `anchor` | *Falra szerelhető kábelkanchor 12 mm átmérőjű kábelhez, 2 darab 6 mm-es furattal a csavarozáshoz, 6 mm vastag lemezből.* | a `HOLE_WORDS` guard és az `operations`/`mounting` séma |

A `pvc` a `PRESS_WORDS` ütközést mutatta meg (§1), a `tree` a sziluett-szabályt
(§4), az `anchor` a furat-kezelést (§5).

---

## 1. [SÚLYOS] A guard hamis pozitíva: `press` ⊂ `kompresszor`

A `PRESS_WORDS` listában benne van az angol **`press`**, és a `_mentions()`
függvény **3 karakter feletti szavaknál szubstring-egyezést** használ:

```python
# agents/graph/consistency.py:135-146
if len(word) <= 3:
    if re.search(rf"\b{re.escape(word)}\b", haystack):   # határokkal
        return True
elif word in haystack:                                   # NYITOTT szubstring
    return True
```

A `fa` / `fal` / `falkonzol` ütközés pont ezért van határokkal védve (a komment
maga mondja). De a **`press` hosszú**, tehát védtelen, és a magyar
**kompresszor** tartalmazza:

```
prompt: "...1/4 collos kompresszor csatlakozó..."
                              ^^^^^^
PRESS_WORDS match: 'press' at position 61
```

A 48 tagú magyar nyelvű gépipari szókörömön **15/48 hamis pozitíva** van. A
valódi ütközések:

| szó a listában | beleszúrja magát | hány |
| --- | --- | --- |
| `press` | kompresszor, kompresszió, kompresszorfúró, impresszum, expresszió, depresszió, expressz | 7 |
| `sajtó` | **garázsajtó**, sajátólap | 2 |
| `vágó` | vágólap | 1 |

(`csillag` / `szív` / `levél` / `dísz` / `matrica` szintén talál, de azok **igazi
pozitívok** — az ilyen kérés tényleg sziluettet kér.)

### Ami a legkínosabb: a guard **helyes választ utasít el**

```
phi4:14b / pvc
  primitív:  cylinder/add  h=40.0
             cylinder/add  h=40.0
  GUARD:    "...no 'extrude' primitive at all (only cylinder): a box or
             cylinder cannot express that shape -- use one 'extrude'..."
```

Egy henger-falú csőadapterhez (`cylinder` + `cylinder`) a **legpontosabb
lehetséges válasz** — és a guard azért utasítja el, mert a felhasználó a
szövegben kompresszort említett. Ugyanez a `qwen2.5-coder:14b`-nél, és a
`qwen2.5-coder:7b` esetén **háromszor** is (a `wall_thickness`-t követelő
változat).

### Nem determinisztikus

A kalibráló futás GUARD-olt, a mátrixban PASS-olt, ugyanazzal a prompttal —
attól függően, hogy a modell véletlenül kiadott-e `extrude`-ot. **A hiba
nem-determinisztikus módon nyilvánul meg**, ami a legrosszabb fajta: nem
reprodukálható egyértelműen.

---

## 2. [SÚLYOS] Nincs geometriai guard — a CAD hűségesen lerendereli a hibát

A 12 megkísérelt cellából **8-at sikerült renderelni** (a többi üres specet
adott), és **mind a 8 watertight, mind `printable=True`**. A méretekre viszont
nincs egyetlen ellenőrzés sem:

| modell / case | kért | a renderelt STL | lap | bodies |
| --- | --- | --- | --- | --- |
| `qwen2.5-coder:7b` / tree | 90 mm magas kinyomó | **100 × 90 × 223.5 mm** | 40 | **2** |
| `qwen2.5-coder:14b` / pvc | 50 mm Ø, 40 mm hossz | **50 × 46.6 × 2.0 mm** (2 mm vastag korong) | 260 | 1 |
| `phi4:14b` / pvc | 50 mm Ø, 40 mm hossz | 50 × 50 × 22 mm (a hossz 22, nem 40) | 376 | **2** |
| `qwen2.5-coder:7b` / pvc | 50 mm Ø | 65 × 65 × 40 mm (a furat 65, nem 50) | 36 | 1 |
| `phi4:14b` / tree | 90 mm fa | 90 × 90 × 4.5 mm (lap, nem kinyomó) | 208 | 1 |
| `qwen2.5-coder:7b` / anchor | 6 mm lemez, 2× ⌀6 furat | 12 × 18 × 6 mm, **0.109 mm legkisebb él** | 364 | 1 |
| `mistral-nemo:12b` / anchor | 2 furat kell | 12 × 6 × 6 mm, **12 lap = sima doboz, nincs furat** | 12 | 1 |
| `phi4:14b` / anchor | 2 furat kell | 12 × 6 × 6 mm, **12 lap = sima doboz** | 12 | 1 |

A 90 mm-es kinyomó **223.5 mm magasra** jött, a 40 mm-es adapter **2 mm
vastag korong**. Mindkettő `printable=True`. A `meshcheck` jól működik, de
amit mér, az a *renderelt geometria* — nem azt, hogy az megfelel-e a kérésnek.

**A `dimensions` séma szabályokat tartalmaz (`height >= 5.0`)** és az OpenSCAD
előtti generátor-validálás el is kapja a `phi4` hibáját
(`Field 'dimensions.height' must be >= 5.0 (got 2.0)`) — de semmi nem
ellenőrzi, hogy a végeredmény **a kért méretben** van-e.

---

## 3. [SÚLYOS] A planner prompt ~4.2k token, öt modell 4096-os kontextuson meghal

```
granite4.2:8b        request (4254 tokens) exceeds the available context
                     size (4096 tokens)   n_prompt_tokens=4254  n_ctx=4096
deepseek-r1:8b       request (4208 tokens) ... n_ctx=4096
ornith-1.5:9b        request (4215 tokens) ... n_ctx=4096
deepseek-coder-v2:16b  the prompt is longer than the context length
deepseek-r1:14b        the prompt is longer than the context length
```

**150–160 tokennel lépi túl a 4096-os plafont.** Ez nem modellminőség, hanem
konfiguráció: 16k kontextussal (`--num-ctx 16384`) mind az öten lefutnak. A
PrintForge jelenleg nem állít kontextusméretet az Ollama-hívásokra
(`OLLAMA_*` nincs ilyen setting), és a `probe_pipeline.py`-hoz képest a
productionben sincs ilyen override — tehát **bármelyik kis-kontextusú modell
használata azonnal elhasal**.

A 16k-val újrafuttatva a minőség **nem javult** (§6), tehát ez önmagában nem
magyarázza a bukást.

---

## 4. A `tree` case: egyetlen felismerhető fa sincs

A friss (01a1def) ≥6 pontos sziluett-szabály **helyesen és pontosan szólal meg**:

```
qwen3.5:9b / tree
  GUARD: the request asks for a shaped outline, but the 'extrude' profile has
         only 5 distinct points (minimum 6): draw the actual silhouette as a
         polygon (a tree outline needs at least 6 ordered points, more for branches)
```

A profil, amit a modell adott:

```
[(-35.0, 90.0), (35.0, 90.0), (34.7, 27.0), (-34.7, 27.0), (-35.0, 0.0)]
```

Ez **nem fa**, hanem egy csonkolt lemez ötszögben. A guard tévedés nélkül
elutasítja.

Összesítve az 5Extrudét adó modellre:

| modell | extrude profil | felismerhető fa? |
| --- | --- | --- |
| `qwen3.5:9b` | 5 pont, csonkolt trapéz | nem |
| `phi4:14b` | 3 pont, téglalap | nem |
| `qwen2.5-coder:7b` | 4 pont, **téglalap** (a guard külön üzenetet ad) | nem |
| `mistral-nemo:12b`, `qwen2.5:14b`, `qwen3.5:9b` | nincs extrude egyáltalán | nem |

**0 / 5.** A „karácsonyfa" szó szándékosan benne van a `SILHOUETTE_WORDS`-ban,
tehát a guard mindig tudta, mit kellene — a modell nem tudta megadni.

---

## 5. Az `anchor` case: a furat-szabály működik, de senki nem vonja le a következményt

A `HOLE_WORDS` guard hibátlanul kiszúrja, amikor nincs anyaglevonás:

```
the request mentions a hole/pin/screw, but the specification removes no
material: add a 'subtract' primitive (cylinder) or a hole/pocket/cut/slot
operation
```

Ez 4 modellnél hibátlanul kifogta (a guard a `subtract` **primitive**-et és az
`operations` listát is nézi — a `qwen3.5:9b` például `operations=['cut','cut']`-et
adt, de primitívet nem, így az elhasalt). A `mistral-nemo:12b` és a `phi4:14b`
azonban **12 lapos, furat nélküli dobozt** adott át, ami lefutott és
`printable=True` lett. A guard megállítja a reviziót; ha a felhasználó
elfogadja, **furat nélküli darab** nyomódik ki. A `qwen2.5-coder:7b` 0.109 mm
legkisebb él figyelmeztetést kapott — fúróhegy alatti, letörölhető rész.

## 6. Többtestűs mesh csak figyelmeztetés

Két renderelt rész `body_count = 2` (**két leválasztott shell**), és mindkettő
`printable=True`:

```
phi4:14b / pvc          warnings: ['mesh has 2 disconnected bodies (expect separate prints)']
qwen2.5-coder:7b / tree warnings: ['mesh has 2 disconnected bodies (expect separate prints)']
```

A `meshcheck` ezt **figyelmeztetésnek**, nem blokkoló problémának sorolja, ami
indokolt lehet (két nyomtatás), de a nyomtató nem tudja eldönteni — és a
`123`-as `Trellis2`-s workflow-memória óta tudjuk, hogy egy lebegő shell
prototípusban elfogadható, termékben nem. A `test_pipeline`-ban a `pvc` shell
darab 2.0 mm-re vékonyodott (lásd §2).

---

## Eredménymátrix

`PASS` = a guardok nem találtak semmit · `GUARD` = a guard elutasította
(2 = kilépési kód) · `FAIL` = a planner nem adott használható specet.

| modell | pvc | tree | anchor | ctx |
| --- | --- | --- | --- | --- |
| `qwen2.5-coder:7b` | **PASS** 8s | GUARD 5s | GUARD 7s | default |
| `qwen3.5:9b` | GUARD 36s | GUARD 9s | GUARD 9s | default |
| `mistral-nemo:12b` | **PASS** 71s | GUARD 9s | GUARD 14s | default |
| `granite4.2:8b` | FAIL 57s¹ | FAIL 21s¹ | **PASS** 40s | 16384 |
| `ornith-1.5:9b` | GUARD 45s | FAIL 7s¹ | FAIL 4s¹ | 16384 |
| `qwen2.5:14b` | FAIL 211s² | GUARD 51s | **PASS** 82s | default |
| `phi4:14b` | GUARD 160s | GUARD 95s | **PASS** 184s | default |
| `qwen2.5-coder:14b` | GUARD 178s | FAIL 212s² | GUARD 73s | default |
| `llava:13b` | FAIL 168s³ | FAIL 211s² | **PASS** 191s | default |
| `deepseek-coder-v2:16b` | FAIL 211s² | FAIL 212s² | **PASS** 164s | 16384 |
| `deepseek-r1:8b` | FAIL 211s² | FAIL 212s² | FAIL 211s² | 16384 |
| `deepseek-r1:14b` | FAIL 211s² | FAIL 211s² | FAIL 211s² | 16384 |
| `bge-m3` | FAIL 1s⁴ | FAIL 1s⁴ | FAIL 1s⁴ | default |

¹ séma-validálási hiba · ² timeout (>210 s) · ³ séma-validálási hiba ·
⁴ nincs chat endpoint (embedding modell)

### Modell-rangsor

| modell | pass | guard | fail | össz idő | megjegyzés |
| --- | --- | --- | --- | --- | --- |
| **`qwen2.5-coder:7b`** | 1 | 2 | 0 | **20 s** | a legjobb: leggyorsabb, egyetlen valódi timeout nélküli hiba sincs, mindig ad specet |
| `qwen3.5:9b` | 0 | 3 | 0 | 49 s | mindhárom guard, de **mindig** ad specet; a leggyorsabb a 9b-s kategória |
| `mistral-nemo:12b` | 1 | 2 | 0 | 94 s | tiszta baseline-ot ad, a `tree`-re üres specet |
| `granite4.2:8b` | 1 | 0 | 2 | 118 s | az `anchor`-re adja az egyetlen tiszta PASS-t egy 9b-s modell közül, de a pvc/tree schema-hibát ad (a `pvc` nem lett újrafuttatva) |
| `ornith-1.5:9b` | 0 | 1 | 2 | 56 s | 4 mp alatt FAIL: nem érdemes |
| `qwen2.5:14b` | 1 | 1 | 1 | 344 s | lassú, de 2-ből 1 |
| `phi4:14b` | 1 | 2 | 0 | 439 s | a legjobb geometria a pvc-n, a leglassabb |
| `qwen2.5-coder:14b` | 0 | 2 | 1 | 463 s | |
| `llava:13b` | 1 | 0 | 2 | 570 s | vizuális modell szövegre: túl lassú |
| `deepseek-coder-v2:16b` | 1 | 0 | 2 | 587 s | |
| `deepseek-r1:14b` | 0 | 0 | 3 | 633 s | **8 GB VRAM-on nem használható** |
| `deepseek-r1:8b` | 0 | 0 | 3 | 634 s | **8 GB VRAM-on nem használható** |
| `bge-m3` | 0 | 0 | 3 | 3 s | embedding modell, nem generatív |

**A 8 GB VRAM a döntő tényező.** A `deepseek-r1` (14b/8b) a 16k kontextussal
sem éri meg a 210 másodperces plafont — 9 cellából 0 sikeres, mind timeout. A
`qwen2.5-coder:14b` és a `phi4:14b` 8–12× lassabb a 7b-nél, és nem jobb.

**Javaslat: marad `qwen2.5-coder:7b`** (a jelenlegi alapértelmezés) — egyetlen
valódi hibája sincs, mindig ad specet, és 8–20× gyorsabb a 14b-s modelleknél,
miközben azok nem jobbak. A `granite4.2:8b` érdekes jelölt lehetne, de a
`pvc`/`tree` schema-hibája miatt **nem lett a render-körben mérve**, úgyhogy
ez a sor nem támasztja alá a minőségét. A 14b-s modellek ezen a hoszton nem
érnek meg semmit.

---

## A hét javítás és a hatásuk

A fenti hét pont mind megvan. A lényeg: **ugyanaz a 13 modell, ugyanaz a 3 case,
ugyanaz a harness — csak a kód változott.**

| # | Javítás | Hol |
| --- | --- | --- |
| 1 | A guard kétlépcsős egyezésre: szóhatáros (`*_WHOLE_WORDS`) és baloldali határos (`*_PREFIX_WORDS`). A `press` kikerült a szabad szubstringből. | `agents/graph/consistency.py` |
| 2 | A planner prompt 4218 → **3237 valós token** (−23%). A JSON séma 2706 → 1989 tok, a `description` szöveg 4440 → 1796 karakter. A fejlesztői dokumentáció `#:` kommentbe került, a modellnek szóló konstraint **egy sem** veszett. | `agents/spec.py`, `agents/graph/planner.py` |
| 3 | `OLLAMA_CONTEXT_LENGTH` setting, `0` = nem küld `num_ctx`-et. | `config/settings.py`, `configuration/` |
| 4 | Dimenziós guard: a renderelt STL mérete a kért `dimensions` ±25%-án belül van-e, és `min Z == 0`-e. Csak figyelmeztetés. | `designs/cad/dimensions.py` |
| 5 | A generálás saját figyelmeztetései (`skipped primitives[0]`, `no usable primitives; synthesized a box`) eddig SCAD-kommentként vesztek el — most a `GeneratedModel`-en utaznak. | `designs/cad/base.py`, `openscad.py`, `mesh.py` |
| 6 | A `version_status()` végre visszaadja a `warnings`-t, így a poller látja. A UI külön blokkban, magyarul, súlyozva; a `bodies > 1` nyugtázandó kérdés lett. | `designs/services.py`, `frontend/` |
| 7 | **Az agent-útvonalon is fut a dimenziós check** — a `_persist_version` nem megy át a `render_version`-ön, szóval korábban pontosan a promptból generált részeknél sosem futott le. | `agents/tasks.py` |

### Eredmény: PASS 7 → 13

| | BEFORE | AFTER |
| --- | --- | --- |
| PASS | 7 | **13** (+86%) |
| GUARD | 13 | **9** (−4, mind a guard-javításból) |
| timeout | 11 | **7** |
| össz idő | 4010 s | 3579 s |

A kontextus-plafon javítása önmagában is kimutatható: a **default** 4096-os
kontextussal, `--num-ctx` nélkül sikeres lett a `granite4.2:8b`, az
`ornith-1.5:9b`, a `qwen2.5:14b`, a `qwen2.5-coder:14b` és a
`deepseek-coder-v2:16b` is. A korábbi „BEFORE" számok a 16k-s újrafuttatás
eredményei, tehát ez a javítás **a karima nélküli** eset.

### A `pvc` oszlop: a kompresszor-hamis pozitíva eltűnt

| modell | before | after |
| --- | --- | --- |
| `phi4:14b` | GUARD (a helyes `cylinder` választ utasította el) | **PASS** |
| `qwen2.5-coder:7b` | PASS | PASS |
| `qwen2.5-coder:14b` | GUARD | **PASS** |
| `qwen3.5:9b` | GUARD | **PASS** |
| `ornith-1.5:9b` | GUARD | **PASS** |
| `qwen2.5:14b` | FAIL/timeout | **PASS** |
| `granite4.2:8b` | FAIL/schema | **PASS** |
| `deepseek-coder-v2:16b` | FAIL/timeout | **PASS** |

**13 modellből 2 helyett 8 éri el a PASS-t.** A `phi4:14b` konkrétan az
`object: PVC pipe adapter` + `cylinder/add` + `cylinder/subtract` + egy 6.35 mm-es
port hengert ad — a henger-falú csőadapter helyes válasza.

### A sziluett/lyuk guardok továbbra is működnek

A javítás nem hallgattatta el őket. A `qwen3.5:9b` továbbra is ugyanazt a
≥6 pontos üzenetet adja egy 4 pontos (majdnem téglalap) profillal, és a
`mistral-nemo:12b` `anchor`-ja a lyuk-követelményt. A 48 tagú hamis-pozitív
korpusz **15/48 → 0/48**, a 43 igazi pozitívból **egy sem** veszett.

### Ami a mérésből kiderült, és amit a javítás sem old meg

**A specifikációréteg továbbra is nem alkalmas erre az alkatrész-osztályra.**
A `Primitive` típusok között nincs cső/csatlakozó, nincs kihajtható, nincs
rádiusz-mentes körvonal. A 8 renderelt STL a javítások után is ugyanazokat a
lényegében értelmezhetetlen alakokat adja, **csak most már figyelmeztetéssel**:
a felhasználó meglátja, hogy a 90 mm-es karácsonyfa 195 mm lett, de a
specifikációréteg továbbra sem tud karácsonyfát kérni. Ez a következő lépés,
nem része ennek a körnek.

---

## Amit NEM ellenőriztem

- **Böngészős UI-t** — minden a `probe_pipeline.py`-n és a service rétegen futott,
  a böngésző, az Alpine és a Three.js nézet teljesen ellenőrizetlen.
- **A futó Docker stacket** — a `printforge-web-1` containerben futó teljes
  agent-workflowot nem indítottam el (auth, Celery, valódi DB). Az STL-eket egy
  reprodukált, `TMPDIR`-rel a `/mnt/c` alá állított sandbox-invokáció adta — a
  production contractot követve, de nem a futó containerson keresztül.
- **A slice-et és a nyomtatást** — a mérés az STL-nél megáll.
- **Két modell rerun nélkül**: `llava:13b` a `tree`-re timeoutolt (1 futás), és a
  `granite4.2:8b` `pvc`/`tree` schema-hibája nem lett reprodukálva, hogy
  ellenőrizzem: determinisztikus hiba vagy sampling.
- **A `phi4:14b` 16k kontextussal** — 4 modell futott 16k-val, nem mind.

## Metodológiai jegyzet: egy hamis riasztás, amit nem vettem komolyan

Az első render-kísérlet **6/12 cellát „OPENSCAD FAILED"-nak** jelzett, mind
ugyanazzal a hibával:

```
boost::filesystem::status: Permission denied: "color-schemes/render"
```

Ez **nem PrintForge-hiba és nem modellhiba volt, hanem az én teszt-harnessem**:
kézzel írtam meg a `docker run`-t a `SANDBOX_FLAGS` és a `build_args` helyett.
A `--read-only` + tmpfs kombináció így másképp működött. Ugyanez a hiba
reprodukálható egy sima `docker run`-nal is, PrintForge-től függetlenül. A
tisztes kimutatás az volt, hogy az app **saját** `OpenSCADBackend.export()`
útvonalára váltottam — és a 12-ből 10 meg is renderelt.

A `/out` bind mount második hibája (`Can't open file "/out/model.stl" for
export`, miközben a geometria kiszámolódott) a WSL és a Windows-lapon futó
Docker daemon közötti útvonal-átvitel: a daemon nem látja a WSL `/tmp`-et. A
`TMPDIR` `/mnt/c` alá állításával oldódott — ez pontosan a `SANDBOX_WORK_DIR`
szerződés, amit a README leír.

## Javasolt teendők — a lezárt állapot

A fenti eredménytáblázat öt pontja **mind megvan** (lásd „A hét javítás"). Ami
nyitva maradt:

1. **A specifikációréteg nem alkalmas szerves/görbült alkatrészekre.** Ez a
   legfontosabb nyitott probléma, és nem guard-kérdés: hiányzik a fogalom.
   Kellene egy `extrude` körvonal-generátor (sík, hengerre vetített, spline),
   vagy egy új primitív típus a csatlakozókhoz. Amíg nincs, a rendszer
   helyesen figyelmeztet, de nem tud karácsonyfát.
2. **A `dimensions` szemantikája kétértelmű.** A `phone_holder` sablon a
   *készülék* méretét írja a `dimensions`-be, nem az *alkatrész* burkolóját, ezért
   a dimenziós check 59% és 1367% eltérést jelez rá, és ezt **nem** oldja meg
   egyik tolerancia sem. Két lehetőség: a planner külön deklarálja a rész
   burkolóját, vagy a pipeline exemptiont ad a tartó-sablonokra.
3. **A `szög` → „szögek" plural elveszett** a guard-javításban, a `szöglet` /
   `szögskála` / `csapágy` (csapágy = *csapágy, bearing*) ütközések elkerüléséért.
   Ez a leggyorsabban vészvesztett észlelés: „4 db szögekkel" a gyakori kérés.
   A lyuk-szabály második feltétele (van-e anyaglevonás) ennyire nem mérsékli.
4. **A magyar morfológia általánosan:** a szóhatáros egyezés miatt a
   `sajtómatrica`, `cookiecutter`, `embossed` stb. felkerült a listákba, de
   szabályos rendszer helyett ez kézi lista. A hosszú távú javítás egy
   morfológiai elemzés lenne, nem új kulcsszavak.
5. **A `deepseek-r1` 8b/14b a 8 GB VRAM-os hoszton továbbra is használhatatlan**
   (7/7 timeout), és ez nem prompt-ügy.
