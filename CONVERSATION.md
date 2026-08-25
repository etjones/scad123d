# scad123d — Conversation Log

## 2026-08-18 — Initial brief

### User

> You might look at ~/Projects/solid123d. That project allowed an import-only
> substitution to use SolidPython code to generate Build123d geometry. I'd like
> to make `scad123d` to allow us to import OpenSCAD code and generate Build123D
> geometry from it. I don't think we need to allow users to write OpenSCAD code
> in a Python file but we'd like to import openSCAD code and use it in a Python
> file. So it could be that this module would contain only one importable
> function, `scad123d.import_scad(path:pathlib.Path) -> build123d.Shape:` Or
> maybe there are others? I think the thing to begin with is probably a
> pyparsing (or other? there are a lot of parser builders in Python) parser for
> OpenSCAD, and then a translation from that AST to Build123d primitives. It
> might be easiest to generate SolidPython code using the OpenSCAD AST and then
> using solid123d to convert that, or it might be good to go straight to
> Build123d. Is that a clear brief, and do you have any questions?

### Assistant

Read the prior art: `/Users/jonese/Dropbox/Projects/3D_geometry/scad_2_build123d`
(package name `solid123d`, on PyPI). ~1000 lines, clean separation into
primitives/transforms/booleans/extrusions/render.

Brief is clear. One correction to the framing, then questions.

#### The framing issue

"pyparsing parser -> AST -> translate to build123d" understates it. OpenSCAD is a
full programming language; a `.scad` file is a *program* and geometry is its
output. You need three layers, not two:

1. **Parser** -> AST
2. **Evaluator** — the actual hard part: lexical scoping, `module`/`function`
   definitions with defaults, `children()`/`$children`, `for`/`intersection_for`/
   `if`/`let`, list comprehensions (`for`/`if`/`each`/`let` inside `[]`),
   `include`/`use`, `$`-variable *dynamic* scoping, and OpenSCAD's
   non-sequential variable semantics (within a scope the last assignment wins for
   the whole scope — `x=1; echo(x); x=2;` prints `2`)
3. **Geometry backend** -> build123d

Layer 3 is essentially solid123d already.

#### Don't route through SolidPython source

Generating SolidPython code doesn't save you layer 2 — SolidPython has no
representation for `for`, `if`, or `module`, so you'd have to evaluate those
first anyway. You'd get a codegen step and two debugging hops for no reduction in
work.

But solid123d is still the right thing to reuse — as a **library, not a codegen
target**. The evaluator calls `solid123d.cube(...)`, `solid123d.translate(v)(...)`
directly; those already return `build123d.Shape`. See
`@/Users/jonese/Dropbox/Projects/3D_geometry/scad_2_build123d/src/solid123d/primitives.py:22`.

#### Parser choice

**Lark** over pyparsing: grammar in EBNF in its own file, Earley for the ambiguous
corners and LALR when you want speed, far better error messages, pure Python. A
C-like expression grammar with precedence is exactly where pyparsing is least
pleasant. `tree-sitter-openscad` also exists and is battle-tested, but adds a
native dep and hands you a CST to walk.

#### Where fidelity actually breaks

- **`$fn`** — the sneaky one. `circle(r=10, $fn=6)` *is a hexagon* in OpenSCAD,
  not an approximation of a circle. solid123d ignores `$fn` (exact BRep curves),
  which is correct for a migration bridge but silently wrong for an importer
  whose job is fidelity.
- **`hull()`** — common in real files, no OCCT equivalent. Either hard-fail or
  approximate via `scipy.spatial.ConvexHull` over tessellated vertices ->
  polyhedron (exact for polyhedral input, wrong for spheres).
- **`minkowski()`** — also common (rounded boxes). Sometimes expressible as
  `offset`; generally not.
- **`polyhedron()`, `import()` (STL/DXF/SVG), `surface()`, `projection()`,
  `linear_extrude(twist=)`** — each needs its own call. Mesh imports especially:
  OCCT booleans on imported triangle soup are miserable.
- **Modifiers** `*` `!` `#` `%` — `*`/`!` are cheap and genuinely useful; `#`/`%`
  map to color/discard.

#### API surface

`import_scad(path) -> Shape` covers "give me the model", but most `.scad` files
worth importing are *libraries of modules*. Proposed:

```python
lib = scad123d.import_scad(path, **overrides)  # -> ScadModule namespace
nut = lib.hex_nut(size=5)  # modules as Python callables -> Shape
part = lib.geometry  # top-level geometry, implicitly unioned
```

`**overrides` sets top-level variables (Customizer-style) — nearly free once the
evaluator exists, and very valuable.

#### Testing

OpenSCAD.app is installed on this machine, so differential testing is practical:
render each fixture with `openscad -o out.stl`, compare volume / bbox / centroid
against the build123d result within tolerance. That's the only way to trust this.
Worth building the harness early rather than bolting it on.

#### Questions

1. **Target corpus** — what do you actually want to import? This decides
   everything. Hand-written single files? Thingiverse downloads? Files built on
   BOSL2/MCAD/dotSCAD? (BOSL2 is a project unto itself: heavy list-comprehension
   math, function literals, `$`-var conventions.) There are ~40 `.scad` files
   under `~/Dropbox/Projects/sp_scratch` — is that representative?
2. **`$fn` fidelity** — honor it (faceted, matches OpenSCAD exactly), ignore it
   (exact BRep, prettier and filletable), or honor-when-explicitly-set?
3. **`hull`/`minkowski`** — hard fail, or best-effort approximation with a
   warning?
4. **solid123d dependency** — depend on the PyPI package, or vendor it so
   scad123d stands alone?
5. **`include`/`use` resolution** — full `OPENSCADPATH` + library-dir search, or
   restricted to paths relative to the imported file? (`include` executes
   arbitrary code from arbitrary paths, so this is a sandboxing question too.)
6. **Depth vs. breadth first** — MVP is primitives + transforms + booleans +
   variables + `for`/`if` + modules + `include`. Full language adds list
   comprehensions, function literals, `assert`/`echo`, `intersection_for`,
   ranges, string ops. Ship the MVP first, or get the language right before
   widening geometry coverage?

---

## 2026-08-18 — Decisions, and the CSG-export discovery

### User

> BOSL2 & MCAD are good goals. For our purposes, let's pick something like 10
> modules from BOSL2 and get fidelity between those two? We'll ignore
> `sp_scratch`; that's just scratch code I've worked on in the past.
>
> When $fn is set globally, it's usually there as a compilation-complexity
> switch; I use something like $fn=48 for development work and $fn=128 for
> exports. When $fn is set explicitly in a function call, (like your hexagon
> example) that's usually intentional. I'd say let's honor explicit $fn
> invocations, and ignore global usage for any $fn >= 20, with explicit notes in
> the README describing behavior and workarounds.
>
> 3. Let's do a hard fail on hull and minkowski for now. We might look to include
>    more universal approximations at a later stage, but for now let's throw a
>    NotImplementedError or something and make similar explicit notes in the README
> 4. Do we actually need solid123d? If so, let's depend on PyPI. If not, let's not
>    do it at all. Definitely no vendoring.
> 5. Yes, let's include OPENSCADPATH logic to mimic OpenSCAD's. There's probably a
>    fair amount of testing to make sure we resolve to the same paths as OpenSCAD.
>    Your point about sandboxing is a good one -- worth including a security note
>    in the docs
> 6. Let's get the language right first. The primitives are the easy part.
>    Interpreting all the language constructs correctly is a bigger job.
>
> *UNLESS* we want to replace OpenSCAD calls with calls to the OpenSCAD executable
> to create STLs, then import those STLs normally. That's a much much smaller
> project and still gets us mostly similar results. Can you tell me some pros and
> cons of this approach? I suspect translating all the code in an OpenSCAD
> repository to Build123d code is a big job. Letting OpenSCAD do its thing and
> then using the output seems like a reasonable approach even if it means we don't
> get e.g. per-face awareness from the OpenSCAD objects.

### Assistant

Ran experiments against OpenSCAD 2025.07.18 (installed at
`/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD`). BOSL2 is already present
in `~/Documents/OpenSCAD/libraries/`.

#### Finding: `openscad -o out.csg` gives a fully-evaluated tree of *analytic primitives*

OpenSCAD's CSG export runs the entire language and emits a flattened tree.
Verified resolved: modules inlined with defaults, `if` decided per-instance,
`for` unrolled, list comprehensions evaluated, `children()` spliced, transforms
collapsed to 4x4 `multmatrix`, `include`/`use`/`OPENSCADPATH` resolved, `*`
disable-modifier stripped.

Two key properties:
- **Primitives stay analytic** — `cylinder(h=8, r1=3, r2=1.5)`, not triangles.
  The BRep survives.
- **`$fn` is emitted per-primitive and distinguishes global-inherited from
  call-site** (`$fn=48` from global vs `$fn=6` explicit). The agreed $fn policy
  falls out for free. `$fn=0` means unset (use `$fa`/`$fs`).

**BOSL2 works.** `include <BOSL2/std.scad> + gears.scad` with `cuboid(rounding=3)`,
`spur_gear()`, `prismoid(chamfer=2)` exported in **0.33s**. Full node vocabulary
of that output:

```
945 group     65 multmatrix    9 polygon    9 linear_extrude
  8 intersection  8 cylinder    4 union      1 polyhedron   1 hull
```

Across all probes the CSG vocabulary is ~24 node types with no expressions, no
variables, no scoping — a ~100-line Lark grammar plus a tree walk, not an
interpreter.

#### Three options

| | A: full interpreter | B: shell -> STL | C: shell -> CSG |
|---|---|---|---|
| Effort | months | ~1 week | ~2-3 weeks |
| BOSL2/MCAD day one | no | yes | yes |
| Geometry out | BRep | **mesh** | **BRep** |
| `fillet`/`chamfer` after import | yes | **no** | yes |
| Face/edge selectors | yes | triangles only | yes |
| Real STEP export | yes | mesh-as-STEP | yes |
| `hull`/`minkowski` | no | **yes** | no (mesh fallback) |
| Needs OpenSCAD binary | no | yes | yes |

#### Pros/cons of the STL route

**Pros:** perfect semantic fidelity by construction including `hull`,
`minkowski`, `projection`, `surface`. Smallest codebase. Zero language-tracking
maintenance.

**Cons** (the mesh consequence is bigger than "no per-face awareness"):
- `fillet()`/`chamfer()` effectively dead — filleting a 128-facet cylinder rim
  gives 128 tiny facet-edge fillets, not a rounded rim.
- Selectors return triangles; `.faces().sort_by(Axis.Z)[-1]` is one triangle.
  No `Plane(face)` workplane placement.
- OCCT booleans on triangle soup: much slower, much more failure-prone.
- STEP export is mesh-as-STEP; downstream CAD imports a mesh body, not a solid.
- Faceting baked at import time; changing `$fn` means re-running.
- STL is unsealed triangle soup, needs sewing/healing. 3MF is the better
  container if going this route.

Decisive point: B's only real advantage over C is that
`hull`/`minkowski`/`projection`/`surface` get evaluated. Everything else B
offers, C offers too — plus analytic geometry. Both need the same binary, so B
buys nothing on the dependency front.

#### Recommendation: C, with B as a localized fallback

Take the CSG route. When the walker hits a node with no BRep equivalent
(`hull`, `minkowski`, `projection`, `surface`, mesh `import`), write *that
subtree alone* back out (CSG syntax is valid OpenSCAD input), re-invoke OpenSCAD
to mesh just it, splice the mesh in with a warning. Analytic everywhere
possible; mesh confined to the smallest subtree that needs it.

**This bears on decision #3.** `cuboid([20,15,10], rounding=3, edges="Z")`
compiled to `hull()` over eight thin cylinders — BOSL2's rounded boxes *are*
`hull()`. A hard `NotImplementedError` would reject one of the most common
BOSL2 idioms. Recommend the mesh fallback instead of a hard fail.

#### Revised answers to the open questions

- **#4 solid123d** — yes, emphatically; it becomes the entire backend. The CSG
  node set is nearly 1:1 with its public API. Depend on the PyPI package. Needs
  two additions: `polyhedron()` and a `multmatrix()` applying a raw 4x4.
- **#5 OPENSCADPATH** — moot; OpenSCAD resolves includes itself. Security note
  gets *stronger*: we execute OpenSCAD on user-supplied `.scad`, which can
  `include`/`import()` arbitrary paths. Prominent docs warning warranted.
- **#6 language first** — moot; OpenSCAD is the interpreter.
- **#2 `$fn`** — implementable exactly as specified; CSG gives per-primitive `$fn`.

Caveats stated: C is not an independent reimplementation — it hard-depends on
the OpenSCAD binary, and a pure-Python no-binary scad123d remains project A.
Verified on OpenSCAD 2025.07.18 against a moderate BOSL2 sample, not proof it
holds across all of BOSL2.

**Open question:** does depending on the OpenSCAD binary kill it? If scad123d
must work without OpenSCAD installed, C is off the table. If acceptable,
suggested a fidelity suite spanning mapping paths rather than 10 arbitrary
modules: `cuboid(rounding=)` / `cyl(rounding=)` (hull path), `spur_gear` /
`star` (linear_extrude+polygon), `prismoid(chamfer=)` / `rounded_prism` /
`path_sweep` (polyhedron), `torus` (rotate_extrude), `tube` (booleans),
`text3d` (fonts).

---

## 2026-08-18 — Scaffolded v1 (rungs 0 and 1)

### User

> Yes, scaffold it with rungs 0 and 1. Please add docs regarding the tradeoffs
> involved, but since 95% of OpenSCAD Minkowski use is just adding rounds, this
> will cover most of our bases and improve on existing solution.

### Assistant

Built the project. 76 tests pass, 1 skipped.

#### Layout

```
src/scad123d/
  csg.lark      grammar for OpenSCAD's flattened .csg format (~24 nodes, no expressions)
  parser.py     .csg text -> CsgNode tree
  nodes.py      CsgNode; UNSUPPORTED set
  emit.py       CsgNode -> .csg text (enables the subtree fallback)
  openscad.py   binary discovery (macOS/Windows/Linux/flatpak/snap) + invocation
  build.py      the walker; delegates primitives to solid123d
  solids.py     polyhedron() and multmatrix (no solid123d equivalent)
  facets.py     the $fn policy + analytic faceted circle/cylinder
  minkowski.py  rung 1
  mesh.py       rung 0
  errors.py
```

Public API: `import_scad(path, **overrides)`, `import_csg(tree|text|path)`,
`to_csg(path)`, plus `find_openscad`/`openscad_version`.

#### Verified results

- **BOSL2 works end to end.** `tube(h=20, or=15, ir=10)` imports as 2 planes +
  **2 analytic CYLINDER faces**, volume exact to 1.16e-16.
  `torus(r_maj=20, r_min=5)` imports as a **single TORUS face**, exact to
  1.84e-16. `prismoid(chamfer=2)` and `spur_gear(teeth=16)` build exactly via
  the polyhedron and linear_extrude paths. `cuboid(rounding=4)` takes the hull
  mesh path as predicted.
- **The payoff claim holds.** Filleting an imported BOSL2 tube selects 2
  circular edges (not triangles) and produces 2 TORUS fillet surfaces, valid
  solid, 14KB analytic STEP.
- **Rung 1 is exact.** minkowski(cube, sphere) matches the Steiner formula to
  6.7e-11, returning 6 planes + 12 cylinders + 8 spheres.
- **Faceted primitives are exact.** Hexagon + octagon + pentagonal frustum +
  square pyramid + exact cylinder summed to 6315.27 by hand vs 6315.2881 built.
- **CI tier runs with no binary**: 44 tests in 0.24s, 33 deselected.

#### Four non-obvious things found while building

1. **OpenSCAD writes CSG matrices at only 6 significant figures**, so a rotation
   arrives orthonormal to just ~3e-7 (cos 45 deg is emitted as `0.707107`). That
   residual is enough to make OCCT report the transformed shape invalid. Fixed
   by decomposing and re-orthonormalizing via Gram-Schmidt.
2. **A `gp_Trsf` carrying any scale factor other than 1 produces a shape that
   fails `BRepCheck`** — the geometry scales but its tolerances do not. Scaling
   must be applied separately, never folded into the rigid transform.
3. **`build123d.transform_shape()` is unusable** for this: it routes through
   `gp_GTrsf.Trsf()`, and OCCT never sets the form flag on an element-wise
   GTrsf, so even a pure translation raises "non-orthogonal GTrsf".
4. **`build123d.scale()` defaults to scaling about the object's location, not
   the origin.** This is an upstream bug in solid123d, which calls it without
   `about=` — OpenSCAD's `scale()` is origin-based, so bridged solid123d code
   with an off-origin object gets the wrong result. Recorded in ROADMAP.md.

Also corrected an earlier claim of mine: I had said the CSG output distinguishes
a global `$fn` from a call-site one. It does not — it records only the effective
value per node. The agreed policy is implemented as a magnitude threshold
(`facet_threshold`, default 20), which matches the intent; documented as a
heuristic in the README with workarounds.

One test-design error found and fixed: I first asserted BRep volume >= OpenSCAD
mesh volume. That flips when a curved feature is *subtracted* — an exact
cylindrical hole removes more material than a faceted one. Replaced with a
direction-agnostic convergence assertion (raising $fn must move OpenSCAD's
volume toward ours).

---

## 2026-08-18 — Committed; patched solid123d's minkowski() upstream

### User

> Yes, commit this and do rung 2
> [Request interrupted by user]
> Let's not do rung 2 yet, but let's commit. Also, can you tell me how
> solid123d handles hull & minkowski? It seems like there could be a problem
> with those calls that our rung 1 solution would resolve for most use cases

### Assistant

Committed scad123d's v1 scaffold (`fb5b59d`).

Checked `src/solid123d/booleans.py`: `hull()` and `minkowski()` both raised
`NotImplementedError` **unconditionally**, regardless of arguments -- so
`minkowski()(box, sphere(3))`, the single most common real-world Minkowski
call, hard-failed for every SolidPython-bridge user. Confirmed this was a
better fit to fix upstream than to route around: the analytic-offset trick
needs no OpenSCAD binary (pure `build123d.offset`), so it's a zero-dependency,
strictly backward-compatible addition to solid123d itself.

Patched `~/Dropbox/Projects/3D_geometry/scad_2_build123d` directly (with the
user's go-ahead): `minkowski()` now detects a bare, untransformed
`sphere()`/`circle()` at the origin as either argument (`BRepAdaptor_Surface`/
`BRepAdaptor_Curve`, checking `GeomType` and that the center is within 1e-6 of
the origin) and computes the sum as `offset(A, r)`. Verified exact against the
3D and 2D Steiner formulas, commutative in either argument order, and correct
on `minkowski(sphere(2), sphere(3))` -> radius-5 sphere. Everything else still
raises `NotImplementedError` exactly as before. Added 6 tests to
`tests/test_solid123d.py`; all 50 pass. Committed as `5ba9785`, with a
CONVERSATION.md entry in that repo per its own house convention (`9037aa0`).

**Note for later**: this fix lives in the local solid123d repo, not yet
released. scad123d depends on `solid123d>=0.1.0` from PyPI but does not
actually call `solid123d.minkowski()` -- scad123d's own rung 1
(`src/scad123d/minkowski.py`) is an independent implementation that the CSG
walker (`build.py`) calls directly, so scad123d is unaffected either way.
The solid123d fix only reaches solid123d's own (SolidPython-bridge) users once
it's released as a new version to PyPI -- that's a separate decision for the
user to make, not bundled into this session.

---

## 2026-08-18 (cont.) — Rung 2, and a real fix upstream instead

### User

> Yes, let's do rung 2.

### Assistant

Before writing code, verified rung 2's premise against the real BOSL2 calls it
was supposed to fix.

#### The premise was half wrong

Traced `cuboid([20,15,10], rounding=3, edges="Z")`'s actual CSG output. The
`hull()` node found during the original design exploration turns out to be
inert: all 8 of its children resolve to *empty* geometry (two intersection
operands with non-overlapping bounding boxes — `z in [-0.005,0.005]` vs
`z in [-0.165,-0.055]`), apparently anchor/attachment bookkeeping. The real
rounded profile is a many-vertex 2D polygon, pre-faceted by BOSL2 itself at
`$fn` resolution, fed straight into `linear_extrude` — already handled
natively by scad123d, no hull involved. Rung 2 as scoped would not have
touched this call at all.

`cuboid([20,15,10], rounding=3)` (every edge, not just `Z`) doesn't use
`hull()` either — it's `minkowski(cube, polyhedron)`, where the polyhedron has
258 vertices all at radius 3.000000 +/- 4e-6 from a shared centroid. A sphere,
expressed as an explicit facet mesh rather than a `sphere()` node. Rung 1 only
recognized a literal `sphere`/`circle`, so this took the mesh path too.

Surfaced this to the user with three options (original rung 2 only /
polyhedron-ball extension only / both) rather than silently redefining scope.
User chose both.

#### Built, in order

**Rung 1.5 — Minkowski with a polyhedron/polygon ball.** Redesigned
`minkowski.py`'s classification to operate on the *already-built* Shape for
each operand rather than the raw CSG node -- mirrors the redesign that made
rung 2 itself work on real code (see below), and was necessary here too: a
raw-node classifier stalled on a 2-child `group()` partway down the real
BOSL2 tree (one inert sibling, one real), which the existing walker's
`_union()` already resolves correctly by filtering empties. Added detection
of a polyhedron/polygon whose vertices are all equidistant from a centroid,
gated at >=24 vertices (above all 5 Platonic solids' vertex counts -- a
cube's 8 corners are *also* equidistant from centroid, so count is what
distinguishes a genuine tessellation from a deliberate few-sided kernel).
Verified against the real BOSL2 file: `cuboid(rounding=3)` now imports as 6
planes + 12 cylinders + 8 spheres, Steiner formula to ~4e-7 (limited by
BOSL2's own kernel precision).

**Rung 2 — hull of equal-radius spheres and parallel cylinders.** Classifies
each hull() child's *built* shape (single-face SPHERE, or 1-cylinder+2-plane
solid) rather than the raw tree, for the same real-code-robustness reason.
Sphere case: qhull `ConvexHull` of centers -> `offset(polyhedron, r, ARC)`,
verified exact (~1e-9) against a box-of-8-corners and a tetrahedron,
reproducing the original design-phase numbers (953.14152, 8285.4424) as
regression fixtures. Cylinder case: project onto the plane perpendicular to
the shared axis, 2D-hull, offset, extrude -- verified exact against a 4-post
rounded-rectangle case and an arbitrarily-tilted 2-cylinder case. A fully
collinear point set (2+ spheres on one line) builds an explicit capsule
instead of a degenerate hull -- also just the common 2-post slot idiom.

Two implementation bugs found and fixed along the way:
- Passing qhull's raw triangulated simplices into `offset_3d` made OCCT raise
  `Null TopoDS_Shape object` -- a box face comes back as 2 triangles sharing a
  plane equation, and offset needs merged planar faces, not adjacent coplanar
  triangles. Fixed by grouping simplices via `hull.equations` and re-deriving
  each merged face as the 2D hull of its own projected vertices.
- The first stadium (2D capsule) construction, built as a union of a
  rectangle and two circles, left 12 lateral faces after extrusion instead of
  4 -- OCCT's fuse doesn't merge the collinear segments the union introduces
  at tangent points. Rebuilt as one closed wire (2 lines + 2 tangent arcs).

Also fixed, orthogonally, discovered while tracing the real BOSL2 tree: an
intersection producing empty geometry (legitimate -- non-overlapping
operands) would later crash `apply_matrix`/`moved()` with "Cannot move an
empty shape" instead of just vanishing from the tree the way OpenSCAD treats
it. `_children()` now filters `shape._wrapped is None` the same as it already
filtered `None` itself.

**What rung 2 deliberately does not cover** (falls back to mesh, does not
crash): coplanar-but-not-collinear sphere hulls (qhull raises on degenerate
input); cylinders not sharing one axial span; mixed sphere+cylinder or
unequal-radius hulls. Documented in ROADMAP.md as Rung 2.5 and folded into
Rung 4/5's existing scope notes.

76 tests before this session; 104 after (58 more in the local run counting
fixture-parametrized cases), all passing across two clean full runs.

#### Note on solid123d, unaffected either way

scad123d's rung 1/1.5/2 are independent implementations the walker calls
directly, not routed through solid123d, so none of this session's work
touches or depends on it. For the record: solid123d's own `minkowski()`
already got the plain-ball fix in the prior session (2026-08-18 earlier,
`5ba9785`); its `hull()` still raises `NotImplementedError` unconditionally,
and the polyhedron-ball extension built here has not been ported upstream.

---

## Session: 2026-08-19 through 2026-08-23 — releases 0.1.0→0.3.0, shared cores, Gridfinity fixes

A long multi-arc session; the durable outcomes, in order:

**Releases and ecosystem.** scad123d 0.1.0/0.2.0/0.3.0 published to PyPI;
`scad2step` created as a separate thin-CLI package (repo
~/Dropbox/Projects/scad2step, PyPI 0.1.2, `uvx scad2step file.scad -o
out.step`); solid123d 0.2.0/0.2.1 published. Publishing uses `just publish`
in each repo: `.env` holds only a 1Password `op://` reference, resolved at
publish time via Touch ID -- the token never touches disk.

**import_module() redesign.** Parses .scad declarations (tokenizer in
scad_declarations.py) for real Python signatures, immediate
file/module-not-found errors, and required-argument enforcement;
`import_module(path)` with no name returns a lazily-built ScadLibrary
namespace; include-vs-use is auto-detected from file content
(suggest_import_style) with `import_style` as override; declaration scans
cached on (path, mtime).

**Color arc.** color() survives to STEP: disjoint/touching colored children
stay separate bodies (Compound) with per-part colors; overlap fuses with
first child's color; parts labeled (CSS name or hex; file stem for
import_scad roots, module name for import_module results). Gated on
authored color so uncolored models are untouched.

**Hull/minkowski rungs.** Rung 3 (all-polyhedral children via
ConvexPolyhedron, exact to float precision vs OpenSCAD) and rung 4a
(two-sphere pair via caps sewn to the external tangent cone -- sewn, not
fused, because OCCT booleans are flakiest at tangent contact; plus the 2D
two-circle keyhole, which also fixed a hard crash: OpenSCAD cannot mesh 2D
subtrees). Strictly pairwise; 3+ unequal spheres need tritangent planes
(ROADMAP rung 4).

**Canonicalization (the big refactor).** All binary-independent geometry
moved to solid123d 0.2.x as its canonical home: analytic hull/minkowski
cores, the color-preserving group()/union(), polyhedron() primitive,
color_label(). scad123d 0.3.0 deleted its local copies (-847 lines) and
imports them; its differential CI now validates the shared cores against
OpenSCAD for both packages. The prior "solid123d unaffected" note above is
obsolete: solid123d now carries everything, plus author-literal color-name
labels scad123d structurally cannot have (CSG discards names).

**Gridfinity fixes (PR #10, open at session end).** A real
gridfinity_basic_cup.scad exposed two bugs: (1) degenerate primitives --
cube([42,42,0]) for disabled features -- crash OCCT where OpenSCAD renders
nothing; now guarded to contribute nothing. (2) Worse, silent-wrong: hull()
of a module-call body arrives as ONE group-wrapped pre-fused child, and
analytic_hull's single-child identity shortcut passed it through -- valid
only for convex children; the cup lost 90% of its volume. Fixed in
solid123d 0.2.1: inputs exploded into component solids before
classification (hulls are decomposition-invariant), shortcut deleted.
Verified: cup volume agrees with OpenSCAD to 0.03%.

**Open at session end:**
- scad123d PR #10 (degenerate guards + solid123d>=0.2.1 floor): CI green,
  awaiting merge. After merge: release 0.3.1 so `uvx scad2step` users get
  the Gridfinity fixes. Local checkout deliberately left on
  fix/degenerate-primitives so the editable venv has the fix.
- User planned a batch sweep of all gridfinity_extended_openscad/*.scad
  through scad2step -- results may surface new bugs to triage.
- Outreach to gumyr (build123d author) sent re: listing scad123d/solid123d
  on the "Transitioning from OpenSCAD" docs page; he asked about seeing
  generated build123d code -- a codegen backend (emit Python instead of
  building shapes, sibling of emit.py) is feasible future work.

---

## Session: 2026-08-23 — gridfinity_silverware.scad: multi-face scaled extrude, 2D polygon hull

**User:** gridfinity_silverware.scad still fails after the PR #10 fixes:
`linear_extrude` with scale dies inside build123d's loft with
`BRep_API: command not done`.

**Diagnosis and fixes (both in solid123d, branch
fix/multiface-scaled-extrude, committed as 055b0a6, version bumped to
0.2.2 -- not yet pushed/released):**

1. **Multi-face scaled extrude.** The failing node is
   `linear_extrude(height=35.52, scale=[0.5,1])` over a profile of six
   disjoint faces (the silverware pockets, from cylsq2-style tapering).
   solid123d lofted the whole compound as one section; OCCT's loft takes
   one face per section and rejects a multi-face compound. Fixed: loft
   face by face, then group(). Second latent bug fixed in the same line:
   build123d's `scale()` defaults to scaling about each object's own
   location, so an off-axis island's top would shrink in place; OpenSCAD
   scales the cross-section about the global Z axis, so pass
   `about=(0,0,0)` -- islands migrate toward the axis as they taper.

2. **2D polygon hull rung.** Next failure downstream: `hull()` of two
   offset rectangles (the label-slot profile) had no analytic rung and
   fell back to mesh -- but OpenSCAD cannot render a 2D subtree to a mesh
   ("Current top level object is not a 3D object"), a hard fail. Added
   `_hull_of_polygons`: all-straight-edged XY faces -> exact 2D convex
   hull of combined vertices (the 2D analog of the polyhedral rung).
   Mixed circle+polygon 2D hulls still raise (no rung yet).

**Also observed:** one intermittent OpenSCAD segfault (-11) meshing a
legitimate hull-of-tapered-cylinders fallback subtree; the identical
.csg renders fine standalone and on rerun. Flaky, cause unknown, no
retry logic added.

**Verification:** solid123d 89 tests pass (5 new); scad123d 227 pass
against the fixed solid123d; gridfinity_silverware.scad now converts
end-to-end, and the STEP volume agrees with OpenSCAD's own render to
0.017%. One region still meshes (hull of 4 tapered cylinders -- unequal
radii, correct fallback).

**Open:**
- Push solid123d fix/multiface-scaled-extrude, PR, merge, publish 0.2.2.
- Then bump scad123d's solid123d floor to >=0.2.2 (PR #10 pins >=0.2.1)
  and release 0.3.1.

---

## Session: 2026-08-23 (cont.) — in-memory mesh-fallback memoization

**User:** in-memory memoization is a win; persistent is "a footgun and a
half" -- skip it. (Analytic cone-hull rung deferred; user suspects the
pattern is Gridfinity-specific and is mulling whether it's worth it.)

**Context recap:** all 120 mesh fallbacks in gridfinity_silverware.scad
come from `pad_oversize` (module_gridfinity.scad:179) -- three stacked
`hull() cornercopy(...)` calls per grid cell: 4 cones (r1≠r2), and
stepped unequal-radius cylinder pairs. 36 cells stamp identical
subtrees; only ~7 distinct.

**Implemented (branch feature/mesh-memoization off main, 1f31160):**
`mesh_subtree` memoizes on the emitted CSG source -- a complete key,
since emit() bakes transforms into multmatrix rows and $fn/$fa/$fs into
primitive calls. Module-level dict + `clear_cache()`. Every return
(including the first) is a `copy.copy` (fresh wrapped TopoDS), so
downstream in-place move()/locate()/recolor can't corrupt the cached
original. Deliberately NOT persistent: that would need OpenSCAD version
+ content of path-referenced inputs (import(), surface(), text() fonts)
in the key.

**Measured on gridfinity_silverware.scad** (temp merge with PR #10's
guards + solid123d 0.2.2 fixes, since this branch is off main): 120 ->
8 OpenSCAD renders, build 383s -> 330s (~14%; OCCT booleans dominate,
~0.4s/render saved). Also 15x less exposure to the flaky OpenSCAD
startup segfault seen earlier.

4 new tests (tests/test_mesh_cache.py): render-once for identical
subtrees, independent placement of copies, distinct subtrees render
separately, cache survives across import_csg calls, clear_cache forces
re-render. Full suite 224+2 passes on the branch; ruff clean on changed
files (3 pre-existing lint errors in untouched test files left alone).

**Branch bookkeeping:** based on main, so it does NOT include PR #10's
degenerate guards; CONVERSATION.md here carries both session entries
(will merge cleanly whichever PR lands first).

---

## Session: 2026-08-23 (cont.) — identical-translates hull rung; everything merged

**Merges (user asked to land all unmerged code first):**
- scad123d PR #10 (degenerate guards) and PR #11 (mesh memoization) ->
  main. PR #11 needed one CI fix: mesh-cache tests marked
  `needs_openscad` so the no-OpenSCAD "fixtures" jobs skip them.
- solid123d PR #3 (multi-face scaled extrude + 2D polygon hull, 0.2.2)
  -> main.

**New feature (solid123d PR #4, merged; version 0.3.0):** the
identical-translates hull rung, from the factorization discussed in
chat: hull(identical translates of X) == conv(centers) ⊕ conv(X).
Implemented for X = vertical-axis revolution solid with cylinder/cone
faces (phase A: no spheres/tori in the profile):
- `_vertical_revolution_profile`: classify a component, profile = its
  vertices as (z, radial distance from axis)
- pool profile points per center (stacked bevels arrive as several
  solids per corner), `_upper_chain` = concave envelope = silhouette
  r(z)
- ruled loft of `_rounded_section` faces (centers' 2D hull offset by r,
  one deterministic wire so loft sections always match) at envelope
  breakpoints; circle/stadium sections for degenerate center sets
- self-check vs closed-form volume: integral of Steiner A(r) = A0 +
  P0·r + πr² per linear segment; mismatch -> decline
Declines: apex (r=0) sections, tilted axes, sphere/torus profile faces,
mismatched per-center profiles. OCCT even reports true GeomType.CONE
faces from the ruled loft.

**Results on gridfinity_silverware.scad:** mesh fallbacks 120 -> 10
(remaining: quarter-torus lip hulls + one rotated-frame hull), build
330s -> 14s (~24x -- meshed pads had been dragging every downstream
boolean through triangle geometry). Volume vs OpenSCAD render: +0.034%,
consistent with exact cones circumscribing OpenSCAD's inscribed facets.
solid123d: 97 tests (8 new); scad123d: 231 pass unchanged against it.

**Open:**
- Publish solid123d 0.3.0 (`just publish`, needs 1Password Touch ID).
- Then bump scad123d floor to solid123d>=0.3.0 (or >=0.2.2 minimum) and
  release 0.3.1 so `uvx scad2step` users get everything.
- Phase B candidates (unbuilt): arcs in the profile (sphere/torus
  corner bands), apex sections, z-offset translates (3D center
  polytopes), scad123d's outdated hull-fallback warning text.

---

## Session: 2026-08-23 (cont.) — releases: solid123d 0.3.0, scad123d 0.3.1, scad2step 0.1.3

All three published to PyPI via `just publish` (1Password/Touch ID),
with the user authorizing each fingerprint prompt:
- solid123d 0.3.0 (identical-translates hull rung + 0.2.2 fixes)
- scad123d 0.3.1: floor bumped to solid123d>=0.3.0, suite re-run green
  against the PyPI wheel before publishing (231 passed)
- scad2step 0.1.3: floor scad123d>=0.3.1 (was >=0.2.0)

End-to-end proof from the published stack:
`uvx scad2step@0.1.3 gridfinity_silverware.scad` -> 51s wall including
download, one residual mesh warning (torus-lip hulls), STEP output
7.8MB -- down from 38MB when the base pads were meshed.

---

## Session: 2026-08-23 (cont.) — CLI feedback, sliver healing; scad123d 0.3.2

**CLI polish (PR #12).** New MeshFallbackWarning category; scad2step
prints fallback notes as one clean line (no file:line / `return
_fallback(` source echo), shows `converting X -> Y` up front and elapsed
seconds on the `wrote` line. Stale hull-fallback reason text updated.

**Sliver healing (PR #13, its own branch after a process stumble: PR #12
got merged mid-push, stranding the heal commit on a deleted branch --
recovered via reflog).** PrusaSlicer reported open edges on a watertight
BRep: 80 micro sliver edges (6.3e-5 mm, one pair per facet chord, all at
z=39.39) born where the analytic cone-hull fuses against a meshed
neighbor sharing a plane -- exact arc crosses inscribed polygon once per
chord. Invisible pre-analytic-rungs (both sides were identically
tessellated meshes). Fix: heal_small_edges() once per import_csg --
small-FACE fix then small-edge drop (edge fix alone leaves one
micro-edge per sliver face), gated by BRepCheck + volume preservation
(ShapeFix over-application once returned volume -5e98; the gates are
load-bearing). Per-child healing preserves colored assemblies. `heal=`
kwarg opts out. Cost: <=55ms detection always; ~5s repair only on
defective shapes. Result: 80 -> 0 micro-edges, watertight tessellation,
volume delta 6e-6 rel. Deterministic no-OpenSCAD regression test
(cylinder + coplanar 60-gon poking 2e-6 out).

**Architecture decisions (discussed, deferred):** heal mechanism
arguably belongs in solid123d (canonical geometry home) with scad123d
keeping only the auto-apply policy -- deferred, shipped in scad123d
as-is. Tessellate-and-hull fallback for solid123d's hull() totality
(OpenSCAD-parity via OCCT meshing + qhull) judged feature creep for
now. All hull *rungs* already live in solid123d, so SolidPython users
have the full analytic coverage; they differ only in failure mode
(raise vs scad123d's mesh fallback).

**Released:** scad123d 0.3.2 (tagged v0.3.2): CLI feedback + healing.
solid123d unchanged (0.3.0); scad2step unchanged (0.1.3, floor >=0.3.1
resolves 0.3.2 automatically).

**Session close:** stale `uvx` environment cache was serving the old
scad123d; `uvx --refresh scad2step ...` re-resolved to 0.3.2 and worked.
Done for the night.

---

## Session: 2026-08-24 — Phase B: arc profiles + any shared axis (solid123d PR #5)

Prefaced by investigation/demo work: mapped every hull() pattern in
gridfinity_extended_openscad (the quarter-torus lives in roundedDisk,
modules/utility/module_utility.scad:530; roundedCylinder:500 hulls two
of them; cup cavity = hull cornercopy roundedCylinder at
module_gridfinity_cup.scad:1341), built ~/Desktop/hull_shapes_demo.scad
visualizing the seven idioms, and discussed the theory: support
functions + normal fans (hull of union = max of supports; Minkowski =
sum; boundary of polytope (+) K = one face family per fan cell). User
pushed on generality; phase B was restructured around the theory.

**Implemented (feature/hull-arc-profiles, PR #5, CI green, 0.3.1
unreleased):**
- Profile = typed elements (points from vertices; circles from
  sphere/torus faces w/ trimmed spans). Envelope = support-function
  sweep: pairwise switch angles + trim-endpoint kinks as candidates,
  argmax per interval. Honest trimmed support (outside spans a circle
  yields -inf; endpoints stand in as points) means the envelope can
  never use boundary the child lacks -- turned the "missing boundary"
  decline class EMPTY (flat-top caps now succeed correctly).
- Any shared axis via conjugation (rotate to +Z, solve, rotate back);
  sphere/torus V-convention flips with axis sign (bug found via
  -Z-landing rotation).
- Arc construction = normal-fan face emitter: piece x edge -> prism
  face (lines->planes, arcs->horizontal cylinders); piece x vertex
  wedge -> revolve face (lines->cones, arcs->sphere/torus bands); caps;
  BRepBuilderAPI_Sewing(1e-5) + ShapeFix_Solid. Line-only profiles keep
  the loft. Tangency float noise handled: candidate clustering (1e-9),
  connector threshold 1e-6, chain-piece merge.
- Volume check: closed-form arc integrals (_arc_f_r/_arc_f_r2).

**Results:** solid123d 104 tests (8 new: sphere-capped posts, 4 tori,
single torus, tilted-axis conjugation, cavity idiom, truncated-sphere,
mixed-axes decline; obsolete tilted/missing-boundary declines flipped
to successes). Silverware: 10 -> 2 fallbacks (only the sideways
pre-fused roundedCube channels remain), build 14s -> 9s, STEP 7.8 ->
4.3 MB, 0 micro sliver edges pre-heal (analytic/mesh seams gone),
volume -0.008% vs OpenSCAD render (tighter than phase A's +0.034%).
scad123d suite: 234 pass + 1 obsolete-limitation failure
(test_coplanar_noncollinear_sphere_hull_falls_back_to_mesh -- coplanar
equal-sphere hulls now analytic; MUST flip to a success test in the
scad123d floor-bump PR or scad123d CI breaks when 0.3.1 publishes).

**Awaiting user go:** merge PR #5, publish solid123d 0.3.1, then
scad123d: floor >=0.3.1 + fix that test + release 0.3.3.

---

## Session: 2026-08-24 (cont.) — released: solid123d 0.3.1, scad123d 0.3.3

User gave the go; executed the full sequence:
- solid123d PR #5 merged, tagged v0.3.1, published to PyPI.
- scad123d PR #14: floor solid123d>=0.3.1, version 0.3.3, and the
  obsolete-limitation test flipped
  (test_coplanar_noncollinear_sphere_hull_is_analytic, checked against
  the rounded-slab closed form 2rA0 + (pi r^2/2)P0 + 4/3 pi r^3). Suite
  235 pass against the PyPI wheel before publishing. Merged, tagged
  v0.3.3, published.
- scad2step unchanged (0.1.3 floor >=0.3.1 resolves 0.3.3).

End-to-end from PyPI: `uvx --refresh scad2step gridfinity_silverware`
-> 10.6s conversion, ONE clean fallback note (the two sideways
roundedCube channel hulls, deduped by the warning system), STEP 4.3 MB.
The new CLI output shows: converting line, single-line note, timed
wrote line.

---

## Session: 2026-08-24 (cont.) — openGrid empty-subtree bugs; scad123d 0.3.4

User reported QuackWorks openGrid/opengrid.scad erroring ("Current top
level object is empty") where OpenSCAD is fine. Investigation found TWO
bugs, one crashing, one silent-wrong:

1. hull()/minkowski() of only-empty children (disabled features leave
   empty groups) hit the mesh fallback; OpenSCAD errors rendering an
   empty subtree standalone. Fixed: walker short-circuits empty children
   to None; mesh_subtree treats the empty-render error as "encloses
   nothing" (safety net).
2. Silent 63% volume error found by differential subtree bisection
   (recursive ours-vs-OpenSCAD volume compare, treating the empty error
   as 0): _children drops empty results, correct for unions but wrong
   for positional booleans. Empty intersection operand must annihilate
   (we returned the other operand: 1600mm^3 phantom); empty FIRST
   difference child must empty the result (we promoted the first
   subtrahend to minuend). New _children_positional keeps empties as
   None in position; both ops fixed, OpenSCAD semantics confirmed
   differentially.

openGrid: errors -> converts in 3s, volume delta 0.038%. 5 new tests
(240 total). PR #15 merged, tagged v0.3.4, published. Verified via
`uvx --refresh scad2step` from PyPI.

---

## Session: 2026-08-24 (cont.) — scad123d-diff productized (PR #16)

The differential bisector from the openGrid debugging is now a real
tool: `scad123d-diff file.scad` (src/scad123d/diff.py, console script).
Compares our volume vs OpenSCAD's render per subtree, descends into
divergence, bottoms out on operations whose children agree but whose
result doesn't; saves culprit CSG next to the input. Empty-render error
= volume 0. Default 2% tolerance. Mocked bisection-logic test + real
smoke test; openGrid now reports "agreement within 2% everywhere".
Merged to main; ships with the next release (unpublished so far).
Saved to Claude's memory as the go-to for silent-wrong suspicions.

---

## Session: 2026-08-24 (cont.) — repo-wide ruff pass

Both repos now pass `ruff check` and `ruff format --check` on every
file. solid123d PR #6 (format-only, 7 files); scad123d PR #17 (2
auto-fixes -- unused import, useless return None -- plus 14 files
reformatted). Suites green (104 / 242). Both merged; no release.

## 2026-08-25

**User**: scad2step didn't apply the customizer parameter-set JSON
sitting next to an input .scad (battery organizer from Downloads).

**Assistant**: scad2step had no customizer support. Now: a sibling
<input>.json applies automatically (announced on stderr), selection is
-P NAME > set named "default" > only set > error naming candidates;
-p/--parameter-file points elsewhere, --no-customizer opts out. Values
flow to OpenSCAD as -D literals; explicit -D flags beat the file.
Selection/loading shared with solid123d via its new
customizer.resolve_param_set (unreleased — bump the solid123d floor
from >=0.3.1 when it ships). 8 new tests; 250 pass. On branch
feature/customizer-param-sets, uncommitted.

**User**: Keep scad2step's customizer behavior as-is; release. Restore
the dependency on PyPI solid123d (was a local editable install).

**Assistant**: Bumped to 0.4.0, floor solid123d>=0.4.0 (provides
resolve_param_set). Lock/sync deferred until solid123d 0.4.0 is on
PyPI (published manually by user).

**Assistant** (release): solid123d 0.4.0 and scad123d 0.4.0 published to
PyPI via `just publish` (op/Touch ID). solid123d: PR #7 merged, tagged
v0.4.0 + GitHub release with artifacts. scad123d: PR #18 merged (CI
green incl. windows), tagged v0.4.0 (no GH release, matching repo
convention), locked against PyPI solid123d 0.4.0 — editable install
replaced; 250 tests pass against the published dependency.
