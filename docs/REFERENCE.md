# Technical reference

The README explains what scad123d is for. This is the detail: how the import
actually works, the exact behavior of every knob, and the full list of what
does and doesn't have an exact geometric answer. See [ROADMAP.md](../ROADMAP.md)
for what's planned next and the verification behind each analytic case.

## How it works

scad123d shells out to the OpenSCAD binary for **`--export-format csg`**,
which runs the entire OpenSCAD language and emits a flattened tree:

```
$ openscad -o out.csg in.scad
multmatrix([[1, 0, 0, 20], [0, 1, 0, 5], [0, 0, 1, 0], [0, 0, 0, 1]]) {
    cylinder($fn = 48, $fa = 12, $fs = 2, h = 8, r1 = 3, r2 = 1.5, center = false);
}
```

OpenSCAD has already resolved everything that makes the language hard: modules
inlined with defaults applied, `if` decided per instance, `for` unrolled, list
comprehensions evaluated, `children()` spliced, `include`/`use` and
`OPENSCADPATH` resolved, transforms collapsed into 4×4 matrices, and the `*`
disable-modifier stripped.

Crucially, **the primitives stay analytic**. That is a real `cylinder`, not a
triangle mesh, so the BRep survives into build123d with genuine faces and
edges. scad123d only has to parse ~24 node types with no expressions, no
variables and no scoping, and map them onto
[solid123d](https://github.com/etjones/solid123d), which already renders
OpenSCAD primitives as build123d shapes.

### Why not import an STL?

Rendering to STL and importing the mesh is a smaller project, and it does give
perfect semantic fidelity. But you get a mesh, and the consequences run deeper
than losing per-face awareness:

- `fillet()` and `chamfer()` stop being useful. Filleting the rim of a
  128-facet cylinder produces 128 tiny facet-edge fillets, not a rounded rim.
- Selectors return triangles. `part.faces().sort_by(Axis.Z)[-1]` is *one
  triangle*, not the top face. No `Plane(face)` workplane placement.
- OCCT booleans against triangle soup are far slower and much more likely to
  fail than booleans against analytic solids.
- STEP export becomes mesh-as-STEP: large files that downstream CAD imports as
  a mesh body rather than a solid.
- Faceting is baked in at import time; changing `$fn` means re-rendering.

The CSG route gives everything the STL route gives *except* evaluated
`hull()`/`minkowski()`, plus analytic geometry — and both need the same
OpenSCAD binary, so the STL route buys nothing on dependencies.

## Requirements and binary discovery

**The OpenSCAD binary must be installed.** scad123d is not a reimplementation
of OpenSCAD; it delegates to it. Discovery order:

1. `$SCAD123D_OPENSCAD` (explicit override)
2. `openscad` / `openscad-nightly` on `$PATH`
3. macOS: `/Applications/OpenSCAD{,-nightly}.app/Contents/MacOS/OpenSCAD`
4. Windows: `C:\Program Files\OpenSCAD{, (Nightly)}\openscad.exe`
5. Linux: flatpak (`org.openscad.OpenSCAD`) and snap paths

CSG export needs no OpenGL, so no `xvfb` wrapper is required on headless Linux.

## Calling a module, or a whole file's worth of them

There is no OpenSCAD-level operation for "just run this one module" — a
`.scad` file's CSG export is always the result of its top-level statements.
`import_module(path, name)` works around this by generating a small
temporary wrapper file per call:

```
{include|use} <reference>
name(args);
```

and running it through the same CSG pipeline as `import_scad()`. Every call
to the returned callable re-invokes OpenSCAD; there is no cheaper path,
since OpenSCAD itself has no "call one module" mode.

**`import_module()` reads and parses `path` before anything runs.** It
tokenizes the file's `module`/`function` declarations — names and parameter
lists, not evaluated — and builds a real Python function with a matching
signature: same parameter names, same positional/keyword order, and a
required parameter (no `= default` in the declaration) enforced before
OpenSCAD ever runs, with a message naming it (`argument 'rad' is required
in call to 'some_func'`). This means several things that used to only fail
at call time now fail immediately, at `import_module()` time:

- A `path` that can't be found (`FileNotFoundError`).
- A `module_name` not declared in `path` — or in anything `path` itself
  `use`s/`include`s, transitively, however deep that goes, since a
  library's entry point commonly has no declarations of its own and just
  gathers them from other files (`UndeclaredModuleError`, listing every
  module name actually found along the way).

And a couple of things now fail as ordinary Python errors on the returned
callable, before OpenSCAD ever runs:

- A typo'd keyword argument the module doesn't have (`TypeError`, from
  Python's own call-signature checking).
- A required argument omitted (`MissingArgumentError`, naming it).

**The declaration is the source of truth, which has one real caveat.** Some
libraries — BOSL2 among them — declare a parameter bare (no `= default`)
and assign its actual default *inside the body* instead
(`chamfer = chamfer == undef ? 0 : chamfer;`). `import_module` has no way to
see that; such a parameter counts as required here even though the module's
own callers can omit it. Pass `None` explicitly for these — it becomes
`undef`, exactly what an omitted argument would have been.

**Path resolution.** `path` is resolved the same way OpenSCAD resolves an
`include <>`/`use <>` written by hand: as given (absolute, or relative to
the current directory), or via `$OPENSCADPATH` and the default library
folders. A file transitively reached through another file's own
`use`/`include` is resolved relative to *that file's* directory, matching
OpenSCAD's own behavior for library-internal references.

**`include` vs `use` is guessed, not something you should normally need to
set.** `include <path>` brings in a library's module/function definitions
*and* its variables and any top-level code it runs. `use <path>` brings in
only definitions. This isn't just a style choice: some libraries' modules
rely on variables defined at the library's top level (BOSL2's modules read
its own `$anchor_override`-style config variables, for instance), and `use`
would silently leave those undefined — while `include` risks the opposite
problem, silently re-running and unioning in any geometry `path` itself
renders at its own top level (a demo call left at the bottom of a file
you're actively developing, say).

`import_module` picks between them by inspecting `path` itself
(`scad_declarations.suggest_import_style`): `"include"` unless `path` has a
top-level statement that isn't a declaration, a `use`/`include` directive,
or a plain variable assignment — anything that looks like it could render
geometry on its own tips the guess to `"use"` instead. This is deliberately
conservative in the "assume geometry" direction, since getting it wrong that
way just means a loud, fixable failure (a module errors on a missing global
variable, easy to diagnose and override) rather than a silent, wrong-answer
one (unwanted geometry quietly unioned into your result with no error at
all). It only looks at `path`'s own top-level content, not anything it
transitively `use`s/`include`s — that's the file whose content literally
gets spliced into the wrapper, so it's the only thing the choice actually
controls.

Confirmed against real libraries: BOSL2's `std.scad` and MCAD's
`involute_gears.scad` are both pure declarations and directives (MCAD's own
demo/test code all lives inside `test_*` modules, never called at the top
level) — both correctly guess `"include"`, and BOSL2's `cuboid()` genuinely
fails under `"use"` (missing config variables), confirming the guess matters
in practice, not just in theory.

Pass `import_style="include"` or `import_style="use"` explicitly to
override the guess — needed if a file both has top-level geometry *and*
some module in it needs file-level variables the guess would then withhold,
a real conflict the guess can't resolve on its own since it's not doing
full semantic analysis of what a module body actually references.

**Arguments.** The returned callable accepts positional and keyword
arguments the same way the OpenSCAD module does; both are rendered to
OpenSCAD literal syntax and spliced into the generated call. `None` becomes
`undef`; lists, strings, bools, and numbers all follow OpenSCAD's own
literal syntax. Only arguments you actually pass are forwarded to
OpenSCAD — an omitted optional argument lets the module's own `= default`
apply, rather than being overridden with an explicit `undef`.

**A module name OpenSCAD itself doesn't recognize at runtime is still only
caught when the call produces no geometry.** Declaration parsing catches a
module name that plainly isn't declared anywhere reachable from `path`, but
can't catch every way a call can still go wrong inside OpenSCAD itself. When
that happens, the raised `UnsupportedNodeError` includes OpenSCAD's own
warning text.

**Importing a whole file's modules as a namespace.** Leave out `name` and
`import_module(path)` returns a `ScadLibrary` — one attribute per `module`
declared in (or reachable from) `path`:

```python
gears = scad123d.import_module("MCAD/involute_gears.scad")
part = gears.gear(number_of_teeth=12, circular_pitch=8, gear_thickness=6, bore_diameter=5)
```

Each attribute is built the first time you access it, not up front — so
importing a file with dozens of declarations only ever builds the ones you
actually call. `dir(gears)` and `hasattr()` work normally; an attribute
that isn't a declared module raises `AttributeError`, same as any other
object. Only `module` declarations are exposed. OpenSCAD `function`s return
plain values (numbers, lists, strings), and there's no mechanism in this
CSG-based pipeline to extract one — functions are parsed (so a name
collision with a module is still detected) but never surfaced as an
attribute.

If two files reachable from `path` both declare a module with the same
name, the one that wins is whichever `find_module`/the namespace resolves
to via a depth-first walk that records a file's own declarations *after*
recursing into what it references — approximating OpenSCAD's actual
"textual substitution, last definition wins" behavior for the common case
(a file's own `use`/`include` directives gathered at the top, its
declarations after). A file that interleaves declarations *between*
includes isn't modeled exactly right; this is rare enough in practice
(most libraries treat their own name collisions as a bug) that it wasn't
worth a fully textual-order scan.

**Caching.** The declaration scan (tokenizing `path` and walking its
`use`/`include` graph) is cached on `path`'s resolved location and
modification time, so repeated `import_module()` calls for the same file
don't re-parse it — whether or not you ask for the same module each time.
Only `path` itself is watched for changes; a file it reaches transitively
being edited won't invalidate the cache. That covers third-party libraries
(static) and the common case of iterating on your own single file directly.
If you're editing something reached only transitively, call
`scad123d.scad_declarations.clear_cache()` to force a rescan.

## `hull()` and `minkowski()`

Neither is a primitive in OpenSCAD either — both are CGAL operations over
tessellated Nef polyhedra. OpenSCAD has no analytic hull; it meshes its
children first, then computes. OCCT has no convex hull operator at all.

scad123d recognizes several analytic special cases and falls back to a mesh
for everything else. Classification runs on the already-built geometry for
each operand, not on the raw OpenSCAD source — real code (BOSL2 especially)
wraps a primitive in many layers of module-call and attachment bookkeeping,
and the existing tree-walker has already resolved all of that by the time
this runs, so these cases fire on real code, not just hand-written examples.

**`minkowski()` with a ball is computed analytically and exactly.** A
Minkowski sum with a ball *is* an offset, which OCCT does natively:

```openscad
minkowski() { cube([20,15,10], center=true); sphere(r=3); }
```

becomes `offset(Box(20,15,10), 3, kind=Kind.ARC)`, and returns proper
analytic topology — the box case yields 6 planes, 12 cylinders and 8 spheres.

The "ball" need not be a literal `sphere()`/`circle()` — a `polyhedron()` (or
`polygon()`) whose vertices are all equidistant from a common centroid, with
enough of them to be a plausible curved-surface tessellation rather than a
deliberate few-sided polytope, is recognized too. This isn't a hypothetical
case: BOSL2's own `cuboid(rounding=r)` builds its rounding kernel as an
explicit ~258-vertex polyhedron rather than calling `sphere()`.

**`hull()` of equal-radius spheres, or of equal-radius parallel cylinders
sharing one axial span, is computed analytically and exactly.** A hull of
equal spheres is exactly `offset(convex_hull_of_centers, r)` — the same
operation as the minkowski case above, since a sphere hull and a
box-minkowski-ball are the same underlying shape. A hull of parallel,
equal-radius, equal-height cylinders reduces the same way one dimension down:
project each cylinder's axis onto the plane perpendicular to the shared
direction, take the 2D convex hull, offset by the radius, and extrude along
the shared direction — the "rounded box from corner posts" idiom seen in
real, hand-written OpenSCAD. A fully collinear point set (any count, so long
as they lie on one line — the common 2-post capsule/slot) is built directly
as a capsule rather than through a degenerate hull.

**Deliberately not handled**, and left to fall back to a mesh: a hull of
equal-radius spheres whose centers are coplanar but not collinear (qhull's 3D
convex hull raises on degenerate/flat input, so this is a hard fallback, not
a silent wrong answer); cylinders that don't all share one axial span; hull
of unequal radii; hull mixing spheres and cylinders; and everything else —
general Minkowski sums, `projection()`, `surface()`, and mesh `import()`.

**The fallback is *exactly as accurate as OpenSCAD*, since it is OpenSCAD.**
For any node with no analytic path, scad123d writes just that subtree back out
as `.csg` (which is itself valid OpenSCAD input), asks OpenSCAD to render it,
and splices the resulting mesh into the tree with a warning naming the node.
What you lose is analytic surfaces in that region: no meaningful fillets
there, and selectors return triangles.

**Scope control.** Meshing a subtree localizes the tessellation, but the mesh
still propagates upward through any later booleans, and OCCT boolean-ing
triangle soup is slow and occasionally fails. Two policies:

```python
scad123d.import_scad(p, mesh_scope="minimal")  # default: mesh only the subtree
scad123d.import_scad(p, mesh_scope="hoist")    # any unsupported node -> mesh whole model
```

`minimal` keeps the most analytic geometry; `hoist` is more robust because
OpenSCAD performs every boolean and you import one clean mesh.

See [ROADMAP.md](../ROADMAP.md) for the verification behind each of these
(exact error bounds, what was checked against what) and the further analytic
cases that are feasible but not yet built.

## `$fn`, `$fa`, `$fs`

`$fn` is genuinely ambiguous. Set globally it is usually a complexity switch
(`$fn=48` while developing, `$fn=128` for export) and you want exact BRep
curves. Set at a call site it is usually intentional geometry —
`circle(r=10, $fn=6)` *is a hexagon* in OpenSCAD, not an approximation.

**The CSG output cannot distinguish the two.** It records only the effective
value at each node, so an inherited `$fn=48` and a call-site `$fn=48` look
identical. scad123d therefore discriminates on magnitude:

| effective `$fn` | behavior |
|---|---|
| `0` (unset; `$fa`/`$fs` driving) | exact BRep curves |
| `1..facet_threshold-1` | faceted — a real N-gon |
| `>= facet_threshold` (default 20) | exact BRep curves |

```python
scad123d.import_scad(p, facet_threshold=20)   # default
scad123d.import_scad(p, facet_threshold=0)    # always exact curves
scad123d.import_scad(p, facet_threshold=1e9)  # always honor $fn
```

**Workaround when the heuristic is wrong.** If you genuinely need a 24-sided
polygon as geometry, either raise `facet_threshold` above it, or make the
intent explicit in the `.scad` by building the polygon directly:

```openscad
// instead of circle(r=10, $fn=24), which scad123d reads as "a circle"
polygon([for (i=[0:23]) [10*cos(i*15), 10*sin(i*15)]]);
```

Faceted `sphere()` at low `$fn` takes the mesh path — OpenSCAD's sphere
tessellation is a ring construction that is not worth reproducing for a case
this rare. `circle()` and `cylinder()` facet analytically.

## Not yet implemented

- `surface()` and `projection()` — mesh fallback only
- `import()` of STL/OFF/AMF/3MF — mesh fallback; DXF/SVG unsupported
- `linear_extrude(twist=...)` — mesh fallback
- `#` and `%` modifiers are parsed; `%` (background) children are dropped, `#`
  (highlight) is treated as a no-op. `!` (show-only) is honored. `*` never
  reaches us — OpenSCAD strips it.

## Security

**scad123d executes the OpenSCAD binary on the file you pass it, and OpenSCAD
executes the `.scad` language.** A `.scad` file can `include <>` and `use <>`
arbitrary paths, and `import()`/`surface()` read arbitrary files. Anything
reachable from an `include` path runs.

Do not call `import_scad()` on untrusted input. There is no sandbox. If you
must process untrusted `.scad`, run it in a container with no network and a
read-only filesystem.

## Testing

```bash
just test      # everything
just test-ci   # only tiers that need no OpenSCAD binary
just fixtures  # regenerate committed .csg fixtures + reference metrics
```

Tests come in two tiers. Tier 1 parses committed `.csg` fixtures and asserts
against committed reference metrics (volume, bounding box, centroid) — no
binary needed, runs anywhere. Tier 2 is marked `needs_openscad` and
auto-skips when no binary is found; it regenerates `.csg` from `.scad`,
checks it against the fixture, and runs the STL differential comparison.

CSG export is deterministic and `.csg` → `.csg` is idempotent, which is what
makes the committed fixtures stable.
