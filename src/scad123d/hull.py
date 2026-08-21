"""Analytic hull(): rung 2 (equal-radius spheres / parallel cylinders) and
rung 3 (all-polyhedral children, exact via ConvexPolyhedron).

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

hull() of children whose faces are ALL planar (rung 3) is exactly the
convex hull of their combined vertices -- a polyhedral solid contributes
only its vertices to any convex hull -- built as real BRep via build123d's
ConvexPolyhedron. That covers cubes, polyhedron()s, extruded polygons,
faceted primitives, and matrix-transformed anything-planar, with no
approximation.

Scope, deliberately: a fully collinear point set (any number of spheres, so
long as they lie on one line -- the common 2-post capsule/slot idiom) is built
directly as a capsule rather than via a degenerate hull. A coplanar-but-not-
collinear sphere arrangement, and cylinders that do not all share one axial
span, are not handled -- qhull raises on the former and the span check simply
declines to match the latter. Both fall back to a mesh, as does any hull
with a curved child outside rung 2's idioms: a curved surface's hull is not
determined by its vertices. See ROADMAP.md.
"""

import math

import numpy as np
from build123d import (
    Align,
    Circle,
    Cone,
    ConvexPolyhedron,
    Cylinder,
    Edge,
    Face,
    GeomType,
    Kind,
    Line,
    Plane,
    Polygon,
    Pos,
    Shape,
    Shell,
    Solid,
    Sphere,
    ThreePointArc,
    Vector,
    Wire,
    extrude,
    make_face,
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


def _two_sphere_hull(ca: Point3, ra: float, cb: Point3, rb: float) -> Shape | None:
    """Exact hull of two spheres with ra < rb, neither containing the other.

    Sewn directly from its three boundary patches -- two spherical caps and
    the external tangent cone -- rather than fusing solids: OCCT booleans
    are at their flakiest exactly at tangent (G1) contact, which is the
    only kind of seam this shape has. The patches share their seam circles
    by construction (each is cut from a primitive at the exact tangency
    latitude), so sewing them into a shell is exact, deterministic, and
    boolean-free.

    The mathematics (with d = |cb - ca|, axis u from small to big):
    sin(a) = (rb - ra)/d. The external tangent cone grazes each sphere not
    at its equator but at latitude -a (measured from each sphere's equator
    plane, tilted toward the small side): a circle of radius r*cos(a) at
    axial offset -r*sin(a) from the center. The kept boundary of the small
    sphere is its back cap (latitude -90 to -a); of the big sphere, its
    front portion (latitude -a to 90) -- more than a hemisphere. This is
    exact for *any* non-contained spacing, overlapping included: sphere i
    supports direction n iff (ci . n + ri) is the max, and that inequality
    splits directions at exactly u . n = -sin(a), independent of overlap.

    The support-function argument does NOT extend to three non-collinear
    spheres of unequal radii -- that hull needs tritangent planes with
    power-diagram combinatorics (see ROADMAP rung 4), so this stays a
    strictly-two-children rung.
    """
    d = _dist(ca, cb)
    sin_a = (rb - ra) / d
    cos_a = math.sqrt(1.0 - sin_a * sin_a)
    alpha_deg = math.degrees(math.asin(sin_a))
    u = (Vector(*cb) - Vector(*ca)) / d

    def sphere_cap(centre: Point3, r: float, lat_lo: float, lat_hi: float) -> Face:
        # align=None: the default align=CENTER would re-center the cap
        # solid's *bounding box* at the origin, sliding the cap off the
        # sphere's true center (confirmed directly). None keeps the native
        # placement: sphere centered at the origin, latitudes about +Z.
        solid = Plane(origin=Vector(*centre), z_dir=u) * Sphere(
            r, arc_size1=lat_lo, arc_size2=lat_hi, align=None
        )
        return solid.faces().filter_by(GeomType.SPHERE)[0]

    x1 = -ra * sin_a
    x2 = d - rb * sin_a
    length = x2 - x1  # = d * cos_a**2, > 0 whenever not contained
    mid = Vector(*ca) + u * ((x1 + x2) / 2)
    cone_solid = Plane(origin=mid, z_dir=u) * Cone(
        bottom_radius=ra * cos_a, top_radius=rb * cos_a, height=length
    )

    try:
        result = Solid(
            Shell(
                [
                    sphere_cap(ca, ra, -90, -alpha_deg),
                    cone_solid.faces().filter_by(GeomType.CONE)[0],
                    sphere_cap(cb, rb, -alpha_deg, 90),
                ]
            )
        )
    except Exception:  # noqa: BLE001
        return None
    if not result.is_valid:
        return None

    # Self-check against the closed form (caps + frustum); a sewing artifact
    # that survived is_valid would be a silently-wrong smooth solid, which is
    # strictly worse than the mesh fallback.
    h1, h2 = ra * (1 - sin_a), rb * (1 + sin_a)
    rho1, rho2 = ra * cos_a, rb * cos_a
    exact = (
        math.pi * h1 * h1 * (3 * ra - h1) / 3
        + math.pi * length / 3 * (rho1**2 + rho1 * rho2 + rho2**2)
        + math.pi * h2 * h2 * (3 * rb - h2) / 3
    )
    if not math.isclose(result.volume, exact, rel_tol=1e-6):
        return None
    return result


def _hull_of_spheres(shapes: list[Shape]) -> Shape | None:
    classified = [_sphere_center_radius(s) for s in shapes]
    if any(c is None for c in classified):
        return None
    radii = [r for _, r in classified]
    r0 = radii[0]
    if any(abs(r - r0) > _RADIUS_REL_TOL * r0 for r in radii):
        if len(shapes) != 2:
            return None
        # Exactly two spheres of unequal radii: the tangent-cone pair case.
        (ca, ra), (cb, rb) = classified
        small, big = (0, 1) if ra <= rb else (1, 0)
        (ca, ra), (cb, rb) = classified[small], classified[big]
        if _dist(ca, cb) + ra <= rb * (1 + _RADIUS_REL_TOL):
            return shapes[big]  # small sphere entirely inside the big one
        return _two_sphere_hull(ca, ra, cb, rb)
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


def _hull_of_polyhedra(shapes: list[Shape]) -> Shape | None:
    """Rung 3: hull() of children whose faces are all planar.

    A polyhedral solid is exactly the hull of its own vertices as far as
    convexity is concerned, so the hull of any collection of them is
    exactly the convex hull of all their vertices combined -- which is
    precisely what build123d's ConvexPolyhedron builds, as a real BRep
    solid (scipy qhull for the hull, OCCT for the sewing, coplanar facets
    merged by clean()). No approximation anywhere: this covers cubes,
    polyhedron()s, prisms from linear_extrude, anything transformed by a
    matrix (planarity survives any affine map), and even children that
    themselves came from the mesh fallback -- their triangles are planar
    too, and OpenSCAD's own hull() of such a child hulls the same
    tessellation.

    Declines (returns None, -> mesh fallback) when any child has a curved
    face -- a curved surface's hull is not determined by its vertices (a
    BRep cylinder has vertices only on its rims) -- or when any child is
    2D: the hull of coplanar children is a 2D operation this rung doesn't
    model, and qhull rejects a flat point set anyway.
    """
    for s in shapes:
        if not s.solids():
            return None
        if any(f.geom_type != GeomType.PLANE for f in s.faces()):
            return None
    points = [(v.X, v.Y, v.Z) for s in shapes for v in s.vertices()]
    try:
        result = ConvexPolyhedron(points)
    except Exception:  # noqa: BLE001
        return None
    return result if result.is_valid else None


def _circle_center_radius(shape: Shape) -> tuple[Point2, float] | None:
    """(center, radius) if shape is a single planar disc in the XY plane."""
    if shape.solids():
        return None
    faces = shape.faces()
    if len(faces) != 1:
        return None
    edges = faces[0].edges()
    if len(edges) != 1 or edges[0].geom_type != GeomType.CIRCLE:
        return None
    centre = edges[0].arc_center
    if abs(centre.Z) > 1e-9:
        return None
    return (centre.X, centre.Y), edges[0].radius


def _hull_of_two_circles(shapes: list[Shape]) -> Shape | None:
    """Exact 2D hull of two discs: two arcs joined by the external tangent
    lines, built directly as a wire -- no booleans at all.

    Same tangent geometry as the sphere pair, one dimension down: with
    sin(a) = (rb - ra)/d, each tangent line grazes circle i at the point
    tilted a past its top (and bottom), so the small circle keeps an arc of
    180 - 2a degrees and the big circle 180 + 2a. Equal radii is just the
    a = 0 case (the classic stadium), so this rung also covers it -- and
    matters beyond exactness: OpenSCAD cannot render a 2D subtree to a
    mesh, so before this rung *any* 2D hull() hard-failed rather than
    falling back.
    """
    if len(shapes) != 2:
        return None
    classified = [_circle_center_radius(s) for s in shapes]
    if any(c is None for c in classified):
        return None
    (ca, ra), (cb, rb) = classified
    small, big = (0, 1) if ra <= rb else (1, 0)
    (ca, ra), (cb, rb) = classified[small], classified[big]
    d = math.hypot(cb[0] - ca[0], cb[1] - ca[1])
    if d + ra <= rb * (1 + _RADIUS_REL_TOL):
        return shapes[big]  # small disc entirely inside the big one
    sin_a = (rb - ra) / d
    cos_a = math.sqrt(1.0 - sin_a * sin_a)
    ux, uy = (cb[0] - ca[0]) / d, (cb[1] - ca[1]) / d
    nx, ny = -uy, ux

    def graze(c: Point2, r: float, side: float) -> tuple[float, float]:
        return (
            c[0] + r * (-sin_a * ux + side * cos_a * nx),
            c[1] + r * (-sin_a * uy + side * cos_a * ny),
        )

    p1p, p1m = graze(ca, ra, +1), graze(ca, ra, -1)
    p2p, p2m = graze(cb, rb, +1), graze(cb, rb, -1)
    back = (ca[0] - ra * ux, ca[1] - ra * uy)  # far point of the small circle
    front = (cb[0] + rb * ux, cb[1] + rb * uy)  # far point of the big circle
    try:
        face = make_face(
            [
                ThreePointArc(p1p, back, p1m),
                Line(p1m, p2m),
                ThreePointArc(p2m, front, p2p),
                Line(p2p, p1p),
            ]
        )
    except Exception:  # noqa: BLE001
        return None
    if face is None or not face.is_valid:
        return None
    # Self-check against the closed form. Decompose by the two tangency
    # chords (P+ to P- on each circle): a circular segment of the small
    # circle (arc angle pi - 2a), the trapezoid between the chords, and a
    # segment of the big circle (arc angle pi + 2a, more than half).
    # Segment area is r^2/2 * (theta - sin theta).
    alpha = math.asin(sin_a)
    sin_2a = math.sin(2 * alpha)
    exact = (
        0.5 * ra * ra * (math.pi - 2 * alpha - sin_2a)
        + 0.5 * rb * rb * (math.pi + 2 * alpha + sin_2a)
        + (ra + rb) * d * cos_a**3
    )
    if not math.isclose(face.area, exact, rel_tol=1e-6):
        return None
    return face


def analytic_hull(shapes: list[Shape]) -> Shape | None:
    """Try to evaluate hull() analytically; None means fall back to a mesh."""
    if not shapes:
        return None
    if len(shapes) == 1:
        return shapes[0]
    result = _hull_of_spheres(shapes)
    if result is not None:
        return result
    result = _hull_of_cylinders(shapes)
    if result is not None:
        return result
    result = _hull_of_two_circles(shapes)
    if result is not None:
        return result
    return _hull_of_polyhedra(shapes)
