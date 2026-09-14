"""Booleans whose operand is a compound of several bodies.

OCCT mishandles a compound of touching or overlapping bodies handed over as
one boolean operand: it reports success and returns nonsense. For solids a
cut came back larger than its argument; for faces a fuse of a two-face
sketch with a square returned a face of area 8e100, which is what silently
destroyed the 2D-morphology fillet idiom (Oskar Linde's `fillet()`, used by
a lot of Thingiverse code). Every boolean here decomposes both sides into
their own bodies first, which is equivalent by OCCT's own definition --
`(union of args) OP (union of tools)` -- and actually works.
"""

import math

import pytest
from build123d import Compound

import solid123d as s
from solid123d._common import bodies_of

AREA_TOL = 1e-6


def two_face_sketch() -> Compound:
    """A compound of two overlapping faces that OCCT keeps as two faces."""
    left = s.square([10, 10], center=True)
    right = s.translate([5, 0, 0])(s.square([10, 10], center=True))
    sketch = Compound([left, right])
    assert len(sketch.faces()) == 2, "the fixture must be a genuine two-face compound"
    return sketch


def two_solid_compound() -> Compound:
    left = s.cube(10, center=True)
    right = s.translate([5, 0, 0])(s.cube(10, center=True))
    compound = Compound([left, right])
    assert len(compound.solids()) == 2
    return compound


def area(shape) -> float:
    return sum(abs(f.area) for f in shape.faces())


def volume(shape) -> float:
    return sum(abs(x.volume) for x in shape.solids())


class TestBodiesOf:
    def test_a_2d_compound_decomposes_to_faces(self):
        assert len(bodies_of(two_face_sketch())) == 2

    def test_a_3d_compound_decomposes_to_solids(self):
        assert len(bodies_of(two_solid_compound())) == 2

    def test_a_single_body_is_itself(self):
        assert len(bodies_of(s.square(4))) == 1
        assert len(bodies_of(s.cube(4))) == 1


class TestTwoDimensional:
    """The overlapping region must be counted once, not twice or never."""

    MERGED = 150.0  # two 10-squares overlapping by 5 in x

    def test_cutting_by_a_multi_face_operand(self):
        big = s.square([100000, 100000], center=True)
        cut = s.difference()(big, two_face_sketch())
        assert len(cut.faces()) == 1
        assert area(cut) == pytest.approx(100000**2 - self.MERGED, rel=AREA_TOL)

    def test_fusing_with_a_multi_face_child(self):
        big = s.square([100000, 100000], center=True)
        fused = s.union()(big, two_face_sketch())
        assert area(fused) == pytest.approx(100000**2, rel=AREA_TOL)

    def test_intersecting_with_a_multi_face_operand(self):
        window = s.translate([-2, 0, 0])(s.square([4, 4], center=True))
        shared = s.intersection()(two_face_sketch(), window)
        # the window sits inside the left square only
        assert area(shared) == pytest.approx(16.0, rel=AREA_TOL)

    def test_a_multi_face_operand_on_either_side_of_a_cut(self):
        cut = s.difference()(two_face_sketch(), s.square([4, 4], center=True))
        assert area(cut) == pytest.approx(self.MERGED - 16.0, rel=AREA_TOL)


class TestThreeDimensional:
    MERGED = 1500.0  # two 10-cubes overlapping by 5 in x

    def test_cutting_by_a_multi_solid_operand(self):
        big = s.cube([1000, 1000, 1000], center=True)
        cut = s.difference()(big, two_solid_compound())
        assert volume(cut) == pytest.approx(1000**3 - self.MERGED, rel=AREA_TOL)

    def test_fusing_with_a_multi_solid_child(self):
        big = s.cube([1000, 1000, 1000], center=True)
        assert volume(s.union()(big, two_solid_compound())) == pytest.approx(
            1000**3, rel=AREA_TOL
        )

    def test_intersecting_with_a_multi_solid_operand(self):
        window = s.translate([-2, 0, 0])(s.cube([4, 4, 4], center=True))
        assert volume(s.intersection()(two_solid_compound(), window)) == pytest.approx(
            64.0, rel=AREA_TOL
        )


class TestFilletIdiom:
    """The real regression: Oskar Linde's 2D `fillet()`, which rounds a
    shape by inverting it against a huge square, offsetting, and inverting
    back. Every step hands a multi-face sketch to a boolean, and the huge
    square makes any failure enormous rather than subtle -- the tweezers
    model that found this reported 6e10 against OpenSCAD's 7802.
    """

    BIG = 100000

    def tweezer_profile(self):
        """A circle joined by two mirrored legs: three overlapping pieces,
        which OCCT keeps as a two-face sketch."""
        points = [
            (2.5, 0),
            (11, 105),
            (11, 135),
            (6.2, 150),
            (6, 150),
            (6, 105),
            (-0.005, 5),
            (-0.005, 0),
        ]
        leg = s.polygon(points)
        return s.union()(s.circle(2.5), leg, s.mirror([1, 0, 0])(s.polygon(points)))

    def outset(self, shape):
        return s.minkowski()(s.circle(2.5), shape)

    def inverse(self, shape):
        return s.difference()(s.square([self.BIG, self.BIG], center=True), shape)

    def test_a_union_of_overlapping_2d_children_merges_to_one_face(self):
        profile = self.tweezer_profile()
        assert len(profile.faces()) == 1
        assert area(profile) == pytest.approx(1205.82, rel=1e-3)

    def test_fillet_stays_the_size_of_the_shape(self):
        profile = self.tweezer_profile()
        filleted = self.inverse(
            s.minkowski()(s.circle(2.5), self.inverse(self.outset(profile)))
        )

        # The bug returned the whole 1e5 square (1e10) instead of the shape.
        assert area(filleted) < 2 * area(profile)
        # OpenSCAD renders this same construction at 1300.42.
        assert area(filleted) == pytest.approx(1300.42, rel=1e-3)
        box = filleted.bounding_box()
        assert (box.min.X, box.max.X) == pytest.approx((-11.0, 11.0), abs=0.01)
        assert (box.min.Y, box.max.Y) == pytest.approx((-2.5, 150.0), abs=0.01)

    def test_a_single_inversion_removes_the_shape_from_the_square(self):
        profile = self.tweezer_profile()
        hole = self.inverse(profile)
        assert len(hole.faces()) == 1
        assert self.BIG**2 - area(hole) == pytest.approx(area(profile), rel=1e-6)

    def test_extruding_the_fillet_matches_openscad(self):
        profile = self.tweezer_profile()
        filleted = self.inverse(
            s.minkowski()(s.circle(2.5), self.inverse(self.outset(profile)))
        )
        body = s.linear_extrude(height=6)(filleted)
        assert volume(body) == pytest.approx(7802.51, rel=1e-3)
        assert math.isclose(volume(body), area(filleted) * 6, rel_tol=1e-6)
