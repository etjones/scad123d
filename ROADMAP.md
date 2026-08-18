# Roadmap

v1 ships **rung 0** (mesh fallback, so nothing ever hard-fails) and **rung 1**
(analytic Minkowski with a ball). The rungs below add analytic coverage for
cases that currently take the mesh path. Each is gated the same way: attempt
analytic, validate with `is_valid`, fall back to a mesh on failure or on a
non-matching pattern. No rung may make a previously-working model fail.

### Rung 2 — hull of equal-radius spheres and parallel cylinders

The convex hull of N equal-radius spheres is exactly
`offset(convex_hull_of_centers, r, kind=Kind.ARC)`. Verified during design:
against a box of 8 centers and against a tetrahedron, the volume matches the
Steiner formula to ~1e-10, and the topology is right — the box case yields 6
planes, 12 cylinders and 8 spheres.

The hull needed is a convex hull of *points*, not surfaces, so it is easy and
robust (qhull, or ~150 lines of incremental hull). Remaining work:

- merge coplanar hull facets before offsetting; a 24-gon prism offset produced
  146 faces because each coplanar triangle pair contributes a degenerate
  cylindrical patch along its shared edge
- hull of parallel equal-radius cylinders → 2D offset of the 2D hull of the
  axis footprints, extruded. **This is BOSL2's `cuboid(rounding=, edges="Z")`**,
  so it is the highest-value case in this rung.

Payoff: rounded boxes become analytic and filletable instead of meshed, and
much faster — OpenSCAD needed over two minutes to hull 8 spheres at `$fn=256`,
reaching only 0.999940 of the exact volume; the analytic offset was exact in
25 ms with 26 faces instead of ~130k triangles.

### Rung 3 — two-child hull as a loft

`hull(){ a; translate(v) b; }` is bounded by parts of `∂a`, parts of `∂b`, and
the ruled surface of tangent lines between them — which is a `loft` between the
two silhouette wires. Exact wherever the silhouette is closed-form (spheres,
circles, discs, coaxial cylinders); needs per-type silhouette derivation.

Note this does **not** generalise to n children: `hull(a,b,c)` strictly contains
the union of the pairwise hulls, so the middle region would be missed.

### Rung 4 — unequal-radius sphere hulls, Minkowski with a box

- Hull of spheres with differing radii is the additively-weighted hull: tangent
  cones per pair, tangent planes per triple. The combinatorics come from a
  power diagram rather than a plain hull of centers.
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

## Upstream bug found in solid123d

`solid123d.scale()` calls `build123d.scale(shape, by=factors)` without
`about=`, so it scales about each object's location position rather than the
origin. OpenSCAD's `scale()` is origin-based, so bridged code with an
off-origin object gets the wrong result. scad123d does not use it — see
`solids.apply_matrix` — but it should be fixed there.
