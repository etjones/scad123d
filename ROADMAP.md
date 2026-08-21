# Roadmap

v1 shipped **rung 0** (mesh fallback, so nothing ever hard-fails), **rung 1**
(analytic Minkowski with a ball), **rung 1.5** (Minkowski with a
polyhedron/polygon ball — see below), and **rung 2** (analytic hull of
equal-radius spheres and of equal-radius parallel cylinders). **Rung 3**
(exact hull of all-polyhedral children, via build123d's `ConvexPolyhedron`)
shipped after v1 — see below. The rungs below add analytic coverage for
cases that still take the mesh path. Each is gated
the same way: attempt analytic, validate with `is_valid`, fall back to a mesh
on failure or on a non-matching pattern. No rung may make a previously-working
model fail.

Classification for every rung below runs on the **already-built Shape** for
each operand, not on the raw CSG node tree. That choice came out of tracing
real BOSL2 output while building rung 2: real module-heavy code wraps a bare
primitive in many layers of `group()` (one per module-call boundary) and
`multmatrix()` (often identity, from attachable() bookkeeping), sometimes
alongside auxiliary sibling nodes that build to nothing. A raw-tree classifier
would have to re-solve all of that; the existing walker has already resolved
it correctly by the time a rung runs, so classification only needs to look at
the built result.

### Rung 1.5 — Minkowski with a polyhedron/polygon ball (shipped)

The "ball" in a `minkowski()` need not be a literal `sphere()`/`circle()`
node. **BOSL2's own `cuboid(rounding=r)` builds its rounding kernel as an
explicit polyhedron** — verified directly: 258 vertices, every one at radius
3.000000 ± 4e-6 from a shared centroid — rather than calling `sphere()`. Rung
1 alone does not see this at all; it only recognizes a bare `sphere`/`circle`
CSG node.

Fix: recognize a `polyhedron()`/`polygon()` whose vertices are all equidistant
from a common centroid, with at least 24 of them (comfortably above all 5
Platonic solids' vertex counts — a cube's 8 corners are *also* equidistant
from its centroid, so vertex count is what separates a genuine curved-surface
tessellation from a deliberate few-sided polytope kernel). Verified against
the real BOSL2 file: `cuboid([20,15,10], rounding=3)` now imports as 6 planes
+ 12 cylinders + 8 spheres, matching the Steiner formula to ~4e-7 relative
error (limited by BOSL2's own kernel precision, not scad123d).

### Rung 2 — hull of equal-radius spheres and parallel cylinders (shipped)

The convex hull of N equal-radius spheres is exactly
`offset(convex_hull_of_centers, r, kind=Kind.ARC)`. Verified: a box of 8
centers and a tetrahedron both match the Steiner formula to ~1e-9, with the
box case returning 6 planes, 12 cylinders and 8 spheres.

A hull of parallel, equal-radius cylinders that all share one axial span
reduces the same way one dimension down: project each cylinder's axis onto
the plane perpendicular to the shared direction, 2D-hull those points, offset
by the radius, extrude along the shared direction.

A fully collinear point set (any count, so long as they lie on one line) is
built directly as a capsule/stadium rather than through a degenerate hull —
qhull's `ConvexHull` has no facets to return for 2 points or a 1D arrangement,
so this needed its own construction, and it is also the common "2-post
slot" idiom in its own right.

**Two non-obvious implementation issues, worth recording:**

- Passing qhull's raw triangulated simplices straight into `offset_3d` made
  OCCT raise `Standard_Failure: Null TopoDS_Shape object` rather than just
  produce clutter — a box's face comes back as 2 triangles sharing a plane
  equation, and `offset_3d` needs genuinely merged planar faces, not adjacent
  coplanar triangles. Fixed by grouping simplices sharing a facet equation
  (qhull's own outward-normal-consistent convention: `hull.equations`) and
  re-deriving each merged face's boundary as the 2D hull of its vertices
  projected onto that plane — valid because a face of a convex polytope is
  itself convex.
- The first stadium (2D capsule) implementation built it as a boolean union of
  a rectangle and two circles. That leaves 12 lateral faces after extrusion
  instead of 4: OCCT's fuse does not merge the collinear edge segments the
  union introduces at the tangent points. Rebuilt as one topologically clean
  closed wire (2 lines + 2 tangent arcs via `Edge.make_tangent_arc`) instead —
  same volume, but 4 faces instead of 12.

**What this does *not* cover, deliberately, and falls back to a mesh:**

- A hull of equal-radius spheres whose centers are coplanar but not collinear.
  qhull's 3D `ConvexHull` raises on degenerate (flat) input, so this is caught
  and falls back rather than crashing — not attempted as a 2D-then-thickened
  construction (see Rung 2.5 below).
- Cylinders that do not all share one axial span (different heights, or
  offset along the axis relative to each other) — the true hull there is not
  a simple extrusion.
- A hull mixing spheres and cylinders, or spheres/cylinders of unequal radius.
- Facets from *this* project's own faceted-primitive path (`$fn` below
  `facet_threshold`): a low-poly `cylinder($fn=8)` has no single CYLINDER
  face, so it does not classify as a plain cylinder here even though it came
  from a `cylinder()` call. Correct as-is: a faceted prism's hull is
  genuinely different from a round cylinder's.

**One finding that changed the plan**, from tracing real BOSL2 CSG output
before writing any of the above: BOSL2's `cuboid(rounding=, edges="Z")` (only
vertical edges rounded) does **not** use `hull()` for its real geometry at
all — the one `hull()` node present resolves to entirely empty children (an
`intersection()` with non-overlapping operands, apparently anchor/attachment
bookkeeping) and is a no-op. The actual rounded profile is a many-vertex 2D
polygon, pre-faceted by BOSL2 itself at `$fn` resolution, fed straight into
`linear_extrude` — which scad123d already handled natively before rung 2
existed. So rung 2 does not improve that specific call; the faceting ceiling
there is BOSL2's own choice, not scad123d's. (`cuboid(rounding=r)`, rounding
*every* edge, is the one that uses `minkowski()` with a polyhedron kernel —
that is rung 1.5, above.)

### Rung 3 — hull of all-polyhedral children (shipped)

A polyhedral solid contributes only its vertices to any convex hull, so
`hull()` of children whose faces are all planar is exactly the convex hull
of their combined vertices — built as real BRep via build123d's
`ConvexPolyhedron` (v0.11.0+: scipy qhull for the hull, OCCT sewing,
coplanar facets merged by `clean()`). Covers cubes, `polyhedron()`s,
extruded polygons, faceted (below-threshold `$fn`) circles/cylinders,
matrix-transformed anything-planar, and mesh-fallback children (their
triangles are planar too). This is the "exact and combinatorial for
polytopes" half of rung 5, done. Verified differentially: same exact
polytope OpenSCAD computes, volumes agree to float precision. Declines
(mesh fallback) on any curved child outside rung 2's idioms — a curved
surface's hull is not determined by its vertices — and on 2D children.

### Rung 2.5 — coplanar sphere hulls as a thickened 2D offset

A planar (coplanar, non-collinear) arrangement of equal-radius spheres is the
Minkowski sum of a flat 2D polygon (the hull of centers, embedded with zero
thickness) and a 3D ball — a "rounded coin" shape. Not the same construction
as the 3D case (a literal zero-thickness solid is degenerate) and not simply
"extrude the 2D hull, then round" (that gives a cylinder with flat ends, not a
lens). Likely buildable as: offset the 2D polygon in-plane by `r` for the
waist, then loft/join hemispherical caps top and bottom. Unexplored.

### Rung 3.5 — two-child hull as a loft

`hull(){ a; translate(v) b; }` is bounded by parts of `∂a`, parts of `∂b`, and
the ruled surface of tangent lines between them — which is a `loft` between the
two silhouette wires. Exact wherever the silhouette is closed-form (spheres,
circles, discs, coaxial cylinders); needs per-type silhouette derivation.

Note this does **not** generalise to n children: `hull(a,b,c)` strictly contains
the union of the pairwise hulls, so the middle region would be missed.

### Rung 4a — two-sphere and two-circle pair hulls (shipped)

The **pair** case of the additively-weighted hull is closed-form: with
`sin α = (r₂ − r₁)/d`, the hull of two spheres is two spherical caps joined
by the external tangent cone, which grazes each sphere at latitude `−α`
(radius `r·cos α`, axial offset `−r·sin α` toward the small side — *not* at
the equator). Built by **sewing** exactly those three boundary patches
(partial-sphere and cone primitives share their seam circles by
construction) rather than fusing solids, since OCCT booleans are flakiest
at tangent contact — the only seam type this shape has. Self-checks
against the closed-form volume; mismatch → mesh fallback. Valid for any
non-contained spacing, overlapping included (the support-function split
`u·n = −sin α` is independent of overlap); containment short-circuits to
the big sphere. The 2D version (two discs → two arcs + two tangent
segments, built directly as a wire, zero booleans) also fixes what was
previously a hard *crash*: OpenSCAD cannot render a 2D subtree to a mesh,
so 2D hulls had no fallback at all. Strictly pairwise — see rung 4.

### Rung 4 — n-sphere unequal-radius hulls, Minkowski with a box

- Hull of **three or more** spheres with differing radii is the general
  additively-weighted hull: tangent cones per pair, tangent planes per
  triple. The combinatorics come from a power diagram rather than a plain
  hull of centers. (The pair case shipped as rung 4a.)
- `minkowski(X, box)`: the obvious decomposition into three successive prism
  sweeps does **not** work — OCCT's `BRepPrimAPI_MakePrism` rejects solids
  (`Standard_Failure: Solids are not Processed`). It only prisms faces and
  shells. Possibly recoverable by prisming the boundary shell and unioning with
  both end positions; unverified.

### Rung 5 — general hull and Minkowski

Research-grade, not a sprint. Tangent surfaces between arbitrary quadrics or
NURBS are not simple analytic patches; you would be fitting B-splines to
tangency curves with degenerate configurations throughout. General `A ⊕ B`
needs normal-fan pairing of surface patches: exact and combinatorial for
polytopes, open-ended for smooth bodies.

## Other work

- **Faceted `sphere()`** currently takes the mesh path. OpenSCAD's sphere is a
  ring construction (`num_rings = (fragments+1)/2`, ring at
  `phi = 180*(i+0.5)/num_rings`); reproducing it analytically is ~20 lines and
  would remove the last common mesh fallback in the `$fn` path.
- **`projection()`** — `cut=true` is a planar section, which OCCT does natively;
  `cut=false` is an outline projection, which is harder.
- **`import()`** of DXF and SVG for 2D profiles.
- **`linear_extrude(twist=)`** — a sweep along a helix.
- **`$fn` provenance.** The CSG export cannot distinguish a global `$fn` from a
  call-site one, so scad123d discriminates on magnitude. A refinement: parse the
  original `.scad` for top-level `$fn` assignments and compare, treating a node
  whose `$fn` differs from the global as call-site. Genuinely ambiguous when
  they coincide, so it stays a heuristic.
- **A translated ball in `minkowski()`.** `minkowski(A, translate(c)(sphere(r)))`
  is mathematically resolvable too — Minkowski sums are equivariant under
  translation of either operand, so the result is just
  `translate(c)(offset(A, r))`. Currently only a ball at the origin is
  recognized (matches the idiom as it's actually written, and keeps parity with
  an explicit test asserting the translated case falls back); worth revisiting
  if translated-ball calls turn out to be common in practice.

## Upstream bug found in solid123d

`solid123d.scale()` calls `build123d.scale(shape, by=factors)` without
`about=`, so it scales about each object's location position rather than the
origin. OpenSCAD's `scale()` is origin-based, so bridged code with an
off-origin object gets the wrong result. scad123d does not use it — see
`solids.apply_matrix` — but it should be fixed there.

Separately: `solid123d.minkowski()` and `.hull()` raised `NotImplementedError`
unconditionally, including for the ball case that rung 1 above handles. Fixed
upstream directly (not merely worked around here) — see solid123d's own
CONVERSATION.md, 2026-08-18 entry.
