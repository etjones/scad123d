"""Rung 2: analytic hull() for equal-radius spheres and parallel cylinders.

hull() of N equal-radius spheres is exactly offset(convex_hull_of_centers, r):
verified during design against a box of 8 corners and a tetrahedron, matching
the Steiner formula to ~1e-10, with the right topology (the box case returns
6 planes, 12 cylinders, 8 spheres -- the same shapes minkowski.py's single-ball
case produces, since both are the same underlying operation).

hull() of N equal-radius, axis-parallel cylinders that all share one axial
span reduces the same way one dimension down: project each cylinder's axis
onto the plane perpendicular to the shared direction, take the 2D convex hull
of those points, offset by the radius, and extrude along the shared direction.
This is the common "rounded box from corner posts" idiom.

Classification works on the already-built Shape for each hull() child, for
the same reason minkowski.py's ball detection does: real code wraps
primitives in layers of module-call and attachment bookkeeping that the
existing walker has already resolved by the time this runs.

Scope, deliberately: a fully collinear point set (any number of spheres, so
long as they lie on one line -- the common 2-post capsule/slot idiom) is built
directly as a capsule rather than via a degenerate hull. A coplanar-but-not-
collinear sphere arrangement, and cylinders that do not all share one axial
span, are not handled -- qhull raises on the former and the span check simply
declines to match the latter. Both fall back to a mesh. See ROADMAP.md.
"""

import math

import numpy as np
from build123d import (
    Align,
    Circle,
    Cylinder,
    Edge,
    Face,
    GeomType,
    Kind,
    Plane,
    Polygon,
    Pos,
    Shape,
    Sphere,
    Vector,
    Wire,
    extrude,
)
from build123d import (
    offset as _bd_offset,
)
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Sphere
from scipy.spatial import ConvexHull

from .solids import polyhedron

Point3 = tuple[float, float, float]
Point2 = tuple[float, float]

_RADIUS_REL_TOL = 1e-6
_COLLINEAR_REL_TOL = 1e-6
_PARALLEL_TOL = 1e-6
_SPAN_TOL = 1e-6


def _sphere_center_radius(shape: Shape) -> tuple[Point3, float] | None:
    solids = shape.solids()
    faces = shape.faces()
    if len(solids) != 1 or len(faces) != 1 or faces[0].geom_type != GeomType.SPHERE:
        return None
    adaptor = BRepAdaptor_Surface(faces[0].wrapped)
    if adaptor.GetType() != GeomAbs_Sphere:
        return None
    sphere = adaptor.Sphere()
    loc = sphere.Location()
    return (loc.X(), loc.Y(), loc.Z()), sphere.Radius()


def _cylinder_axis_radius(shape: Shape) -> tuple[Point3, Point3, float] | None:
    """The two cap-face centers and radius, if ``shape`` is a plain cylinder.

    Requiring exactly one CYLINDER face and two PLANE faces (three total)
    excludes cones (r1 != r2), whose lateral face is a GeomType.CONE, so
    "equal top/bottom radius" falls out of the topology check for free.
    """
    solids = shape.solids()
    faces = shape.faces()
    if len(solids) != 1 or len(faces) != 3:
        return None
    cyl_faces = [f for f in faces if f.geom_type == GeomType.CYLINDER]
    plane_faces = [f for f in faces if f.geom_type == GeomType.PLANE]
    if len(cyl_faces) != 1 or len(plane_faces) != 2:
        return None
    adaptor = BRepAdaptor_Surface(cyl_faces[0].wrapped)
    if adaptor.GetType() != GeomAbs_Cylinder:
        return None
    radius = adaptor.Cylinder().Radius()
    a = tuple(plane_faces[0].center())
    b = tuple(plane_faces[1].center())
    return a, b, radius


def _dist(a: Point3, b: Point3) -> float:
    return math.dist(a, b)


def _pairwise_extremes(points: list[Point3]) -> tuple[float, Point3, Point3]:
    """The most widely separated pair -- cheap for the small N a hull() has."""
    best = (0.0, points[0], points[0])
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            d = _dist(points[i], points[j])
            if d > best[0]:
                best = (d, points[i], points[j])
    return best


def _is_collinear(points: list[Point3], a: Point3, b: Point3, span: float) -> bool:
    if span < 1e-12:
        return True
    direction = tuple((b[i] - a[i]) / span for i in range(3))
    for p in points:
        rel = tuple(p[i] - a[i] for i in range(3))
        t = sum(rel[i] * direction[i] for i in range(3))
        foot = tuple(a[i] + t * direction[i] for i in range(3))
        if _dist(p, foot) > _COLLINEAR_REL_TOL * span:
            return False
    return True


def _capsule(a: Point3, b: Point3, r: float) -> Shape:
    if _dist(a, b) < 1e-9:
        return Pos(*a) * Sphere(r)
    direction = Vector(*b) - Vector(*a)
    plane = Plane(origin=Vector(*a), z_dir=direction)
    cyl = plane.location * Cylinder(
        r, direction.length, align=(Align.CENTER, Align.CENTER, Align.MIN)
    )
    return cyl + Pos(*a) * Sphere(r) + Pos(*b) * Sphere(r)


def _merge_coplanar_facets(hull: ConvexHull) -> list[list[int]]:
    """Group qhull's triangular simplices into their true polygonal faces.

    qhull always triangulates; a box's face comes back as 2 triangles sharing
    a plane equation, not 1 quad. Passing that straight to offset_3d makes
    OCCT raise ("Null TopoDS_Shape object") rather than just produce clutter --
    it needs genuinely merged planar faces, not adjacent coplanar triangles.
    Simplices sharing a facet equation (qhull's own outward-normal-consistent
    convention) are merged by re-deriving their boundary as the 2D hull of
    their vertices projected onto that plane -- valid because a face of a
    convex polytope is itself convex.
    """
    groups: list[tuple[np.ndarray, list[int]]] = []
    for eq, simplex in zip(hull.equations, hull.simplices):
        target = next((g for g in groups if np.allclose(eq, g[0], atol=1e-6)), None)
        if target is None:
            groups.append((eq, list(simplex)))
        else:
            target[1].extend(i for i in simplex if i not in target[1])

    faces: list[list[int]] = []
    for eq, indices in groups:
        if len(indices) == 3:
            faces.append(indices)
            continue
        normal = eq[:3]
        arbitrary = (
            np.array([1.0, 0.0, 0.0])
            if abs(normal[0]) < 0.9
            else np.array([0.0, 1.0, 0.0])
        )
        u = np.cross(normal, arbitrary)
        u = u / np.linalg.norm(u)
        v = np.cross(normal, u)
        planar = np.array(
            [[np.dot(hull.points[i], u), np.dot(hull.points[i], v)] for i in indices]
        )
        ordered = ConvexHull(planar).vertices
        faces.append([indices[i] for i in ordered])
    return faces


def _hull3d_offset(points: list[Point3], r: float) -> Shape | None:
    span, a, b = _pairwise_extremes(points)
    if span < 1e-9:
        return Pos(*points[0]) * Sphere(r)
    if _is_collinear(points, a, b, span):
        return _capsule(a, b, r)

    try:
        hull = ConvexHull(np.asarray(points))
        faces = _merge_coplanar_facets(hull)
        verts = [tuple(float(x) for x in p) for p in hull.points]
        poly = polyhedron(verts, faces)
        result = _bd_offset(poly, amount=r, kind=Kind.ARC)
    except Exception:  # noqa: BLE001
        return None
    return result if result is not None and result.is_valid else None


def _stadium2d(a: Point2, b: Point2, r: float) -> Shape:
    """A 2D capsule as one closed wire (2 lines + 2 tangent arcs).

    Built as a single wire rather than a union of a rectangle and two circles:
    the union version left 12 lateral faces after extrusion instead of 4 --
    OCCT's boolean fuse does not merge the collinear edge segments the union
    introduces, only a topologically clean wire does.
    """
    if math.dist(a, b) < 1e-9:
        return Pos(a[0], a[1], 0) * Circle(r)
    ax, ay = a
    bx, by = b
    length = math.dist(a, b)
    ux, uy = (bx - ax) / length, (by - ay) / length
    nx, ny = -uy, ux
    c1 = (ax + nx * r, ay + ny * r)
    c2 = (bx + nx * r, by + ny * r)
    c3 = (bx - nx * r, by - ny * r)
    c4 = (ax - nx * r, ay - ny * r)
    wire = Wire(
        [
            Edge.make_line(c1, c2),
            Edge.make_tangent_arc(c2, (ux, uy, 0), c3),
            Edge.make_line(c3, c4),
            Edge.make_tangent_arc(c4, (-ux, -uy, 0), c1),
        ]
    )
    return Face(wire)


def _hull2d_offset(points: list[Point2], r: float) -> Face | None:
    span, a, b = _pairwise_extremes([(p[0], p[1], 0.0) for p in points])
    if span < 1e-9:
        return Pos(points[0][0], points[0][1], 0) * Circle(r)
    a2, b2 = (a[0], a[1]), (b[0], b[1])
    if _is_collinear([(p[0], p[1], 0.0) for p in points], a, b, span):
        return _stadium2d(a2, b2, r)

    try:
        hull = ConvexHull(np.asarray(points))
    except Exception:  # noqa: BLE001
        return None

    ordered = [tuple(float(x) for x in points[i]) for i in hull.vertices]
    try:
        poly = Polygon(*ordered, align=None)
        result = _bd_offset(poly, amount=r, kind=Kind.ARC)
    except Exception:  # noqa: BLE001
        return None
    return result if result is not None and result.is_valid else None


def _hull_of_spheres(shapes: list[Shape]) -> Shape | None:
    classified = [_sphere_center_radius(s) for s in shapes]
    if any(c is None for c in classified):
        return None
    radii = [r for _, r in classified]
    r0 = radii[0]
    if any(abs(r - r0) > _RADIUS_REL_TOL * r0 for r in radii):
        return None
    centers = [c for c, _ in classified]
    return _hull3d_offset(centers, r0)


def _hull_of_cylinders(shapes: list[Shape]) -> Shape | None:
    classified = [_cylinder_axis_radius(s) for s in shapes]
    if any(c is None for c in classified):
        return None
    radii = [r for _, _, r in classified]
    r0 = radii[0]
    if any(abs(r - r0) > _RADIUS_REL_TOL * r0 for r in radii):
        return None

    a0, b0, _ = classified[0]
    length0 = _dist(a0, b0)
    if length0 < 1e-9:
        return None  # zero-height cylinder; not a usable reference direction
    dir0 = tuple((b0[i] - a0[i]) / length0 for i in range(3))

    span: tuple[float, float] | None = None
    axis_points: list[Point3] = []
    for a, b, _ in classified:
        length = _dist(a, b)
        if length < 1e-9:
            return None
        direction = tuple((b[i] - a[i]) / length for i in range(3))
        dot = sum(direction[i] * dir0[i] for i in range(3))
        if abs(abs(dot) - 1.0) > _PARALLEL_TOL:
            return None
        if dot < 0:
            a, b = b, a
        t_a = sum((a[i] - a0[i]) * dir0[i] for i in range(3))
        t_b = sum((b[i] - a0[i]) * dir0[i] for i in range(3))
        this_span = (min(t_a, t_b), max(t_a, t_b))
        if span is None:
            span = this_span
        elif (
            abs(this_span[0] - span[0]) > _SPAN_TOL * length0
            or abs(this_span[1] - span[1]) > _SPAN_TOL * length0
        ):
            return None
        axis_points.append(tuple((a[i] + b[i]) / 2 for i in range(3)))

    plane = Plane(origin=Vector(*a0), z_dir=Vector(*dir0))
    points2d = []
    for p in axis_points:
        rel = Vector(*p) - plane.origin
        points2d.append((rel.dot(plane.x_dir), rel.dot(plane.y_dir)))

    face2d = _hull2d_offset(points2d, r0)
    if face2d is None:
        return None

    # plane.origin is a0, i.e. t=0 on the shared axis; span[0] is always 0 by
    # construction (span is seeded from classified[0], whose own t_a is 0
    # relative to itself), so placing the extrusion at `plane` with no
    # further offset lands exactly on the shared span's start.
    placed = plane * face2d
    try:
        result = extrude(placed, amount=span[1] - span[0])
    except Exception:  # noqa: BLE001
        return None
    return result if result is not None and result.is_valid else None


def analytic_hull(shapes: list[Shape]) -> Shape | None:
    """Try to evaluate hull() analytically; None means fall back to a mesh."""
    if not shapes:
        return None
    if len(shapes) == 1:
        return shapes[0]
    result = _hull_of_spheres(shapes)
    if result is not None:
        return result
    return _hull_of_cylinders(shapes)
