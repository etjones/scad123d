"""The invariant a cut must satisfy: it may not keep material inside the
shapes it cut with.

This is a statement about the answer rather than about how OCCT reached
it, so checking it needs no reference render and no tolerance to compare
against. It is what replaced a blanket fuzzy tolerance: that fixed the
sphere below and regressed a model of small meshed hulls, because a fixed
distance means something different at every scale. Retrying only when the
invariant is violated, with fuzzy values scaled to the model, fixes the
sphere and leaves well-behaved cuts untouched.
"""

import math

import pytest

import solid123d as s
from solid123d._common import _cut as plain_cut
from solid123d._common import (
    cut_all,
    extent,
    interior_point,
    material_left_in_tools,
)


def sphere_and_lower_half():
    """The failing case: OCCT splits this correctly and then keeps the half
    it was told to remove, returning the sphere's whole volume."""
    return (
        s.translate([0, 0, -100])(s.sphere(r=109.659, segments=128)),
        s.translate([-109.659, -109.659, -209.659])(
            s.cube([219.317, 219.317, 209.659])
        ),
    )


class TestInteriorPoint:
    def test_finds_a_point_inside_a_box(self):
        box = s.cube(10, center=True)
        point = interior_point(box)
        assert point is not None
        assert abs(point.X) < 5 and abs(point.Y) < 5 and abs(point.Z) < 5

    def test_finds_a_point_inside_a_shape_whose_centre_is_not(self):
        """A C-shape: its centre of mass sits in the notch, outside the
        material, so the centre alone would answer wrongly."""
        c_shape = s.difference()(
            s.cube([30, 30, 10], center=True),
            s.translate([10, 0, 0])(s.cube([20, 10, 20], center=True)),
        )
        point = interior_point(c_shape.solids()[0])
        assert point is not None
        assert material_left_in_tools(c_shape, [s.cube([30, 30, 10], center=True)]), (
            "a point genuinely inside the C must classify inside its own bounds"
        )


class TestTheInvariant:
    def test_flags_a_cut_that_kept_the_half_it_removed(self):
        sphere, half = sphere_and_lower_half()
        kept = plain_cut([sphere], [half])
        assert extent(kept) == pytest.approx(extent(sphere), rel=1e-9)
        assert material_left_in_tools(kept, [half])

    def test_passes_a_cavity(self):
        """The tool wholly inside the argument: the result surrounds a void
        and no material is inside the tool."""
        tool = s.cube(4, center=True)
        result = plain_cut([s.cube(10, center=True)], [tool])
        assert not material_left_in_tools(result, [tool])

    def test_passes_a_partial_removal(self):
        tool = s.translate([5, 0, 0])(s.cube(10, center=True))
        result = plain_cut([s.cube(10, center=True)], [tool])
        assert extent(result) == pytest.approx(500)
        assert not material_left_in_tools(result, [tool])

    def test_passes_tools_that_miss(self):
        tool = s.translate([100, 0, 0])(s.cube(4))
        result = plain_cut([s.cube(10, center=True)], [tool])
        assert not material_left_in_tools(result, [tool])

    def test_no_tools_is_no_violation(self):
        assert not material_left_in_tools(s.cube(10), [])


class TestCutAll:
    def test_repairs_the_sphere(self):
        sphere, half = sphere_and_lower_half()
        result = cut_all([sphere], [half])
        assert not material_left_in_tools(result, [half])
        # the spherical cap above z=0: pi h^2 (3R - h) / 3, h = 9.659
        h, radius = 9.659, 109.659
        assert extent(result) == pytest.approx(
            math.pi * h * h * (3 * radius - h) / 3, rel=1e-3
        )

    def test_leaves_a_well_behaved_cut_exactly_alone(self):
        """No fuzzy value is applied when the plain cut already satisfies
        the invariant -- which is what keeps small, dense models safe."""
        args = [s.cube(10, center=True)]
        tools = [s.translate([5, 0, 0])(s.cube(10, center=True))]
        assert extent(cut_all(args, tools)) == extent(plain_cut(args, tools))

    def test_still_folds_when_one_pass_removes_nothing(self):
        """The other defect: five tools where any four cut correctly, and
        all five returned the minuend untouched. Fuzzy does not help;
        cutting one tool at a time does."""
        rot_y, rot_z = (
            math.degrees(math.acos(0.725194)),
            math.degrees(math.asin(0.207912)),
        )
        base = s.union()(
            s.translate([0, 0, -200.649])(
                s.cylinder(h=371.298, r1=0, r2=391.061, center=True, segments=400)
            ),
            s.translate([0, 0, -7.5])(
                s.cylinder(h=15, r=391.061, center=True, segments=400)
            ),
        )
        tools = [
            s.rotate([0, rot_y, 0])(s.cube([486.957, 802.123, 823.753], center=True)),
            s.rotate([0, rot_y, 0])(
                s.translate([225.479, 0, 0])(
                    s.cube([450.957, 782.123, 823.753], center=True)
                )
            ),
            s.rotate([0, 0, -rot_z])(
                s.translate([0, 391.061, 0])(
                    s.cube([792.123, 782.123, 823.753], center=True)
                )
            ),
            s.rotate([0, 0, rot_z])(
                s.translate([0, -391.061, 0])(
                    s.cube([792.123, 782.123, 823.753], center=True)
                )
            ),
            s.translate([0, 0, -183.649])(
                s.cylinder(h=371.298, r1=0, r2=391.061, center=True, segments=400)
            ),
        ]
        assert extent(plain_cut([base], tools)) == pytest.approx(
            extent(base), rel=1e-6
        ), "fixture guard: the single pass should still remove nothing here"
        assert extent(cut_all([base], tools)) == pytest.approx(65168, rel=0.01)

    def test_difference_goes_through_it(self):
        sphere, half = sphere_and_lower_half()
        assert extent(s.difference()(sphere, half)) < 0.01 * extent(sphere)
