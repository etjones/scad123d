"""Tier 1: build committed .csg fixtures. No OpenSCAD binary needed."""

import math
from collections import Counter

import pytest
from build123d import GeomType

import scad123d
from scad123d.parser import parse_csg
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
        with pytest.warns(UserWarning, match="unrecognised CSG node"):
            with pytest.raises(scad123d.UnsupportedNodeError):
                scad123d.import_csg("bogus_node(x = 1);")
