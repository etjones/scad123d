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

## Calling a single module

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

**Path resolution.** `path` is resolved two ways:

- If it names a real local file, it's resolved to an absolute path before
  being written into the wrapper. This matters because the wrapper lives in
  a fresh temporary directory on every call, so a relative `include` would
  resolve against the wrong directory (OpenSCAD resolves a relative
  `include <>`/`use <>` against the directory of the file *containing* the
  statement) if left relative.
- Otherwise (`"BOSL2/std.scad"`, `"MCAD/involute_gears.scad"`), it's passed
  through untouched, exactly as if you'd written that `include <>`/`use <>`
  by hand — resolved via `$OPENSCADPATH` and the default library folders.

**`include` vs `use`.** `import_module` defaults to `include`, which brings
in a library's module/function definitions *and* its variables and any
top-level code it runs. `use` brings in only definitions. This matters
because it's not just a style choice: some libraries' modules rely on
variables defined at the library's top level (BOSL2 is documented and
commonly used via `include`), and `use` would silently leave those
undefined. Other libraries are conventionally brought in with `use` (MCAD's
own examples do this) specifically to avoid re-running any demo geometry a
library file might render at its own top level. Pass
`import_style="use"` when you know a library expects it.

**Arguments.** The returned callable accepts positional and keyword
arguments the same way the OpenSCAD module does; both are rendered to
OpenSCAD literal syntax and spliced into the generated call. `None` becomes
`undef`; lists, strings, bools, and numbers all follow OpenSCAD's own
literal syntax.

**A wrong module or argument name is not a Python-level error where you
might expect it.** OpenSCAD treats an unknown module name, or an argument
name a module doesn't recognize, as a *warning*, not a failure — it exits
successfully and just renders less than you asked for (an unrecognized
argument silently falls back to that parameter's default; an unknown module
call renders nothing at all). `import_module` cannot detect this at the
point you call `import_module()` itself, since nothing runs until you
actually call the returned function. When a call produces no geometry at
all, the raised `UnsupportedNodeError` includes OpenSCAD's own warning text,
which is usually enough to spot a typo'd name immediately. A typo'd
*argument* name that still produces geometry (because the module fell back
to a default) will not raise anything — check the result if you're not sure
your arguments were actually recognized.

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
