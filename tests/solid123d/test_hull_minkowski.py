"""Analytic hull() and minkowski(): every case with a closed-form BRep
answer evaluates exactly; everything else raises NotImplementedError.

The geometry engines are shared with scad123d (which grew them first,
verified differentially against OpenSCAD's own output there); these tests
pin the solid123d-facing behavior and the closed forms.
"""

import math
from collections import Counter

import pytest
from build123d import GeomType

import solid123d as s


def _steiner(
    volume: float, area: float, edges: list[tuple[float, float]], r: float
) -> float:
    return (
        volume
        + area * r
        + r * r / 2 * sum(length * angle for length, angle in edges)
        + (4 / 3) * math.pi * r**3
    )


def _pair_hull_volume(ra: float, rb: float, d: float) -> float:
    sin_a = (rb - ra) / d
    cos_a = math.sqrt(1 - sin_a**2)
    h1, h2 = ra * (1 - sin_a), rb * (1 + sin_a)
    rho1, rho2 = ra * cos_a, rb * cos_a
    length = d * cos_a**2
    return (
        math.pi * h1 * h1 * (3 * ra - h1) / 3
        + math.pi * length / 3 * (rho1**2 + rho1 * rho2 + rho2**2)
        + math.pi * h2 * h2 * (3 * rb - h2) / 3
    )


class TestHullEqualSpheres:
    def test_two_spheres_make_a_capsule(self):
        shape = s.hull()(s.sphere(3), s.translate([10, 0, 0])(s.sphere(3)))
        assert shape.volume == pytest.approx(
            math.pi * 9 * 10 + (4 / 3) * math.pi * 27, rel=1e-9
        )

    def test_eight_corner_spheres_match_steiner(self):
        a, b, c, r = 20.0, 15.0, 10.0, 3.0
        corners = [
            s.translate([x, y, z])(s.sphere(r))
            for x in (0, a)
            for y in (0, b)
            for z in (0, c)
        ]
        shape = s.hull()(*corners)
        edges = [(a, math.pi / 2)] * 4 + [(b, math.pi / 2)] * 4 + [(c, math.pi / 2)] * 4
        expected = _steiner(a * b * c, 2 * (a * b + b * c + c * a), edges, r)
        assert shape.volume == pytest.approx(expected, rel=1e-9)


class TestHullSpherePair:
    def test_unequal_pair_matches_the_closed_form(self):
        shape = s.hull()(s.sphere(3), s.translate([14, 0, 0])(s.sphere(5)))
        assert shape.volume == pytest.approx(_pair_hull_volume(3, 5, 14), rel=1e-9)
        kinds = {f.geom_type for f in shape.faces()}
        assert kinds == {GeomType.SPHERE, GeomType.CONE}

    def test_contained_sphere_yields_the_big_sphere(self):
        shape = s.hull()(s.sphere(1), s.translate([2, 0, 0])(s.sphere(5)))
        assert shape.volume == pytest.approx((4 / 3) * math.pi * 125, rel=1e-9)

    def test_overlapping_pair_still_exact(self):
        shape = s.hull()(s.sphere(3), s.translate([5, 0, 0])(s.sphere(5)))
        assert shape.volume == pytest.approx(_pair_hull_volume(3, 5, 5), rel=1e-9)

    def test_three_unequal_spheres_raise(self):
        with pytest.raises(NotImplementedError, match="tritangent"):
            s.hull()(
                s.sphere(3),
                s.translate([14, 0, 0])(s.sphere(5)),
                s.translate([7, 12, 0])(s.sphere(4)),
            )


class TestHullCylinders:
    def test_four_corner_posts_make_a_rounded_box(self):
        r, h, side = 2.0, 10.0, 20.0
        posts = [
            s.translate([x, y, 0])(s.cylinder(r=r, h=h))
            for x in (0, side)
            for y in (0, side)
        ]
        shape = s.hull()(*posts)
        expected = (side * side + 4 * r * side + math.pi * r * r) * h
        assert shape.volume == pytest.approx(expected, rel=1e-9)


def _slab(
    area: float, perim: float, z1: float, r1: float, z2: float, r2: float
) -> float:
    """integral of the 2D Steiner area A0 + P0*r(z) + pi*r(z)^2 over one
    linear envelope segment -- the exact volume of a rounded-polygon
    frustum slab. Derived independently of the implementation's own check
    (same mathematics, but the test would catch a sign/term slip in either).
    """
    dz = z2 - z1
    return dz * (
        area + perim * (r1 + r2) / 2 + math.pi * (r1 * r1 + r1 * r2 + r2 * r2) / 3
    )


class TestHullRevolvedTranslates:
    """hull() of one vertical revolution profile repeated by XY translation:
    conv(centers) (+) conv(child), the ``hull() cornercopy(...)`` idiom."""

    def test_four_cones_make_a_tapered_rounded_box(self):
        shape = s.hull()(
            *[
                s.translate([x, y, 0])(s.cylinder(r1=2, r2=5, h=3))
                for x in (-10, 10)
                for y in (-10, 10)
            ]
        )
        assert shape.volume == pytest.approx(_slab(400, 80, 0, 2, 3, 5), rel=1e-6)
        from collections import Counter

        kinds = Counter(f.geom_type for f in shape.faces())
        assert kinds == {GeomType.PLANE: 6, GeomType.CONE: 4}

    def test_gridfinity_stepped_bevel(self):
        """The pad_oversize bottom bevel: two disjoint coaxial cylinders per
        corner; the envelope must bridge the concave step (the short fat
        cylinder's top rim at (0.1, 0.8) falls inside the hull)."""

        def leg():
            return s.union()(
                s.cylinder(r=0.8, h=0.1),
                s.translate([0, 0, 0.8])(s.cylinder(r=1.6, h=4.5)),
            )

        shape = s.hull()(
            *[s.translate([x, y, 0])(leg()) for x in (-17, 17) for y in (-17, 17)]
        )
        expected = _slab(1156, 136, 0, 0.8, 0.8, 1.6) + _slab(
            1156, 136, 0.8, 1.6, 5.3, 1.6
        )
        assert shape.volume == pytest.approx(expected, rel=1e-6)

    def test_two_centers_loft_stadium_sections(self):
        shape = s.hull()(
            s.cylinder(r1=2, r2=5, h=3),
            s.translate([12, 0, 0])(s.cylinder(r1=2, r2=5, h=3)),
        )
        assert shape.volume == pytest.approx(_slab(0, 24, 0, 2, 3, 5), rel=1e-6)

    def test_single_stack_is_its_own_convex_hull(self):
        shape = s.hull()(
            s.union()(
                s.cylinder(r=3, h=1),
                s.translate([0, 0, 4])(s.cylinder(r=1, h=1)),
            )
        )
        expected = _slab(0, 0, 0, 3, 1, 3) + _slab(0, 0, 1, 3, 5, 1)
        assert shape.volume == pytest.approx(expected, rel=1e-6)

    def test_four_turned_table_legs(self):
        """rotate_extrude legs with a concave waist: envelope keeps the
        (1, 2.5) shoulder and bridges straight to the (30, 2.0) top."""
        profile = s.polygon(
            points=[[0, 0], [2.5, 0], [2.5, 1], [1.2, 2], [1.2, 28], [2.0, 30], [0, 30]]
        )
        leg = s.rotate_extrude()(profile)
        shape = s.hull()(
            *[s.translate([x, y, 0])(leg) for x in (0, 40) for y in (0, 40)]
        )
        expected = _slab(1600, 160, 0, 2.5, 1, 2.5) + _slab(1600, 160, 1, 2.5, 30, 2.0)
        assert shape.volume == pytest.approx(expected, rel=1e-6)

    def test_tilted_shared_axis_now_succeeds(self):
        """Phase B: a shared non-vertical axis conjugates to vertical.
        (This declined in phase A.) Translation is perpendicular to the
        tilt axis (X), so profiles stay identical across centers."""
        leg = s.rotate([30, 0, 0])(s.cylinder(r1=2, r2=4, h=3))
        shape = s.hull()(leg, s.translate([10, 0, 0])(leg))
        assert shape.volume == pytest.approx(_slab(0, 20, 0, 2, 3, 4), rel=1e-6)

    def test_mixed_axes_decline(self):
        with pytest.raises(NotImplementedError):
            s.hull()(
                s.rotate([30, 0, 0])(s.cylinder(r1=2, r2=4, h=3)),
                s.translate([10, 0, 0])(s.cylinder(r1=2, r2=4, h=3)),
            )

    def test_mismatched_profiles_decline(self):
        with pytest.raises(NotImplementedError):
            s.hull()(
                s.cylinder(r1=2, r2=5, h=3),
                s.translate([12, 0, 0])(s.cylinder(r1=2, r2=4, h=3)),
            )

    def test_apex_cones_decline(self):
        """r2=0 puts the envelope at radius zero: an apex section, which
        the ruled loft doesn't model. Must decline cleanly, not crash."""
        cone = s.cylinder(r1=2, r2=0, h=3)
        with pytest.raises(NotImplementedError):
            s.hull()(cone, s.translate([10, 0, 0])(cone))


class TestHullArcProfiles:
    """Phase B: profiles with sphere/torus arcs, and any shared axis.
    Volumes check against independent closed forms: for the half-circle
    arc pieces here, integral(r)dz = rc*2rho + pi*rho^2/2 per quarter, and
    integral(r^2)dz uses integral(rho^2 - u^2)du = 4 rho^3 / 3."""

    def _posts_volume(self) -> float:
        # square 30x30 of posts: cylinder r=6 h=20 + hemisphere cap
        a0, p0 = 900.0, 120.0
        int_r = 6 * 20 + math.pi * 36 / 4
        int_r2 = 36 * 20 + (36 * 6 - 6**3 / 3)
        return a0 * 26 + p0 * int_r + math.pi * int_r2

    def test_sphere_capped_posts(self):
        def post():
            return s.union()(
                s.cylinder(r=6, h=20), s.translate([0, 0, 20])(s.sphere(6))
            )

        shape = s.hull()(
            *[s.translate([x, y, 0])(post()) for x in (-15, 15) for y in (-15, 15)]
        )
        assert shape.volume == pytest.approx(self._posts_volume(), rel=1e-6)
        kinds = Counter(f.geom_type for f in shape.faces())
        assert kinds[GeomType.SPHERE] == 4  # corner bands
        assert kinds[GeomType.CYLINDER] == 8  # 4 walls + 4 edge rounds

    def test_four_tori(self):
        torus = s.rotate_extrude()(s.translate([5, 0])(s.circle(2)))
        shape = s.hull()(
            *[s.translate([x, y, 0])(torus) for x in (-12, 12) for y in (-12, 12)]
        )
        a0, p0, rc, rho = 576.0, 96.0, 5.0, 2.0
        int_r = rc * 2 * rho + math.pi * rho * rho / 2
        int_r2 = rc * rc * 2 * rho + math.pi * rho * rho * rc + 4 * rho**3 / 3
        expected = a0 * 2 * rho + p0 * int_r + math.pi * int_r2
        assert shape.volume == pytest.approx(expected, rel=1e-6)
        kinds = Counter(f.geom_type for f in shape.faces())
        assert kinds[GeomType.TORUS] == 4

    def test_single_torus_is_a_filled_rounded_disk(self):
        torus = s.rotate_extrude()(s.translate([5, 0])(s.circle(2)))
        shape = s.hull()(torus)
        rc, rho = 5.0, 2.0
        int_r2 = rc * rc * 2 * rho + math.pi * rho * rho * rc + 4 * rho**3 / 3
        assert shape.volume == pytest.approx(math.pi * int_r2, rel=1e-6)

    def test_shared_tilted_axis_conjugates(self):
        """The axis may point anywhere as long as every child shares it:
        rotate-to-vertical, solve, rotate back must reproduce the
        vertical volume exactly."""

        def post():
            return s.union()(
                s.cylinder(r=6, h=20), s.translate([0, 0, 20])(s.sphere(6))
            )

        shape = s.hull()(
            *[
                s.rotate([90, 0, 30])(s.translate([x, y, 0])(post()))
                for x in (-15, 15)
                for y in (-15, 15)
            ]
        )
        assert shape.volume == pytest.approx(self._posts_volume(), rel=1e-6)

    def test_gridfinity_cavity_idiom(self):
        """The module_gridfinity_cup.scad:1341 pattern: hull() cornercopy
        of roundedCylinders (cylinder + torus floor fillet)."""

        def rounded_cyl():
            return s.union()(
                s.translate([0, 0, 2])(s.cylinder(r=8, h=18)),
                s.rotate_extrude()(s.translate([6, 2])(s.circle(2))),
                s.cylinder(r=6, h=2),
            )

        shape = s.hull()(
            *[
                s.translate([x, y, 0])(rounded_cyl())
                for x in (-17, 17)
                for y in (-17, 17)
            ]
        )
        a0, p0 = 34.0 * 34, 4 * 34.0
        # z in [0,2]: quarter-torus fillet rc=6 rho=2; z in [2,20]: wall r=8
        int_r = (6 * 2 + math.pi * 4 / 4) + 8 * 18
        int_r2 = (36 * 2 + math.pi * 4 * 6 / 2 + (4 * 2 - 2**3 / 3)) + 64 * 18
        expected = a0 * 20 + p0 * int_r + math.pi * int_r2
        assert shape.volume == pytest.approx(expected, rel=1e-6)
        kinds = Counter(f.geom_type for f in shape.faces())
        assert kinds[GeomType.TORUS] == 4

    def test_truncated_sphere_uses_only_existing_boundary(self):
        """A spherical cap with its flat side UP: the envelope may use the
        sphere arc only over the latitudes the face actually has, ending
        at the rim -- the honest (trimmed) support function makes this the
        correct hull rather than a decline. Volume: stadium cross-section
        2*d*r(z) + pi*r(z)^2 with r(z) = sqrt(25 - z^2) over z in [-5, -1].
        """
        cap = s.difference()(
            s.sphere(5), s.translate([0, 0, 5])(s.cube(12, center=True))
        )
        shape = s.hull()(cap, s.translate([14, 0, 0])(cap))

        def int_sqrt(z: float) -> float:  # antiderivative of sqrt(25 - z^2)
            return z / 2 * math.sqrt(25 - z * z) + 12.5 * math.asin(z / 5)

        int_r = int_sqrt(-1) - int_sqrt(-5)
        int_r2 = 25 * 4 - ((-1) ** 3 - (-5) ** 3) / 3
        expected = 2 * 14 * int_r + math.pi * int_r2
        assert shape.volume == pytest.approx(expected, rel=1e-6)


class TestHull2D:
    def test_keyhole_of_two_circles(self):
        ra, rb, d = 3.0, 5.0, 10.0
        face = s.hull()(s.circle(ra), s.translate([d, 0, 0])(s.circle(rb)))
        alpha = math.asin((rb - ra) / d)
        expected = (
            0.5 * ra * ra * (math.pi - 2 * alpha - math.sin(2 * alpha))
            + 0.5 * rb * rb * (math.pi + 2 * alpha + math.sin(2 * alpha))
            + (ra + rb) * d * math.cos(alpha) ** 3
        )
        assert face.area == pytest.approx(expected, rel=1e-9)

    def test_equal_circles_make_a_stadium(self):
        face = s.hull()(s.circle(5), s.translate([10, 0, 0])(s.circle(5)))
        assert face.area == pytest.approx(math.pi * 25 + 100, rel=1e-9)

    def test_coincident_equal_circles_yield_the_circle(self):
        face = s.hull()(s.circle(5), s.circle(5))
        assert face.area == pytest.approx(math.pi * 25, rel=1e-9)

    def test_two_offset_squares(self):
        # The Gridfinity silverware tray's label-slot profile: hull of two
        # axis-aligned rectangles at different widths/offsets. The hull is a
        # trapezoid-sided hexagon; check against the shoelace area of the
        # exact vertex hull.
        face = s.hull()(
            s.translate([0, 10])(s.square([101.616, 20], center=True)),
            s.translate([0, -12.1])(s.square([84, 24.2], center=True)),
        )
        assert face is not None and not face.solids()
        # vertices of the hull, counterclockwise
        pts = [
            (50.808, 20.0),
            (-50.808, 20.0),
            (-50.808, 0.0),
            (-42.0, -24.2),
            (42.0, -24.2),
            (50.808, 0.0),
        ]
        shoelace = 0.5 * abs(
            sum(
                pts[i][0] * pts[(i + 1) % 6][1] - pts[(i + 1) % 6][0] * pts[i][1]
                for i in range(6)
            )
        )
        assert face.area == pytest.approx(shoelace, rel=1e-9)

    def test_contained_square_yields_outer_square(self):
        face = s.hull()(s.square(20, center=True), s.square(5, center=True))
        assert face.area == pytest.approx(400, rel=1e-9)

    def test_square_circle_mix_still_raises(self):
        # mixed curved + polygonal 2D hull has no rung yet
        with pytest.raises(NotImplementedError):
            s.hull()(s.square(10), s.translate([20, 0])(s.circle(3)))


class TestHullPolyhedral:
    def test_two_cubes(self):
        shape = s.hull()(s.cube(10), s.translate([20, 0, 0])(s.cube(10)))
        assert shape.volume == pytest.approx(3000, rel=1e-9)

    def test_rotated_cube_still_polyhedral(self):
        shape = s.hull()(
            s.cube(10), s.rotate([0, 0, 45])(s.translate([25, 0, 0])(s.cube(10)))
        )
        assert shape.is_valid
        assert shape.volume > 2000

    def test_mixed_curved_child_raises(self):
        with pytest.raises(NotImplementedError):
            s.hull()(s.cube(10), s.translate([20, 0, 0])(s.sphere(3)))


class TestHullBasics:
    def test_single_child_is_returned_unchanged(self):
        shape = s.hull()(s.cube(10))
        assert shape.volume == pytest.approx(1000, rel=1e-9)

    def test_empty_raises_value_error(self):
        with pytest.raises(ValueError):
            s.hull()()


class TestMinkowski:
    def test_cube_plus_sphere_matches_steiner(self):
        shape = s.minkowski()(s.cube([20, 15, 10], center=True), s.sphere(3))
        edges = (
            [(20, math.pi / 2)] * 4 + [(15, math.pi / 2)] * 4 + [(10, math.pi / 2)] * 4
        )
        expected = _steiner(3000, 2 * (300 + 200 + 150), edges, 3)
        assert shape.volume == pytest.approx(expected, rel=1e-9)

    def test_polyhedron_ball_kernel_is_recognized(self):
        # BOSL2-style: a rounding kernel tessellated as an explicit
        # polyhedron rather than sphere(). Build a 96-vertex ball.
        pts = []
        for i in range(8):
            theta = math.pi * (i + 0.5) / 8
            for j in range(12):
                phi = 2 * math.pi * j / 12
                pts.append(
                    [
                        2 * math.sin(theta) * math.cos(phi),
                        2 * math.sin(theta) * math.sin(phi),
                        2 * math.cos(theta),
                    ]
                )
        faces = []
        for i in range(7):
            for j in range(12):
                a = i * 12 + j
                b = i * 12 + (j + 1) % 12
                faces.append([a, b, b + 12, a + 12])
        top = list(range(11, -1, -1))
        bottom = list(range(84, 96))
        faces += [top, bottom]
        ball = s.polyhedron(pts, faces)
        shape = s.minkowski()(s.cube(10, center=True), ball)
        assert shape.is_valid
        assert shape.volume > 1000

    def test_unsupported_minkowski_raises(self):
        with pytest.raises(NotImplementedError):
            s.minkowski()(s.cube(10), s.cube(5))


class TestHullGroupedChildren:
    """hull() of pre-fused inputs: OpenSCAD wraps module-call bodies in
    group(), so real hulls often arrive as ONE compound child. Inputs are
    exploded into component solids before classification (decomposition
    never changes a hull), and there is deliberately no single-child
    identity shortcut -- hull(X) == X only for convex X. A Gridfinity cup
    silently lost its rounded-box hull to that shortcut.
    """

    def test_hull_of_prefused_corner_posts_is_the_rounded_box(self):
        import math

        posts = s.union()(
            *[
                s.translate([x, y, 0])(s.cylinder(r=2, h=10))
                for x in (0, 20)
                for y in (0, 20)
            ]
        )
        shape = s.hull()(posts)
        expected = (400 + 4 * 2 * 20 + math.pi * 4) * 10
        assert shape.volume == pytest.approx(expected, rel=1e-9)

    def test_hull_of_lifted_cylinder_pair_stays_on_their_span(self):
        """Two equal cylinders both translated up: the stadium prism must
        occupy their z-span, not extrude away from whichever cap OCCT
        happened to list first. A headset hanger's oval boss came out at
        z 16..28 instead of 4..16 -- right volume, wrong place."""
        shape = s.hull()(
            s.translate([40, 14, 4])(s.cylinder(r=6, h=12)),
            s.translate([44, 6, 4])(s.cylinder(r=6, h=12)),
        )
        bb = shape.bounding_box()
        assert (bb.min.Z, bb.max.Z) == pytest.approx((4, 16), abs=1e-6)
        expected = (math.pi * 36 + 2 * 6 * math.hypot(4, 8)) * 12
        assert shape.volume == pytest.approx(expected, rel=1e-9)

    def test_hull_of_prefused_sphere_pair_is_the_tangent_cone_hull(self):
        pair = s.union()(s.sphere(3), s.translate([14, 0, 0])(s.sphere(5)))
        shape = s.hull()(pair)
        assert shape.volume == pytest.approx(_pair_hull_volume(3, 5, 14), rel=1e-9)

    def test_hull_of_single_convex_primitives_is_identity_via_rungs(self):
        import math

        assert s.hull()(s.cube(10)).volume == pytest.approx(1000, rel=1e-9)
        assert s.hull()(s.sphere(3)).volume == pytest.approx(
            (4 / 3) * math.pi * 27, rel=1e-9
        )
        assert s.hull()(s.cylinder(r=2, h=5)).volume == pytest.approx(
            math.pi * 4 * 5, rel=1e-9
        )

    def test_hull_of_single_nonconvex_child_is_its_true_hull(self):
        lshape = s.union()(s.cube([20, 10, 10]), s.cube([10, 20, 10]))
        shape = s.hull()(lshape)
        # The L's hull fills in the missing corner: a 10x10 right-triangle
        # prism of volume 500 on top of the L's own 3000.
        assert shape.volume == pytest.approx(3500, rel=1e-9)
