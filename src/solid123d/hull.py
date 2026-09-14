"""Analytic hull(): every case with a closed-form BRep answer.

- N equal-radius spheres: exactly offset(convex_hull_of_centers, r);
  verified against the Steiner formula to ~1e-10. A fully collinear set
  (the 2-post capsule/slot idiom) is built directly as a capsule.
- N equal-radius parallel cylinders sharing one axial span: the same
  reduction one dimension down -- 2D hull of the projected axes, offset by
  the radius, extruded ("rounded box from corner posts").
- Exactly two spheres of ANY radii: two spherical caps sewn to the external
  tangent cone (see _two_sphere_hull for the mathematics). Overlap and
  containment included.
- Exactly two 2D discs: the same tangent construction one dimension down,
  built directly as a wire (keyhole/slot idiom; equal radii = stadium).
- All-polyhedral children: exactly the convex hull of their combined
  vertices, via build123d's ConvexPolyhedron. Covers cubes, polyhedron()s,
  extruded polygons, and matrix-transformed anything-planar.
- Identical revolution solids (cylinder/cone/sphere/torus faces about
  one shared axis, pointing anywhere) repeated by translation
  perpendicular to that axis -- the ``hull() cornercopy(...)`` idiom:
  hull(identical translates of X) == conv(centers) (+) conv(X), realized
  over the normal fan of the centers polygon. Line-only profiles use a
  ruled loft; profiles with arcs get sewn sphere/torus corner bands and
  horizontal-cylinder side bands. Covers tapered pads, stacked bevels,
  filleted cavity posts (roundedCylinder/roundedDisk idioms), and turned
  legs. See _hull_of_revolved_translates.

``analytic_hull`` returns None for anything else -- notably three or more
non-collinear spheres of unequal radii, whose hull needs tritangent planes
with power-diagram combinatorics, and any curved child outside the idioms
above (a curved surface's hull is not determined by its vertices). The
public ``hull()`` applier raises NotImplementedError on None; scad123d
instead falls back to rendering that subtree as a mesh via OpenSCAD.

Classification works on already-built Shapes rather than call arguments:
real code (BOSL2 especially) wraps primitives in layers of module-call and
attachment bookkeeping, and by classifying results those layers have
already been resolved.
"""

import math
from itertools import pairwise

import numpy as np
from build123d import (
    Align,
    Circle,
    Compound,
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
    loft as _bd_loft,
)
from build123d import (
    offset as _bd_offset,
)
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import (
    GeomAbs_Cone,
    GeomAbs_Cylinder,
    GeomAbs_Sphere,
    GeomAbs_Torus,
)
from scipy.spatial import ConvexHull

from .primitives import polyhedron

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
    # further offset lands exactly on the shared span's start. The direction
    # is passed explicitly: a0 is whichever cap OCCT listed first, and the
    # placed face's own normal does not reliably follow the plane's z_dir,
    # so extruding "along the face" from a top cap goes the wrong way.
    placed = plane * face2d
    try:
        result = extrude(placed, amount=span[1] - span[0], dir=Vector(*dir0))
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


def _hull_of_polygons(shapes: list[Shape]) -> Face | None:
    """Rung 3 one dimension down: hull() of children that are all
    straight-edged faces in the XY plane is exactly the 2D convex hull of
    their combined vertices -- same vertex argument as _hull_of_polyhedra.

    Like the two-circle rung, this matters beyond exactness: OpenSCAD
    cannot render a 2D subtree to a mesh, so a 2D hull that reaches the
    fallback hard-fails ("Current top level object is not a 3D object")
    rather than degrading. Declines on any curved edge, and on vertices
    off the XY plane (a 2D shape should never have them, but a hull built
    from bad input would be silently wrong rather than loudly absent).
    """
    for s in shapes:
        if s.solids():
            return None
        faces = s.faces()
        if not faces:
            return None
        if any(e.geom_type != GeomType.LINE for f in faces for e in f.edges()):
            return None
        if any(abs(v.Z) > 1e-9 for v in s.vertices()):
            return None
    points = [(v.X, v.Y) for s in shapes for v in s.vertices()]
    try:
        hull = ConvexHull(np.asarray(points))
    except Exception:  # noqa: BLE001
        return None
    # qhull returns 2D hull vertices already in counterclockwise order.
    ordered = [tuple(float(x) for x in points[i]) for i in hull.vertices]
    try:
        result = Polygon(*ordered, align=None)
    except Exception:  # noqa: BLE001
        return None
    return result if result.is_valid else None


# --- phase B: profiles with arcs, arbitrary shared axis ------------------
#
# The theory: hull(identical translates of X) == conv(centers) (+) conv(X)
# (support functions add under Minkowski sum, max under union). For X a
# solid of revolution, conv(X) is determined by the upper convex envelope
# of its (z, r) profile -- computed below as a literal support-function
# sweep -- and the sum's boundary decomposes over the normal fan of the
# centers polygon: each envelope piece extrudes along every polygon edge
# and revolves around every polygon vertex. Lines become planes/cones,
# arcs become horizontal cylinders and sphere/torus bands; all native OCCT
# surfaces, sewn (not fused -- every junction is tangent contact, where
# OCCT booleans are least reliable) into one solid.

# ("pt", (z, r)) or ("circle", (zc, rc), rho, key) in the (z, r) plane.
_ProfileElement = tuple

_ANG_EPS = 1e-12


def _psi_in_spans(psi: float, spans: list[tuple[float, float]]) -> bool:
    two_pi = 2 * math.pi
    for a, b in spans:
        if b - a >= two_pi - 1e-6:
            return True
        rel = (psi - a) % two_pi
        if rel <= (b - a) % two_pi + 1e-9:
            return True
    return False


def _elem_support(
    elem: _ProfileElement, psi: float, trims: dict
) -> tuple[float, Point2]:
    """Support value and point of an element in direction (cos psi, sin
    psi). A trimmed circle only supports directions inside its trimmed
    spans -- outside them its true support is an endpoint, and those
    endpoints are separate point elements -- so it drops out (-inf) rather
    than lending boundary the child does not have.
    """
    u = (math.cos(psi), math.sin(psi))
    if elem[0] == "pt":
        p = elem[1]
        return p[0] * u[0] + p[1] * u[1], p
    _, c, rho, key = elem
    spans = trims.get(key)
    if spans is not None and not _psi_in_spans(psi, spans):
        return -math.inf, c
    return (
        c[0] * u[0] + c[1] * u[1] + rho,
        (c[0] + rho * u[0], c[1] + rho * u[1]),
    )


def _switch_angles(e1: _ProfileElement, e2: _ProfileElement) -> list[float]:
    """psi in (0, pi) where the two elements' support values tie."""
    c1, r1 = e1[1], (0.0 if e1[0] == "pt" else e1[2])
    c2, r2 = e2[1], (0.0 if e2[0] == "pt" else e2[2])
    a, b, k = c1[0] - c2[0], c1[1] - c2[1], r2 - r1
    m = math.hypot(a, b)
    if m < 1e-12 or abs(k) > m:
        return []
    base = math.atan2(b, a)
    off = math.acos(max(-1.0, min(1.0, k / m)))
    out = []
    for cand in ((base + off) % (2 * math.pi), (base - off) % (2 * math.pi)):
        if _ANG_EPS < cand < math.pi - _ANG_EPS:
            out.append(cand)
    return out


def _upper_envelope(
    elements: list[_ProfileElement], trims: dict | None = None
) -> list[tuple]:
    """Upper convex envelope of points and circles in the (z, r) plane, as
    an ordered curve chain left (min z) to right (max z). Pieces:
    ("line", p1, p2) and ("arc", center, rho, t_hi, t_lo, key) -- arc
    parameter t is the direction angle, point = c + rho*(cos t, sin t) in
    (z, r); t decreases along the chain. This sweep over support
    directions IS the 1D normal-fan computation: between consecutive
    switch angles the supporting element is constant, and each interval
    contributes that element's boundary. Trim-span endpoints join the
    switch-angle candidates because the support function kinks there.
    """
    trims = trims or {}
    angles = {_ANG_EPS, math.pi - _ANG_EPS}
    n = len(elements)
    for i in range(n):
        for j in range(i + 1, n):
            angles.update(_switch_angles(elements[i], elements[j]))
    for spans in trims.values():
        for span in spans:
            for a in span:
                a = a % (2 * math.pi)
                if _ANG_EPS < a < math.pi - _ANG_EPS:
                    angles.add(a)
    # cluster near-identical candidates: a trim endpoint that lands exactly
    # on a tangency otherwise splits one arc into two co-surface pieces
    ordered: list[float] = []
    for a in sorted(angles, reverse=True):
        if not ordered or ordered[-1] - a > 1e-9:
            ordered.append(a)

    intervals: list[tuple[int, float, float]] = []
    for hi, lo in pairwise(ordered):
        mid = (hi + lo) / 2
        best = max(range(n), key=lambda i: _elem_support(elements[i], mid, trims)[0])
        if intervals and intervals[-1][0] == best:
            intervals[-1] = (best, intervals[-1][1], lo)
        else:
            intervals.append((best, hi, lo))

    chain: list[tuple] = []
    prev_end: Point2 | None = None
    for idx, hi, lo in intervals:
        elem = elements[idx]
        if elem[0] == "pt":
            p = elem[1]
            if prev_end is not None and math.dist(prev_end, p) > 1e-6:
                chain.append(("line", prev_end, p))
            prev_end = p
        else:
            _, c, rho, key = elem
            start = (c[0] + rho * math.cos(hi), c[1] + rho * math.sin(hi))
            end = (c[0] + rho * math.cos(lo), c[1] + rho * math.sin(lo))
            if prev_end is not None and math.dist(prev_end, start) > 1e-6:
                chain.append(("line", prev_end, start))
            if (
                chain
                and chain[-1][0] == "arc"
                and chain[-1][5] == key
                and abs(chain[-1][4] - hi) < 1e-9
            ):
                prev = chain.pop()
                chain.append(("arc", c, rho, prev[3], lo, key))
            else:
                chain.append(("arc", c, rho, hi, lo, key))
            prev_end = end
    return chain


def _centers_hull2d(centers: list[Point2]) -> list[Point2]:
    """CCW extreme points of the centers; 1 or 2 points for the degenerate
    coincident/collinear layouts (which _rounded_section builds directly)."""
    span, a, b = _pairwise_extremes([(c[0], c[1], 0.0) for c in centers])
    if span < 1e-9:
        return [centers[0]]
    if _is_collinear([(c[0], c[1], 0.0) for c in centers], a, b, span):
        return [(a[0], a[1]), (b[0], b[1])]
    hull = ConvexHull(np.asarray(centers))
    return [tuple(float(x) for x in hull.points[i]) for i in hull.vertices]


def _rounded_section(hull_pts: list[Point2], r: float, z: float) -> Shape:
    """The rounded polygon (2D hull of hull_pts offset by r) as a Face at
    height z, built as one deterministic wire so every section of a loft
    has identical edge structure and orientation.
    """
    if len(hull_pts) == 1:
        c = hull_pts[0]
        return Pos(c[0], c[1], z) * Circle(r)
    if len(hull_pts) == 2:
        return Pos(0, 0, z) * _stadium2d(hull_pts[0], hull_pts[1], r)

    k = len(hull_pts)
    normals: list[Point2] = []
    for i in range(k):
        ax, ay = hull_pts[i]
        bx, by = hull_pts[(i + 1) % k]
        d = math.dist((ax, ay), (bx, by))
        # CCW polygon: outward normal of edge (dx, dy) is (dy, -dx)
        normals.append(((by - ay) / d, -(bx - ax) / d))
    edges = []
    for i in range(k):
        ax, ay = hull_pts[i]
        bx, by = hull_pts[(i + 1) % k]
        nx, ny = normals[i]
        edges.append(
            Edge.make_line((ax + r * nx, ay + r * ny), (bx + r * nx, by + r * ny))
        )
        # corner arc at b, tangent to the incoming offset edge
        ux, uy = (bx - ax), (by - ay)
        d = math.hypot(ux, uy)
        n2x, n2y = normals[(i + 1) % k]
        edges.append(
            Edge.make_tangent_arc(
                (bx + r * nx, by + r * ny),
                (ux / d, uy / d, 0),
                (bx + r * n2x, by + r * n2y),
            )
        )
    return Pos(0, 0, z) * Face(Wire(edges))


def _profile_elements(
    shape: Shape,
) -> tuple[Point2, list[_ProfileElement], dict] | None:
    """(center_xy, envelope elements, arc trims) for a vertical-axis
    revolution child. Vertices contribute points (they cover every
    silhouette extreme of line-profile faces); sphere and torus faces
    contribute circles in the (z, r) plane, with their trimmed angular
    spans recorded so the envelope can verify it only used boundary that
    actually exists on the child.
    """
    if not shape.solids():
        return None
    axis_xy: Point2 | None = None
    circles: list[tuple] = []  # (zc, rc, rho, t_span or None, sphere_xy)
    for f in shape.faces():
        if f.geom_type == GeomType.PLANE:
            n = f.normal_at()
            if abs(abs(n.Z) - 1.0) > _PARALLEL_TOL:
                return None
            continue
        surf = BRepAdaptor_Surface(f.wrapped)
        kind = surf.GetType()
        if kind == GeomAbs_Sphere:
            sph = surf.Sphere()
            loc = sph.Location()
            xy = (loc.X(), loc.Y())
            # profile angle t = pi/2 - s*latitude, s the sign of the
            # surface's own axis: a rotation that lands the shared axis on
            # -Z flips the V convention with it.
            s_ax = sph.Position().Direction().Z()
            if abs(abs(s_ax) - 1.0) > _PARALLEL_TOL:
                return None
            v1, v2 = surf.FirstVParameter(), surf.LastVParameter()
            if s_ax > 0:
                t_span = (math.pi / 2 - v2, math.pi / 2 - v1)
            else:
                t_span = (math.pi / 2 + v1, math.pi / 2 + v2)
            circles.append((loc.Z(), 0.0, sph.Radius(), t_span, xy))
        elif kind == GeomAbs_Torus:
            tor = surf.Torus()
            ax = tor.Axis()
            if abs(abs(ax.Direction().Z()) - 1.0) > _PARALLEL_TOL:
                return None
            if tor.MinorRadius() > tor.MajorRadius() - 1e-9:
                return None  # spindle/degenerate torus
            loc = ax.Location()
            xy = (loc.X(), loc.Y())
            # t = pi/2 - s*v (OCCT: r = R + rho cos v, z = zc + s*rho sin v)
            s_ax = ax.Direction().Z()
            v1, v2 = surf.FirstVParameter(), surf.LastVParameter()
            if s_ax > 0:
                t_span = (math.pi / 2 - v2, math.pi / 2 - v1)
            else:
                t_span = (math.pi / 2 + v1, math.pi / 2 + v2)
            circles.append((loc.Z(), tor.MajorRadius(), tor.MinorRadius(), t_span, xy))
        elif kind == GeomAbs_Cylinder:
            ax = surf.Cylinder().Axis()
            if abs(abs(ax.Direction().Z()) - 1.0) > _PARALLEL_TOL:
                return None
            loc = ax.Location()
            xy = (loc.X(), loc.Y())
        elif kind == GeomAbs_Cone:
            ax = surf.Cone().Axis()
            if abs(abs(ax.Direction().Z()) - 1.0) > _PARALLEL_TOL:
                return None
            loc = ax.Location()
            xy = (loc.X(), loc.Y())
        else:
            return None
        if axis_xy is None:
            axis_xy = xy
        elif math.hypot(xy[0] - axis_xy[0], xy[1] - axis_xy[1]) > 1e-6:
            return None
    if axis_xy is None:
        return None

    elements: list[_ProfileElement] = [
        ("pt", (v.Z, math.hypot(v.X - axis_xy[0], v.Y - axis_xy[1])))
        for v in shape.vertices()
    ]
    trims: dict[tuple, list[tuple[float, float]]] = {}
    seen_keys: set[tuple] = set()
    for zc, rc, rho, t_span, _xy in circles:
        key = (round(zc, 6), round(rc, 6), round(rho, 6))
        if key not in seen_keys:
            seen_keys.add(key)
            elements.append(("circle", (zc, rc), rho, key))
        trims.setdefault(key, []).append(t_span)
        # span endpoints are the support outside the trim; give the sweep
        # those points explicitly (vertices usually coincide, but faces
        # split by booleans need not carry a vertex at every rim)
        for a in t_span:
            elements.append(("pt", (zc + rho * math.cos(a), rc + rho * math.sin(a))))
    return axis_xy, elements, trims


def _span_covered(lo: float, hi: float, spans: list[tuple[float, float]]) -> bool:
    """Is the angular interval [lo, hi] covered by the union of spans
    (each possibly wrapping), to 1e-6? Envelope arcs live in (0, pi)."""
    two_pi = 2 * math.pi
    segs: list[tuple[float, float]] = []
    for a, b in spans:
        if b - a >= two_pi - 1e-6:  # a full circle: covers anything
            return True
        a, b = a % two_pi, b % two_pi
        if b < a - 1e-12:
            segs += [(a, two_pi), (0.0, b)]
        else:
            segs.append((a, b))
    need = [(lo + 1e-6, hi - 1e-6)] if hi - lo > 2e-6 else []
    for a, b in sorted(segs):
        need = [
            piece
            for nlo, nhi in need
            for piece in ((nlo, min(nhi, a)), (max(nlo, b), nhi))
            if piece[1] - piece[0] > 1e-9
        ]
    return not need


def _chain_endpoints(chain: list[tuple]) -> tuple[Point2, Point2]:
    def pt(piece: tuple, end: bool) -> Point2:
        if piece[0] == "line":
            return piece[2] if end else piece[1]
        _, c, rho, hi, lo = piece[:5]
        t = lo if end else hi
        return (c[0] + rho * math.cos(t), c[1] + rho * math.sin(t))

    return pt(chain[0], False), pt(chain[-1], True)


def _chain_integrals(chain: list[tuple]) -> tuple[float, float, float]:
    """(dz, integral r dz, integral r^2 dz) over the whole chain --
    everything the Steiner cross-section volume formula needs, exactly."""
    dz_total = int_r = int_r2 = 0.0
    for piece in chain:
        if piece[0] == "line":
            (z1, r1), (z2, r2) = piece[1], piece[2]
            dz = z2 - z1
            dz_total += dz
            int_r += dz * (r1 + r2) / 2
            int_r2 += dz * (r1 * r1 + r1 * r2 + r2 * r2) / 3
        else:
            _, (_zc, rc), rho, hi, lo = piece[:5]
            # z = zc + rho cos t, r = rc + rho sin t, t from hi down to lo
            dz_total += rho * (math.cos(lo) - math.cos(hi))
            int_r += _arc_f_r(rc, rho, hi) - _arc_f_r(rc, rho, lo)
            int_r2 += _arc_f_r2(rc, rho, hi) - _arc_f_r2(rc, rho, lo)
    return dz_total, int_r, int_r2


def _arc_f_r(rc: float, rho: float, t: float) -> float:
    """Antiderivative of r dz on an envelope arc, in the parameter t."""
    return -rc * rho * math.cos(t) + rho * rho * (t - math.sin(t) * math.cos(t)) / 2


def _arc_f_r2(rc: float, rho: float, t: float) -> float:
    """Antiderivative of r^2 dz on an envelope arc, in the parameter t."""
    return (
        -rc * rc * rho * math.cos(t)
        + rc * rho * rho * (t - math.sin(t) * math.cos(t))
        + rho**3 * (-math.cos(t) + math.cos(t) ** 3 / 3)
    )


def _polygon_frame(
    hull_pts: list[Point2],
) -> tuple[list[tuple[int, int]], list[float]]:
    """Directed edges and their outward-normal angles for the centers
    polygon (CCW); a 2-point "polygon" gets both directed edges so the
    generic vertex-wedge formula yields two 180-degree wedges, and a
    single point gets no edges (one 360-degree wedge)."""
    k = len(hull_pts)
    if k >= 3:
        edges_idx = [(i, (i + 1) % k) for i in range(k)]
    elif k == 2:
        edges_idx = [(0, 1), (1, 0)]
    else:
        edges_idx = []
    normals = []
    for i, j in edges_idx:
        dx = hull_pts[j][0] - hull_pts[i][0]
        dy = hull_pts[j][1] - hull_pts[i][1]
        normals.append(math.atan2(-dx, dy))  # outward normal of a CCW edge
    return edges_idx, normals


def _fan_solid(hull_pts: list[Point2], chain: list[tuple]) -> Shape | None:
    """Realize conv(centers) (+) conv(profile) face by face over the
    normal fan and sew: every envelope piece extrudes along every polygon
    edge (lines -> planes, arcs -> horizontal cylinders) and revolves
    through every polygon vertex wedge (lines -> cones/cylinders, arcs ->
    sphere/torus bands), plus flat caps. Sewn rather than fused: every
    junction is tangent contact, where OCCT booleans are least reliable.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Vec
    from OCP.ShapeFix import ShapeFix_Solid
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    k = len(hull_pts)
    edges_idx, normals = _polygon_frame(hull_pts)
    two_pi = 2 * math.pi

    def curve_edge(piece: tuple, cx: float, cy: float, theta: float) -> Edge:
        ct, st = math.cos(theta), math.sin(theta)

        def p3(zr: Point2) -> tuple[float, float, float]:
            return (cx + zr[1] * ct, cy + zr[1] * st, zr[0])

        if piece[0] == "line":
            return Edge.make_line(p3(piece[1]), p3(piece[2]))
        _, c, rho, hi, lo = piece[:5]

        def at(t: float) -> tuple[float, float, float]:
            return p3((c[0] + rho * math.cos(t), c[1] + rho * math.sin(t)))

        return Edge.make_three_point_arc(at(hi), at((hi + lo) / 2), at(lo))

    faces = []
    for piece in chain:
        for (i, j), na in zip(edges_idx, normals):
            cx, cy = hull_pts[i]
            edge = curve_edge(piece, cx, cy, na)
            vec = gp_Vec(hull_pts[j][0] - cx, hull_pts[j][1] - cy, 0)
            faces.append(BRepPrimAPI_MakePrism(edge.wrapped, vec).Shape())
        for i in range(k):
            cx, cy = hull_pts[i]
            if k >= 2:
                a_in = normals[(i - 1) % len(edges_idx)]
                a_out = normals[i % len(edges_idx)] if k >= 3 else a_in + math.pi
                sweep = (a_out - a_in) % two_pi
            else:
                a_in, sweep = 0.0, two_pi
            edge = curve_edge(piece, cx, cy, a_in)
            axis = gp_Ax1(gp_Pnt(cx, cy, 0), gp_Dir(0, 0, 1))
            faces.append(BRepPrimAPI_MakeRevol(edge.wrapped, axis, sweep).Shape())

    (z0, r0), (z1, r1) = _chain_endpoints(chain)
    for z, r in ((z0, r0), (z1, r1)):
        if r > 1e-7:
            faces.append(_rounded_section(hull_pts, r, z).wrapped)
        elif k >= 3:
            pts3 = [(x, y, z) for x, y in hull_pts]
            wire = Wire([Edge.make_line(pts3[i], pts3[(i + 1) % k]) for i in range(k)])
            faces.append(Face(wire).wrapped)

    sew = BRepBuilderAPI_Sewing(1e-5)
    for f in faces:
        sew.Add(f)
    sew.Perform()
    exp = TopExp_Explorer(sew.SewedShape(), TopAbs_SHELL)
    if not exp.More():
        return None
    solid = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(exp.Current())).Solid()
    fix = ShapeFix_Solid(solid)
    fix.Perform()
    return Compound.cast(fix.Solid())


def _loft_solid(hull_pts: list[Point2], chain: list[tuple]) -> Shape | None:
    """The proven phase-A construction for all-line profiles: ruled loft
    of rounded-polygon sections at the envelope breakpoints."""
    breakpoints: list[Point2] = [chain[0][1]]
    for piece in chain:
        breakpoints.append(piece[2])
    if any(r < 1e-9 for _, r in breakpoints):
        return None
    sections = [_rounded_section(hull_pts, r, z) for z, r in breakpoints]
    return _bd_loft(sections, ruled=True)


def _chains_match(a: list[tuple], b: list[tuple]) -> bool:
    if len(a) != len(b):
        return False
    for pa, pb in zip(a, b):
        if pa[0] != pb[0]:
            return False
        va = [
            x
            for part in pa[1:5]
            for x in (part if isinstance(part, tuple) else (part,))
            if isinstance(x, float)
        ]
        vb = [
            x
            for part in pb[1:5]
            for x in (part if isinstance(part, tuple) else (part,))
            if isinstance(x, float)
        ]
        if len(va) != len(vb) or not np.allclose(va, vb, rtol=1e-6, atol=1e-7):
            return False
    return True


def _hull_vertical_translates(shapes: list[Shape]) -> Shape | None:
    classified = [_profile_elements(s) for s in shapes]
    if any(c is None for c in classified):
        return None

    groups: dict[tuple[float, float], tuple[Point2, list, dict]] = {}
    for center, elements, trims in classified:  # type: ignore[misc]
        key = (round(center[0], 6), round(center[1], 6))
        if key in groups:
            groups[key][1].extend(elements)
            for tk, spans in trims.items():
                groups[key][2].setdefault(tk, []).extend(spans)
        else:
            groups[key] = (center, list(elements), dict(trims))

    chains = []
    for _, elements, trims in groups.values():
        chain = _upper_envelope(elements, trims)
        # every arc the envelope used must lie on boundary the child has
        for piece in chain:
            if piece[0] == "arc" and not _span_covered(
                piece[4], piece[3], trims.get(piece[5], [])
            ):
                return None
        chains.append(chain)
    chain = chains[0]
    for other in chains[1:]:
        if not _chains_match(chain, other):
            return None
    if not chain:
        return None
    (z0, _r0), (z1, _r1) = _chain_endpoints(chain)
    if z1 - z0 < 1e-9:
        return None

    centers = [center for center, _, _ in groups.values()]
    try:
        hull_pts = _centers_hull2d(centers)
        if all(p[0] == "line" for p in chain):
            solid = _loft_solid(hull_pts, chain)
        else:
            solid = _fan_solid(hull_pts, chain)
    except Exception:  # noqa: BLE001
        return None
    if solid is None or not solid.is_valid:
        return None

    # Exact volume: integral of the Steiner cross-section area
    # A0 + P0*r(z) + pi*r(z)^2 over the envelope, arcs included.
    if len(hull_pts) < 3:
        area = 0.0
        perimeter = 2 * math.dist(hull_pts[0], hull_pts[-1])
    else:
        k = len(hull_pts)
        area = 0.5 * abs(
            sum(
                hull_pts[i][0] * hull_pts[(i + 1) % k][1]
                - hull_pts[(i + 1) % k][0] * hull_pts[i][1]
                for i in range(k)
            )
        )
        perimeter = sum(math.dist(hull_pts[i], hull_pts[(i + 1) % k]) for i in range(k))
    dz, int_r, int_r2 = _chain_integrals(chain)
    exact = area * dz + perimeter * int_r + math.pi * int_r2
    if not math.isclose(solid.volume, exact, rel_tol=1e-6):
        return None
    return solid


def _hull_of_revolved_translates(shapes: list[Shape]) -> Shape | None:
    """hull() of one revolution profile repeated by translation
    perpendicular to a shared axis -- the ``hull() cornercopy(...)`` idiom
    (tapered pads, stacked bevels, filleted cavity posts, turned legs).

    hull(identical translates of X) == conv(centers) (+) conv(X); see the
    phase-B block comment above for how the normal fan realizes it. The
    axis may point anywhere as long as every child shares it: the problem
    is conjugated to vertical (rotate, solve, rotate back), which is all
    the generality a common axis needs. Line-only profiles keep the
    proven ruled-loft construction; profiles with arcs (sphere/torus
    faces) go through the sewn fan construction. Every result is
    self-checked against the closed-form Steiner volume integral and
    declines on mismatch.
    """
    from build123d import Axis as _Axis

    dirs = []
    for s in shapes:
        d = _child_axis_direction(s)
        if d is False:
            return None
        if d is not None:
            dirs.append(d)
    if dirs:
        d0 = dirs[0]
        for d in dirs[1:]:
            if abs(abs(d0[0] * d[0] + d0[1] * d[1] + d0[2] * d[2]) - 1.0) > 1e-9:
                return None
    else:
        d0 = (0.0, 0.0, 1.0)

    if abs(abs(d0[2]) - 1.0) < 1e-12:
        return _hull_vertical_translates(shapes)

    # conjugate: rotate the shared axis onto +Z, solve, rotate back
    ax = (d0[1], -d0[0], 0.0)  # d0 x Z: rotating about it takes d0 to +Z
    norm = math.hypot(ax[0], ax[1])
    axis = _Axis((0, 0, 0), (ax[0] / norm, ax[1] / norm, 0.0))
    angle = math.degrees(math.acos(max(-1.0, min(1.0, d0[2]))))
    rotated = [s.rotate(axis, angle) for s in shapes]
    result = _hull_vertical_translates(rotated)
    if result is None:
        return None
    return result.rotate(axis, -angle)


def _child_axis_direction(shape: Shape):
    """The unit axis direction shared by a child's cylinder/cone/torus
    faces: a tuple; None if nothing constrains it (spheres and planes
    only); False if faces disagree or an unsupported surface appears."""
    if not shape.solids():
        return False
    direction = None
    for f in shape.faces():
        if f.geom_type == GeomType.PLANE:
            continue
        surf = BRepAdaptor_Surface(f.wrapped)
        kind = surf.GetType()
        if kind == GeomAbs_Sphere:
            continue
        if kind == GeomAbs_Cylinder:
            d = surf.Cylinder().Axis().Direction()
        elif kind == GeomAbs_Cone:
            d = surf.Cone().Axis().Direction()
        elif kind == GeomAbs_Torus:
            d = surf.Torus().Axis().Direction()
        else:
            return False
        v = (d.X(), d.Y(), d.Z())
        if direction is None:
            direction = v
        elif (
            abs(
                abs(direction[0] * v[0] + direction[1] * v[1] + direction[2] * v[2])
                - 1.0
            )
            > 1e-9
        ):
            return False
    return direction


def _component_shapes(shapes: list[Shape]) -> list[Shape]:
    """Explode each input into its independent component solids (or faces,
    for 2D input) before classification.

    A hull is decomposition-invariant -- hull(A union B) == hull(A, B) -- so
    splitting inputs never changes the answer, and it is what lets real
    call patterns reach the rungs at all: OpenSCAD wraps a module-call body
    in group(), so ``hull() corner_posts();`` arrives as ONE pre-fused
    compound of four cylinders, not four cylinder children. Found the hard
    way: a Gridfinity cup silently lost the rounded-box hull this way.
    """
    out: list[Shape] = []
    for s in shapes:
        solids = s.solids()
        if len(solids) > 1:
            out.extend(solids)
        elif solids:
            out.append(s)
        else:
            faces = s.faces()
            out.extend(faces if len(faces) > 1 else [s])
    return out


def analytic_hull(shapes: list[Shape]) -> Shape | None:
    """Try to evaluate hull() analytically; None means no closed form here.

    There is deliberately no single-child identity shortcut: hull(X) == X
    only when X is convex, which is unknowable cheaply -- and a single
    child is often a pre-fused group of many parts (see
    _component_shapes). Single convex primitives still come back exact
    through their rungs; anything unclassifiable returns None rather than
    risking a silently-wrong passthrough.
    """
    if not shapes:
        return None
    shapes = _component_shapes(shapes)
    result = _hull_of_spheres(shapes)
    if result is not None:
        return result
    result = _hull_of_cylinders(shapes)
    if result is not None:
        return result
    result = _hull_of_revolved_translates(shapes)
    if result is not None:
        return result
    result = _hull_of_two_circles(shapes)
    if result is not None:
        return result
    result = _hull_of_polygons(shapes)
    if result is not None:
        return result
    return _hull_of_polyhedra(shapes)
