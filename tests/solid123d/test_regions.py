"""The color-region invariant through every operation: leaves carry one
resolved color each, bodies never overlap, and the agreed precedence rule
(assigned beats uncolored; between assigned, the later operand wins) holds
for intersection and difference as it already did for union. color() is a
fill of uncolored bodies. scale()/mirror() keep the tree and its colors.
hull()/minkowski() announce that children's colors are dropped."""

import math

import pytest

import solid123d as s
from solid123d._common import own_rgba, total_volume, world_leaves

RED = (1.0, 0.0, 0.0, 1.0)
BLUE = (0.0, 0.0, 1.0, 1.0)


def bodies(shape) -> list[tuple[tuple | None, float]]:
    return sorted(
        ((own_rgba(b), round(total_volume(b), 6)) for b in world_leaves(shape)),
        key=lambda t: (t[0] is not None, t[0] or (), t[1]),
    )


def red_blue_overlap():
    """Red 10-cube at origin, blue 10-cube shifted 5 in x: overlap 500."""
    return s.union()(
        s.color("red")(s.cube(10)),
        s.color("blue")(s.translate([5, 0, 0])(s.cube(10))),
    )


class TestDifference:
    def test_retained_material_keeps_its_colors(self):
        d = s.difference()(red_blue_overlap(), s.translate([7, 0, 0])(s.cube(3)))
        plain = s.cube(10).fuse(s.translate([5, 0, 0])(s.cube(10))) - s.translate(
            [7, 0, 0]
        )(s.cube(3))
        assert total_volume(d) == pytest.approx(plain.volume, rel=1e-9)
        assert bodies(d) == [(BLUE, pytest.approx(1000 - 27)), (RED, 500.0)]

    def test_cutter_colors_are_ignored(self):
        d = s.difference()(s.color("red")(s.cube(10)), s.color("blue")(s.cube(5)))
        assert bodies(d) == [(RED, 1000 - 125)]

    def test_uncolored_base_gets_the_plain_cut(self):
        d = s.difference()(s.cube(10), s.color("red")(s.cube(5)))
        assert d.children == () or len(d.children) == 0
        assert own_rgba(d) is None and d.volume == pytest.approx(875)

    def test_fully_removed_body_disappears(self):
        d = s.difference()(red_blue_overlap(), s.translate([5, -1, -1])(s.cube(12)))
        assert bodies(d) == [(RED, 500.0)]

    def test_difference_of_a_moved_colored_tree_cuts_in_world_coordinates(self):
        moved = s.translate([0, 0, 100])(red_blue_overlap())
        d = s.difference()(moved, s.translate([7, 0, 100])(s.cube(3)))
        assert bodies(d) == [(BLUE, pytest.approx(1000 - 27)), (RED, 500.0)]
        assert d.bounding_box().min.Z == pytest.approx(100)


class TestIntersection:
    def test_red_with_uncolored_is_red(self):
        i = s.intersection()(
            s.color("red")(s.cube(10)), s.translate([5, 0, 0])(s.cube(10))
        )
        assert bodies(i) == [(RED, 500.0)]

    def test_uncolored_with_red_is_red(self):
        i = s.intersection()(
            s.cube(10), s.color("red")(s.translate([5, 0, 0])(s.cube(10)))
        )
        assert bodies(i) == [(RED, 500.0)]

    def test_red_with_blue_is_blue(self):
        i = s.intersection()(
            s.color("red")(s.cube(10)),
            s.color("blue")(s.translate([5, 0, 0])(s.cube(10))),
        )
        assert bodies(i) == [(BLUE, 500.0)]

    def test_partitioned_operand_keeps_its_regions(self):
        i = s.intersection()(red_blue_overlap(), s.translate([2, 0, 0])(s.cube(10)))
        # red keeps x 2..5 (150), blue keeps x 5..12 (700)
        assert bodies(i) == [(BLUE, 700.0), (RED, 300.0)]

    def test_uncolored_operands_take_the_plain_path(self):
        i = s.intersection()(s.cube(10), s.translate([5, 0, 0])(s.cube(10)))
        assert own_rgba(i) is None and i.volume == pytest.approx(500)


class TestColorFill:
    def test_fills_only_uncolored_bodies(self):
        inner = s.union()(
            s.color("red")(s.cube(10)), s.translate([20, 0, 0])(s.cube(5))
        )
        painted = s.color("blue")(inner)
        assert bodies(painted) == [(BLUE, 125.0), (RED, 1000.0)]

    def test_tree_node_itself_stays_uncolored(self):
        painted = s.color("blue")(s.cube(10), s.translate([20, 0, 0])(s.cube(5)))
        assert own_rgba(painted) is None or not painted.children
        for leaf in world_leaves(painted):
            assert own_rgba(leaf) == BLUE

    def test_single_body_is_colored_directly(self):
        c = s.color("red")(s.cube(10))
        assert own_rgba(c) == RED and c.label == "red"


class TestTransforms:
    def test_scale_keeps_the_tree_and_colors(self):
        sc = s.scale(2)(red_blue_overlap())
        assert bodies(sc) == [(BLUE, 8000.0), (RED, 4000.0)]
        assert sc.bounding_box().max.X == pytest.approx(30)

    def test_mirror_keeps_the_tree_and_colors(self):
        m = s.mirror([1, 0, 0])(red_blue_overlap())
        assert bodies(m) == [(BLUE, 1000.0), (RED, 500.0)]
        assert m.bounding_box().min.X == pytest.approx(-15)

    def test_resize_keeps_colors(self):
        r = s.resize([30, 10, 10])(red_blue_overlap())
        assert bodies(r) == [(BLUE, 2000.0), (RED, 1000.0)]

    def test_scale_of_a_moved_tree_scales_world_positions(self):
        sc = s.scale(2)(s.translate([0, 0, 10])(red_blue_overlap()))
        assert sc.bounding_box().min.Z == pytest.approx(20)
        assert bodies(sc) == [(BLUE, 8000.0), (RED, 4000.0)]

    def test_uncolored_scale_is_the_plain_fast_path(self):
        sc = s.scale(2)(s.cube(10))
        assert sc.volume == pytest.approx(8000) and not sc.children


class TestNewMaterial:
    def test_hull_of_colored_children_warns_and_drops_colors(self):
        with pytest.warns(UserWarning, match="hull\\(\\) creates new material"):
            h = s.hull()(
                s.color("red")(s.sphere(3)), s.translate([10, 0, 0])(s.sphere(3))
            )
        assert own_rgba(h) is None

    def test_enclosing_color_fills_the_hull(self):
        with pytest.warns(UserWarning):
            h = s.color("blue")(
                s.hull()(
                    s.color("red")(s.sphere(3)), s.translate([10, 0, 0])(s.sphere(3))
                )
            )
        assert own_rgba(h) == BLUE

    def test_uncolored_hull_is_silent(self):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            s.hull()(s.sphere(3), s.translate([10, 0, 0])(s.sphere(3)))


def test_volumes_never_change_through_a_colored_pipeline():
    model = s.difference()(
        s.color("green")(
            s.union()(red_blue_overlap(), s.translate([0, 20, 0])(s.cube(6)))
        ),
        s.translate([3, 3, -1])(s.cylinder(r=1.5, h=20)),
    )
    plain = s.cube(10).fuse(
        s.translate([5, 0, 0])(s.cube(10)), s.translate([0, 20, 0])(s.cube(6))
    ) - s.translate([3, 3, -1])(s.cylinder(r=1.5, h=20))
    assert math.isclose(total_volume(model), plain.volume, rel_tol=1e-9)
    colors = {tuple(round(v, 3) for v in rgba) for rgba, _ in bodies(model)}
    assert colors == {RED, BLUE, (0.0, 0.502, 0.0, 1.0)}


class TestAgreedContract:
    """The precedence table the project settled on, row by row.

    A is a 10-cube at the origin, B the same cube shifted 5 in x: 500 of
    A-only, 500 shared, 500 of B-only. Every row asserts the whole
    partition, so a rule that moved material to the wrong body fails even
    though the total is right.
    """

    @staticmethod
    def a():
        return s.cube(10)

    @staticmethod
    def b():
        return s.translate([5, 0, 0])(s.cube(10))

    def test_red_a_union_uncolored_b(self):
        u = s.union()(s.color("red")(self.a()), self.b())
        assert bodies(u) == [(None, 500.0), (RED, 1000.0)]

    def test_red_a_union_blue_b(self):
        u = s.union()(s.color("red")(self.a()), s.color("blue")(self.b()))
        assert bodies(u) == [(BLUE, 1000.0), (RED, 500.0)]

    def test_blue_b_union_red_a(self):
        u = s.union()(s.color("blue")(self.b()), s.color("red")(self.a()))
        assert bodies(u) == [(BLUE, 500.0), (RED, 1000.0)]

    def test_red_a_intersect_uncolored_b(self):
        i = s.intersection()(s.color("red")(self.a()), self.b())
        assert bodies(i) == [(RED, 500.0)]

    def test_red_a_intersect_blue_b(self):
        i = s.intersection()(s.color("red")(self.a()), s.color("blue")(self.b()))
        assert bodies(i) == [(BLUE, 500.0)]

    def test_red_a_minus_blue_b(self):
        d = s.difference()(s.color("red")(self.a()), s.color("blue")(self.b()))
        assert bodies(d) == [(RED, 500.0)]

    def test_every_row_conserves_total_volume(self):
        for result, expected in (
            (s.union()(s.color("red")(self.a()), self.b()), 1500.0),
            (s.union()(s.color("red")(self.a()), s.color("blue")(self.b())), 1500.0),
            (s.intersection()(s.color("red")(self.a()), self.b()), 500.0),
            (
                s.difference()(s.color("red")(self.a()), s.color("blue")(self.b())),
                500.0,
            ),
        ):
            assert total_volume(result) == pytest.approx(expected, rel=1e-9)
