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
nut = lib.hex_nut(size=5)                       # modules as Python callables -> Shape
part = lib.geometry                             # top-level geometry, implicitly unioned
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
