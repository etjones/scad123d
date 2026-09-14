"""An N-ary cut that silently removes nothing.

One OCCT Cut with every subtrahend at once is much cheaper than folding
tool by tool -- one pass over the argument instead of one per tool -- and
it is what keeps a model with many subtrahends from crawling. But OCCT can
hand the argument straight back: found on a CodeCAD model whose five tools
included a cone, where four cut correctly and adding the fifth returned
the minuend untouched. The plausibility guard cannot catch it, because a
cut that removes nothing is perfectly legitimate when the tools miss.

So `cut_all` folds only when the fast path removed *exactly* nothing, and
keeps the fold only if it removed something.
"""

import math

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

import solid123d as s
from solid123d._common import boolean, cut_all, extent

# The angles the model's matrices encode.
ROT_Y = math.degrees(math.acos(0.725194))
ROT_Z = math.degrees(math.asin(0.207912))


def cone_and_disk():
    """The model's minuend: a wide cone capped by a disk, touching at z=-15."""
    return s.union()(
        s.translate([0, 0, -200.649])(
            s.cylinder(h=371.298, r1=0, r2=391.061, center=True, segments=400)
        ),
        s.translate([0, 0, -7.5])(
            s.cylinder(h=15, r=391.061, center=True, segments=400)
        ),
    )


def five_tools():
    """The model's five subtrahends: four big rotated boxes and a cone."""
    return [
        s.rotate([0, ROT_Y, 0])(s.cube([486.957, 802.123, 823.753], center=True)),
        s.rotate([0, ROT_Y, 0])(
            s.translate([225.479, 0, 0])(
                s.cube([450.957, 782.123, 823.753], center=True)
            )
        ),
        s.rotate([0, 0, -ROT_Z])(
            s.translate([0, 391.061, 0])(
                s.cube([792.123, 782.123, 823.753], center=True)
            )
        ),
        s.rotate([0, 0, ROT_Z])(
            s.translate([0, -391.061, 0])(
                s.cube([792.123, 782.123, 823.753], center=True)
            )
        ),
        s.translate([0, 0, -183.649])(
            s.cylinder(h=371.298, r1=0, r2=391.061, center=True, segments=400)
        ),
    ]


class TestExtent:
    def test_measures_volume_for_solids(self):
        assert extent(s.cube(3)) == pytest.approx(27)

    def test_measures_area_for_2d(self):
        assert extent(s.square(3)) == pytest.approx(9)

    def test_a_multi_body_compound_is_summed(self):
        pair = s.union()(s.cube(2), s.translate([10, 0, 0])(s.cube(3)))
        assert extent(pair) == pytest.approx(8 + 27)


class TestSilentNoOp:
    """The regression itself, with the geometry that produces it."""

    def test_the_fast_path_really_does_nothing_here(self):
        """Guards the fixture: if OCCT ever fixes this, the test below stops
        proving anything and this one says so."""
        base = cone_and_disk()
        at_once = boolean([base], five_tools(), BRepAlgoAPI_Cut())
        assert extent(at_once) == pytest.approx(extent(base), rel=1e-12)

    def test_cut_all_removes_the_material_anyway(self):
        base = cone_and_disk()
        before = extent(base)
        result = cut_all([base], five_tools())
        assert extent(result) < 0.01 * before
        # OpenSCAD renders this same difference at 65,168.
        assert extent(result) == pytest.approx(65168, rel=0.01)

    def test_difference_uses_it(self):
        base = cone_and_disk()
        result = s.difference()(base, *five_tools())
        assert extent(result) == pytest.approx(65168, rel=0.01)


class TestNoFalsePositives:
    def test_tools_that_miss_leave_the_argument_alone(self):
        cube = s.cube(10, center=True)
        away = [
            s.translate([100, 0, 0])(s.cube(5)),
            s.translate([0, 100, 0])(s.cube(5)),
        ]
        assert extent(cut_all([cube], away)) == pytest.approx(1000)

    def test_a_single_tool_is_the_plain_cut(self):
        cube = s.cube(10, center=True)
        assert extent(cut_all([cube], [s.cube(4, center=True)])) == pytest.approx(
            1000 - 64
        )

    def test_ordinary_multi_tool_cuts_are_unchanged(self):
        cube = s.cube(10, center=True)
        tools = [
            s.translate([-5, 0, 0])(s.cube(4, center=True)),
            s.translate([5, 0, 0])(s.cube(4, center=True)),
        ]
        # each tool takes half of its 4-cube out of the big cube
        assert extent(cut_all([cube], tools)) == pytest.approx(1000 - 2 * 32)

    def test_a_tool_that_swallows_everything_leaves_nothing(self):
        cube = s.cube(10, center=True)
        tools = [s.cube(50, center=True), s.translate([100, 0, 0])(s.cube(5))]
        assert extent(cut_all([cube], tools)) == pytest.approx(0, abs=1e-9)
