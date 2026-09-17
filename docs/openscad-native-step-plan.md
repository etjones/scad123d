# Plan: STEP export inside OpenSCAD

Written 2026-09-17 against `openscad/openscad` master at `4c1d479` (2026-09-16)
and scad123d 0.8.0.

## What the survey found

**OpenSCAD has no OCCT anywhere.** No CMake, script, or source mention. The
only second-kernel precedent is Manifold, added beside CGAL behind
`ENABLE_MANIFOLD` / `--backend`, which is the pattern to copy.

**Exporters receive a `Geometry`, never the node tree.** `exportFile()` in
`src/io/export.cc:209` switches on `FileFormat` and hands every exporter a
`shared_ptr<const Geometry>` (a `PolySet`, `ManifoldGeometry`, or
`CGALNefGeometry`), i.e. an already-meshed result. The one exception is CSG
export, which is not in that switch at all: both the CLI
(`src/openscad.cc:444`) and the GUI (`src/gui/MainWindow.cc:2613`) walk the
evaluated `AbstractNode` tree with `Tree::getString`. **STEP export must take
the CSG path, not the exporter path.** That is the whole architectural point:
analytic geometry exists only in the node tree.

**The node tree is richer than the `.csg` text.** Two lessons from scad123d
that were forced by the text format disappear in-process:

- `TransformNode::toString()` streams `Transform3d` doubles at the default 6
  significant figures (`src/core/TransformNode.cc:252`), which is why scad123d
  needs Gram-Schmidt re-orthonormalization. In-tree we read the full-precision
  matrix.
- `$fn` provenance. The text records only the effective value, hence
  scad123d's magnitude heuristic (`facet_threshold = 20`). In-tree every node
  carries `modinst->arguments` (`src/core/node.h:58`,
  `ModuleInstantiation.h:39`), so "was `$fn` written at this call site" is a
  direct lookup. See question 4.

**The Python engine exists but is not a delivery vehicle.** `ENABLE_PYTHON`
is a CMake option, default OFF, not in release builds, gated again by the
experimental `python-engine` feature flag. It does have a venv and pip
mechanism (`pythonCreateVenv`, `--python-module`, `Settings::SettingsPython`),
so the embedded route is *possible*, but it stacks an off-by-default
interpreter, a per-user venv, and the 100 MB+ `cadquery-ocp` wheel under a
menu item. The cheaper proof of concept is a subprocess (Phase 0).

**Feature flags** (`src/Feature.cc:28`) only activate in `ENABLE_EXPERIMENTAL`
builds and appear automatically in Preferences > Features and `--enable=`.
The right home for a first version is `Feature::ExperimentalStepExport`.

**Tests** are `add_cmdline_test(... SUFFIX <ext> EXPECTEDDIR ...)` in
`tests/CMakeLists.txt` with line-diffed expected files. STEP text is not
diff-stable (timestamps, entity numbering), so STEP tests need a metrics
comparison, which scad123d already has the fixtures for (`tests/fixtures/*.csg`
plus `metrics.json` with OpenSCAD-rendered volume/bbox/centroid/face counts).

## Phases

### Phase 0: subprocess proof of concept (days)

Add `FileFormat::STEP` and an `Export to STEP` menu item that:

1. dumps the tree with `Tree::getString` to a temp `.csg` (exactly what the
   CSG exporter does),
2. runs an external `scad2step tmp.csg -o out.step` (path from a new
   preference, default: search `$PATH`),
3. surfaces stdout/stderr in the console.

scad123d side: teach `scad2step` to accept a `.csg` input (`import_csg`
already exists; the CLI only takes `.scad`). Mesh fallback still works, since
scad123d re-invokes OpenSCAD on the subtree as today.

What this buys: the menu wiring, the `FileFormat` plumbing, a preference, the
console UX, and a shippable result for anyone who `uv tool install scad123d`.
No embedding, no venv, no wheel packaging. It is also the harness for Phase 1:
the same menu item switches to the native path when compiled in.

What it does not buy: anything toward the C++ kernel. Treat it as scaffolding
plus a user-facing stopgap, not as a stepping stone.

Recommendation: do Phase 0, skip embedded Python entirely.

**Status (2026-09-17): done, on branch `feature/step-export` in the
OpenSCAD checkout** (one commit, not pushed; scad123d side is PR #51).
Verified on macOS: `openscad -o demo.step demo.scad` and the menu item both
produce a STEP file, the temp `.csg` is removed, converter output lands in
the console. The default `uvx scad2step` works with the released scad123d
0.8.0 too, because OpenSCAD accepts a `.csg` file as input and re-exports
it; PR #51 just skips that round trip. Known limits: the GUI blocks while
the converter runs (no progress or cancel), STEP to stdout is refused, and
Windows quoting of the command line is untested. Build notes: Homebrew deps
per `scripts/macosx-build-homebrew.sh` (the tap must be `brew trust`ed),
then `cmake -B build -G Ninja -DEXPERIMENTAL=ON
-DCMAKE_PREFIX_PATH="$(brew --prefix qt);$(brew --prefix qscintilla2)"`.

### Phase 1: native OCCT evaluator (3 to 5 weeks for the baseline)

**Status (2026-09-17): baseline landed** on `feature/step-export` (second
commit). `ENABLE_OCCT=ON` links Homebrew OCCT 7.9.3 and adds
`src/geometry/occt/` (`OcctBuilder`, `OcctBoolean`, `OcctMesh`,
`OcctBridge`) and `src/io/export_step_native.cc`. Settings
`export-step/engine` (`builtin` default, `external`) and
`export-step/facet-threshold` (20), also in Preferences > Advanced.
Verified against `tests/fixtures/*.csg` + `metrics.json` with the
checker in `scratch/openscad-native/step_metrics.py` (needs OCP; `uv run python` from this repo): booleans, extrusions,
facets, params, polyhedron, primitives, transforms, twod match to ~1e-14;
the five hull/minkowski fixtures take the mesh path and match OpenSCAD's
own render to 1e-6 (the fixtures expect the analytic rungs, Phase 2).
Colors verified on a six-case model (grouping, precedence, outer-wins,
cutter-does-not-paint). GUI menu export runs through the built-in engine.

Lessons: OCCT's global `class Message` collides with `printutils.h`
(reached via `Tree.h` and `PolySet.h`), hence `OcctBridge`; a rigid
transform read from six-significant-figure `.csg` text has column norms
differing by ~3e-7, so the uniform-scale detector uses 1e-6 or the
cylinder is approximated as B-splines; STEP writes pure red/green/blue
as `DRAUGHTING_PRE_DEFINED_COLOUR`, not `COLOUR_RGB`.

Still open in Phase 1: progress/cancel (the GUI blocks during a long
build), the fuse "bodies overlap" invariant (only piece-count and volume
bounds are ported), non-manifold polyhedron handling matches OpenSCAD
only for free edges, an in-tree regression test (the checker needs OCP;
a metrics debug export would let ctest compare JSON), cut-face colors
from cutters as the preview paints them (contract says no; preview says
yes; needs a decision), Windows/Linux builds.

**Build.** `option(ENABLE_OCCT ...)` default OFF, `find_package(OpenCASCADE)`,
link only the toolkits needed: TKernel TKMath TKG2d TKG3d TKGeomBase TKBRep
TKGeomAlgo TKTopAlgo TKPrim TKBO TKBool TKShHealing TKOffset TKFillet TKMesh
TKXSBase TKSTEPBase TKSTEPAttr TKSTEP209 TKSTEP TKCDF TKLCAF TKCAF TKXCAF
TKXDESTEP. Homebrew has `opencascade 7.9.3` (installed here), vcpkg and msys2
both package it, Debian/Fedora ship `libocct-*`. Add an entry to
`scripts/macosx-build-dependencies.sh` for release bundles later; Homebrew is
enough for development. License is LGPL 2.1 with the OCCT exception,
compatible with OpenSCAD's GPL 2+.

**Evaluator.** New `src/geometry/occt/OcctEvaluator.{h,cc}`, a `NodeVisitor`
parallel to `GeometryEvaluator`, producing a `TopoDS_Shape` per node with the
same prefix/postfix traversal and child-collection pattern. Visit overloads:

| Node | Native OCCT | Notes from scad123d |
|---|---|---|
| Cube/Sphere/Cylinder/Square/Circle | `BRepPrimAPI_*` | zero or negative critical dimension yields empty, not an exception (`build.py:200`) |
| Polyhedron/Polygon | faces from wires, sewn | non-manifold input: warn and contribute nothing, as OpenSCAD does; non-planar face: mesh fallback |
| Transform | `gp_Trsf` for rigid part, `BRepBuilderAPI_GTransform` for scale | scale must never be folded into `gp_Trsf` (tolerances do not scale); a reflection must rebuild solids FORWARD or N-ary fuse silently drops them; a flattening transform (zero singular value) removes the object; 2D children ignore z and stay in XY |
| CsgOp union/difference/intersection | `BRepAlgoAPI_Fuse/Cut/Common` with N-ary argument and tool lists | see invariants below; an empty *first* difference child empties the result; any empty intersection operand empties it |
| LinearExtrude (no twist, no scale) | `BRepPrimAPI_MakePrism` | scale: loft; twist: mesh fallback (no exact BRep twist exists, per the fidelity rule) |
| RotateExtrude | `BRepPrimAPI_MakeRevol` | 360 vs partial angle |
| Text | reuse `FreetypeRenderer` outlines, build polygon faces | outlines are already polylines in OpenSCAD; no font-substitution mismatch, unlike the Python path |
| Color | tag shape in an `XCAFDoc` color tool | the agreed contract: color never changes geometry; cutters do not paint; grouped by color in STEP |
| Group/Root/List/Render | fuse of children | `render()` is a union here |
| CgalAdv hull/minkowski, Projection, Surface, Import, Offset, Roof, Resize | **mesh fallback** in v1 | see below |

**Mesh fallback, in-process.** Run the existing `GeometryEvaluator` on the
subtree, take its `PolySet`, build one planar face per triangle, sew with
`BRepBuilderAPI_Sewing`, make a solid, `ShapeUpgrade_UnifySameDomain` to
merge coplanar triangles. No re-invocation of the binary, no `.csg` round
trip, and the `NodeCache`/`GeometryCache` already memoize repeated subtrees
(Gridfinity stamps the same hull per cell). Warn on the console with the
node's location, as scad123d's `MeshFallbackWarning` does.

**Booleans and healing, ported from `occt_workarounds.py`, `_common.py`,
`heal.py`.** These are the accumulated result of the 76k-file corpus work and
are the difference between a demo and a tool:

- N-ary fuse and cut (200 cylinders: 19.5 s to 1.1 s).
- Pass every body of a compound as its own operand, never a compound.
- Plausibility bounds on each boolean's volume; on failure retry up a fuzzy
  tolerance ladder scaled to the operands' size; warn if no rung helps.
- Fuse invariants: no gained bodies, no overlapping bodies.
- Cut invariant: no material left inside the tools; retry fuzzy, then fold
  tool by tool.
- `SetRunParallel(false)` everywhere: `polychannel.scad` flipped between 442
  and 174 mm³ across runs with parallel booleans on.
- Guarded clean: `UnifySameDomain` only when volume is conserved (it deletes
  faces crossing a parametric seam).
- Post-boolean small-edge/small-face healing at 1e-4 where analytic and
  meshed regions meet.
- Colored operands: sum volume over all solids, not direct children.

**STEP writer.** `STEPCAFControl_Writer` over an XDE document, mirroring
`solid123d/export.py`: one product per region body, body-level color, bodies
grouped under a named assembly per color plus `uncolored`, layer mode on,
header name from the source file. Colors in OpenSCAD are already resolved on
`ColorNode`s, so the region partition logic ports too (that is the part of
solid123d that decides which material owns overlap; see question 5).

**GUI and CLI.** `FileFormat::STEP` (`3D`, suffix `step`), menu action in the
`exportMap`, `-o file.step` and `--export-format step`. Progress and cancel
via `Message_ProgressIndicator` wired to the existing progress widget: OCCT
booleans on models like `lense.scad` (22 coincident `$fn=500` circles) run
for many minutes, and the GUI must stay cancellable.

**Tests.**

1. Port `tests/fixtures/*.csg` + `metrics.json` as OpenSCAD regression
   inputs; the runner exports STEP, reads it back with OCCT, and compares
   volume/bbox/centroid/face count to the expected JSON. Needs a small
   test-only OCCT executable or a `--export-format step-metrics` debug
   output.
2. Differential CI against `scad123d-diff` and `scad123d-batch`: add an
   engine switch so the batch runner drives `openscad -o x.step` instead of
   the Python pipeline. The corpus ledger, the mismatch classification, and
   the timeout/memory machinery then apply unchanged, and the known-open
   corpus bugs become the acceptance list.
3. `test_hull_minkowski`, `test_cut_invariant`, `test_fuse_invariant`,
   `test_self_intersection`, `test_regions` port as Catch2 unit tests on
   the evaluator.

### Phase 2: analytic rungs (3 to 4 weeks, optional, incremental)

**Status (2026-09-17): the core rungs landed** (`src/geometry/occt/OcctHull.cc`,
third commit on `feature/step-export`). Ported: equal-radius spheres
(offset of the convex hull of centers; capsule when collinear), two
spheres of any radii (sewn caps and tangent cone), parallel equal-radius
cylinders sharing a span, all-polyhedral children (Manifold convex hull
of the vertices, coplanar facets merged by the mesh builder), two discs,
straight-edged polygons, and minkowski with a ball at the origin
(sphere, circle, or a many-vertex tessellated kernel such as BOSL2's).
Components are exploded before classification. All 14 fixtures match to
~1e-13; `scratch/openscad-native/idioms/compare.py` checks eight idiom
files against the Python pipeline (all match to 1e-6, same face counts).

**Not yet ported:** the revolved-translates rung (`hull() cornercopy()`
of identical revolution solids: tapered pads, filleted posts, turned
legs; solid123d `hull.py` lines 619 to 1210, the largest single rung) and
the coplanar-sphere "rounded coin" case (Rung 2.5, unexplored in Python
too). Those subtrees take the mesh path today, matching OpenSCAD's
render exactly.

**Decision recorded (2026-09-17):** cut faces are not painted with the
cutter's color, as the contract says; revisit only if upstream asks.
`sphere(0)` inside `hull()` produces nothing in current OpenSCAD
("Current top level object is empty", 2025.07 and master), so the
evaluator's empty result for a zero-radius sphere matches.

Port `solid123d/hull.py` (1298 lines) and `minkowski.py` rung by rung, each
gated as today: attempt analytic, validate against a closed form, fall back
to mesh on mismatch. Order by corpus frequency: minkowski with a
sphere/circle or spherical polyhedron kernel (rounded boxes), hull of
equal-radius spheres/cylinders, hull of all-polyhedral children
(`ConvexPolyhedron` equivalent needs a convex hull; OpenSCAD already has
one via Manifold or CGAL, so this is nearly free), two-sphere pair hulls, 2D
hull of circles (15 of 21 remaining 2D fallbacks in the corpus).

### Phase 3: packaging and upstreaming

macOS: OCCT into `macosx-build-dependencies.sh` and `macdeployqt` picks up
the dylibs. Windows: msys2 `mingw-w64-opencascade` and the vcpkg manifest.
Linux: distro `libocct-*-dev`. Each adds roughly 60 to 100 MB of libraries
to the bundle. Upstream acceptance is a maintainer conversation, not a
technical question; see question 1.

## Decisions (2026-09-17)

1. **Personal build now, upstream release is the goal.** Every choice
   favors upstreamability: `ENABLE_OCCT` off by default, an experimental
   feature flag, OpenSCAD code style, no dependence on scad123d at runtime
   once Phase 1 lands.
2. **No embedded Python.** Phase 0 shells out. The command is a preference,
   default `uvx scad2step`, so anyone with `uv` gets it with no install
   step. `uv` itself is not bundled into the app: it is a stopgap, and the
   first `uvx` run downloads ~200 MB (OCP wheel) that a release build should
   not depend on.
3. **Phase 1 scope as written, and Phase 2 (the existing analytic rungs)
   is in scope**, in corpus-frequency order.
4. **`$fn`: facet only below an adjustable threshold** (default 20), as
   scad123d does today. Provenance is not used for the decision: a great
   deal of existing code sets `$fn` explicitly only because nothing better
   existed. Provenance stays available for a possible future
   "explicit-only" mode.
5. **Colors: STEP output must look identical to the preview's `color()`
   rendering.** Requirement, not optional. Sequence: body-level colors in
   Phase 1 so the writer and XDE plumbing exist, then the full region
   contract (overlap precedence, cutters never paint, grouped by color)
   before anything ships.
6. **macOS first**, Homebrew OCCT 7.9.3.
7. **Mesh fallback backend: follow the user's `--backend` setting.** The
   fallback is "whatever OpenSCAD would have rendered", and that is by
   definition the configured backend. Manifold is the default and is fast;
   it rejects non-manifold input where CGAL Nef will grind through it, so a
   user who already switched to CGAL for a model has done so for a reason.
8. **No standalone C++ `scad2step`.**

