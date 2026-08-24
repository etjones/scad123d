"""Tier 1: build committed .csg fixtures. No OpenSCAD binary needed."""

import math
from collections import Counter

import pytest
from build123d import GeomType
from scipy.spatial import ConvexHull

import scad123d
from scad123d.solids import apply_matrix

from .conftest import FIXTURES, assert_close, shape_metrics

FIXTURE_NAMES = sorted(p.stem for p in FIXTURES.glob("*.csg"))


def test_fixtures_exist():
    assert FIXTURE_NAMES, "no .csg fixtures committed; run `just fixtures`"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_matches_reference_metrics(name, metrics):
    if name not in metrics:
        pytest.skip(f"no reference metrics for {name}")
    shape = scad123d.import_csg(FIXTURES / f"{name}.csg")
    assert shape.is_valid
    assert_close(shape_metrics(shape), metrics[name], rel=1e-9, abs_=1e-9)


class TestMinkowski:
    """Rung 1: minkowski with a ball is an exact offset."""

    @staticmethod
    def steiner(volume, area, edges, r):
        return (
            volume
            + area * r
            + r * r / 2 * sum(length * angle for length, angle in edges)
            + (4 / 3) * math.pi * r**3
        )

    def test_box_plus_sphere_is_exact(self):
        shape = scad123d.import_csg(
            "minkowski(convexity = 0) {\n"
            "\tcube(size = [20, 15, 10], center = true);\n"
            "\tsphere($fn = 0, $fa = 12, $fs = 2, r = 3);\n"
            "}"
        )
        edges = [(20, math.pi / 2)] * 4 + [(15, math.pi / 2)] * 4 + [(10, math.pi / 2)] * 4
        expected = self.steiner(20 * 15 * 10, 2 * (300 + 200 + 150), edges, 3)
        assert shape.volume == pytest.approx(expected, rel=1e-9)

    def test_result_is_analytic_not_a_mesh(self):
        """The whole point of rung 1: real surfaces, not triangles."""
        shape = scad123d.import_csg(
            "minkowski(convexity = 0) {\n"
            "\tcube(size = [20, 15, 10], center = true);\n"
            "\tsphere($fn = 0, $fa = 12, $fs = 2, r = 3);\n"
            "}"
        )
        kinds = Counter(f.geom_type for f in shape.faces())
        assert kinds == {
            GeomType.PLANE: 6,
            GeomType.CYLINDER: 12,
            GeomType.SPHERE: 8,
        }

    def test_beats_openscad_on_a_faceted_sphere(self):
        """Analytic offset must exceed OpenSCAD's inscribed faceted hull."""
        shape = scad123d.import_csg(
            "minkowski(convexity = 0) {\n"
            "\tcube(size = [20, 15, 10], center = true);\n"
            "\tsphere($fn = 0, $fa = 12, $fs = 2, r = 3);\n"
            "}"
        )
        edges = [(20, math.pi / 2)] * 4 + [(15, math.pi / 2)] * 4 + [(10, math.pi / 2)] * 4
        exact = self.steiner(3000, 1300, edges, 3)
        assert shape.volume >= exact - 1e-6


class TestFacets:
    """$fn below the threshold is honored as real geometry."""

    def test_hexagon_area_is_exact(self):
        shape = scad123d.import_csg(
            "linear_extrude(height = 5, center = false, $fn = 6, $fa = 12, $fs = 2) {\n"
            "\tcircle($fn = 6, $fa = 12, $fs = 2, r = 10);\n"
            "}"
        )
        expected = (3 * math.sqrt(3) / 2) * 100 * 5
        assert shape.volume == pytest.approx(expected, rel=1e-9)

    def test_large_fn_gives_exact_curves(self):
        shape = scad123d.import_csg(
            "cylinder($fn = 64, $fa = 12, $fs = 2, h = 10, r1 = 8, r2 = 8, center = false);"
        )
        assert shape.volume == pytest.approx(math.pi * 64 * 10, rel=1e-9)
        assert shape.faces()[0].geom_type == GeomType.CYLINDER

    def test_threshold_zero_always_uses_exact_curves(self):
        source = "cylinder($fn = 6, $fa = 12, $fs = 2, h = 10, r1 = 8, r2 = 8, center = false);"
        faceted = scad123d.import_csg(source).volume
        exact = scad123d.import_csg(source, facet_threshold=0).volume
        assert faceted < exact
        assert exact == pytest.approx(math.pi * 64 * 10, rel=1e-9)

    def test_square_pyramid_from_cone_with_fn_4(self):
        shape = scad123d.import_csg(
            "cylinder($fn = 4, $fa = 12, $fs = 2, h = 10, r1 = 8, r2 = 0, center = false);"
        )
        assert shape.volume == pytest.approx(128 * 10 / 3, rel=1e-9)


class TestTransforms:
    def test_rigid_transform_preserves_analytic_surfaces(self):
        shape = scad123d.import_csg(
            "multmatrix([[0.707107, -0.707107, 0, 0], [0.707107, 0.707107, 0, 0], "
            "[0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
            "\tsphere($fn = 0, $fa = 12, $fs = 2, r = 5);\n"
            "}"
        )
        assert shape.faces()[0].geom_type == GeomType.SPHERE
        assert shape.volume == pytest.approx((4 / 3) * math.pi * 125, rel=1e-6)

    def test_reflection_yields_positive_volume(self):
        """mirror() has determinant -1 and inverts face orientation."""
        shape = scad123d.import_csg(
            "multmatrix([[-1, -0, -0, 0], [-0, 1, -0, 0], [-0, -0, 1, 0], [0, 0, 0, 1]]) {\n"
            "\tcube(size = [8, 6, 4], center = false);\n"
            "}"
        )
        assert shape.volume == pytest.approx(192, rel=1e-9)
        assert shape.is_valid

    def test_non_uniform_scale_becomes_an_ellipsoid(self):
        shape = scad123d.import_csg(
            "multmatrix([[1, 0, 0, 0], [0, 2, 0, 0], [0, 0, 3, 0], [0, 0, 0, 1]]) {\n"
            "\tsphere($fn = 0, $fa = 12, $fs = 2, r = 4);\n"
            "}"
        )
        assert shape.volume == pytest.approx((4 / 3) * math.pi * 64 * 6, rel=1e-3)

    def test_uniform_scale_stays_rigid(self):
        shape = scad123d.import_csg(
            "multmatrix([[2, 0, 0, 0], [0, 2, 0, 0], [0, 0, 2, 0], [0, 0, 0, 1]]) {\n"
            "\tsphere($fn = 0, $fa = 12, $fs = 2, r = 5);\n"
            "}"
        )
        assert shape.faces()[0].geom_type == GeomType.SPHERE
        assert shape.volume == pytest.approx((4 / 3) * math.pi * 1000, rel=1e-6)

    def test_shear_matrix_is_applied(self):
        shape = apply_matrix(
            scad123d.import_csg("cube(size = [10, 10, 10], center = false);"),
            [[1, 0.5, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        )
        assert shape.volume == pytest.approx(1000, rel=1e-6)  # shear preserves volume


class TestPolyhedron:
    def test_cube_from_quad_faces(self):
        shape = scad123d.import_csg(
            "polyhedron(points = [[0,0,0],[10,0,0],[10,10,0],[0,10,0],"
            "[0,0,10],[10,0,10],[10,10,10],[0,10,10]], "
            "faces = [[0,1,2,3],[4,7,6,5],[0,4,5,1],[1,5,6,2],[2,6,7,3],[3,7,4,0]], "
            "convexity = 1);"
        )
        assert shape.volume == pytest.approx(1000, rel=1e-9)
        assert shape.is_valid

    def test_reversed_winding_is_tolerated(self):
        """OpenSCAD accepts either winding; so must we."""
        shape = scad123d.import_csg(
            "polyhedron(points = [[0,0,0],[10,0,0],[5,10,0],[5,5,12]], "
            "faces = [[0,1,2],[0,3,1],[1,3,2],[2,3,0]], convexity = 1);"
        )
        assert shape.volume > 0


def _translated(x, y, z, leaf: str) -> str:
    return f"multmatrix([[1,0,0,{x}],[0,1,0,{y}],[0,0,1,{z}],[0,0,0,1]]) {{ {leaf} }}"


def _hull_of(*children: str) -> str:
    return "hull() {\n" + "\n".join(children) + "\n}"


_SPHERE = "sphere($fn = 0, $fa = 12, $fs = 2, r = {r});"
_CYLINDER_CENTERED = "multmatrix([[1,0,0,0],[0,1,0,0],[0,0,1,-10],[0,0,0,1]]) {{ cylinder($fn = 0, $fa = 12, $fs = 2, h = 20, r1 = {r}, r2 = {r}, center = false); }}"


class TestHull:
    """Rung 2: hull() of equal-radius spheres, and of equal-radius parallel
    cylinders sharing one axial span. Everything else falls back to a mesh.
    """

    def test_box_corner_spheres_matches_original_verification(self):
        """The exact box-of-8-corners case verified during design, reproduced
        here as a regression fixture: matches the Steiner formula to ~1e-9,
        with the true rounded-box topology (6 planes, 12 cylinders, 8 spheres).
        """
        centers = [
            (-10, -7.5, -5), (10, -7.5, -5), (-10, 7.5, -5), (10, 7.5, -5),
            (-10, -7.5, 5), (10, -7.5, 5), (-10, 7.5, 5), (10, 7.5, 5),
        ]
        source = _hull_of(*(_translated(x, y, z, _SPHERE.format(r=3)) for x, y, z in centers))
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(8285.4424, rel=1e-6)
        kinds = Counter(f.geom_type for f in shape.faces())
        assert kinds == {GeomType.PLANE: 6, GeomType.CYLINDER: 12, GeomType.SPHERE: 8}

    def test_tetrahedron_of_spheres(self):
        centers = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
        source = _hull_of(*(_translated(x, y, z, _SPHERE.format(r=2)) for x, y, z in centers))
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(953.14152, rel=1e-6)
        kinds = Counter(f.geom_type for f in shape.faces())
        assert kinds == {GeomType.PLANE: 4, GeomType.CYLINDER: 6, GeomType.SPHERE: 4}

    def test_two_spheres_build_an_exact_capsule(self):
        source = _hull_of(
            _translated(-7, 0, 0, _SPHERE.format(r=3)),
            _translated(7, 0, 0, _SPHERE.format(r=3)),
        )
        shape = scad123d.import_csg(source)
        expected = math.pi * 9 * 14 + (4 / 3) * math.pi * 27
        assert shape.volume == pytest.approx(expected, rel=1e-9)
        kinds = Counter(f.geom_type for f in shape.faces())
        assert kinds == {GeomType.CYLINDER: 1, GeomType.SPHERE: 2}

    def test_collinear_spheres_of_any_count_build_a_capsule(self):
        """An interior point on the same line changes nothing -- only the two
        extremes matter, matching what a real convex hull would give anyway.
        """
        source = _hull_of(
            _translated(-7, 0, 0, _SPHERE.format(r=3)),
            _translated(0, 0, 0, _SPHERE.format(r=3)),
            _translated(7, 0, 0, _SPHERE.format(r=3)),
        )
        shape = scad123d.import_csg(source)
        expected = math.pi * 9 * 14 + (4 / 3) * math.pi * 27
        assert shape.volume == pytest.approx(expected, rel=1e-9)

    def test_parallel_cylinders_matches_2d_offset_extruded(self):
        centers = [(-10, -7.5), (10, -7.5), (-10, 7.5), (10, 7.5)]
        source = _hull_of(*(_translated(x, y, 0, _CYLINDER_CENTERED.format(r=3)) for x, y in centers))
        shape = scad123d.import_csg(source)
        area = 20 * 15 + 2 * (20 + 15) * 3 + math.pi * 9
        assert shape.volume == pytest.approx(area * 20, rel=1e-6)
        kinds = Counter(f.geom_type for f in shape.faces())
        assert kinds == {GeomType.PLANE: 6, GeomType.CYLINDER: 4}

    # Cases that fall back to a mesh live in test_differential.py, not here:
    # the fallback itself invokes OpenSCAD, so a tier-1 test (no binary
    # required, this file) can't exercise it. That mistake shipped once
    # already -- these tests passed locally (this machine has OpenSCAD) but
    # failed in CI's no-binary tier, which is exactly what tier 1 is for.

    def test_single_child_hull_is_a_no_op(self):
        shape = scad123d.import_csg(_hull_of(_SPHERE.format(r=3)))
        assert shape.volume == pytest.approx((4 / 3) * math.pi * 27, rel=1e-9)


def _pair_hull_volume(ra: float, rb: float, d: float) -> float:
    """Closed-form hull volume of two spheres: two caps + tangent frustum."""
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


def _pair_hull_area_2d(ra: float, rb: float, d: float) -> float:
    """Closed-form hull area of two discs: two segments + tangent trapezoid."""
    alpha = math.asin((rb - ra) / d)
    sin_2a = math.sin(2 * alpha)
    return (
        0.5 * ra * ra * (math.pi - 2 * alpha - sin_2a)
        + 0.5 * rb * rb * (math.pi + 2 * alpha + sin_2a)
        + (ra + rb) * d * math.cos(alpha) ** 3
    )


class TestPairHull:
    """hull() of exactly two unequal-radius spheres (3D) or discs (2D):
    exact, via the external tangent cone / tangent lines. The 3D solid is
    sewn from its three boundary patches (two spherical caps + the cone)
    rather than fused -- OCCT booleans are flakiest exactly at tangent
    contact, which is the only seam this shape has. Strictly pairwise:
    three non-collinear unequal spheres need tritangent planes with
    power-diagram combinatorics, and fall back to a mesh.
    """

    def test_two_unequal_spheres_match_the_closed_form(self):
        import warnings as _warnings

        source = _hull_of(
            _SPHERE.format(r=3), _translated(14, 0, 0, _SPHERE.format(r=5))
        )
        with _warnings.catch_warnings():
            _warnings.simplefilter("error")  # any fallback warning = failure
            shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(_pair_hull_volume(3, 5, 14), rel=1e-9)
        kinds = Counter(f.geom_type for f in shape.faces())
        assert kinds == {GeomType.SPHERE: 2, GeomType.CONE: 1}
        assert shape.is_valid

    def test_order_does_not_matter(self):
        big_first = _hull_of(
            _SPHERE.format(r=5), _translated(14, 0, 0, _SPHERE.format(r=3))
        )
        shape = scad123d.import_csg(big_first)
        assert shape.volume == pytest.approx(_pair_hull_volume(3, 5, 14), rel=1e-9)

    def test_contained_sphere_hulls_to_the_big_sphere(self):
        source = _hull_of(
            _SPHERE.format(r=1), _translated(2, 0, 0, _SPHERE.format(r=5))
        )
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx((4 / 3) * math.pi * 125, rel=1e-9)

    def test_near_containment_stays_valid_and_exact(self):
        # sin(a) = 2/2.05 = 0.9756 -- the cone is nearly a tangent plane
        # disc and the small kept cap nearly vanishes. The rung's
        # volume-vs-formula self-gate would send any sewing artifact to the
        # mesh fallback instead of shipping bad geometry.
        source = _hull_of(
            _SPHERE.format(r=1), _translated(2.05, 0, 0, _SPHERE.format(r=3))
        )
        shape = scad123d.import_csg(source)
        assert shape.is_valid
        assert shape.volume == pytest.approx(_pair_hull_volume(1, 3, 2.05), rel=1e-9)

    def test_internal_tangency_is_exactly_the_big_sphere(self):
        # d == r2 - r1 exactly: the small sphere touches the big one from
        # inside, and the hull IS the big sphere. The containment branch
        # (d + r1 <= r2 within tolerance) covers this boundary.
        source = _hull_of(
            _SPHERE.format(r=1), _translated(4, 0, 0, _SPHERE.format(r=5))
        )
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx((4 / 3) * math.pi * 125, rel=1e-9)

    def test_barely_outside_containment_survives_extreme_degeneracy(self):
        # sin(a) = 4/4.0000004 -- the exact hull exceeds the big sphere by
        # a vanishing sliver. Verified empirically that the sewn
        # construction (not the mesh fallback: warnings are errors here)
        # still produces a valid solid whose volume matches the closed
        # form; the self-gate stands behind it if OCCT ever degrades.
        import warnings as _warnings

        source = _hull_of(
            _SPHERE.format(r=1), _translated(4.0000004, 0, 0, _SPHERE.format(r=5))
        )
        with _warnings.catch_warnings():
            _warnings.simplefilter("error")
            shape = scad123d.import_csg(source)
        assert shape.is_valid
        assert shape.volume == pytest.approx(
            _pair_hull_volume(1, 5, 4.0000004), rel=1e-6
        )

    def test_overlapping_spheres_still_hull_exactly(self):
        # The support-function argument is independent of overlap: the same
        # tangent cone bounds the hull whether or not the spheres intersect.
        source = _hull_of(
            _SPHERE.format(r=3), _translated(5, 0, 0, _SPHERE.format(r=5))
        )
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(_pair_hull_volume(3, 5, 5), rel=1e-9)

    def test_three_unequal_spheres_decline(self):
        from build123d import Pos, Sphere
        from solid123d.hull import _hull_of_spheres

        shapes = [
            Sphere(3),
            Pos(14, 0, 0) * Sphere(5),
            Pos(7, 12, 0) * Sphere(4),
        ]
        assert _hull_of_spheres(shapes) is None

    def test_2d_keyhole_extrudes_exactly(self):
        import warnings as _warnings

        source = (
            "linear_extrude(height = 4, center = false, convexity = 1) {\n"
            "\thull() {\n"
            "\t\tcircle($fn = 0, $fa = 12, $fs = 2, r = 3);\n"
            "\t\tmultmatrix([[1,0,0,10],[0,1,0,0],[0,0,1,0],[0,0,0,1]]) {\n"
            "\t\t\tcircle($fn = 0, $fa = 12, $fs = 2, r = 5);\n\t\t}\n"
            "\t}\n}"
        )
        with _warnings.catch_warnings():
            _warnings.simplefilter("error")
            shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(4 * _pair_hull_area_2d(3, 5, 10), rel=1e-9)
        kinds = Counter(f.geom_type for f in shape.faces())
        assert kinds == {GeomType.PLANE: 4, GeomType.CYLINDER: 2}

    def test_2d_equal_circles_make_a_stadium(self):
        # Equal radii is the alpha = 0 case of the same construction. Also
        # notable: before this rung, *any* 2D hull hard-failed -- OpenSCAD
        # cannot render a 2D subtree to a mesh, so there was no fallback.
        source = (
            "linear_extrude(height = 4, center = false, convexity = 1) {\n"
            "\thull() {\n"
            "\t\tcircle($fn = 0, $fa = 12, $fs = 2, r = 5);\n"
            "\t\tmultmatrix([[1,0,0,10],[0,1,0,0],[0,0,1,0],[0,0,0,1]]) {\n"
            "\t\t\tcircle($fn = 0, $fa = 12, $fs = 2, r = 5);\n\t\t}\n"
            "\t}\n}"
        )
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(4 * (math.pi * 25 + 100), rel=1e-9)

    def test_2d_contained_circle_hulls_to_the_big_circle(self):
        source = (
            "linear_extrude(height = 4, center = false, convexity = 1) {\n"
            "\thull() {\n"
            "\t\tcircle($fn = 0, $fa = 12, $fs = 2, r = 1);\n"
            "\t\tmultmatrix([[1,0,0,2],[0,1,0,0],[0,0,1,0],[0,0,0,1]]) {\n"
            "\t\t\tcircle($fn = 0, $fa = 12, $fs = 2, r = 5);\n\t\t}\n"
            "\t}\n}"
        )
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(4 * math.pi * 25, rel=1e-9)

    def test_2d_coincident_equal_circles_hull_to_the_circle(self):
        # d = 0 with equal radii: the containment inequality
        # (d + r1 <= r2 within relative tolerance) absorbs this before any
        # division by d or degenerate wire construction can happen --
        # important because a 2D decline has no mesh fallback to save it.
        source = (
            "linear_extrude(height = 4, center = false, convexity = 1) {\n"
            "\thull() {\n"
            "\t\tcircle($fn = 0, $fa = 12, $fs = 2, r = 5);\n"
            "\t\tcircle($fn = 0, $fa = 12, $fs = 2, r = 5);\n"
            "\t}\n}"
        )
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(4 * math.pi * 25, rel=1e-9)

    def test_2d_micro_stadium_past_the_containment_tolerance(self):
        # Equal circles separated by d = 1e-4: past the containment
        # tolerance window, so the real tangent-wire construction runs with
        # segments of length 1e-4 -- and the result is exactly the stadium,
        # circle area plus 2*r*d.
        source = (
            "linear_extrude(height = 4, center = false, convexity = 1) {\n"
            "\thull() {\n"
            "\t\tcircle($fn = 0, $fa = 12, $fs = 2, r = 5);\n"
            "\t\tmultmatrix([[1,0,0,0.0001],[0,1,0,0],[0,0,1,0],[0,0,0,1]]) {\n"
            "\t\t\tcircle($fn = 0, $fa = 12, $fs = 2, r = 5);\n\t\t}\n"
            "\t}\n}"
        )
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(4 * (math.pi * 25 + 2 * 5 * 1e-4), rel=1e-9)


class TestPolyhedralHull:
    """Rung 3: hull() of all-polyhedral children is exactly the convex hull
    of their combined vertices, built as a real BRep solid via build123d's
    ConvexPolyhedron -- no mesh fallback, no approximation.
    """

    def test_two_cubes_with_matching_cross_section_hull_to_a_box(self):
        # Same YZ extents, separated along X: the hull is exactly a
        # 40 x 10 x 10 box -- volume checkable by hand.
        source = _hull_of(
            "cube(size = [10, 10, 10], center = false);",
            _translated(30, 0, 0, "cube(size = [10, 10, 10], center = false);"),
        )
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(4000, rel=1e-9)
        kinds = Counter(f.geom_type for f in shape.faces())
        assert kinds == {GeomType.PLANE: 6}

    def test_rotated_cube_hull_matches_scipy_exactly(self):
        # A generic polyhedral hull with no hand-computable volume: check
        # against scipy's own qhull volume over the same vertex set. Also
        # proves matrix transforms don't break the rung -- planarity
        # survives any affine map.
        c = s = math.sqrt(2) / 2
        source = _hull_of(
            "cube(size = [10, 10, 10], center = true);",
            f"multmatrix([[{c}, {-s}, 0, 30], [{s}, {c}, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) "
            "{ cube(size = [10, 10, 10], center = true); }",
        )
        shape = scad123d.import_csg(source)
        base = [(x, y, z) for x in (-5, 5) for y in (-5, 5) for z in (-5, 5)]
        rotated = [(c * x - s * y + 30, s * x + c * y, z) for x, y, z in base]
        expected = ConvexHull(base + rotated).volume
        assert shape.volume == pytest.approx(expected, rel=1e-9)
        assert all(f.geom_type == GeomType.PLANE for f in shape.faces())
        assert shape.is_valid

    def test_polyhedron_child_qualifies(self):
        source = _hull_of(
            "polyhedron(points = [[0,0,0],[10,0,0],[5,10,0],[5,5,12]], "
            "faces = [[0,2,1],[0,1,3],[1,2,3],[2,0,3]], convexity = 1);",
            _translated(20, 0, 0, "cube(size = [2, 2, 2], center = false);"),
        )
        shape = scad123d.import_csg(source)
        pts = [(0, 0, 0), (10, 0, 0), (5, 10, 0), (5, 5, 12)]
        pts += [(x + 20, y, z) for x in (0, 2) for y in (0, 2) for z in (0, 2)]
        assert shape.volume == pytest.approx(ConvexHull(pts).volume, rel=1e-9)

    def test_faceted_cylinders_now_hull_exactly(self):
        # Faceted ($fn below threshold) cylinders are polygonal prisms --
        # polyhedral, so this rung hulls them exactly. These used to be the
        # test suite's canonical "guaranteed mesh fallback" example.
        source = _hull_of(
            "cylinder($fn = 6, $fa = 12, $fs = 2, h = 2, r1 = 3, r2 = 3, center = true);",
            _translated(
                14, 0, 0,
                "cylinder($fn = 6, $fa = 12, $fs = 2, h = 2, r1 = 3, r2 = 3, center = true);",
            ),
        )
        shape = scad123d.import_csg(source)
        # Expected volume from the vertices of a single imported cylinder
        # (whose tessellation is pinned by TestFacets), replicated at both
        # positions -- this checks the hulling, not the hexagon's phase.
        one = scad123d.import_csg(
            "cylinder($fn = 6, $fa = 12, $fs = 2, h = 2, r1 = 3, r2 = 3, center = true);"
        )
        base = [(v.X, v.Y, v.Z) for v in one.vertices()]
        pts = base + [(x + 14, y, z) for x, y, z in base]
        assert shape.volume == pytest.approx(ConvexHull(pts).volume, rel=1e-9)
        assert all(f.geom_type == GeomType.PLANE for f in shape.faces())

    # Decline paths are checked at the unit level: going through import_csg
    # would hit the mesh fallback, which invokes OpenSCAD -- not available
    # in tier 1 (see the comment in TestHull above).

    def test_curved_child_declines(self):
        from build123d import Box, Sphere
        from solid123d.hull import _hull_of_polyhedra

        assert _hull_of_polyhedra([Box(10, 10, 10), Sphere(5)]) is None

    def test_2d_child_declines(self):
        from build123d import Box, Rectangle
        from solid123d.hull import _hull_of_polyhedra

        assert _hull_of_polyhedra([Box(10, 10, 10), Rectangle(5, 5)]) is None


class TestStructure:
    def test_empty_group_produces_nothing(self):
        with pytest.raises(scad123d.UnsupportedNodeError):
            scad123d.import_csg("group();")

    def test_background_modifier_children_are_dropped(self):
        shape = scad123d.import_csg(
            "group() {\n\tcube(size = [10, 10, 10], center = false);\n"
            "\t%cube(size = [20, 20, 20], center = false);\n}"
        )
        assert shape.volume == pytest.approx(1000, rel=1e-9)

    def test_show_only_modifier_discards_siblings(self):
        shape = scad123d.import_csg(
            "group() {\n\tcube(size = [10, 10, 10], center = false);\n"
            "\t!cube(size = [2, 2, 2], center = false);\n}"
        )
        assert shape.volume == pytest.approx(8, rel=1e-9)

    def test_difference_subtracts_in_order(self):
        shape = scad123d.import_csg(
            "difference() {\n\tcube(size = [10, 10, 10], center = false);\n"
            "\tcube(size = [5, 5, 5], center = false);\n}"
        )
        assert shape.volume == pytest.approx(1000 - 125, rel=1e-9)

    def test_render_node_is_a_passthrough(self):
        shape = scad123d.import_csg(
            "render(convexity = 3) {\n\tcube(size = [3, 3, 3], center = false);\n}"
        )
        assert shape.volume == pytest.approx(27, rel=1e-9)

    def test_unrecognised_node_warns_and_is_ignored(self):
        with (
            pytest.warns(UserWarning, match="unrecognised CSG node"),
            pytest.raises(scad123d.UnsupportedNodeError),
        ):
            scad123d.import_csg("bogus_node(x = 1);")


class TestColor:
    """color() on more than one child of a group.

    Gated on authored color: a group with no color() anywhere always gets
    the plain fuse, bit-identical to pre-color-support behavior (and skips
    the volume bookkeeping entirely). For colored groups, shared volume is
    the deciding line:

    - Zero shared volume (disjoint, or touching along a surface -- a part
      sitting exactly in a cavity cut for it): returned as a Compound,
      each child keeping its own color. Colors are evidence the author
      means distinct parts, so touching parts deliberately stay separate
      bodies rather than getting OpenSCAD's merged-solid union.
    - Overlapping: a real fuse. A bare fuse of colored shapes carries no
      color at all (confirmed directly), so scad123d applies the first
      child's color rather than leaving it uncolored.
    """

    _RED = (1.0, 0.0, 0.0, 1.0)
    _BLUE = (0.0, 0.0, 1.0, 1.0)

    def test_disjoint_colored_children_keep_their_own_color(self):
        shape = scad123d.import_csg(
            "color([1, 0, 0, 1]) {\n\tcube(size = [10, 10, 10], center = false);\n}\n"
            "color([0, 0, 1, 1]) {\n"
            "\tmultmatrix([[1, 0, 0, 20], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
            "\t\tsphere($fn = 32, $fa = 12, $fs = 2, r = 5);\n\t}\n}"
        )
        assert len(shape.children) == 2
        cube, sphere = shape.children
        assert tuple(cube.color) == pytest.approx(self._RED)
        assert tuple(sphere.color) == pytest.approx(self._BLUE)
        # Geometrically unaffected -- same total volume as a real fuse would give.
        assert shape.volume == pytest.approx(1000 + (4 / 3) * math.pi * 125, rel=1e-6)

    def test_overlapping_colored_children_fall_back_to_a_real_fuse(self):
        # Same shapes as the disjoint case, but overlapping -- a real fuse
        # must still happen (unaffected by this feature), and the exact
        # pre-existing overlap-corrected volume is preserved. Which color
        # "wins" the merged region is genuinely undefined, but a real fuse
        # doesn't even keep the first child's color on its own -- confirmed
        # directly -- so this checks that scad123d applies one anyway
        # (first child with a color) rather than leaving it uncolored,
        # which is what a real, reported .scad file (cube+cylinder both
        # near the origin, genuinely overlapping) hit in practice.
        shape = scad123d.import_csg(
            "union() {\n"
            "\tcolor([1, 0, 0, 1]) { cube(size = [10, 10, 10], center = false); }\n"
            "\tcolor([0, 0, 1, 1]) {\n"
            "\t\tmultmatrix([[1, 0, 0, 5], [0, 1, 0, 5], [0, 0, 1, 5], [0, 0, 0, 1]]) {\n"
            "\t\t\tcube(size = [10, 10, 10], center = false);\n\t\t}\n\t}\n}"
        )
        assert len(shape.solids()) == 1
        assert shape.volume == pytest.approx(1000 + 1000 - 125, rel=1e-9)
        assert tuple(shape.color) == pytest.approx(self._RED)

    def test_overlap_fallback_color_is_not_hardcoded_to_the_first_child(self):
        # Same overlap shape, but only the *second* child has a color --
        # proves the fallback searches for the first child that has one,
        # rather than always reading shapes[0].
        shape = scad123d.import_csg(
            "union() {\n"
            "\tcube(size = [10, 10, 10], center = false);\n"
            "\tcolor([0, 0, 1, 1]) {\n"
            "\t\tmultmatrix([[1, 0, 0, 5], [0, 1, 0, 5], [0, 0, 1, 5], [0, 0, 0, 1]]) {\n"
            "\t\t\tcube(size = [10, 10, 10], center = false);\n\t\t}\n\t}\n}"
        )
        assert len(shape.solids()) == 1
        assert tuple(shape.color) == pytest.approx(self._BLUE)

    def test_touching_colored_children_stay_separate_bodies(self):
        # Zero shared volume, real shared surface: a part designed to sit
        # exactly in a cavity cut for it (from a user's actual .scad: a red
        # cube with a cylindrical hole, a blue cylinder filling it). Volume
        # matches the naive sum, area doesn't -- a fuse would glue the
        # shared face away and merge the parts. Colors mark them as
        # intentionally distinct parts, so they must stay two bodies.
        shape = scad123d.import_csg(
            "union() {\n"
            "\tcolor([1, 0, 0, 1]) { cube(size = [10, 10, 10], center = false); }\n"
            "\tcolor([0, 0, 1, 1]) {\n"
            "\t\tmultmatrix([[1, 0, 0, 10], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
            "\t\t\tcube(size = [10, 10, 10], center = false);\n\t\t}\n\t}\n}"
        )
        assert len(shape.children) == 2
        left, right = shape.children
        assert tuple(left.color) == pytest.approx(self._RED)
        assert tuple(right.color) == pytest.approx(self._BLUE)
        assert shape.volume == pytest.approx(2000, rel=1e-9)

    def test_uncolored_disjoint_children_get_the_plain_fuse(self):
        # The gate: all the volume bookkeeping and Compound-building exists
        # only for authored colors. A group with no color() anywhere takes
        # the plain-fuse path, bit-identical to pre-color-support behavior
        # -- an OCCT compound of 2 solids, but no assembly children.
        shape = scad123d.import_csg(
            "cube(size = [10, 10, 10], center = false);\n"
            "multmatrix([[1, 0, 0, 20], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
            "\tcube(size = [5, 5, 5], center = false);\n}"
        )
        assert len(shape.children) == 0
        assert len(shape.solids()) == 2
        assert shape.volume == pytest.approx(1000 + 125, rel=1e-9)

    def test_touching_uncolored_children_still_fuse_to_one_solid(self):
        # The color-free counterpart of the test above: without colors
        # there's no evidence the author means separate parts, so touching
        # children keep OpenSCAD's faithful union semantics -- one merged
        # solid, shared face glued away.
        shape = scad123d.import_csg(
            "union() {\n"
            "\tcube(size = [10, 10, 10], center = false);\n"
            "\tmultmatrix([[1, 0, 0, 10], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
            "\t\tcube(size = [10, 10, 10], center = false);\n\t}\n}"
        )
        assert len(shape.solids()) == 1
        assert shape.volume == pytest.approx(2000, rel=1e-9)

    def test_nested_color_overrides_the_outer_color_for_that_child(self):
        # color("red") union() { cube(...); color("blue") sphere(...); }
        shape = scad123d.import_csg(
            "color([1, 0, 0, 1]) {\n\tunion() {\n"
            "\t\tcube(size = [10, 10, 10], center = false);\n"
            "\t\tcolor([0, 0, 1, 1]) {\n"
            "\t\t\tmultmatrix([[1, 0, 0, 20], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
            "\t\t\t\tsphere($fn = 32, $fa = 12, $fs = 2, r = 5);\n\t\t\t}\n\t\t}\n\t}\n}"
        )
        assert tuple(shape.color) == pytest.approx(self._RED)
        cube, sphere = shape.children
        # The cube has no color of its own -- build123d's Shape.color is a
        # property that, when unset locally, walks up .parent and resolves
        # to the nearest ancestor's color (matching OpenSCAD; this is also
        # exactly what export_step's own color-inheritance docs describe).
        assert cube._color is None
        assert tuple(cube.color) == pytest.approx(self._RED)
        # The sphere's own nested color() overrides the ancestor's.
        assert tuple(sphere.color) == pytest.approx(self._BLUE)

    def test_same_colored_children_still_group_correctly(self):
        shape = scad123d.import_csg(
            "color([1, 0, 0, 1]) { cube(size = [10, 10, 10], center = false); }\n"
            "color([1, 0, 0, 1]) {\n"
            "\tmultmatrix([[1, 0, 0, 20], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
            "\t\tcube(size = [5, 5, 5], center = false);\n\t}\n}"
        )
        assert shape.volume == pytest.approx(1000 + 125, rel=1e-9)

    def test_named_color_labels_the_shape_with_its_css_name(self):
        # OpenSCAD's color names are the CSS/SVG names, so even though the
        # CSG export only records the rgba value, an exact reverse lookup
        # recovers the name the author wrote.
        shape = scad123d.import_csg(
            "color([1, 0, 0, 1]) { cube(size = [5, 5, 5], center = false); }"
        )
        assert shape.label == "red"

    def test_unnamed_color_labels_the_shape_with_its_hex_value(self):
        shape = scad123d.import_csg(
            "color([0.2, 0.3, 0.4, 1]) { cube(size = [5, 5, 5], center = false); }"
        )
        assert shape.label == "#334c66"

    def test_overlap_fused_result_is_labeled_with_its_color(self):
        shape = scad123d.import_csg(
            "union() {\n"
            "\tcolor([1, 0, 0, 1]) { cube(size = [10, 10, 10], center = false); }\n"
            "\tcolor([0, 0, 1, 1]) {\n"
            "\t\tmultmatrix([[1, 0, 0, 5], [0, 1, 0, 5], [0, 0, 1, 5], [0, 0, 0, 1]]) {\n"
            "\t\t\tcube(size = [10, 10, 10], center = false);\n\t\t}\n\t}\n}"
        )
        assert shape.label == "red"

    @pytest.mark.needs_openscad
    def test_colors_survive_into_a_real_step_file(self, tmp_path):
        # The one link in this chain the tier-1 tests above can't cover:
        # that Compound(children=...) is actually what makes export_step's
        # own XCAF/PreOrderIter walk write a distinct color per part,
        # rather than one color for the whole assembly. No test anywhere
        # in this repo exercised export_step at all before this.
        from build123d import export_step

        scad = tmp_path / "colors.scad"
        scad.write_text(
            'color("red") cube([10, 10, 10]);\n'
            'color("blue") translate([20, 0, 0]) sphere(r=5, $fn=32);\n'
        )
        part = scad123d.import_scad(scad)
        step_path = tmp_path / "colors.step"
        export_step(part, str(step_path))

        # Confirmed directly: pure red/blue round-trip through OCCT's STEP
        # writer as named DRAUGHTING_PRE_DEFINED_COLOUR entities.
        text = step_path.read_text().lower()
        assert "draughting_pre_defined_colour('red')" in text
        assert "draughting_pre_defined_colour('blue')" in text
        # Labels ride along as STEP PRODUCT names: the parts under their
        # color names, the root under the source file's stem -- no
        # OCCT-auto-generated 'COMPOUND' products left anywhere.
        assert "product('red'" in text
        assert "product('blue'" in text
        assert "product('colors'" in text
        assert "product('compound'" not in text


class TestDegeneratePrimitives:
    """Primitives with a zero (or negative) critical dimension produce no
    geometry in OpenSCAD's render, but OCCT raises Standard_Failure on
    them. Real code hits this constantly -- libraries disable optional
    features by collapsing a dimension to zero (Gridfinity emits
    cube([42, 42, 0]) for a disabled lip). Found via a real Gridfinity cup
    that crashed on exactly that.
    """

    def test_zero_height_cube_contributes_nothing(self):
        shape = scad123d.import_csg(
            "union() {\n"
            "\tcube(size = [10, 10, 10], center = false);\n"
            "\tcube(size = [42, 42, 0], center = false);\n"
            "}"
        )
        assert shape.volume == pytest.approx(1000, rel=1e-9)

    def test_model_of_only_degenerate_geometry_is_empty(self):
        with pytest.raises(scad123d.UnsupportedNodeError):
            scad123d.import_csg("cube(size = [42, 42, 0], center = false);")

    def test_zero_height_cylinder_contributes_nothing(self):
        shape = scad123d.import_csg(
            "union() {\n"
            "\tcube(size = [10, 10, 10], center = false);\n"
            "\tcylinder($fn = 0, $fa = 12, $fs = 2, h = 0, r1 = 5, r2 = 5, center = false);\n"
            "}"
        )
        assert shape.volume == pytest.approx(1000, rel=1e-9)

    def test_cone_with_one_zero_radius_still_builds(self):
        # r1 or r2 alone may be zero: that's a cone, not degenerate.
        shape = scad123d.import_csg(
            "cylinder($fn = 0, $fa = 12, $fs = 2, h = 9, r1 = 3, r2 = 0, center = false);"
        )
        assert shape.volume == pytest.approx(math.pi * 9 * 9 / 3, rel=1e-9)

    def test_zero_radius_circle_contributes_nothing(self):
        shape = scad123d.import_csg(
            "linear_extrude(height = 4, center = false, convexity = 1) {\n"
            "\tunion() {\n"
            "\t\tsquare(size = [10, 10], center = false);\n"
            "\t\tcircle($fn = 0, $fa = 12, $fs = 2, r = 0);\n"
            "\t}\n}"
        )
        assert shape.volume == pytest.approx(400, rel=1e-9)


class TestHullOfGroupedChildren:
    """OpenSCAD wraps a module-call body in group(), so `hull()
    corner_posts();` arrives as hull() with ONE group child -- which the
    builder pre-fuses into a single compound. solid123d>=0.2.1 explodes
    that back into component solids before classification, so the classic
    rounded-box idiom stays exact. A Gridfinity cup silently lost 90% of
    its volume to this before the fix.
    """

    def test_hull_of_a_grouped_post_ring_is_the_exact_rounded_box(self):
        posts = "\n".join(
            f"\t\tmultmatrix([[1,0,0,{x}],[0,1,0,{y}],[0,0,1,0],[0,0,0,1]]) "
            "{ cylinder($fn = 0, $fa = 12, $fs = 2, h = 10, r1 = 2, r2 = 2, center = false); }"
            for x in (0, 20)
            for y in (0, 20)
        )
        shape = scad123d.import_csg(
            "hull() {\n\tgroup() {\n" + posts + "\n\t}\n}"
        )
        expected = (400 + 4 * 2 * 20 + math.pi * 4) * 10
        assert shape.volume == pytest.approx(expected, rel=1e-9)
        kinds = {f.geom_type for f in shape.faces()}
        assert kinds == {GeomType.PLANE, GeomType.CYLINDER}

    def test_hull_of_a_single_nonconvex_group_is_its_true_hull(self):
        # hull(X) == X only for convex X -- a single L-shaped child must
        # gain its missing-corner prism, not pass through unchanged.
        shape = scad123d.import_csg(
            "hull() {\n\tgroup() {\n"
            "\t\tcube(size = [20, 10, 10], center = false);\n"
            "\t\tcube(size = [10, 20, 10], center = false);\n"
            "\t}\n}"
        )
        assert shape.volume == pytest.approx(3500, rel=1e-9)


class TestEmptySubtrees:
    """Subtrees that enclose nothing are legal OpenSCAD (disabled features
    leave empty groups); they must vanish, not error."""

    def test_hull_of_nothing_builds_nothing(self):
        source = (
            "union() {\n"
            "  cube(size = [10, 10, 10], center = false);\n"
            "  hull() { group() { group(); } group(); }\n"
            "}"
        )
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(1000, rel=1e-9)

    def test_minkowski_of_nothing_builds_nothing(self):
        source = (
            "union() {\n"
            "  cube(size = [10, 10, 10], center = false);\n"
            "  minkowski() { group(); }\n"
            "}"
        )
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(1000, rel=1e-9)

    @pytest.mark.needs_openscad
    def test_mesh_fallback_of_empty_render_is_none(self):
        """The safety net below the walker: if a fallback subtree renders
        empty in OpenSCAD, that means 'encloses nothing', not 'failed'."""
        from scad123d.mesh import clear_cache, mesh_subtree
        from scad123d.parser import parse_csg

        clear_cache()
        node = parse_csg("hull() { group(); }")
        assert mesh_subtree(node) is None

    def test_intersection_with_empty_operand_is_empty(self):
        """An empty intersection operand annihilates the result -- dropping
        it would silently return the other operand (found in a real model:
        a disabled feature left an empty group inside an intersection)."""
        empty = (
            "intersection() {\n"
            "  cube(size = [1, 1, 1], center = false);\n"
            "  multmatrix([[1,0,0,50],[0,1,0,0],[0,0,1,0],[0,0,0,1]]) "
            "{ cube(size = [1, 1, 1], center = false); }\n"
            "}"
        )
        source = (
            "union() {\n"
            "  cube(size = [10, 10, 10], center = false);\n"
            "  multmatrix([[1,0,0,20],[0,1,0,0],[0,0,1,0],[0,0,0,1]]) {\n"
            "    intersection() {\n"
            "      cube(size = [5, 5, 5], center = false);\n"
            f"      {empty}\n"
            "    }\n"
            "  }\n"
            "}"
        )
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(1000, rel=1e-9)

    def test_difference_with_empty_minuend_is_empty(self):
        """If the FIRST difference child is empty the result is empty;
        dropping it would promote the first subtrahend to minuend."""
        source = (
            "union() {\n"
            "  cube(size = [10, 10, 10], center = false);\n"
            "  difference() {\n"
            "    group();\n"
            "    cube(size = [5, 5, 5], center = false);\n"
            "  }\n"
            "}"
        )
        shape = scad123d.import_csg(source)
        assert shape.volume == pytest.approx(1000, rel=1e-9)
