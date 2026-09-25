# Verzió-history: újragenerálás és szerkesztés

A verziólista jelenleg csak megtekinthető: a felhasználó kiválaszt egy verziót,
és vagy az annotációs szerkesztést indítja, vagy új promptot ad. Ez a
dokumentum a history-elemekre két műveletet vezet be:

1. **Újragenerálás** – ugyanaz a prompt (vagy annak szerkesztett változata)
   újrafut, és egy **új, a forrásverzióból származó** verzió készül.
2. **Szerkesztés** – a history-elem promptja (és/vagy `specification_json`-ja)
   módosítható, majd abból generálunk.

Alapelv: a verziók **immutable-ek** (terv.md 11. fejezet). A szerkesztés sosem
írja át a meglévő verziót, hanem **új verziót** hoz létre, amelynek
`parent_version`-je a szerkesztett forrás. Így a teljes lánc auditálható.

## 1. Adatmodell (`designs/models.py`, core-model)

- `parent_version` **már létezik** (docs/visual-editing.md 3.2): az a verzió,
  amiből ez származik. Az újragenerálás ugyanezt használja.
- Új, opcionális mező a UI/provenance megkülönböztetéséhez:

  ```python
  class ModelVersionOrigin(models.TextChoices):
      GENERATE = "generate"        # friss prompt -> agent
      ANNOTATION = "annotation"    # vizuális szerkesztés
      REGENERATE = "regenerate"    # history újragenerálás
      MANUAL = "manual"            # kézzel adott specification_json

  origin = models.CharField(max_length=16, choices=..., default="generate")
  ```

  Ha a mező nem kerül be, az eset kikövetkeztethető
  (`annotations_json` nem üres → `annotation`; `parent_version` + üres
  `annotations_json` → `regenerate`). Az explicit mező tisztább, de nem
  blokkoló.
- Új migráció: `designs/0004_...` (core-model).

## 2. Service (`designs/services.py`, api-dev)

```python
def regenerate_version(
    *,
    base_version: ModelVersion,
    prompt: str | None = None,          # None -> base_version.prompt
    specification: dict | None = None,  # ha kézzel szerkesztett spec-et renderelünk
    created_by: User | None = None,
) -> None:
    ...
```

- Ha `specification` nincs: enqueue `run_agent_workflow.delay(..., prompt=prompt
  or base_version.prompt, base_version_id=base_version.pk, regenerate=True)`.
  A task a mentett verzióra beírja `parent_version=base_version`-t,
  `annotations_json=[]`-t és `origin="regenerate"`-et.
- Ha `specification` megvan: `create_next_version(..., specification=...,
  parent_version=base_version, origin="manual")` + `start_render(version)`
  (a meglévő manuális render-út újrahasznosítása).
- `create_next_version` új opcionális kwargs: `parent_version=None`,
  `origin="generate"`.
- Broker-hiba → `RenderEnqueueError` (503), mint a többi enqueue útnál.

## 3. API (`api/`, api-dev)

- Új action a `ModelVersionViewSet`-en:
  `POST /api/v1/versions/{id}/regenerate/` (MEMBER+ a workspace-ben).

  Body:

  ```json
  { "prompt": "opcionális, szerkesztett prompt",
    "specification_json": { "...": "opcionális, kézi spec" } }
  ```

  - `prompt` nélkül → tiszta újragenerálás.
  - `prompt`-tal → szerkesztés + újragenerálás.
  - `specification_json`-nal → manuális, származtatott render.
  - Válasz `202 {"queued": true, "parent_version": <id>}`; a UI a
    `/projects/{id}/versions/` listát pollozza, és a
    `parent_version == base` verziót várja (mint az annotációs folyam).
- `ModelVersionSerializer` read-only mezői: `parent_version`, `origin`
  (a meglévő `annotations_json` mellett).
- Kézi törlés: a viewset a `DestroyModelMixin`-t is használja, így
  `DELETE /api/v1/versions/{id}/}` töröl egy verziót (MEMBER+ a workspace-ben).
  Ha a verzióhoz nyomtatási feladat (`print_jobs`) vagy tányérbeli elem
  (`plate_items`) tartozik, a FK `CASCADE` miatt a kérés `409 Conflict`-ot
  ad — előbb ezeket kell törölni. `designs.services.delete_version` a
  sort és a tárolt artifact fájlokat (scad/stl/glb/preview/reference)
  best-effort törli; a származtatott verziók megmaradnak
  (`parent_version` `SET_NULL`).

## 4. Frontend (`project.js`, `viewer.html`, viewer-frontend)

- A verziólista minden eleme kap három gombot:
  - **„Újragenerálás”** → `POST .../regenerate/` üres body-val, pollozás.
  - **„Szerkesztés”** → a verzió `prompt`-jával előtöltött inline szerkesztő;
    mentés → `POST .../regenerate/ {prompt}`.
  - **„Törlés”** → inline megerősítés („Igen, törlés” / „Mégse"), majd
    `DELETE .../versions/{id}/`; siker esetén a lista frissül, és ha a
    törölt verzió volt kiválasztva, a UI a következő verzióra vált.
- A kiválasztott verzió fejléce mutassa a láncot: `v3 ← v2 (regenerate)` /
  `(annotation)`.
- A pollozás és a „csak akkor váltok verzióra, ha valóban új született”
  logika ugyanaz, mint az annotációs folyamnál (docs/cad-primitives.md 4.).

## 5. Kapcsolat a visszakérdezéssel

Az újragenerált run ugyanazt a `run_agent_workflow`-t futtatja, ezért
automatikusan örökli a Planner visszakérdezést/feltételezéseket
(docs/planner-clarification.md): a `status="clarification"` kérdések és a
`review_required` feltételezések a history-vezérelt folyamnál is megjelennek.

## 6. Nem cél

- Meglévő verzió in-place módosítása (a verziók immutable-ek).
- Elágazás-kezelő fa/merge UI – a `parent_version` lánc lineáris marad.
- A fájlok (scad/stl) kézi szerkesztése.
- Verzió **visszaállítása** törölés után; a history csak előre épül.
