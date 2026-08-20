"""Tier 1: build committed .csg fixtures. No OpenSCAD binary needed."""

import math
from collections import Counter

import pytest
from build123d import GeomType

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
    """color() on more than one child of a group -- a real boolean fuse
    can't tell you which color survives once it's merged material away, so
    a disjoint (non-overlapping) group is returned as a Compound of its
    children instead, each keeping its own color -- geometrically identical
    to the fused shape, but grouping (unlike fusing) doesn't erase each
    child's own attributes. An overlapping group still falls back to a real
    fuse, matching the pre-existing (uncolored) behavior exactly.
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
        # pre-existing overlap-corrected volume is preserved.
        shape = scad123d.import_csg(
            "union() {\n"
            "\tcolor([1, 0, 0, 1]) { cube(size = [10, 10, 10], center = false); }\n"
            "\tcolor([0, 0, 1, 1]) {\n"
            "\t\tmultmatrix([[1, 0, 0, 5], [0, 1, 0, 5], [0, 0, 1, 5], [0, 0, 0, 1]]) {\n"
            "\t\t\tcube(size = [10, 10, 10], center = false);\n\t\t}\n\t}\n}"
        )
        assert len(shape.solids()) == 1
        assert shape.volume == pytest.approx(1000 + 1000 - 125, rel=1e-9)

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
