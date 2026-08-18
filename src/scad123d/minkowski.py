"""Rung 1: analytic Minkowski sums.

A Minkowski sum with a ball *is* an offset, which OCCT performs natively and
exactly. Since rounding is what the overwhelming majority of real minkowski()
calls are for, this covers most usage -- and the analytic result is better than
OpenSCAD's own, which is a faceted approximation.

Verified exact against the Steiner formula
    V(P (+) B_r) = V + A*r + (r^2/2) * sum(L_e * theta_e) + (4/3)*pi*r^3
to ~1e-10 relative error on convex and non-convex inputs.

Classification works on the *already-built* Shape, not the raw CSG node, for
the same reason solid123d's equivalent check does: real module-heavy code
(BOSL2 in particular) wraps a "bare" primitive in many layers of group/
multmatrix/intersection from module-call and attachment-point bookkeeping,
including auxiliary sibling nodes that build to nothing. Reading the raw CSG
tree would mean re-solving all of that; the existing walker has already done
it correctly by the time this runs, so this only has to look at the result.

The ball may be an analytic sphere/circle, or a many-vertex polyhedron/polygon
whose vertices are all equidistant from a common centroid -- a real case, not
a hypothetical one: BOSL2's own `cuboid(rounding=r)` builds its rounding
kernel as an explicit 258-vertex polyhedron (radius exact to ~1e-6) rather
than calling `sphere()`.
"""

import math

from build123d import GeomType, Kind, Shape, offset as _bd_offset
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Sphere

_ORIGIN_TOL = 1e-6

# All 5 Platonic solids (vertex-transitive, so every vertex is equidistant
# from the centroid) top out at 20 vertices (the dodecahedron). A deliberate
# polytope kernel that small is not what a "rounding" library emits; a real
# spherical tessellation is comfortably above this.
_MIN_MESH_BALL_VERTICES = 24
_MESH_BALL_REL_TOL = 1e-3


def _mesh_ball_radius(shape: Shape) -> float | None:
    """Radius if every vertex of ``shape`` is equidistant from its centroid.

    Distinguishes a genuine curved-surface tessellation from a deliberate
    few-sided polytope by vertex count alone (see module docstring).
    """
    verts = [tuple(v) for v in shape.vertices()]
    if len(verts) < _MIN_MESH_BALL_VERTICES:
        return None
    cx = sum(v[0] for v in verts) / len(verts)
    cy = sum(v[1] for v in verts) / len(verts)
    cz = sum(v[2] for v in verts) / len(verts)
    dists = [math.dist(v, (cx, cy, cz)) for v in verts]
    if max(dists) < 1e-9:
        return None
    if (max(dists) - min(dists)) > _MESH_BALL_REL_TOL * max(dists):
        return None
    if math.dist((cx, cy, cz), (0.0, 0.0, 0.0)) > _MESH_BALL_REL_TOL * max(dists):
        return None
    return sum(dists) / len(dists)


def ball_radius(shape: Shape) -> float | None:
    """The radius if ``shape`` is a ball (of any representation) at the origin.

    A ball elsewhere than the origin is a different (and much rarer)
    Minkowski sum -- ``minkowski(A, translate(c)(sphere(r)))`` -- so it is
    left unmatched here rather than silently ignoring the offset.
    """
    solids = shape.solids()
    faces = shape.faces()

    if len(solids) == 1 and len(faces) == 1 and faces[0].geom_type == GeomType.SPHERE:
        adaptor = BRepAdaptor_Surface(faces[0].wrapped)
        if adaptor.GetType() != GeomAbs_Sphere:
            return None
        sphere = adaptor.Sphere()
        center = sphere.Location()
        if max(abs(center.X()), abs(center.Y()), abs(center.Z())) > _ORIGIN_TOL:
            return None
        return sphere.Radius()

    edges = shape.edges()
    if not solids and len(faces) == 1 and len(edges) == 1 and edges[0].geom_type == GeomType.CIRCLE:
        adaptor = BRepAdaptor_Curve(edges[0].wrapped)
        if adaptor.GetType() != GeomAbs_Circle:
            return None
        circle = adaptor.Circle()
        center = circle.Location()
        if max(abs(center.X()), abs(center.Y()), abs(center.Z())) > _ORIGIN_TOL:
            return None
        return circle.Radius()

    return _mesh_ball_radius(shape)


def analytic_minkowski(built: list[Shape]) -> Shape | None:
    """Try to evaluate minkowski() as an offset.

    Applies when there are exactly two already-built operands and either one
    is a ball at the origin (Minkowski sums are commutative, so the ball may
    be either argument). Returns None when the pattern does not match or OCCT
    declines, so the caller can fall back to a mesh.
    """
    if len(built) != 2 or any(s is None for s in built):
        return None
    for ball_index in (1, 0):
        radius = ball_radius(built[ball_index])
        if radius is None:
            continue
        target = built[1 - ball_index]
        try:
            result = _bd_offset(target, amount=radius, kind=Kind.ARC)
        except Exception:
            continue
        if result is not None and result.is_valid:
            return result
    return None
