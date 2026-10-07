# Changelog

Release notes for scad123d and the bundled solid123d. Newest first. Each
entry says what changed and, where it matters, what to do about it. The
**Breaking** list is the one to read when an upgrade stops working.

## 0.9.0 (unreleased)

### Breaking

- Requires build123d 0.13 or later, which moves the kernel to OCCT 8.0.1 (`cadquery-ocp-novtk` 8.0.1). build123d 0.13 is "otherwise identical to v0.12.0", so this is a kernel upgrade, not a feature change.
- Requires Python 3.11 to 3.14; 3.10 is dropped because build123d 0.13 no longer supports it.
- Code that reaches into OCP alongside scad123d needs the OCCT 8 names: typed collections live in `OCP.collections` (`TopTools_IndexedDataMapOfShapeListOfShape` is now `IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher`, `TDF_LabelSequence` is `Sequence_TDF_Label`), and downcasts drop the `_s` suffix (`TopoDS.Edge_s(s)` is now `TopoDS.Edge(s)`).

### Fixed

- The union invariant's overlap pre-filter now uses `BoundBox.intersects`, so a body lying wholly inside another is sampled again. build123d 0.12 had redefined `BoundBox.overlaps` to exclude containment, which skipped exactly that case.

### Added

- `scad2step` accepts a `.csg` file that OpenSCAD has already exported, for OpenSCAD's own Export-as-STEP menu item; `-D` and `-p` are rejected for that input since nothing is left to override.

## 0.8.0 (2026-09-14)

### Breaking

- solid123d ships inside the scad123d package; there is no separate `solid123d` distribution to install any more. `import solid123d` keeps working.

### Changed

- Geometry OpenSCAD tessellates (`projection()`, twisted or scaled `linear_extrude`, meshes) is rendered by OpenSCAD and imported, rather than approximated.
- A boolean that fails verification is re-rendered in OpenSCAD, and the exact CGAL kernel is consulted more often before blaming the conversion.
- An analytic result may exceed OpenSCAD's mesh volume but never fall short of it.
- Colored bodies are partitioned where they actually sit, so color boundaries land on the right faces.
- An intersection that comes back empty from shapes that visibly meet is retried before being accepted.
- `scad123d-batch` times every conversion, including the ones it killed.

Earlier releases (0.5.0 through 0.7.0) predate this file; their commit
messages on the `v0.5.0` to `v0.7.0` tags describe each change.
