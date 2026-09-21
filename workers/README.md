# workers

Out-of-process job workers. The Django app never shells out to CAD/slicer
binaries from the request cycle (see `terv.md` 9. fejezet).

- `cad/` – OpenSCAD generation (Phase 2)
- `slicing/` – PrusaSlicer / OrcaSlicer slicing (Phase 5)
- `validation/` – trimesh-based mesh validation (Phase 4)

OpenSCAD always runs in a locked-down container:
`--network none --read-only --cap-drop ALL --security-opt no-new-privileges
--pids-limit 256 --memory 1g --cpus 1.0` plus a hard timeout.
