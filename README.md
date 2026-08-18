# scad123d

Import OpenSCAD files as native [build123d](https://build123d.readthedocs.io/)
BRep geometry.

```python
import scad123d

part = scad123d.import_scad("bracket.scad")          # -> build123d Shape
part = scad123d.import_scad("bracket.scad", width=40)  # override top-level vars
```

The result is a plain build123d `Shape`, so it mixes freely with native
build123d — fillet it, select faces, sweep along it, export a real STEP:

```python
from build123d import fillet, Axis

part = scad123d.import_scad("bracket.scad")
part = fillet(part.edges().group_by(Axis.Z)[-1], radius=2)
```

BOSL2 and MCAD work out of the box, because scad123d does not reimplement the
OpenSCAD language.

## How it works

scad123d shells out to the OpenSCAD binary for **`--export-format csg`**, which
runs the entire OpenSCAD language and emits a flattened tree:

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

## Requirements

**The OpenSCAD binary must be installed.** scad123d is not a reimplementation
of OpenSCAD; it delegates to it. Discovery order:

1. `$SCAD123D_OPENSCAD` (explicit override)
2. `openscad` / `openscad-nightly` on `$PATH`
3. macOS: `/Applications/OpenSCAD{,-nightly}.app/Contents/MacOS/OpenSCAD`
4. Windows: `C:\Program Files\OpenSCAD{, (Nightly)}\openscad.exe`
5. Linux: flatpak (`org.openscad.OpenSCAD`) and snap paths

CSG export needs no OpenGL, so no `xvfb` wrapper is required on headless Linux.

## Tradeoffs and behavior

### `hull()` and `minkowski()`

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

becomes `offset(Box(20,15,10), 3, kind=Kind.ARC)`. Verified exact against the
Steiner formula to ~1e-10 relative error, on convex and non-convex inputs
alike, and it returns proper analytic topology — the box case yields 6 planes,
12 cylinders and 8 spheres. This is **better than OpenSCAD's own result**,
which is a faceted approximation, and dramatically faster.

The "ball" need not be a literal `sphere()`/`circle()` — a `polyhedron()` (or
`polygon()`) whose vertices are all equidistant from a common centroid, with
enough of them to be a plausible curved-surface tessellation rather than a
deliberate few-sided polytope, is recognized too. This isn't a hypothetical
case: **BOSL2's own `cuboid(rounding=r)` builds its rounding kernel as an
explicit ~258-vertex polyhedron** (radius exact to ~1e-6) rather than calling
`sphere()`, and without this it would silently take the mesh path. With it,
real BOSL2 `cuboid(rounding=3)` imports as 6 planes + 12 cylinders + 8
spheres, matching the Steiner formula to ~4e-7 — the limit is BOSL2's own
kernel precision, not scad123d.

Since rounding is what the overwhelming majority of real `minkowski()` calls
are for, this covers most usage in practice.

**`hull()` of equal-radius spheres, or of equal-radius parallel cylinders
sharing one axial span, is computed analytically and exactly.** A hull of
equal spheres is exactly `offset(convex_hull_of_centers, r)` — the same
operation as the minkowski case above, since a sphere hull and a
box-minkowski-ball are the same underlying shape:

```openscad
// the classic "8 corner posts" rounded-box idiom
hull() {
    translate([-10,-7.5,-5]) sphere(r=3);
    translate([ 10,-7.5,-5]) sphere(r=3);
    // ... 6 more corners
}
```

Verified exact (~1e-9 relative error) against a box of 8 corners and a
tetrahedron. A hull of parallel, equal-radius, equal-height cylinders reduces
the same way one dimension down: project each cylinder's axis onto the plane
perpendicular to the shared direction, take the 2D convex hull, offset by the
radius, and extrude along the shared direction — the "rounded box from corner
posts" idiom seen in real, hand-written OpenSCAD. A fully collinear point set
(any count, so long as they lie on one line — the common 2-post
capsule/slot) is built directly as a capsule rather than through a
degenerate hull.

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

A ladder of further analytic cases — a two-child hull as a `loft` between
silhouettes, unequal-radius sphere hulls, coplanar sphere hulls — is feasible
and planned. See `ROADMAP.md`.

### `$fn`, `$fa`, `$fs`

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

### Not yet implemented

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

## Development

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

## License

MIT
