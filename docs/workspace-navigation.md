# Workspace-first navigáció (főoldal = workspace lista)

Jelenleg a főoldal (`/`) egy statikus bemutatkozó lap, és a `/projects/` egy
lapos, minden workspace-t összemosó projektlista, ahol az „Új projekt” és az
„Új workspace” is egy helyen van. Ez a dokumentum a navigációt a
**workspace-first** modellre állítja: a főoldal a workspace-ek listája, és az
**elemek** (projektek) egy workspace-en belül jönnek létre.

> Megnevezés: a modellben marad a `Project` (terv.md 5. fejezet). A UI-ban az
> „elem” szó használható (telefontartó, süti kinyomó, …), de a route/API
> `projects` marad, hogy ne törjön a meglévő szerződés.

## 1. Információs architektúra

```text
/                        -> Workspace lista (főoldal)
/workspaces/{id}/        -> Elemek (projektek) listája az adott workspace-ben
/projects/{id}/          -> Elem részlete (viewer, verziók, history controls)
/projects/{id}/plates/   -> Build plate (változatlan)
/community/...           -> Változatlan
```

Breadcrumb: `Workspaces / <Workspace> / <Elem>`.

## 2. Oldalak

### 2.1 `/` – Workspace lista (`workspaces/list.html`)

- Kártyák: név, tagok száma, projektek száma, `updated_at`.
- „Új workspace” gomb (bejelentkezve).
- Üres állapot: „Hozz létre egy workspace-t, és kezdj el elemeket készíteni.”

### 2.2 `/workspaces/{id}/` – Elemek (`workspaces/detail.html`)

- Az adott workspace projektjei (kártyák), keresés/szűrés.
- „Új elem” gomb → a létrehozó űrlap (név, leírás, **opcionális: kezdő skill**,
  lásd `docs/skills.md`). A workspace fix (a route-ból).
- A projektkártya a `/projects/{id}/`-re visz.

### 2.3 `/projects/{id}/` – Elem részlete

- Változatlan (`projects/detail.html`, viewer, verziólista).
- Új: history-vezérlők (docs/version-history-controls.md) és a használt
  skillek megjelenítése (docs/skills.md 7.).

## 3. Backend

- `config/urls.py`: `path("workspaces/", include("workspaces.urls"))`; a `""`
  route `WorkspaceListView`-ra vált (`projects.views` → `workspaces.views`).
- `workspaces/urls.py`:
  - `""` → `WorkspaceListView` (`workspaces/list.html`)
  - `"<int:pk>/"` → `WorkspaceDetailView` (`workspaces/detail.html`, átadja
    `workspace_id`-t)
- `projects/urls.py`: a `/projects/` lista megszűnik (a per-workspace lista
  veszi át); a `detail`/`plates` marad.
- API: `ProjectViewSet.get_queryset` kapjon `?workspace=<id>` szűrést
  (a membership-scope megtartásával); `GET /api/v1/workspaces/` már van.
- A projekt létrehozásakor a workspace a route-ból jön (nem választólista).

## 4. Frontend

- Új `frontend/static/js/workspaces.js` (`workspaceList`, `workspaceDetail`),
  a `projectList` Alpine komponensből kiemelve a workspace-kezelést.
- `base.html` navigáció: „Workspaces” fő link; a „Projektek” link megszűnik.
- `projects/list.html` törölhető, helyette `workspaces/list.html` +
  `workspaces/detail.html`.
- `api-extras.js`: `endpoints.projectsByWorkspace(workspaceId)`.

## 5. Átállás / kompatibilitás

- A régi `/projects/` linkek `/`-re irányíthatók (redirect), hogy a
  könyvjelzők ne törjenek.
- A meglévő API (`/api/v1/projects/`) változatlan; csak a `?workspace=`
  szűrő új.

## 6. Nem cél

- Workspace-en belüli almappák/fa (workspace → projekt → alprojekt).
- Új modell a „workspace” fogalomra; a `Workspace`/`WorkspaceMember` marad.
