"""Color preservation through group()/union(), and part labeling.

Ported from scad123d, where this logic was developed and verified against
real STEP output in slicers. Three regimes, gated on authored color:

- No color() anywhere: plain fuse, bit-identical to prior behavior (and no
  mass-property bookkeeping).
- Colored children with zero shared volume (disjoint, or touching along a
  surface): kept as a Compound of separate bodies, each with its own color
  and label -- what multi-material workflows need from STEP export.
- Colored children genuinely overlapping: partitioned into touching
  bodies. Later children claim contested volume; each earlier child
  keeps its color on whatever nothing later covers. Adjacent
  same-colored children fuse first, and if partitioning ever loses
  volume to a boolean glitch, the plain fuse is returned instead.
"""

import math

import pytest
from build123d import Box, Color, Compound, Location, Sphere

import solid123d as s


class TestColorLabels:
    def test_named_color_labels_with_the_authors_literal_name(self):
        assert s.color("red")(s.cube(1)).label == "red"
        assert s.color("SteelBlue")(s.cube(1)).label == "steelblue"

    def test_numeric_color_labels_with_css_name_when_exact(self):
        assert s.color([1, 0, 0])(s.cube(1)).label == "red"

    def test_numeric_color_without_a_name_labels_with_hex(self):
        assert s.color([0.2, 0.3, 0.4])(s.cube(1)).label == "#334c66"

    def test_existing_label_is_never_overwritten(self):
        shape = s.cube(1)
        shape.label = "my-part"
        colored = s.color("red")(shape)
        assert colored.label == "my-part"


class TestColorGroups:
    def test_disjoint_colored_children_keep_their_own_colors(self):
        u = s.union()(
            s.color("red")(s.cube(10)),
            s.color("blue")(s.translate([20, 0, 0])(s.cube(5))),
        )
        assert len(u.children) == 2
        (c1, c2) = u.children
        assert (str(c1.color), c1.label) == (
            "Color: (1.0, 0.0, 0.0, 1.0) is 'RED'",
            "red",
        )
        assert c2.label == "blue"
        assert u.volume == pytest.approx(1000 + 125, rel=1e-9)

    def test_touching_colored_children_stay_separate_bodies(self):
        u = s.union()(
            s.color("red")(s.cube(10)),
            s.color("blue")(s.translate([10, 0, 0])(s.cube(10))),
        )
        assert len(u.children) == 2
        assert u.volume == pytest.approx(2000, rel=1e-9)

    def test_touching_uncolored_children_still_fuse_to_one_solid(self):
        u = s.union()(s.cube(10), s.translate([10, 0, 0])(s.cube(10)))
        assert len(u.solids()) == 1
        assert u.volume == pytest.approx(2000, rel=1e-9)

    def test_uncolored_disjoint_children_get_the_plain_fuse(self):
        u = s.union()(s.cube(10), s.translate([20, 0, 0])(s.cube(5)))
        assert len(u.children) == 0
        assert len(u.solids()) == 2

    def test_overlapping_colored_children_partition_into_bodies(self):
        # Later children claim contested volume: red keeps cube-minus-blue,
        # blue survives whole. Total volume matches the plain fuse.
        u = s.union()(
            s.color("red")(s.cube(10)),
            s.color("blue")(s.translate([5, 5, 5])(s.cube(10))),
        )
        red, blue = u.children
        assert u.volume == pytest.approx(1000 + 1000 - 125, rel=1e-9)
        assert red.volume == pytest.approx(1000 - 125, rel=1e-9)
        assert blue.volume == pytest.approx(1000, rel=1e-9)
        assert (red.label, blue.label) == ("red", "blue")
        assert tuple(red.color) == pytest.approx((1.0, 0.0, 0.0, 1.0))
        assert tuple(blue.color) == pytest.approx((0.0, 0.0, 1.0, 1.0))

    def test_nested_color_overrides_the_outer_color(self):
        inner_blue = s.color("blue")(s.translate([20, 0, 0])(s.sphere(3)))
        outer = s.color("red")(s.cube(10), inner_blue)
        cube, sphere = outer.children
        # An enclosing color() fills what is still uncolored and leaves
        # explicit inner colors alone; the group node itself carries no
        # color, so structure and color stay independent.
        assert outer._color is None
        assert tuple(cube._color) == pytest.approx((1.0, 0.0, 0.0, 1.0))
        assert tuple(sphere._color) == pytest.approx((0.0, 0.0, 1.0, 1.0))
        assert outer.label == "red" and sphere.label == "blue"

    def test_an_assigned_color_beats_a_later_uncolored_child(self):
        # The motivating example, under the settled precedence: an
        # assigned color wins over uncolored material, so the red sphere
        # stays whole and the uncolored cube keeps only what is left.
        u = s.union()(
            s.color("red")(s.sphere(5)),
            s.cube(8, center=True),
        )
        red, base = u.children
        sphere_vol = 4 / 3 * math.pi * 125
        overlap = (s.sphere(5) & s.cube(8, center=True)).volume
        assert red.label == "red"
        assert red.volume == pytest.approx(sphere_vol, rel=1e-6)
        assert base._color is None
        assert base.volume == pytest.approx(512 - overlap, rel=1e-6)
        assert u.volume == pytest.approx(sphere_vol + 512 - overlap, rel=1e-6)

    def test_between_two_assigned_colors_the_later_child_wins(self):
        u = s.union()(
            s.color("blue")(s.cube(8, center=True)),
            s.color("red")(s.sphere(5)),
        )
        blue, red = u.children
        sphere_vol = 4 / 3 * math.pi * 125
        assert red.volume == pytest.approx(sphere_vol, rel=1e-6)
        assert blue.volume < 512

    def test_order_of_a_colored_child_does_not_matter_against_uncolored(self):
        # Same two bodies, colored child second: precedence is the color's,
        # not the position's, so the result is the same either way.
        u = s.union()(
            s.cube(8, center=True),
            s.color("red")(s.sphere(5)),
        )
        base, red = u.children
        sphere_vol = 4 / 3 * math.pi * 125
        overlap = (s.sphere(5) & s.cube(8, center=True)).volume
        assert red.volume == pytest.approx(sphere_vol, rel=1e-6)
        assert base.volume == pytest.approx(512 - overlap, rel=1e-6)

    def test_fully_covered_child_disappears(self):
        u = s.union()(
            s.color("red")(s.cube(2, center=True)),
            s.color("blue")(s.cube(10, center=True)),
        )
        # red is entirely inside blue: only the blue body remains
        assert u.label == "blue"
        assert u.volume == pytest.approx(1000, rel=1e-9)

    def test_adjacent_same_color_children_fuse_to_one_body(self):
        u = s.union()(
            s.color("red")(s.cube(10)),
            s.color("red")(s.translate([5, 0, 0])(s.cube(10))),
            s.color("blue")(s.translate([100, 0, 0])(s.cube(5))),
        )
        red, blue = u.children
        assert len(red.solids()) == 1
        assert red.volume == pytest.approx(1500, rel=1e-9)
        assert red.label == "red"
        assert blue.label == "blue"

    def test_uncolored_material_between_two_colors_is_claimed_by_both(self):
        # red x0..10, uncolored x5..15, blue x10..20. Both colors keep
        # their whole extent; the uncolored body is left with nothing, and
        # the total is unchanged.
        u = s.union()(
            s.color("red")(s.cube(10)),
            s.translate([5, 0, 0])(s.cube(10)),
            s.color("blue")(s.translate([10, 0, 0])(s.cube(10))),
        )
        red, blue = u.children
        assert (red.label, blue.label) == ("red", "blue")
        assert red.volume == pytest.approx(1000, rel=1e-9)
        assert blue.volume == pytest.approx(1000, rel=1e-9)
        assert u.volume == pytest.approx(2000, rel=1e-9)

    def test_mixed_2d_3d_children_warn_and_drop_the_2d(self):
        with pytest.warns(UserWarning, match="mixes 2D and 3D"):
            u = s.union()(s.cube(10), s.circle(5))
        assert u.volume == pytest.approx(1000, rel=1e-9)

    def test_color_applies_alpha(self):
        shape = s.color("red", alpha=0.5)(s.cube(1))
        assert tuple(shape.color) == pytest.approx((1.0, 0.0, 0.0, 0.5))
        assert math.isclose(tuple(shape.color)[3], 0.5)


def _colored(shape, rgb):
    shape.color = Color(*rgb)
    return shape


class TestPartitionUsesWorldCoordinates:
    """A moved Compound carries the move on itself and its children stay in
    the frame they were built in, so expanding a nested colored group
    without composing the ancestor location hands the partition bodies in
    the wrong place."""

    def test_a_moved_colored_group_partitions_where_it_sits(self):
        from solid123d._common import _color_leaves

        inner = Compound(
            children=[
                _colored(Box(10, 10, 10), (1, 0, 0)),
                _colored(Sphere(6), (1, 0, 1)),
            ]
        )
        moved = inner.moved(Location((20, 0, 0)))
        leaves = _color_leaves([moved])
        assert len(leaves) == 2
        for leaf in leaves:
            box = leaf.bounding_box()
            assert box.min.X > 5, (
                f"leaf left behind at x={box.min.X:.1f}; the partition would "
                "cut it against geometry 20mm away that it overlaps there"
            )

    def test_an_unmoved_group_is_unchanged(self):
        from solid123d._common import _color_leaves

        inner = Compound(
            children=[
                _colored(Box(10, 10, 10), (1, 0, 0)),
                _colored(Sphere(6), (1, 0, 1)),
            ]
        )
        leaves = _color_leaves([inner])
        assert len(leaves) == 2
        assert all(leaf.bounding_box().min.X < 0 for leaf in leaves)


class TestPartialColorSalvage:
    """Dropping every color because one body came up short is a heavy
    price: a model whose far group partitioned badly lost the colors of
    parts nowhere near it."""

    def test_the_unaccounted_material_is_carried_uncolored(self):
        from solid123d._common import checked, total_volume

        plain = Box(20, 10, 10)  # 2000
        covered = _colored(Box(10, 10, 10).moved(Location((-5, 0, 0))), (1, 0, 0))
        result = checked([covered], plain, "union")
        assert total_volume(result) == pytest.approx(2000, rel=1e-6)
        colors = (
            [b.color for b in result.children] if result.children else [result.color]
        )
        assert any(c is not None for c in colors), "the red body must survive"
        assert any(c is None for c in colors), "the remainder must be uncolored"

    def test_overlapping_bodies_still_drop_color(self):
        """Bodies that overlap would export doubled material, so correct
        geometry wins there and the plain result is returned."""
        from solid123d._common import checked, total_volume

        plain = Box(10, 10, 10)
        a = _colored(Box(10, 10, 10), (1, 0, 0))
        b = _colored(Box(10, 10, 10), (0, 0, 1))  # the same material twice
        with pytest.warns(UserWarning, match="lost volume"):
            result = checked([a, b], plain, "union")
        assert total_volume(result) == pytest.approx(1000, rel=1e-6)
