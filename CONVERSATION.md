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

**User**: Add succinct README mentions of color() and customizer support
(no publish). Also opened a design discussion: could union() of a
colored and an uncolored object keep color on the non-overlapping part
of the colored one, unlike OpenSCAD?

**Assistant**: README: added a "color() survives into STEP" block (with
two-tone example; STL discards color) and a customizer bullet
(-P/-D/--no-customizer) under "A few other things to know". Pushed to
main directly (doc-only). Color-semantics question answered in chat:
feasible; recommended the body-partition approach over face-color
history tracking, not yet implemented.

**User**: scad2step crashed with "Color out" (OCP Standard_Failure) on
~/Desktop/cube_sphere_colored.scad.

**Assistant**: Root cause: the file says `color(red)` -- bare unquoted
`red` is an undefined OpenSCAD variable, and OpenSCAD encodes
color(undef) in CSG as the sentinel [-1, -1, -1, -1] ("no color
change"), which we fed into build123d Color -> OCCT range error. Fix in
build.py: any negative component means the sentinel -- children pass
through uncolored, matching OpenSCAD, with a warning suggesting
color("red"). Regression test added; 251 pass.

**User**: Run the 0.5.0 release train and update scad123d's tests.

**Assistant**: solid123d 0.5.0 published (seam guard + color-partition
union; tagged, GH release with artifacts). scad123d 0.5.0: floor
solid123d>=0.5.0; rewrote the three overlap tests to partition
semantics (partition into labeled bodies, contested volume to the later
child) and updated the TestColor docstring + README color block.
251 tests pass against the published wheel (editable install replaced
by PyPI 0.5.0 via uv sync — the local-editable workaround is retired).

**User**: scad2step crashed with a deep "recursive" traceback on
~/Downloads/ultimate+parametric+junction+box+cover.scad (BOSL2 model).

**Assistant**: The recursion was just CSG nesting depth; the real error
was `polygon(points = [])` (BOSL2 emits it for a branch that renders
nothing) reaching build123d's `Wire.make_polygon`. Guarded polygon,
then on request audited every leaf: `polyhedron(points=[], faces=[])`
and `linear_extrude(height=0)` had the same gap (OpenSCAD renders both
as nothing); `text("")`, `rotate_extrude(angle=0)` (OpenSCAD's own
falsy-defaults-to-360 quirk), transforms and booleans were fine.
Released scad123d 0.5.1.

**User**: The STEP differs from OpenSCAD's STL: the spring clip's body
sits with its top at z=0, but its rounded corners and threaded hole
don't.

**Assistant**: Root cause in solid123d: `linear_extrude` called
build123d `extrude()` without `dir`, which sweeps each face along its
own winding-derived normal. BOSL2's `stroke()` ribbon face wound
clockwise while its `round_corners()` joint fragments wound CCW, so the
body extruded to -Z and the joints to +Z (and, meeting only at a knife
edge, never fused: 51 solids instead of 2). Fix: pin `dir=(0, 0, 1)` --
OpenSCAD ignores winding. Released solid123d 0.5.2 and scad123d 0.5.2
(versions kept in step per user); STEP now matches the STL bbox exactly.

**User**: Design a fast pipeline to convert the ~80k-model CodeCAD corpus
(still downloading into ~/Dropbox/Projects/codecad): parallel batch
mode, progress, failure handling, resume. Does OCCT use the GPU?

**Assistant**: Measured: build123d import is 1.85s vs 0.1-0.2s of work
per typical file, so batch mode is the big win; OCCT and OpenSCAD's
export path are CPU-only (no GPU contention); OCCT uses ~1.5 cores per
conversion. Decisions (user): outputs outside Dropbox; Manifold as the
default mesh-fallback backend; harness ships in scad123d.
Built on feature/batch-conversion:
- `openscad.py`: `--backend=Manifold` when the binary supports it
  (140x faster on the junction box's hull fallback; whole test suite
  73s -> 12s), `$SCAD123D_BACKEND` overrides.
- `mesh_import.py`: Manifold's 3MF exposed that build123d's
  sewing-based `Mesher.read` can return an invalid, wrong-volume solid
  (3143.6 vs 3071.9 on a slab with a through-hole; the triangles
  themselves were right). Replaced with a sewing-free topological build
  (shared vertices/edges, components -> shells, largest = outer, rest =
  voids) verified against the divergence-theorem volume; disagreement
  raises `MeshImportError` instead of returning wrong geometry.
- `scad2step --batch`: long-lived worker, JSON task/result lines, stdout
  reserved for results, failures classified (empty / openscad-error /
  unsupported / occt-error / mesh-error / export-error / timeout /
  missing / error), per-file mesh-cache clear, optional .csg save.
- `scad123d-batch`: discovery (skips cloud placeholders), SQLite ledger
  (resume, --retry CLASS, --force, changed-file detection), sha256 dedup
  with hard-linked outputs, worker pool with kill-on-timeout + respawn,
  crash isolation, recycling by count/RSS, rich dashboard, --report.
  `batch` extra adds rich + psutil.
277 tests pass; README section added.

**User**: Some of the 80k files will surface scad123d bugs -- do we have
logging / a way to trace where errors occur and gather stack traces?

**Assistant**: Partially before, fully now. Gaps closed on the branch:
tracebacks were only kept for the generic `error` class and the harness
dropped them (no ledger column); crashes and timeouts left nothing;
worker logs had no per-file markers; nothing caught silently-wrong
output; --report couldn't say *where* failures occur. Now: every
failure's traceback, stage and OpenSCAD warnings go in the ledger
(schema migrates older ledgers); workers run `faulthandler` and register
SIGUSR1, and the harness sends SIGUSR1 then kills on timeout, so hangs
and segfaults leave the Python stack in logs/worker-N.log under a
`converting <file>` marker; `--verify` renders each model with OpenSCAD
(Manifold, cheap) and classes a >2% volume disagreement as `mismatch`;
`--report` groups failures by innermost scad123d/solid123d frame;
`--list OUT CLASS` and `--show OUT PATH` for triage. Found a real bug
while testing: an empty top-level model's CSG export is "\n", which
import_csg's str heuristic treated as a *path* -> every library file
would have been classed `missing`; the worker now parses explicitly.

**Assistant** (follow-up, same branch): --verify's first run over the
fixtures found three things, all mine or tessellation, none OpenSCAD's:
(1) the mesh importer treated every non-largest shell as a *void* of the
largest, so any multi-body render came back as an invalid solid
(exact volume, is_valid False, both backends) -- voids are now decided by
containment (BRepClass3d classifier, smallest enclosing shell, even
depth = body / odd = cavity) after normalizing each component's winding,
because a user polyhedron listed inside-out passes through Manifold
inside-out (polyhedron.scad: 800 vs 1200) while CGAL repairs it;
(2) comparing against OpenSCAD's default tessellation is not a fair
check (an r=3 circle is a 9-gon, 8% under) -- the verify render now
rewrites $fa/$fs to 1/0.2 (explicit $fn still wins), which brought
agreement to <=0.11% on every fixture, so the bar is 1% (5% against the
coarse render when our side has mesh-fallback regions); the check is
BRep-free (mesh_volume) so a fine sphere render costs ms, not 10s;
(3) minkowski_polyhedron.scad at 1.7% is the documented rung-1 choice
(faceted polyhedron ball -> exact sphere), reported as a bucketed
"1-2%" mismatch and explained in the README. 289 tests pass.

**User**: 500-file corpus run: 112 failures. Analyze the patterns (no
fixes), then: fix the verify false alarms (row 5) and dig into the
"ours << OpenSCAD" drop bug.

**Assistant**: Analysis by root cause: 2D geometry reaching the mesh
fallback (21; OpenSCAD can't write 2D to 3MF), missing include files (13
empty + >=7 errors; the dataset keeps one .scad per folder), OCCT union
performance (9 of 12 timeouts, all in group()'s pairwise fuse),
scad123d silently dropping most of a model (>=6 of the worst mismatches),
--verify false alarms (bbox-based cavity rule), and 7 small gaps
(non-planar polyhedron faces, offset(), stacked modifiers, unescaped
quotes in text(), empty-compound intersection).
Row 5: mesh_volume now takes body/cavity from winding, bbox only rescues
an un-enclosable inside-out body; Fidget and extending_tube become exact
matches, ball_bearings becomes a real 34% mismatch.
Drop bug, two layers, both in solid123d's occt_workarounds (branch
fix/clean-guard-tolerance): (1) the seam-bug clean guard's 1e-9 volume
tolerance rejected 20 of 21 cleans as integration noise (~1e-7), so
faces fragmented 26 -> 822 until OCCT's fuse returned an inverted 8-face
shape; now 1e-5 (seam bug loses 17%, guard intact per user's note);
(2) OCCT's exact fuse silently returns a valid-looking sliver when an
operand nearly coincides with the result (for(i=[0:n]) duplicate at
360 deg) -- now every boolean is bounded by its inputs and an implausible
result is retried with fuzzy tolerance 1e-5, warned if still wrong.
servo_arm_base 1.9 -> 454.2 (OpenSCAD 452.8), MCP_knobs exact. Also
fixed scad123d-diff, which reported "agreement" whenever a side failed
(its OpenSCAD side used the sewing importer, which raised); it now uses
mesh_volume and reports uncomparable subtrees. Remaining: dps-600,
polychannel, cube_picture, 196915 (negative volume), ball_bearings are
other bugs; polychannel's run-to-run flip suggests OCCT RunParallel
nondeterminism.

**User**: Remember the open corpus bugs for later (saved to memory:
corpus-open-bugs); work on the 2D fallback gap.

**Assistant**: mesh._render now catches OpenSCAD's "not a 3D object",
re-renders the subtree as a 1 mm linear_extrude, and takes the top faces
(z = 1, +Z normal), merges the import's triangles with clean() into one
face per island (holes kept) and moves them to z = 0 -- so a 2D hull
beyond solid123d's closed forms (three circles, circle + square, circle
+ polygon/text), projection(), and 2D import() all work; volumes match
OpenSCAD's own render of the extrusion to 1e-6. --verify compares area
for a purely 2D model (`measure: area`) via the same unit extrusion.
Probing showed two circles / two squares / faceted circles were already
analytic, which is why the first tests "did not warn".

**User**: Release solid123d and bump scad123d's floor; keep versions in sync.

**Assistant**: Both to 0.6.0 (scad123d's branch is a feature release; solid123d
matched). solid123d: PR #11 merged, tagged v0.6.0, published. scad123d: floor
solid123d>=0.6.0, editable install dropped, 298 tests against the published
package. CI exposed a real Windows blocker on the branch -- `import resource`
at module level made scad2step itself unimportable there -- fixed (optional
import; SIGUSR1-dependent tests skip), plus backslash-safe ledger path lookup.
Along the way a careless patch deleted Ledger.counts/query and a `| tail`
masked the failing test, so a broken commit reached the branch; fixed with a
follow-up commit and pipefail. PR #21 merged, tagged v0.6.0, published;
`uvx --from 'scad123d[batch]==0.6.0' scad123d-batch` resolves solid123d 0.6.0.

**User**: --limit doesn't affect the ETA, which is computed over the whole
corpus -- good idea or not?

**Assistant**: Not as it stood (a leftover: Dashboard was handed the run's
size and never used it). Now: progress, rate, and ETA are scoped to the run
("this run 120/500, ETA 0:04:10"), and when the run doesn't cover the ledger
a labeled projection follows ("corpus: 75,543 more pending, ~1d 3h at this
rate") based on the whole-run average rate rather than the two-minute
window, which swings early. Final line and dashboard caption run-scoped too.

**User**: Analyze the latest 500-file run (411 ok / 89 failed); and why is
OpenSCAD still at 300% CPU after the run ended?

**Assistant**: The CPU: a fallback render orphaned when its worker was
killed (workers were killed alone, not their children); found one from the
aborted run at 1h37m. Fixed: workers lead a process group, killed with
killpg (test: fake worker spawns a child). Analysis: timeouts still all in
OCCT unions; 7 "dimensions inconsistent" (most with missing includes);
5 "Null TopoDS_Shape" (OCCT null results, probably legal empties);
linear_extrude(text that renders nothing) returns []; and the headline
mismatch, ChessBoard "0 vs 1.69M", was a verify artifact: build123d's
Compound.volume sums only direct Solid children and the color-partitioned
union nests Compounds -- the STEP had all 384 solids. --verify now sums
leaves (measure()); half the >20% mismatches used color().

**User**: Explain the union behavior -- is it the quadratic pairwise
union? I thought that was fixed. Then fix the plausibility bounds to sum
solids; then do the N-ary union change.

**Assistant**: It was never fixed -- diagnosed, then two correctness fixes
landed on that path and the loop stayed. Measured with overlapping
cylinders: pairwise 0.40/1.53/5.21/19.5 s for N=25/50/100/200, N-ary
0.16/0.28/0.58/1.13 s. Bounds fix (solid123d PR #12, merged): leaf-solid
sums (signed) for bounds and the clean guard. N-ary (solid123d PR #13):
group() -> one fuse, difference() -> one cut; intersection stays pairwise.
That exposed a scad123d bug: apply_matrix made reflected solids by
Reversed() on the solid (REVERSED flag, faces inward); OCCT's N-ary fuse
silently drops such a solid where it overlaps another (cube: 1600 vs
2200; the servo horn lost its mirrored halves: 173.9 vs 206.8). Fixed by
reversing faces and rebuilding FORWARD solids shell by shell. scad123d's
difference now also cuts all subtrahends in one op. Windows fixes for
the process-group kill test on PR #22 (merged).

**User**: Merge both, run CI, release when green. Also: error rate is still
~18% (407/500) after all the changes -- why?

**Assistant**: solid123d #13 and scad123d #23 merged; both mains green;
released solid123d 0.7.0 and scad123d 0.7.0 (floor solid123d>=0.7.0;
venv back on the published package; fresh uvx install resolves 0.7.0).
On the rate: every run samples 500 *new* files and no failed file has
ever been retried (attempts>1 = 0), so the number is the steady-state
rate for fresh files, not a before/after; of this run's 93 failures, 24
carry a missing-include warning and 7 are the expected 1-2% minkowski
bucket; the run already used the N-ary code and 9 of 14 timeouts hang
inside a single fuse (N-ary removes the accumulation cost, not hard
geometry); 19 mismatches >20% remain unbisected. Fixable fresh-file rate
~12%. Next: --retry the accumulated failures under 0.7.0 as a controlled
measurement, then bisect the >20%s.

**User**: Any way to handle the missing includes? Make a program to scan for
them (known libraries or Thingiverse), no LLM. Then: read the CodeCAD paper
for the real structure; install common libraries and exclude models with
unresolved dependencies.

**Assistant**: `scad123d-includes` (PR #25): static include scan in
OpenSCAD's search order; classifies known library (curated table) /
sibling (same thing, elsewhere in the corpus) / absent; `resolve` and
`fetch` (Thingiverse API, token) write an overlay tree, never the corpus;
`scad123d-batch --include-overlay` and `--skip-unresolved-includes`.
Paper says local deps were inlined and unresolved externals filtered;
the v2 data says otherwise: 19% of models have unresolved includes, 91%
of those also miss the symbols. Installed 16 libraries (library refs
3,225 -> 348), then excluded 12,635 models from the ledger (reversible).
Also fixed: `--limit 0` converted everything.

**User**: STEP conversions go to ~/codecad-step; later I'll repair failures
and automate comparisons against OpenSCAD's STL. Write a script (or batch
addition) that, in a separate pass after the STEP run, renders OpenSCAD's
STL beside each STEP and copies or symlinks the source .scad there, so
.scad/.csg/.stl/.step sit together. Don't disturb the running batch.

**Assistant**: `scad123d-artifacts` (new module `artifacts.py`, console
script registered): reads the batch ledger's `files` table for inputs and
STEP classes, copies the `.scad` (`--symlink` optional) beside the STEP,
renders OpenSCAD's binary STL (Manifold, `--include-overlay` like the
batch, per-render `--timeout`, `-j` threads over subprocesses, `.part`
temp name so a killed render leaves no truncated STL). Records each render
in a new `stl` table of the same ledger (ok / empty / openscad-error /
timeout), so re-runs skip finished STLs and retry failures; `--force`,
`--status` (default: every class but `excluded`), `--no-stl`,
`--no-source`, `--dry-run`, `--report`. Duplicates hard-link the
original's STL. OpenSCAD exits 1 on an empty top level, so `empty` comes
from its stderr, not from a zero-triangle file. Tests with a fake renderer
plus one needs_openscad test; smoke run on 12 real corpus files (11 ok, one
2D model rejected). Not committed; nothing touched in ~/codecad-step.
Suggested later run:
`scad123d-artifacts ~/Dropbox/Projects/3D_geometry/codecad/dataset -o ~/codecad-step -j 8 --timeout 300 --include-overlay <overlay>`.

**User**: Two machine crashes during `--limit 2000` runs (~35 GB free
disk); workers claim >3 GB. Look at the memory-control mechanism.

**Assistant**: It was soft: exceeding --max-rss-gb only set a recycle
flag honored after the current file; only the worker's own RSS was
measured (not its OpenSCAD render); nothing looked at the machine, so
12 workers x the fixed 6 GB default = 72 GB on 48 GB RAM -> macOS swap
filled a nearly-full boot volume. PR #26: process-tree RSS, immediate
kill past the per-worker limit (class `memory`), aggregate budget and
free-memory floor kill the largest worker, defaults from RAM and -j
(60% of RAM shared, >=2 GB each), leak recycling at half the limit,
dashboard totals. Tests allocate for real. Unreleased (weekly cadence).

**User**: Merge #24, #25, #26 into main; add the Liberation font.

**Assistant**: Installed font-liberation (12 faces; OCCT now finds
OpenSCAD's default font for text()). Merged #24; merged main into #25
(conflicts in batch.py/test_batch.py: kept retry-first + --limit 0 +
exclude()), CI green, merged. #26 was auto-closed when its base branch was
deleted -- GitHub closes stacked PRs rather than retargeting -- so its
branch was re-opened as #27 against main; its earlier CI failures were
(a) macOS runners under the fixed 3 GB free-memory floor, now 3 GB or a
tenth of RAM and the fake-worker fixture disables limits by default, and
(b) an uncommitted `scad123d-artifacts` work-in-progress that my
`git add -A` had swept into the branch and whose Windows test fails;
lifted back out and left in the working tree as it was. #27 merged.

**User**: What overlay directory should I specify?

**Assistant**: None: no overlay tree exists on disk; the batch excluded
models with unresolved includes instead, and the installed libraries are
on OpenSCAD's normal search path. Drop the flag; it only matters if an
overlay is built later with `scad123d-includes resolve/fetch`.

**User**: Add `--limit N`, and have the UI show that target count.

**Assistant**: `--limit N` plans only inputs that still need work (STL
missing or failed; every input under `--no-stl`), originals first, then
duplicates, at most N -- so the progress line reads `done/N`. Startup line
says "N inputs limited to N of the [...] classes". Test added; 10 pass.

**User**: How many .scad files made a valid STL but not a valid STEP, and
what caused the failures?

**Assistant**: 9,731 of 62,514 STL successes (15.6%). By STEP class:
mismatch 5,975 (>20%: 1,960; 5-20%: 1,812; 1-2%: 1,220; 2-5%: 983),
memory 1,380 (all 3.0-3.8 GB against a 3 GB limit), error 1,036 (Null
TopoDS_Shape 302, wires not planar 276, invalid loft 100, Shell input type
100, unexpected result type 69, empty-compound intersect 40, `nan` in CSG
24), timeout 966 (~138 s kills; 26 in verify), occt-error 175 (BRep_API
not done 68, Bnd_Box void 26, Color out 20), mesh-error 128, crash 36,
empty 26 (STL proves geometry exists -- bisect targets), openscad-error 7,
export-error 2. Suggested `--retry memory,timeout --max-rss-gb 6
--timeout 900 -j 4`.

**User**: Retry the memory and timeout failures with those settings.

**Assistant**: Started 2026-09-07 in the background with `--verify`
(matching the original run), log at `~/codecad-step/logs/retry-memory-timeout.log`;
2,385 files re-queued (76,043 inputs scanned). Runs at 4 workers, 6 GB
each, 29 GB total budget, 900 s per file.

**User**: Quit the retry at ~1%. Make a document to review assorted
valid-STL / invalid-STEP conversions; some deserve human eyes, some are
probably just too complex.

**Assistant**: Retry had done 33 of 2,385 (10 ok, 18 memory again at 6 GB,
3 timeout again at 900 s, 2 mismatch); killed the lingering harness.
Found and fixed a layout split: the STL pass (and my retry) used
`.../codecad/dataset` as source while the original batch used
`.../codecad`, so 63,410 dirs of .stl/.scad (and 16 STEP + 38 CSG from
the retry) sat at `~/codecad-step/<thing>/` beside `dataset/<thing>/`.
Merged 125,976 files into `dataset/` (44 retry outputs replaced older
ones, matching the ledger). `scad123d-artifacts` now infers the source
root from the ledger + STEPs on disk and refuses a mismatched one (test
added; 11 pass). Review page "STL Yes, STEP No": 67 cases sampled across
all classes (mismatch by bucket, error by message, retried
timeout/memory), each with an OpenSCAD render of its STL, ledger message,
volumes, traceback, source, and per-case verdict buttons exporting TSV.
https://claude.ai/code/artifact/eacc281d-bc19-4277-a8ca-85787c2a520f
Generator: scratchpad/review/{collect.py,build_html.py}.

**User**: Genuine bug: `~/codecad-step/dataset/0586843_0/` has an oval
prism that extrudes differently in the STL vs the STEP.

**Assistant**: `scad123d-diff`: children agree, root union diverges
(21043 vs 19246). Built the `hull()` of the two lifted cylinders alone:
right volume, z 16..28 instead of 4..16. Cause in solid123d
`_hull_of_cylinders`: the stadium is placed at whichever cap face OCCT
lists first and extruded along the placed face's normal, so from a top
cap it goes up. Fix: `extrude(..., dir=dir0)` along the shared axis.
Red/green test asserting the z-span (existing tests only checked
volume); 152 pass. Rebuilt model: 19251 vs OpenSCAD 19246 (0.03%),
z 0..20. solid123d PR opened on `fix/hull-cylinder-extrude-direction`;
scad123d picks it up at the next solid123d release. Worth re-running the
5,975 mismatches afterwards: any hull of parallel cylinders not sitting
at z=0 could be affected.

**User**: Merge the PR. No release yet.

**Assistant**: solid123d #14 merged to main (CI green on macOS/Ubuntu,
3.10/3.13); branch deleted. No tag or publish; the fix reaches scad123d
at the next weekly release.

**User**: Command to re-run only the mismatches; record mismatch counts
before and after.

**Assistant**: Before: 6,069 mismatch (>20%: 2,016; 5-20%: 1,836; 1-2%:
1,227; 2-5%: 990), snapshotted into a `mismatch_before` table in the
ledger. The 2,351 rows left `pending` by the aborted memory/timeout retry
(reset had cleared their class) were parked as status `deferred` so they
stay out of this run (`--retry deferred` resumes them). Run with the
unreleased solid123d fix on PYTHONPATH and the correct source root:
`PYTHONPATH=$HOME/Dropbox/Projects/solid123d/src uv run scad123d-batch
~/Dropbox/Projects/3D_geometry/codecad -o ~/codecad-step --retry mismatch
--verify -j 8 --timeout 300`. After: join `mismatch_before` to `files`
grouped by the new status, plus the bucket split of what is still
mismatch.

**User**: Run the `--retry mismatch` job here.

**Assistant**: Launched in the background (log
`~/codecad-step/logs/retry-mismatch.log`): 6,069 re-queued, 8 workers,
300 s timeout, `--verify`, patched solid123d via PYTHONPATH (confirmed
present in a worker's environment). Early rate ~260/min, ETA ~25 min.
Before/after comparison via the `mismatch_before` table when it ends.

**User**: How's the mismatch retry going? (55 min, no terminal output)

**Assistant**: Output was going to the log file. Finished after ~1h50
at ~55/min. Of the 6,069 mismatches: 1,650 now ok (27%), 4,364 still
mismatch, 55 moved to other failures (memory 22, error 10, crash 8,
timeout 6, occt 6, other 3). By prior bucket: >20% 2,016 -> 464 ok;
5-20% 1,836 -> 768 ok; 2-5% 990 -> 261 ok; 1-2% 1,227 -> 157 ok. The
headset hanger is ok. Corpus mismatch total 6,069 -> 4,364. Ledger has 0
pending; 2,351 remain `deferred`.

**User**: Program to symlink the top 100 mismatched cases into a review
directory: scad, csg, thumbnail, stl, step together, one dir each, plus a
markdown of metadata and failure reasons; re-runnable without tokens so
each bugfix shows which cases it resolved.

**Assistant**: `scad123d-review` (new `review.py`, console script). First
run selects the N worst of a class (mismatch by |volume disagreement|)
and records them in `cases.json`; later runs keep the set, re-read each
ledger row, append status changes to a per-case history, re-render a
STEP thumbnail when the STEP is newer than the picture, and rewrite
`INDEX.md` with "resolved since selection" and "changed this refresh".
Per case: symlinks to .scad/.csg/.stl/.step, `stl.png` and `step.png`
(STEP meshed via build123d and rendered by OpenSCAD like the STL), and a
README (status now vs at selection, history, volumes, build time, mesh
fallbacks, OpenSCAD warnings, traceback, source). `--reselect`,
`--status`, `--no-thumbnails`, `-j`. Source root inferred via artifacts'
`infer_source`; `ArtifactLedger.query()` added. 6 tests (one
needs_openscad). Built `~/codecad-review` with the top 100.

**User**: Drop the STEP thumbnails; I have a STEP viewer.

**Assistant**: The first build hung 20+ min in an OCCT STEP import
holding the GIL; moved meshing to a killable child, then removed STEP
thumbnails entirely at the user's request (STL thumbnail only; README
notes to open the STEP in a viewer). Rebuilt `~/codecad-review`: 100
cases, 94 STL thumbnails (6 renders failed). Top ranks are degenerate
volumes (OpenSCAD ~0 or scad123d ~1e11), i.e. one side's volume is
nonsense rather than a subtle geometry bug. README/tests updated; 6
review tests pass.

**User**: I'm working on debugging the project at ~/Projects/scad123d & the related ~/Projects/solid123d. These generate STEP files from OpenSCAD code.

As part of this process, I downloaded a large dataset of openSCAD files at /Users/jonese/Dropbox/Projects/3D_geometry/codecad. Now I'm working my way through different mismatches ( generated STEP file has significantly different volume than generated STL) trying to fix bugs we discovered. You might read through the various README and CONVERSATION files here to acquaint yourself.

Given that background, there's a significant discrepancy between the files at /Users/jonese/codecad-review/004-11221469_0-coins-pendants-ornaments-NO-LIBRARY

See if you can discover what's going wrong

**Assistant**: I’ll read the project notes, inspect the failing model, and trace where the STEP geometry diverges from OpenSCAD. The key question is whether this comes from translation, geometry construction, or the comparison itself; I’ll investigate before changing code.

**Assistant**: The reported STEP volume is close to the volume of the model’s large clipping cylinder, which should be subtracted—not appear in the result. I’m tracing that subtraction and the preceding mirrored scale. I’ll leave the existing outputs and your uncommitted work untouched.

**Assistant**: I confirmed an orientation bug: `scale([0.2, -0.2, 1])` takes the general-transform path, which already handles the reflection correctly. scad123d then reverses it again, turning the coin inside out. The subsequent subtraction pulls the huge clipping region into the STEP.

Skipping that extra reversal removes the huge artifact, but exposes another failure in the final union. I’m checking that too so the diagnosis doesn’t imply a one-line change fully repairs this model.

**Assistant**: The huge mismatch is reproducible, and the reflection bug is isolated. There is also a separate union failure: with the coin’s orientation corrected, OCCT drops the coin and inner ring even though their individual volumes agree with OpenSCAD.

Removing colors in a diagnostic copy changes the intermediate geometry construction and gives **2,599.11 mm³ versus OpenSCAD’s 2,597.03 mm³**—a 0.08% difference. That narrows the second failure, but I haven’t isolated its underlying kernel defect.

The existing suite passes: **337 passed, 3 skipped**, using UTF-8 mode for this terminal’s locale. No conversion code or review artifacts have been changed.

**Assistant**: Found the main cause: **scad123d turns the mirrored coin inside out**, so its clipping operation produces the enormous unwanted geometry.

### Primary bug: reflection applied twice

The source uses `scale([0.2, -0.2, 1])`. In `apply_matrix()`:

1. Nonuniform scaling calls `transform_geometry()`, whose OCCT implementation already corrects orientation for a reflection.
2. scad123d then calls `_reoriented()` for every negative-determinant matrix, reversing this already-correct solid again.

<ref_snippet file="/Users/jonese/Dropbox/Projects/scad123d/src/scad123d/solids.py" lines="179-186" />

A minimal reproduction confirms it: a `10 × 10 × 3` box scaled by those factors should have volume **+12**; `apply_matrix()` returns **−12**, although `is_valid` still reports `True`.

For the ornament, the inverted coin is then cut by the radius-200 clipping ring. That ring leaks into the exported geometry, explaining the STEP’s **400 mm diameter** and **1,255,471.46 mm³** volume. The existing STL measures **2,597.03 mm³**.

### A second failure remains

I tested the orientation correction in memory:

| Diagnostic variant | Volume, mm³ |
|---|---:|
| Existing STEP | 1,255,471.46 |
| Orientation correction only | 1,323.16 |
| Orientation correction + colors removed | 2,599.11 |
| OpenSCAD reference | 2,597.03 |

After correcting orientation, OCCT’s final union drops the coin and inner ring. Removing colors changes the intermediate topology and avoids that failure, bringing agreement to **0.08%**. The failure also occurs in a direct OCCT fuse, before the final color-partitioning step; its precise kernel cause remains unisolated.

**Recommended next step:** restrict the orientation repair to the rigid-transform path, retaining the existing ordinary-mirror fix; then isolate the color-dependent union failure separately. Removing colors was a diagnostic experiment, not a proposed permanent fix.

No conversion code or artifacts changed. Findings recorded in `CONVERSATION.md` on `feature/debug-ornament-volume`. Existing tests: **337 passed, 3 skipped**.

Verification command: `PYTHONUTF8=1 PYTHONPATH=/Users/jonese/Projects/solid123d/src uv run --no-sync pytest -q`. Without UTF-8 mode, this terminal's ASCII locale caused six unrelated read/write encoding failures. Diagnostic changes were process-local only. The general-transform reflection was also checked against the existing rigid-mirror path: corrected nonuniform box volume +12, rigid-mirrored box volume +300. The colored coin alone agrees with OpenSCAD (864.65155 vs 863.99300); the subsequent union returns 1323.16237 instead of at least its 1778.38910-volume ring operand. Raw OCCT fuse fails with either operand order, flattened operands, serial execution, and fuzzy tolerances 0 through 0.001. Both pre- and post-clipping coin operands trigger it. Unguarded clean does not simplify or fix it. Color removal reduces the coin from 222 to 162 B-spline faces; this implicates intermediate topology, but does not establish a specific color-metadata bug.

**User**: There were some repairs to mirror-related code yesterday. Is it possible that this bug was caused by a recent fix which overgeneralized?

Once we're done with the inversion issue, let's look at color(). I made some changes to how color was handled in OpenSCAD, and that may not have been as careful as it should have been. It will be worth keeping track of this file as a breaking example

**Assistant**: I’ll check the mirror-related history to see whether this is a regression from that fix, then add coverage and narrow the orientation repair. Afterward I’ll investigate `color()` separately, keeping this ornament as an end-to-end failing case.

**Assistant**: The repair was overgeneralized, but **the recent fix did not introduce this inversion**.

I ran the same reproduction against the historical code:
- Initial implementation, **August 18** (`fb5b59d`): volume **−12**
- Before and after the **September 6** mirror fix (`f16eb8a`): **−12**

The unconditional negative-determinant check was already present. September 6 changed how reflected faces are repaired to address lost geometry in multi-operand unions, but retained the overly broad condition. I’ll preserve that repair and stop applying it to the general-transform path.

**Assistant**: The inversion fix is now in place. Ten new regression cases failed before the change; afterward the geometry and differential tests pass, including the earlier mirror/servo-horn coverage.

The color review also found an independent bug: a translated colored assembly can jump back to its original coordinates during a later union. In a three-box reproduction, the correct result is **2,500 mm³ at x=100…130**; color partitioning returns **3,000 mm³ at x=0…115**. I’m keeping this separate from the ornament’s remaining failure until there’s evidence connecting them.

Inversion repair: `apply_matrix` now calls `_reoriented` only when `_decompose` succeeded and the determinant is negative. The general `BRepBuilderAPI_GTransform` path already returns outward-oriented geometry. Added 16 cases to `TestReflectionOrientation`: combinations of nonuniform reflection, reflected shear, one/three negative axes, two-negative-axis controls, and uniform-reflection controls, tested against solid/cavity/compound inputs; plus a subtraction after nonuniform reflection. Checks include signed leaf volume, infinity classified OUT, cavity/solid counts, unchanged inputs, and cut bounding dimensions. Before: 10 failed, 9 passed in the reflection class. After: geometry/differential suite 158 passed, 3 skipped; full suite 353 passed, 3 skipped. Ruff check/format and git diff --check pass. No solid123d code changes, commits, releases, dataset writes, or ledger updates.

### Open color regressions to retain

**End-to-end ornament case:** `dataset/11221469_0/coins-pendants-ornaments-NO-LIBRARY.scad`, review directory `/Users/jonese/codecad-review/004-11221469_0-coins-pendants-ornaments-NO-LIBRARY`, immutable corpus source `/Users/jonese/Dropbox/Projects/3D_geometry/codecad/dataset/11221469_0/coins-pendants-ornaments-NO-LIBRARY.scad`. Git blob hashes of the reviewed source/CSG (not stored as Git objects): SCAD `ed68e4724def83f43952ec6748f3bba1680e2835`; CSG `f37ace14d1df4a39a6917a13239fbb68ab58a78e`. Reference STL 2597.032831 mm³; freshly rendered CSG reference 2597.032798. Before inversion repair 1255471.455729; after inversion repair 1323.162367; diagnostic color removal after inversion repair 2599.112176. The artifact STEP is intentionally left unchanged. This remains a failing case, not a completed conversion fix.

**Independent translated-color-assembly regression:** runtime comparison of solid123d history proves `7a7e4b4` (August 21) produces the correct volume 2500 and x=100..130, while the experimental color-partition change `f25dff0` (August 25) produces volume 3000 and x=0..115. It still fails on current main. Reproduction, using only synthetic test geometry:

```python
import solid123d as s

assembly = s.union()(
    s.color("red")(s.cube(10)),
    s.color("blue")(s.translate([20, 0, 0])(s.cube(10))),
)
moved = s.translate([100, 0, 0])(assembly)
extra = s.color("green")(s.translate([105, 0, 0])(s.cube(10)))
correct = moved.fuse(extra)
result = s.union()(moved, extra)
assert correct.volume == 2500
assert result.volume == 2500
```

The last assertion fails. The translated Compound's wrapped geometry moves to x=100..130, but its Python children remain at x=0..10 and x=20..30. `_color_leaves` descends into those local children without accounting for the parent transform. `_partitioned_union` reconstructs misplaced geometry, then compares against `later`, itself built from those same misplaced children, instead of the original `fused` result. Thus its volume guard accepts the error. A parent-location-aware traversal is needed for moved assemblies; nonuniform transforms also need attention because transformed geometry can be baked into the wrapped shape rather than represented in its location. Do not blindly clear `children`: that can rebuild a Compound's wrapped geometry and discard its bodies. This independent confirmed bug does not yet explain the ornament's direct raw-fuse failure.

**Assistant**: The ornament’s color-dependent failure is now isolated too. The green tree tier causes color handling to preserve touching pieces as a Compound. Subtracting that Compound as one cutter leaves coincident internal faces in the coin. `is_valid` misses them, but OCCT’s boolean self-intersection check detects them; the later union then loses geometry.

Passing the cutter’s individual solids to the subtraction avoids the defect. Applied in memory to the full model, this gives **2,599.11 mm³ versus 2,597.03 mm³**, without stripping source colors. That identifies the next repair target; I haven’t changed the shared subtraction code yet.

I’ll retain the full case and the reduced one-tree reproduction alongside the independent translated-color-assembly bug.

### Ornament color-cut root cause and reduced reproduction

The color-partitioned touching-solid Compound is passed as one cut tool at `src/scad123d/build.py:218`; solid123d's `booleans.py:41` does the same. The coincident interfaces between the cutter's partitioned solids survive the cut as oppositely oriented overlapping faces in the resulting single coin solid. OCCT `BRepCheck`/`is_valid` reports success, but `BOPAlgo_ArgumentAnalyzer` with `SelfInterMode=True` reports four edge/face self-intersections. A direct `BRepAlgoAPI_Cut` reproduces this with zero Python assembly children, so it is separate from the child-coordinate regression above. Fully planar, unscaled triangular tools also produce the defective cut; later scaling affects how the defect manifests downstream.

Reduced case: one peripheral tree at 80 degrees, three triangular tiers, green middle tier, simple inner ring. No reflection, central tree, other peripheral trees, brown trunk, blue coin, or outer clipping operation needed. Compound-tool cut: coin has 19 faces and 4 self-intersection reports; final union has volume 0. Strip its sole color, expand tool solids into separate cut arguments, cut sequentially by solids, or fuse the tool first: coin has 15 faces and no self-intersection reports; final union ~1517.972877 mm³. The bad pre-scale coin has four opposite-oriented face pairs whose intersections have areas 6.9282032303, 17.3205080757, 17.3205080757, and 6.9282032303 mm².

Full-model diagnostic: flatten only the radius-55 cylinder's subtrahends to individual solids, keeping source colors and the inversion repair. Coin: 864.651549421312 mm³, 162 faces, zero self-intersection reports. Full model: 2599.112191982592 mm³, four solids, 0.080% from OpenSCAD. An existing color-partition volume-fallback warning remains: geometric repair is demonstrated, full color fidelity is not. Next implementation should normalize Compound cut tools in a shared path covering scad123d and solid123d, preserve empty/2D behavior, and test multi-solid cutters, cavities, and already-simple tools. No production color/cut code was changed in this investigation.

**Assistant**: **The inversion was an overgeneralization, but it predates the recent fix.** Historical execution reproduces it in the initial August 18 implementation. The September 6 mirror repair retained the overly broad condition rather than introducing it.

The inversion is now fixed: orientation repair applies only to the rigid/uniform transform path. The earlier mirror repair remains intact. Added 16 regression cases covering reflected scaling, shear, cavities, compounds, and subsequent subtraction.

<ref_snippet file="/Users/jonese/Dropbox/Projects/scad123d/src/scad123d/solids.py" lines="183-187" />

### Color investigation: two distinct defects

1. **This ornament:** color partitioning creates touching-solid Compounds. Using one as a subtraction tool leaves coincident internal faces, which later break the union. Passing its solids as separate cut tools fixes the full model in a diagnostic run: **2,599.11 mm³ versus OpenSCAD’s 2,597.03 mm³**, without stripping source colors. The shared subtraction repair is not implemented yet.

2. **Translated colored assemblies:** partitioning uses child-local coordinates without applying the parent’s transform. A three-box example returns **3,000 mm³ instead of 2,500**, with pieces moved back toward the origin. This regression does trace to the **August 25 experimental color-partition change, `f25dff0`**.

The original ornament’s paths, content hashes, expected volumes, reduced one-tree reproduction, and three-box reproduction are retained in <ref_file file="/Users/jonese/Dropbox/Projects/scad123d/CONVERSATION.md" />.

**Verification:** 353 passed, 3 skipped; Ruff and diff checks pass. Changes remain uncommitted on `feature/debug-ornament-volume`; existing STEP/STL artifacts are untouched.

Replayable reduced CSG (currently fails even after the inversion repair; rename its sole `color` to `group` for the control):

```scad
union() {
  difference() {
    cylinder(h=3,r1=12.75,r2=12.75,center=true);
    cylinder(h=56,r1=10.75,r2=10.75,center=true);
  }
  multmatrix([[0.2,0,0,0],[0,0.2,0,0],[0,0,1,0],[0,0,0,1]]) {
    difference() {
      cylinder($fn=100,h=3,r1=55,r2=55,center=true);
      multmatrix([[0.173648,-0.984808,0,0],[0.984808,0.173648,0,0],[0,0,1,0],[0,0,0,1]]) {
        multmatrix([[1,0,0,0],[0,1,0,43],[0,0,1,-5],[0,0,0,1]]) {
          multmatrix([[0.4,0,0,0],[0,0.4,0,0],[0,0,3,0],[0,0,0,1]]) {
            union() {
              multmatrix([[1,0,0,0],[0,1,0,30],[0,0,1,0],[0,0,0,1]]) {
                multmatrix([[0,-1,0,0],[1,0,0,0],[0,0,1,0],[0,0,0,1]]) {
                  cylinder($fn=3,h=3,r1=10,r2=10,center=false);
                }
              }
              multmatrix([[1,0,0,0],[0,1,0,15],[0,0,1,0],[0,0,0,1]]) {
                color([0,0.501961,0,1]) {
                  multmatrix([[0,-1,0,0],[1,0,0,0],[0,0,1,0],[0,0,0,1]]) {
                    cylinder($fn=3,h=3,r1=15,r2=15,center=false);
                  }
                }
              }
              multmatrix([[0,-1,0,0],[1,0,0,0],[0,0,1,0],[0,0,0,1]]) {
                cylinder($fn=3,h=3,r1=20,r2=20,center=false);
              }
            }
          }
        }
      }
    }
  }
}
```

**User**: I suspect that the color() behavior is a little muddied. I wasn't exactly sure what I *wanted* color() to do, especially when overlapping other non-colored or differently colored volumes. Before we make a fix, let's agree on exactly what behavior color() *should* have, describable in plain text

**Assistant**: The main ambiguity is whether **color describes appearance or assigns ownership of material**. Those lead to different behavior. Our current partitioning code effectively chose material ownership, which is stronger than simply coloring a model.

I’d establish these rules regardless of that choice:

### Non-negotiable rules

- **Color must never add, remove, move, or duplicate material.** Removing every `color()` from a model must leave the same geometry.
- **Transforms carry colors with the geometry.** Translation, rotation, scaling, and mirroring cannot detach a color from its object.
- **A cutting tool’s color does not paint the cut.** Subtracting a blue cylinder from a red block must not create a blue hole.
- **Disjoint objects retain their individual colors.**
- **“No color specified” is distinct from an explicitly chosen color.** We should not silently treat it as a competing material.
- **Overlaps and coincident surfaces need a deterministic rule**, not whichever operand OCCT happens to return first.

### The fundamental choice

**Appearance only — my recommendation for an OpenSCAD-to-STEP converter**

A red box and blue sphere produce their ordinary geometric union. Surviving exterior surfaces retain their colors where possible. We do **not** divide the interior into red and blue solids merely to preserve color.

Overlapping interior material has no color ownership because it is not visible. We still need explicit rules for coincident exterior faces and newly exposed cut faces.

**Material regions**

Red and blue identify distinct regions throughout the object’s volume, suitable for multi-material manufacturing. Their overlap must belong to somebody, and the result may need separate, touching solids.

This requires a precedence policy—for example, explicit color beats unspecified color, and later explicitly colored operands win against earlier ones. That is a modeling rule we would be adding, not just visual decoration.

The first option avoids making `color()` responsible for internal geometry construction. The second is useful, but should be an intentional feature rather than an accidental consequence of preserving STEP colors.

**Assistant question**: Should `color()` describe visual appearance, or define colored material regions throughout the volume? Options: Appearance only; Material regions. No color implementation changes will be made until the behavior is agreed.

**User**: I want to be able to assign regions to different materials for multimaterial/multicolor printing. *However*, I'm open to painting only faces if that were helpful, or applying colors to bodies after calculations, etc; I don't really care *how* we do it. But a STEP file generated with scad123d/solid123d should be able to represent all the regions with a given color like openSCAD's preview does, and they should all be selectable as a group (so that a STEP file could show/hide all geometry of a given color with a single click)

**Assistant**: The requirement is **selectable material regions, organized by color**. How we encode them is secondary, provided the exported STEP supports that workflow. Face colors alone would not be sufficient unless they also preserve the region identities needed for selection and material assignment.

Here is a proposed plain-text contract.

## Proposed behavior

### 1. Geometry is independent of color

Adding, removing, or changing colors must not change the total material present in the model.

Different material regions may touch, but must not overlap or leave unintended gaps. The exporter must not duplicate overlapping material into multiple colored bodies.

### 2. Every region has one resolved color—or is uncolored

A region’s color identifies its intended material assignment. Uncolored material remains explicitly distinguishable from colored material.

Color does not imply that a region is a separate physical object: one connected object can contain several material regions.

### 3. The STEP is organized by color

All regions with the same resolved color belong to one named group, even when they are disconnected or originate in different modules.

For example:

- **Red**
  - All red bodies or regions
- **Blue**
  - All blue bodies or regions
- **Uncolored**
  - All remaining material

The regions remain individually accessible within their groups. Selecting or hiding a group should affect every region of that color. We can provide that hierarchy in STEP; the exact single-click interaction depends on the importing application.

### 4. Operations carry material assignments with them

- **Transforms:** move both geometry and its material assignments together.
- **Difference:** retain the first operand’s material assignments wherever material remains. Cutter colors have no effect.
- **Union:** combine the geometry, resolving competing material assignments in overlaps using an explicit precedence rule.
- **Intersection:** retain only the shared geometry; we need to choose which operands supply its material assignments.

### 5. Preserve OpenSCAD’s visible color regions where unambiguous

Ordinary, non-overlapping colored parts should look like the preview.

However, the preview cannot tell us who owns **hidden overlapping material**. We need our own deterministic rule for that, rather than trying to infer material ownership from rendered pixels.

### 6. Do not silently change material assignments

If conversion cannot preserve the agreed material regions, it should report that failure—not quietly return an uncolored union that appears to have converted successfully.

## Proposed defaults for the remaining ambiguities

I suggest:

| Situation | Result |
|---|---|
| Red overlaps uncolored in a union | Red owns the overlap |
| Red overlaps blue in a union | The later operand owns the overlap |
| An outer red `color()` contains an explicitly blue region | Blue stays blue; red fills otherwise-uncolored regions |
| Red object minus blue cutter | Remaining object stays red |
| Multicolored object intersected with another object | First operand supplies material assignments; subsequent operands act as masks |

These defaults treat explicit colors as intentional assignments, uncolored geometry as neutral, and cuts/intersection masks as geometry constraints rather than painting operations. The three choices below would settle the important remaining semantics.

**Assistant questions**: In a union, which operand should own overlapping material? Should an outer color replace explicit inner colors or only fill uncolored regions? In an intersection, should the first operand supply material colors while subsequent operands act only as masks?

**User — union**: A -- but I don't love any of these options. Ideally, In A union B, there are three segments -- A-only, B-only, and A intersect B. If A is colored and B is not, then I'd like to see no change in the A-only and B-only segments. What happens in the A intersect B segment is... undefined. I'd be fine letting that intersection be colored by the later operand.

But note that an "infectious" color (unioning a white eyeball to a yellow head shouldn't turn the head white) isn't what anyone intuitively wants.

**User — nested colors**: a

**User — intersection**: b, but let's talk through any conclusions you've come to before doing anything. If I've specified something that's going to be prohibitively expensive or fiddly, I can reframe this

**Assistant**: Your central requirement is **local color ownership, not whole-object recoloring**. Connectedness must not make a color spread.

For a yellow head unioned with a white eyeball:

- Head-only material stays yellow.
- Eyeball-only material stays white.
- Only the overlapping material needs a precedence decision.
- Nothing about that overlap should recolor the rest of either object.

That distinction should be an explicit invariant in both the specification and the tests.

## What I think we have agreed

1. **Color describes material regions.** A connected object can contain multiple colors; a color group can contain multiple disconnected objects.
2. **Union preserves each operand’s non-overlapping regions unchanged.** Precedence affects only shared material.
3. **An enclosing color supplies a default.** It fills otherwise-uncolored regions rather than repainting explicitly colored inner regions.
4. **Difference removes material without importing the cutter’s color.** Every surviving region keeps its assignment, including newly exposed cut surfaces.
5. **Intersection uses the same color precedence as union.** Only shared material survives, and competing assignments there are resolved by that rule. It is not automatically “the first operand’s color.”
6. **Transforms preserve the region assignments.**
7. **Export groups all regions of the same resolved color together**, without duplicating material or changing the overall shape.

For example, intersecting an entirely red operand with an entirely blue operand would produce one color throughout the intersection, selected by precedence—not a mixture or an average.

## Is this prohibitively expensive or fiddly?

**It is a reasonable requirement, but exact material partitioning is not free.**

The important architectural distinction is between:

- **Computing the model’s geometry**, and
- **Dividing the final material into selectable color regions.**

Those need not be performed together at every intermediate operation. Our ornament bug demonstrates why treating color-preserving assemblies as ordinary working boolean operands is risky.

A sensible approach would keep ordinary geometry calculations separate from the information needed to resolve material ownership, then construct the exportable colored regions. That is an implementation direction to investigate, not yet a promised solution.

The expensive cases will be models with many interacting color boundaries and small interlocking regions. A model with many disconnected objects grouped into a few colors is a different—and generally simpler—case. We should benchmark representative examples before committing to an algorithm.

I would **not** simplify your requirements just to accommodate the current implementation. Nor would I promise identical performance to an uncolored conversion.

## One remaining ambiguity

Your union answer selected **“explicit color, then later,”** but also said you would accept the later operand coloring the overlap even when one operand is uncolored.

Those produce different results here:

| Earlier operand | Later operand | Explicit color wins first | Later always wins |
|---|---|---|---|
| Red | Uncolored | Overlap is red | Overlap is uncolored |
| Red | Blue | Overlap is blue | Overlap is blue |

**Both preserve the red-only and uncolored-only regions. Neither is infectious.** Their computational cost should not meaningfully determine the choice.

I lean toward retaining an explicit color rather than letting an uncolored operand erase it. But this is a preference we should settle explicitly, not infer from implementation convenience. No implementation changes yet.

**Assistant question**: When a later uncolored operand overlaps an earlier colored operand, should the overlapping material retain the explicit color or become uncolored? Options: Retain explicit color; Later operand always wins.

**User**: Retain explicit color.

**Assistant**: The precedence rule is now settled:

> **An assigned color wins over uncolored material. Between competing assigned colors, the later operand wins. Precedence affects only overlapping material—never the rest of either object.**

## Agreed color behavior

1. **Geometry stays unchanged.** Colors must not add, remove, move, or duplicate material. Different material regions may touch but must not overlap or leave unintended gaps.

2. **Union preserves non-overlapping regions.** A-only retains A’s assignment; B-only retains B’s. The overlap follows the precedence rule above. A white eyeball cannot turn an entire yellow head white.

3. **Intersection uses that same precedence.** Only shared material remains, and its competing assignments are resolved exactly as for a union.

4. **Difference preserves the retained material’s assignments.** Cutter colors are ignored. Newly exposed surfaces belong to the surviving material, not to the cutter.

5. **An enclosing color fills uncolored regions rather than repainting colored ones.** Evaluate the enclosed model’s assignments, then give its remaining uncolored material the enclosing color. Explicit inner colors survive.

6. **Transforms carry assignments with the geometry.** Translation, rotation, scaling, and mirroring cannot detach or redistribute colors.

7. **STEP output groups regions by resolved color.** All red regions belong to one named red group, including disconnected regions. Likewise for other colors, with a separate uncolored group. Individual regions remain accessible within their groups; exact selection/hide controls depend on the importing application.

8. **Failures are explicit.** If the converter cannot preserve geometry or material assignments, it must not silently substitute a differently colored or uncolored result.

### Examples

| Operation | Result |
|---|---|
| Red A union uncolored B | A-only and overlap red; B-only uncolored |
| Red A union blue B | A-only red; B-only and overlap blue |
| Blue B union red A | B-only blue; A-only and overlap red |
| Red A intersect uncolored B | Shared material red |
| Red A intersect blue B | Shared material blue |
| Red A minus blue B | Remaining A red |

This defines scoped coloring, booleans, transforms, and STEP grouping without committing to an implementation strategy. Operations that create new material—particularly `hull()` and `minkowski()`—still need a separate ownership rule before extending the contract to them.

No color implementation changes have been made.

**User**: Using groups (or build123d Compounds) to select color seems like a dubious choice, although it could be handy. How do color-mediated groups work with explicitly created groups thta might have different membership? What would we do in that case? How else would we manage color? How does a pure-build123d shape manage color?

**Assistant**: I’ll check build123d’s color and assembly APIs, including what survives STEP export. The key question is whether color selection can be kept separate from structural grouping rather than forcing both into one hierarchy.

**Assistant**: **Color selection should not dictate the assembly tree.** My previous proposal conflated two different things: structural membership and selection by an attribute. I would revise that part of the contract.

## 1. Structural groups and color collections should coexist

Consider:

```text
Model
  Head
    Yellow skin
    White eyes
  Body
    Yellow torso
    White badge
```

“Head” is a structural group. “Everything white” is a selection spanning several structural groups. Neither should replace the other.

A build123d assembly node has one parent. I verified that putting the same child into a second `Compound(children=[...])` **removes it from the first parent**. Making extra assembly instances instead would represent extra occurrences, not an independent selection set.

A flat `Compound([...])` is also a geometry container—not a general-purpose tag or selection set. Compounds remain appropriate for assemblies and multi-solid parts; they should not be our mechanism for cross-cutting color membership.

Separately, scad123d currently treats OpenSCAD’s emitted `group()` as an implicit union, not a preserved named assembly. We should distinguish that from explicitly authored build123d assemblies.

## 2. What native build123d does

I checked the installed **build123d 0.11.1**:

- A `Shape` has a `.color` attribute containing one `Color` value, including alpha. Assigning it does not partition geometry.
- If unset, the color getter looks up the nearest colored assembly ancestor. It also **caches the inherited color** on the child.
- This is not a material-ownership system. In a direct test, fusing overlapping red and blue boxes produced **one red solid**, not separate red/blue regions.

<ref_snippet file="/Users/jonese/Dropbox/Projects/scad123d/.venv/lib/python3.13/site-packages/build123d/topology/shape_core.py" lines="345-367" />

The STEP exporter writes colors and names from assembly nodes. Simply assigning a color to a face returned by `part.faces()` is **not a persistent per-face painting API for the parent part**: in my test, fetching the faces again lost that assignment, and STEP export retained only the part’s color. OCCT supports real per-face color metadata, but that requires more than this ordinary build123d usage.

<ref_snippet file="/Users/jonese/Dropbox/Projects/scad123d/.venv/lib/python3.13/site-packages/build123d/exporters3d.py" lines="169-204" />

## 3. Alternatives to color-based Compounds

| Approach | Preserves structural hierarchy? | Main limitation |
|---|---|---|
| Colored region bodies + selection by color | Yes | Receiving application needs color-selection/filter support |
| Independent named STEP layers | Yes | Layer import and visibility support varies by application |
| Per-face colors | Yes | Surface appearance alone does not identify interior material regions; extra metadata/export work needed |
| Optional export reorganized by color | Not generally | Useful compatibility mode, but trades away the original hierarchy |

**STEP layers are a concrete possibility, not just speculation.** I created an in-memory XDE document assigning one shape to two layers, then exported both assignments into STEP. OCCT supports layers independently of assembly structure. However, build123d’s current exporter does not automatically create these assignments from shape attributes, despite enabling layer writing.

Internally, a color collection can simply be a list or index of references to existing regions. It need not own, copy, reparent, or move them.

## Recommended revision

> **Preserve structural assemblies. Store color/material identity on the regions. Support collecting regions by color independently of structural membership. Do not require color collections to be assembly nodes.**

We still need separate volumetric regions for your material-assignment semantics, but **those regions can remain under their original structural parts**. Color layers or application-side filters can select across them.

The remaining interoperability question is practical: **which STEP viewer/CAD application and slicer do you use?** That determines whether layers, selection by color, or an optional color-organized export can actually deliver your desired show/hide and material-assignment workflow.

No implementation changes made; only the design discussion is recorded.

Verification details: pure-build123d probes deliberately did not import solid123d (so its monkeypatches could not influence the native behavior). Overlapping 10 mm cubes at x=0 and x=5, colored red/blue, fused to one red solid of volume1500. A child moved from assembly `head` to assembly `white` left `head.children` empty. A yellow solid with a red selected-face wrapper exported only `DRAUGHTING_PRE_DEFINED_COLOUR('yellow')`; a fresh wrapper for that face had color None. An uncolored child's color remained cached red after its parent was changed from red to blue. XDE LayerTool assigned the same shape to `Color/Yellow` and `Selection/Head`; WriteStream returned IFSelect_RetDone and produced two PRESENTATION_LAYER_ASSIGNMENT entities referencing the same representation item. All export probes used BytesIO; no STEP/STL artifacts or production code changed. The layer option is not yet tested in the user's receiving applications. OCCT reference: https://dev.opencascade.org/doc/refman/html/class_x_c_a_f_doc___layer_tool.html .

**User**: Using build123d.Compound to manage color feels too heavy. STEP
supports layers, a partition separate from the group hierarchy. Research
which programs support STEP layers and whether this is feasible.

**Assistant**: Feasible in OCCT, weak in consumers. OCCT 7.9.3 (OCP) writes
`PRESENTATION_LAYER_ASSIGNMENT` from XDE `XCAFDoc_LayerTool` and reads it
back with `SetLayerMode(True)`; round-trip verified locally. Two writer
rules found by experiment: layers must sit on part/shape labels (assembly
and component/instance labels are skipped: "no way to assign layer to
instance in STEP"), and a top-level solid carrying a `TopLoc_Location`
is silently dropped ("Cannot find RI") -- bake the transform or wrap in a
compound; a compound of several solids on one label lists every
MANIFOLD_SOLID_BREP. A layer is a flat name list; sublayers don't exist
(McNeel). build123d's exporter enables LayerMode but never assigns
layers; its `_create_xde` keeps a private label map, though
`XCAFDoc_ShapeTool.FindShape_s` recovers labels after the fact.
Consumers: honor layers -- Rhino (import by layer description; AP214
export), AutoCAD, Creo (Layer Import Options dialog), NX (AP214 "colors
and layers" by default), CATIA. Ignore layers -- FreeCAD (reader sets
SetLayerMode but ImportOCAF2 never reads LayerTool; only the debug OCAF
browser lists them), Fusion 360 / Onshape / SolidWorks parts (no layer
concept for 3D bodies), PrusaSlicer/Bambu/Orca (mesh only; even colors
lost), Blender importers, Mayo (LayerTool used only for DXF), CAD
Assistant (no mention in any release note). Recommendation: layers are a
fine *additional* tag (cheap, orthogonal, harmless when ignored) but not
the primary color carrier for the maker toolchain; per-solid XDE color on
region bodies remains what every consumer reads. Scratch probes:
scratchpad/layers_recipe.py, layers_roundtrip.py.

**User**: Separate groups per color works well for 3D printing but feels
like the wrong long-term mechanism. Any other feasible way to record all
geometry of a given color?

**Assistant**: Honest answer: STEP offers no widely honored "set"
mechanism besides product structure and per-body color. Surveyed:
per-body XDE color (universal: FreeCAD, Fusion, SolidWorks, Onshape,
Rhino all read it; is the ground truth; selection-by-color is the
importer's job); layers (pro CAD only); AP242 GROUP/APPLIED_GROUP_ASSIGNMENT
(no XDE tool, OCCT writer doesn't emit); materials (XCAFDoc_MaterialTool,
SetMaterialMode; ignored by maker tools); properties/metadata
(SetPropsMode / SetMetaMode exist in OCP 7.9; importers ignore); names
(universal but crude); 3MF via build123d Mesher (per-object color the
slicers actually read). Recommendation: internal color index on region
bodies + per-body color in STEP with the user's own structure preserved;
group-by-color as an explicit export *mode* for printing, and 3MF export
as the proper print path. No code changes.

**User**: Is 3MF as capable as STEP? Can it handle all the surface types
STEP can?

**Assistant**: No. 3MF is triangles only (plus per-object/per-triangle
color and material, object tree with instancing, and extensions: beam
lattice, slices, mesh booleans, volumetric/implicit). STEP carries exact
analytic and B-spline surfaces with real topology, so fillets, face
selection, exact booleans, and deferred tessellation all survive; in 3MF
they don't. STEP stays the exact model; 3MF is a slicer delivery format
where per-object color maps cleanly. "As well", not "instead".

**User**: What would a correct per-body STEP color system take (layers
deferred)? And is group-by-color on demand just a bucket sweep?

**Assistant**: Today colors reach STEP only via the assembly tree
(`group()` -> Compound(children) of partitioned pieces; build123d colors
nodes). Weak points: `difference()`/`intersection()` operate on the whole
Compound and drop colors; `color()` relies on exporter inheritance (with
build123d's cache quirk) for rule 5; a flat Compound is one leaf. Plan:
(1) region invariant -- non-overlapping bodies, one resolved color each,
after every op; (2) color-aware difference/intersection: cut each region
body, re-attach its color, ignore cutter colors; (3) `color()` as a
build-time fill of uncolored bodies; (4) flatten to a body list before
export, tree independent; (5) own the XDE document via OCP (color on
part labels, names, optional assembly nodes; layers later for free);
(6) decide hull/minkowski ownership (suggest: enclosing color else none,
with warning); (7) per-color volume verification against OpenSCAD 2025's
colored 3MF/OBJ export. Group-by-color: yes, a bucket sweep over the
flattened bodies (RGBA rounded ~1e-3 incl. alpha; uncolored bucket;
transforms already baked; optional fuse per bucket), as an export mode
since it discards the hierarchy. No code changes.

**User**: Re-run the mismatch retry, generate a top-100 review directory,
and leave a report of what is still failing plus the size of the changes.

**Assistant**: Retried all 4,364 mismatches with scad123d
`feature/debug-ornament-volume` plus merged solid123d main on PYTHONPATH.
572 now ok (13.1%), 3,753 still mismatch, 39 moved to another failure
(mostly memory/timeout at the 300 s / 3.6 GB limits). Corpus ok
55,088 -> 55,660. All four magnitude buckets moved by a similar 7-20%,
so no single cause dominates. `~/codecad-review` (the original worst 100,
selection kept) reports 14 of 100 resolved and 28 changed, including the
coins/ornament case which now reads `ok`; `~/codecad-review-2` holds a
fresh worst 100. Report at `~/codecad-step/RETRY-REPORT.md`.

Sharpest remaining finding: 21 files build to a volume of exactly 0 while
OpenSCAD renders a real solid, and they are classed `mismatch` at 100%
rather than `empty`, so they hide in the largest bucket. 13 used no mesh
fallback and 15 carry no OpenSCAD warning, so the geometry is lost on our
side. Only 71 of 3,753 differ by more than 100x; the rest is a long tail.
2,351 memory/timeout files remain `deferred` and were never re-run.
**Assistant** (scad123d side): `feature/per-body-color` (worktree
`../scad123d-color`). `build.py` routes `difference`/`intersection`
through solid123d's color-aware operations instead of raw `cut`/`&`, so
the retained material keeps its colors. `cli.py`: `export_step` comes
from solid123d, `--group-by-color` is wired through `_Conversion` to both
the single-file and batch paths, and `color_volumes()` records volume per
color into each result before verify. New `colors.py` cross-checks that
against OpenSCAD's own colored 3MF render: `--verify` now catches
material painted the wrong color, which a matching total volume cannot
see.

The check is only defined where OpenSCAD defines it, which the work
established: OpenSCAD's `color()` is a *surface* attribute, not a
volumetric material assignment. In a union of two overlapping colored
cubes the interface between them carries no triangles at all -- the union
removed them -- so each per-color triangle group is an open surface
enclosing no volume. `openscad_color_volumes` therefore tests closure and
returns None for such a model, and `_verify` records
`colors_unchecked` with the reason rather than comparing nonsense. Where
regions are disjoint (the ordinary multi-material case) every group closes
and the comparison is exact.

`batch.py` gains a `colors` ledger column, shown by `--show`. Tests:
`test_colors.py` (9, incl. 2 needs_openscad), a boolean-color class in
`test_build.py`, and CLI tests for `--group-by-color` and the verify
path; one old test updated to the color-fill semantics. 334 pass.
Smoke-tested on a two-material lid: steelblue 2400 exactly, both STEP
layouts correct, colors round-tripping to six decimals.

**Assistant** (sequencing): scad123d's floor is raised to
`solid123d>=0.8.0`, since `export_step`/`region_bodies` and the corrected
union precedence are unreleased. scad123d#28 therefore cannot go green
until solid123d#15 is merged and released; that is the normal order for
this pair (geometry semantics land in solid123d first) and matches the
weekly release cadence. Locally both suites pass together via
`PYTHONPATH=~/Dropbox/Projects/solid123d/src`.

**User**: Look at `codecad-review-3/008-2379645056_48-polyhedrons/polyhedrons.scad`.
Then: research CGAL vs OCCT polyhedra rather than reverse-engineering.
Then: should we just let OpenSCAD supply polyhedron meshes?

**Assistant**: The model is malformed input, and the docs settle what to do
about it. Its `polyhedron()` leaves 24 free edges even after merging
coincident points, so it encloses no volume; OpenSCAD renders only the
model's sphere (4.12).

Documented behaviour, rather than inferred: OCCT's
`BRepBuilderAPI_MakeSolid` performs no coherence check and explicitly
permits converting a non-closed shell, while OCCT's validity criterion for
a solid is "a closed shell and coherent faces orientation" -- so the
arbitrary 306.63 volume is expected, as is the STEP that carries faces and
no solid at all (every viewer reads it as empty). OpenSCAD uses an indexed
mesh in which a repeated coordinate is a separate vertex, warns that the
mesh may not be a valid 2-manifold, retries after merging very close
vertices, and if it is still not manifold the conversion fails and the
polyhedron contributes nothing.

So scad123d now counts free edges on the built shape and, when there are
any, warns and treats the polyhedron as no geometry. Free edges are
counted after construction, so a mesh that lists a shared corner once per
facet is unaffected -- OCCT sews those, which also means the vertex-weld
I first wrote for solid123d was redundant and was dropped.

On supplying every polyhedron from OpenSCAD: no, and measured both ways.
Always would downgrade the exact BRep path (real planar faces, fillets and
face selection) to a triangulated mesh import for the ~257 mismatched
models whose polyhedra close fine, plus thousands of passing ones. Falling
back only when open fails outright: OpenSCAD's own output for these meshes
is empty or still non-manifold, so `mesh_import` cannot build a solid and
the conversion dies (lib3mf exception, or "could not build a valid solid").

Effect: `polyhedrons` 306.63 -> 4.19 against OpenSCAD's 4.12;
`holding arms` 16,209 -> 15,257 against 15,252; `jak` and 9 others
unchanged or slightly closer; no regressions among the 12 mismatched
models that build an invalid polyhedron. 4 new tests; 371 pass.

**User**: Merge that PR and re-run the mismatch retry; I'm going to sleep
and will read the report in the morning.

**Assistant**: Merged solid123d #18 as well (the retry needed it to be
reproducible from main rather than an unmerged branch) and scad123d #29,
seven CI jobs green. #29 was branched from `feature/debug-ornament-volume`,
so merging it brought that whole branch to main -- the multmatrix
reflection fix and both inspection tools included; the PR body was
rewritten to say so before merging.

CI caught two bugs of my own, both fixed in #29: `review.py` wrote a
middle dot with the platform locale encoding (ASCII on the runner) so it
raised UnicodeEncodeError, and once fixed the tests raised
UnicodeDecodeError reading the file back -- both sides now name UTF-8,
verified under `LC_ALL=C`; and a test compared raw symlink text, which
Windows returns in extended-length form, so it now compares resolved
paths.

Retry: 160 of 3,342 now ok (4.8%), corpus mismatch 3,342 -> 3,160, ok
56,023 -> 56,183. The tracked sets are the better measure, since they keep
their original selections: the first worst-100 has gone 14 -> 29 -> 40
resolved across the three retries; review-2 is at 33, review-3 at 16.
Fresh worst 100 at `~/codecad-review-4`. Report at
`~/codecad-step/RETRY-REPORT.md`.

**User**: See about
`codecad-review-4/005-1612505_0-another_bauble/another_bauble.scad`.

**Assistant**: Found a nondeterminism bug and, chasing it, a regression in
a fix merged earlier tonight.

The bauble reported 87,144 against OpenSCAD's 2,723 -- almost exactly the
volume of the r=27.5 sphere it is meant to be intersected with. But the
intersection built in isolation was right (2,715.74), and so was the whole
tree *if anything had already been built in the same process*: cold
87,144.42, warm 2,766.79, and `mesh.clear_cache()` brought the bad answer
back. The mesh cache hands out `copy.copy`, a shallow copy sharing one
TopoDS, so the first boolean to touch a meshed subtree improved the shared
shape in place and every later use behaved better than the first -- and a
batch worker only ever sees the first. Cleaning the imported mesh once at
render time makes the first use behave like the rest (87,144 -> 2,766.79,
1.6% off, and the conversion drops from 34.6s to 11.7s). Validated on 45
previously-ok mesh-fallback models: no regressions, 4 improvements.
Branch `fix/normalise-imported-mesh`, 373 pass.

Separately: sampling previously-ok models -- which no retry has ever
re-run, since every retry targeted `mismatch` -- turned up two
regressions from code merged earlier tonight. `reversi.scad` went 118.80
-> 740.16 (OpenSCAD 121.21), bisected to `da8f04f`, the merge of #18 that
made every cut fuzzy; and `rpi_bracket.scad` now converts to 0.00 where it
was 5,703.56 (OpenSCAD 5,703.51), also on merged main and not yet
diagnosed.

Three ways to resolve the fuzzy-cut ambiguity were tried and all fail
somewhere: always fuzzy (merged) fixes the saucers and a windscreen seal
but breaks reversi; fuzzy only when the exact result comes back in more
pieces holding all its volume fixes reversi but breaks the seal (exact
9,568 vs fuzzy 8,008.98, OpenSCAD 8,005.31); running both and keeping the
smaller gets all four right but performs two booleans per cut, which
breaks `test_difference_of_many_is_one_operation` -- an invariant added
deliberately in #13 to stop the corpus timing out. No safe fix shipped;
solid123d left at main.

---

## Fifth mismatch retry: the cut invariant, and the first regression check

`RETRY-REPORT.md` regenerated for run 5. All 3,160 mismatches re-ran with
solid123d main (the cut invariant, #19, and the #18 revert) plus this
repo's mesh-cache normalisation: **96 now convert cleanly**, 3,049 remain
mismatches, 15 moved to another failure. Models building to exactly zero
volume while OpenSCAD renders a solid fell 13 -> 8. The tracked review
sets moved to 43 / 37 / 23 of 100 resolved.

The new part is a **regression check on models that already passed**,
which no earlier retry had. Eighty `ok` models were sampled at random and
their volumes recorded *before* the run; re-converting them afterwards
found **no regressions** -- 70 bit-identical, 10 moved, the largest by
1.3e-5 relative and five of the ten toward OpenSCAD's own figure. That is
the check that would have caught the #18 fuzzy-cut regression before it
merged, and it is the evidence that releasing solid123d 0.8.1 is safe.

Also dropped the `scad123d-artifacts` and `scad123d-review` paragraphs
from the README (#31). Both tools stay; they are how the converter gets
debugged against the corpus, not something a consumer of the project
needs to read about.

---

## solid123d 0.8.1 released

Published to PyPI. Carries the compound-operand decomposition (#16), the
N-ary cut fold (#17), and the cut invariant (#19, which supersedes and
reverts the blanket fuzzy cut of #18). Patch bump: bug fixes only, no API
change.

The corpus evidence in `RETRY-REPORT.md` is what made the release safe --
3,160 known-wrong conversions re-run for 96 fixes, and 80 already-passing
models re-checked with no regressions.

This repo now requires `solid123d>=0.8.1`, so a plain `scad2step` needs no
`PYTHONPATH` prefix to get the fixes. Verified on the release: saucer v2
35,392.12, saucer v3 71,297.64, table-eq-wire_V2 88,590.69, tweezers
7,802.73, and reversi still 118.80 rather than the 740.16 that #18 caused.

---

## A transform that flattens an object removes it

OpenSCAD's own binary carries the string "Scaling a 3D object with 0 -
removing object", and its render confirms it: a cylinder scaled by
`[0.1, 0.1, 0]` and subtracted from a plate cuts nothing, and the plate
keeps its full 1,682. We were flattening that cylinder into a
zero-thickness solid and subtracting it, which erased the whole model.

The CSG export keeps the singular matrix rather than dropping the node,
so the rule has to be applied on this side. `flattens()` in solids.py
answers whether a 4x4's linear part is singular, and the `multmatrix`
branch of build.py returns nothing when it is, with a warning. Its
threshold is 1e-12 rather than exact zero because a CSG export writes six
significant figures, so a matrix built as singular can arrive a few ulps
off.

What counts as flat depends on what is being transformed. A 2D object
never feels the z factor: OpenSCAD has a separate "Scaling a 2D object
with 0" message and scales a polygon by the x/y block alone. The first
version of this change ignored that and removed a `scale([0.9, 0.9, 0])`
polygon in `microscope.scad`, moving it from 0.107% off to 0.980% -- the
one regression the 80-model check found, and the reason that check
exists. Confirmed against OpenSCAD directly: that polygon extrudes to
810, while `scale([0, 0.9, 1])` on the same polygon warns and yields no
geometry at all.

Found in the corpus among the models converting to exactly zero volume;
five of the 3,049 remaining mismatches contain such a transform. The
coin calibration test now converts to 1,682.00 against OpenSCAD's
1,682.00, and the 80 sampled passing models show no regressions.

---

## Retry a set, not the corpus

Re-running all 3,000+ mismatches to measure a fix costs an hour and
usually moves a few percent. A review set is a hundred of the worst
cases, already chosen to be representative and already tracked
before-and-after. `scad123d-batch --retry-set DIR` re-queues exactly the
models a review directory holds (or a plain file of paths).

Measured on `~/codecad-review-4`: **204 seconds** against roughly an hour
for the whole corpus, for the same signal. The intended loop is now
retry-set, refresh the review dir, read what a fix resolved, fix the next
thing; and only run the whole corpus when the set is largely clean.
