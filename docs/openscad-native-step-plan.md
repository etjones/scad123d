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
as `DRAUGHTING_PRE_DEFINED_color`, not `color_RGB`.

**Tests in the OpenSCAD tree (2026-09-17):** `--export-format
step-metrics` writes JSON measurements (extent, bbox, centroid, face and
solid counts, volume per color) of the built B-rep and again of the
written STEP read back through XDE, rounded to four decimals. 33 inputs
in `tests/data/scad/step/` (fixtures, hull idioms, color models) with
expected files in `tests/regression/step-metrics/`, registered as
`step-metrics_*` under `ENABLE_OCCT`; every round trip matches its built
shape, colors included. Catch2 cases in `src/geometry/occt/Occt_test.cc`
(tag `[occt]`, 47 assertions) cover the boolean invariants, the guarded
unify against the seam bug, mesh-to-solid, the collinear-triangle
repair, and the hull rungs against closed forms. Regenerate expected
files with the loop in the commit message of c8b65bee8 when geometry
changes deliberately. Not covered: the external (scad2step) engine path
and the preferences UI.

Still open in Phase 1: progress/cancel (the GUI blocks during a long
build), the fuse "bodies overlap" invariant (only piece-count and volume
bounds are ported), non-manifold polyhedron handling matches OpenSCAD
only for free edges, Windows/Linux builds.

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

**Revolved-translates rung ported too** (fourth commit,
`OcctRevolvedHull.cc`): seven idiom files (tapered pads, filleted posts,
stacked bevels, turned leg, tilted axis, two posts, torus posts) match
the Python pipeline exactly. Two fallback fixes came out of them:
rotate_extrude's axis check used a tolerance-padded box, and Manifold's
hull emits zero-area collinear triangles that left T-junctions after
sewing; they are now repaired by inserting the middle vertex into the
neighbor across the long edge. Fallback meshes are triangulated before
sewing and coplanar triangles merged after, so a fallback region comes
out with about a third of the faces the Python path produces.

**Rung 2.5 (coplanar equal spheres, the "rounded coin") turns out to be
covered by the revolved-translates rung:** a sphere is a solid of
revolution about the plane normal, so the fan construction yields the
exact shape (two flat polygon caps, a half-cylinder per edge, a
spherical wedge per vertex). `coin_rect`, `coin_tri` and `coin_tilted`
match the Steiner volume 2rA + (pi r^2/2)P + 4/3 pi r^3 to 1e-13. The
Python ROADMAP entry predates phase B and is stale. Every rung solid123d
has is now in, and this case besides.

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

**Universal macOS release (2026-09-17), through upstream's own path:**
`scripts/macosx-build-dependencies.sh -d -a -x` builds all 27
dependencies fat (arm64 + x86_64, macOS 12+) into `../libraries/install`
(here `~/Dropbox/Projects/libraries` is a symlink to
`~/openscad-libraries`, 697 MB, kept out of Dropbox); `build_opencascade`
was added to the script (OCCT 7.9.3, modelling + OCAF + data exchange
only, 17 min). Then `DEPLOYDIR=$PWD/build-universal
scripts/release-common.sh -v <version>` with `-DENABLE_OCCT=ON` now in
its macOS `CMAKE_CONFIG`. Result: `build-universal/OpenSCAD.app`, fat,
269 MB, 26 OCCT dylibs bundled, no prefix references, runs under Rosetta
too; ad-hoc signed and packaged as `OpenSCAD-<date>-universal.dmg`
(107 MB). Environment the script assumes and does not set itself:
`source scripts/setenv-macos.sh` first (pkg-config path, or harfbuzz
picks Homebrew's arm64 graphite2), `DEVELOPER_DIR` pointing at a full
Xcode (Qt's configure refuses the Command Line Tools), and the system
`seq`. One script defect fixed: harfbuzz installed to a relative prefix
instead of `$DEPLOYDIR`. Upstream's `macosx-sanity-check.py` flags
`@rpath` references that dyld resolves inside the bundle; a
`DYLD_PRINT_LIBRARIES` trace is the reliable check.

**CI on the fork (etjones/openscad#1):** format, tidy, Ubuntu 22.04
(without OCCT) pass; Ubuntu 24.04 (OCCT 7.6) builds and passes the
[occt] unit tests, and the exact step-metrics comparison is gated to
OCCT >= 7.8 because 7.6 leaves different valid topology (a capsule as 4
faces, touching solids merged). Windows (msys2): gdb backtraces put the crash inside
`Extrema_ExtCC::Perform` in the msys2 `opencascade-7.9.3-3` DLL (built
with GCC 16, which had a MinGW regression that spring), reached from the
boolean kernel and from `BRepClass3d_SolidClassifier`; box-only
booleans, sewing, offsets and mesh conversion work. The Windows job now
builds without `ENABLE_OCCT` until that package is fixed; a vcpkg/MSVC
build would be the alternative. macOS Intel: the job is cancelled at
its 90-minute limit while still installing Homebrew packages, and
upstream's own runs of that workflow show the same, so it is not ours.

**Mac release built (2026-09-17):** `scripts/macosx-deploy-homebrew.sh
build-release` in the OpenSCAD checkout produces
`release-mac/OpenSCAD.app` (189 MB, arm64, Qt + OCCT + all Homebrew
dylibs bundled, zero references to /opt/homebrew, ad-hoc signed) and
`release-mac/OpenSCAD-<date>.dmg` (77 MB). Build recipe:
`cmake -B build-release -G Ninja -DCMAKE_BUILD_TYPE=Release
-DENABLE_OCCT=ON -DEXPERIMENTAL=OFF -DENABLE_TESTS=OFF
-DCMAKE_PREFIX_PATH="$(brew --prefix qt);$(brew --prefix
qscintilla2);$(brew --prefix opencascade)"`. `ENABLE_TESTS=OFF` because
upstream's tests/CMakeLists.txt references an experimental-only test when
EXPERIMENTAL is off (a pre-existing upstream issue). Ad-hoc signing means
Gatekeeper will ask the user to allow it (right-click > Open, or
`xattr -d com.apple.quarantine`); notarization needs an Apple developer
identity. The project's `macosx-sanity-check.py` reports false
"not found" errors for `@rpath`/`@loader_path` references that dyld
resolves fine; a `DYLD_PRINT_LIBRARIES` trace shows every library
loading from the bundle or the OS.

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
   "explicit-only" mode. Amended 2026-09-18: the count also comes from
   `$fa`/`$fs`/`$fe` when they are set away from their defaults
   (`CurveDiscretizer::explicitSegmentCount`), so `cylinder($fa=70)` is the
   hexagon the preview shows. Default fineness never facets.
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


## Review homework harness (2026-09-18)

`scratch/openscad-native/homework/homework.py --all` measures the objections a
maintainer is likely to raise and writes `REPORT.md` + `results.json` next to
it (see its README). Needs `build-release` (OCCT on) and `build-nocct` (OCCT
off, same flags) in the openscad checkout. First run, 50 corpus models:

- Download: universal dmg +37 MB (+53%) over the upstream snapshot; OCCT gzip is 33.4 MB of that; 11.2 MB of the fat payload is visualization toolkits pulled in by TKXCAF.
- Launch: 73 ms / 31 MB RSS with OCCT vs 36 ms / 23 MB without. The 26 extra dylibs cost ~35 ms per launch. This is the argument for a dlopen'd plugin if maintainers care.
- OFF build: no OCCT symbols, 47 vs 73 dylibs, STL output volume-identical on all 18 idioms (Manifold triangle order is not deterministic even within one binary, so byte comparison is meaningless).
- Corpus: 49/50 STEP ok, 0 crashes, 1 timeout, 11 needed a mesh fallback; volume parity median 0.0%, p90 0.3%; STEP median 0.25 s vs STL 0.08 s (2.7x median, 37x p90).

Open items the run surfaced:
- `servo_rotate_rod.scad`: `cylinder($fa=70)` was a hexagon in preview but a true cylinder in STEP (14% volume gap). Fixed in df3ba01d4: non-default `$fa`/`$fs`/`$fe` now count as deliberate faceting; the remaining 2.2% gap is the default-fineness holes (pentagons in the mesh, exact in STEP), which is the intended policy.
- `FidgetWrench.scad`: 1.9% volume drift on STEP read-back of a mesh-fallback solid.

500-model run (seed 1, jobs 4, binary f589c4bfc): STL ok 489, STEP ok 493 (8 succeed where STL fails), 0 crashes, 3 timeouts, 4 "produced no geometry" errors, 92 models with a mesh fallback; parity median 0.0%, p90 0.7%, 10 beyond 2%; STEP 0.20 s vs STL 0.09 s median (2.2x median, 20x p90). Warning frequency: hull no closed form 62, union unjoined 39, minkowski non-ball 27, offset 15.
- **Unjoined unions (39 models) are benign for volume**: every one has parity <= 1.3%; the failed fuses are touching or near-coincident operands. Cost is internal faces in the STEP and slower downstream booleans (two of the three timeouts). solid123d has the identical fallback, so it is outstanding in both.
- **Fixed (f589c4bfc)**: `pop_can_hold` at 2x volume was ShapeUpgrade_UnifySameDomain inflating a vertex tolerance to 45.8 mm while merging two sphere faces, and mutating the shared input even when the volume guard rejected the result. Unify now runs on a copy and is rejected when tolerance grows past 1e-5 or 100x. solid123d's guarded clean() already copied; the port had dropped it.
- Not ours: `utils.scad` (+33%) and `skadismount` (+23%) agree with scad123d, skadismount also with CGAL's exact render; Manifold disagreements.
- Unexplained, STEP below mesh with no warning: `15013_0/screw_thread` (10%), `1580796_0/Hand_FingerControlMK6` (10.7%), `0868816_0/plant-pot-wall-mount` (20.6%); smaller: `132966_0/screw_bit` 5.1%, `163839_0/bacteriophagering` 7.3%, `1564428_0/dome` 3.6%, `11277916_0/airvent` 3.6%.
- "the model produced no geometry" on 4 models where STL exports fine (`Mug2`, `chain`, `parasolFootBushings`, `pyramid4frustum`).
- Timeouts: `servo_arm_parmetric_gear`, `toothbrush-organizer` (union unjoined loops), `qubit` (hull fallback).
- `servo_arm_parmetric_gear.scad`: >120 s with repeated "union came back damaged" warnings; STL takes 0.2 s.

## Boolean verification (2026-09-18, commit ab1114cf4)

Two invariants on the boolean layer, ported in spirit from solid123d but
new in substance: `OcctBoolean::keepsOperands` (a union contains
everything it was given) and `keepsUncoveredMaterial` (a cut removes only
what its tools cover). Both sample up to 8 interior points per body, so
they are detectors, not proofs. Rejected results go through the fuzzy
ladder and then a fold, one body at a time.

Policy, arrived at by measurement, not taste:
- An unverified **union** keeps handing back its operands unjoined. That is
  materially correct, only unmerged.
- An unverified **cut or intersection** renders its node through OpenSCAD's
  evaluator. The old behaviour was to use the failed result.
- Sending unions to the mesh as well was measured and **made things worse**:
  5 models went from agreeing with the mesh to 50-76% off, 2 new timeouts,
  fallbacks 92 -> 112, total export time 893 -> 1730 s. Do not revisit
  without re-running the corpus.

Also `splitClosedFaces`: faces wrapping a periodic surface close on a seam
edge, and a seam did not survive the STEP round trip (an ellipsoid fused to
a cylinder came back with no volume, in OCCT's reader and in the user's
viewer). Split before writing, guarded at 0.5% extent. Geometrically
lossless: sampled against the exact ellipsoid the deviation is 9e-16 either
way; the 0.2% volume wobble on rational surfaces is a quadrature artifact.

Performance: the first version spent 86% of one model's export rebuilding
`BRepClass3d_SolidClassifier` per point. Building one per solid and reusing
it took the corpus from 1789 s back to 1107 s (baseline 893 s, so +24%
overhead), and model.scad from 109 s to 14 s.

Corpus, 500 models, seed 1, vs the run before the invariants:
| | before | after |
|---|---|---|
| repaired | - | 3 (plant-pot, screw_thread, Hand_FingerControl) |
| timeouts resolved | - | 1 (toothbrush-organizer) |
| worsened | - | 0 |
| new timeouts | - | 2 (switch_stand, airpot) |
| mesh fallbacks | 92 | 95 |
| total STEP export | 893 s | 1107 s |

Open:
- **switch_stand, airpot time out.** Profiled: all of it in
  `BRepBuilderAPI_Sewing::FindCandidates` inside `meshFallback`. The
  fallback's sewing is quadratic-ish on large meshes. Next thing to fix.
- **screw_thread is caught, not repaired** (cut fails, region degrades to a
  mesh: 4621 planar faces, 8 cylindrical for the shaft, no toroidal at all).
  A difference distributes over a union, so a natural retry rung is to cut
  the union's members individually when the fused cut fails. Tested by
  rewriting the source that way: it works on a reduced 3-turn coil
  (analytic, no fallback, faster) but **not on the real 5-turn file**, where
  the unions then fail instead ("union came back damaged"), the export still
  ends as a mesh, and it takes 34.3 s against 18.8 s. So the rung is
  unproven; do not implement it on the strength of the reduced case.
- Remaining +24% export time; one corpus seed; no CI run on ab1114cf4 yet.

## Mesh stitching and the qubit (2026-09-24)

Stitching (7a4adfc12): shells built from the mesh's own index topology,
sewing only as fallback -- switch_stand >180 s -> 0.5 s. Subtree cache
keyed on the tree's id string + inherited color -- airpot (810 copies of
one vent) never finished -> 31 s. Bounding-box pre-filter and a 512-test
ceiling on the containment checks.

qubit: `$fn=180` at the top; every hull falls back to a mesh at that $fn
(17k-33k triangles each) and OpenCASCADE's boolean is superlinear in facet
count (body: 5.5 s at 60, 11.6 at 90, 21.6 at 120, timeout at 180).
Rejected: Manifold::Simplify (conservative; worse volume error than a 60
render at 3x the faces). Rejected: always re-tessellating hulls at 60
segments (rack.13, which sets no $fn, went 8.6 s -> timeout because its
own render was far coarser). Adopted: OpenSCAD's own render when it is
<= 10,000 triangles, else `OcctHull::hullOfTessellation` of the exact
children at 60 segments (BRepMesh on a *copy*, since meshing writes into
the shape and a meshed shape is not re-meshed; points sorted + deduped).
qubit: never finished -> 73 s, 0.0% parity; every previously-regressed
model back to its old time.

Manifold's hull is nondeterministic in triangulation (volume stable,
face count wanders by a few). The metrics tests therefore compare through
`compare_stepmetrics` (feb70ec80, `.stepmetrics` suffix): every number to
1e-9 relative, `faces` to 1%, so `hull_tessellated.scad` is a regression
input after all, alongside the Catch2 case on volume properties.

Trial files: ~/Desktop/slow_scad_converts/ (hullA/B/C, cutters, body at
several $fn).

## Export-time verification earns its keep (2026-09-24, 5385acdf0)

Asked what the read-back check buys for its 14%: on the corpus it flags
2 of 495 files, one at 55%. `Ergotron_post_lamp_adapter` (tube + oblate
ellipsoid, then cut by a ring whose inner radius equals the ellipsoid's,
trimming it exactly at its equator) had read back 55% too large since the
seam split landed in ab1114cf4, and nobody noticed for six days because
compare_runs only looked at built-vs-mesh parity. Seam splitting cuts
both ways: it fixed the phage and broke this one; no in-memory guard can
see either, since the damage exists only in the file.

Fix: `writeVerified` writes split, reads back; if off, writes unsplit
and reads back; keeps whichever is faithful; warns only if neither is.
Metrics use the same path. `compare_runs.py` now reports round-trip
drift and flags new offenders. Regression inputs: `seam_split.scad`
(needs the split) and `seam_kept.scad` (needs it not). 61 tests pass.

Verdict on the 14%: keep it on. It is the only thing that has caught a
file-level defect, and it has now caught two, one of them ours.

## Windows is a blocker (2026-09-25)

Facts: (1) the msys2 CI job is a *test* build; upstream's shipped Windows
binary is cross-compiled with their MXE fork (openscad/mxe, scripts/
mingw-x-build-dependencies.sh, `plugins/gcc11`, x86_64-w64-mingw32.static
.posix). (2) msys2 opencascade 7.9.3-3 (GCC 16, rebuilt 2026-08-09 after
the GCC 16 native-TLS switch, so not a TLS mismatch) segfaults in
Extrema_ExtCC::Perform via BRepClass3d_SolidClassifier on any curved
boolean; no OCCT upstream report found. (3) msys2 also builds opencascade
for CLANG64. (4) Neither mxe/mxe nor openscad/mxe has an opencascade
package.

Experiment running: branch ci/windows-occt-toolchains, run 36165289679,
STEP evaluator on under UCRT64 (GCC 16) and CLANG64. If clang passes, the
CI job is fixed by switching toolchain and the crash is a GCC 16 build
issue to report to msys2/OCCT. Either way the release needs an MXE
opencascade package (static, the DE/XCAF toolkits, GCC 11) -- real work,
and the thing that actually ships.

Results so far (2026-09-25 evening):
- **MXE: OpenCASCADE 7.9.3 builds** under openscad/mxe (GCC 11.5, static,
  x86_64-w64-mingw32.static.posix) from `etjones/mxe` branch `opencascade`
  (src/opencascade.mk): all STEP/XDE toolkits present (TKDESTEP, TKXCAF,
  TKDE, TKLCAF, TKCAF, TKXSBase, TKVCAF, TKV3d). 87 min cold, incl. MXE's
  own gcc. So the shipped Windows binary *can* carry it; next is linking
  OpenSCAD's release build against it.
- msys2 UCRT64/GCC 16: 31/2743 tests fail, all ours, by SEGFAULT. CLANG64
  cannot build OpenSCAD itself (boost source_location) -- not a usable
  discriminator.
- Probe (scripts/ci/occt-probe.cpp: classify a point in a sphere, fuse a
  sphere and a box): running against the msys2 package and against OCCT
  built from source with GCC 16 at -O2 and -O1; plus, cross-compiled with
  MXE/GCC 11 and run on a windows-latest runner (mxe-occt.yml, cached).
- **Probe v2 result (decisive):** OCCT 7.9.3 from source with msys2's own
  GCC 16, -O2 and -O1: every step OK. The msys2 package: segfault (139) at
  the first point classified against a boolean result -- the OpenSCAD
  crash site. So the compiler is fine and the *package build* is broken.
  It differs in USE_TBB=ON (tbb12 + tbbmalloc), D3D, OpenVR, RapidJSON.
  Windows CI therefore builds OCCT from source (cached, ~20 min cold) with
  ENABLE_OCCT=ON; a probe v3 tests MMGT_OPT=0/1/2 and a USE_TBB=ON source
  build to pin the cause for the msys2 bug report.

## Windows resolved: GCC 16 -fipa-cp-clone (2026-09-25, night)

Bisection by standalone probe (scripts/ci/occt-probe.cpp, workflow
occt-probe.yml on `ci/windows-occt-toolchains`), all OCCT 7.9.3 from
source with msys2 GCC 16.2, modelling toolkits only unless noted:
- msys2's patches applied: pass. Module set (all modules + FreeType vs
  modelling only): irrelevant, both pass at -O2.
- -O3 (CMake's Release default, which the msys2 PKGBUILD inherits by
  appending CMAKE_BUILD_TYPE=Release after makepkg's -O2): **crash**.
- -O3 -fno-tree-vectorize: crash. -O3 -fno-inline-functions: crash.
  -O3 -fno-ipa-cp-clone: **pass**. -O2 -fipa-cp-clone alone: **crash**.
So `-fipa-cp-clone` under GCC 16.2 breaks `Extrema_ExtCC::Perform`
(gdb: SIGSEGV at a virtual call through an Adaptor3d_Curve vtable,
reached from BRepClass3d_SClassifier::Perform via OcctBoolean::
interiorPoints). Dead ends recorded: mimalloc off changes nothing
(identical 32 failures); process conditions (rounding modes, x87 control
word, loading gmp/mpfr/tbb/Qt/boost) change nothing.

Why earlier runs misled: the Windows job's OpenCASCADE was never cached
(actions/cache saves only on a green job), so the "source build passes"
probe jobs built their own OCCT at explicit -O2 while the Windows job
built at default -O3. Cache is now restore + early save.

Fix in windows.yml: `-DCMAKE_CXX_FLAGS_RELEASE="-O2 -DNDEBUG"` (cache key
occt-7.9.3-UCRT64-src-O2-v2). Confirmed: run 36202500221 went 32 -> 1
failure (seam_split built bbox 4.9913 vs 5.0, a bound not a measurement;
bboxes now held to 1% like face counts), and run 36212180240 on
feature/step-export passed 2743/2743. Ported to feature/step-export with
upstream's qt5/qt6 matrix restored.
MXE: probe-mxe.exe (GCC 11, static) passes on windows-latest. To do:
report to msys2 (package) and OCCT/GCC (reduced case wanted); link
OpenSCAD's MXE release build against the MXE opencascade package; port
windows.yml + msys2-install-dependencies.sh changes to feature/step-export.
