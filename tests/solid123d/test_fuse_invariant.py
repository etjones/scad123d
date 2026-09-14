"""The two things a union cannot do, and what happens when OCCT does them.

A union joins material: it can never return more separate pieces than it
was given, and its pieces can never overlap. Both are statements about
the answer, so they need no reference render -- which is the point, since
the volume guard's interval (largest input .. sum of inputs) is wide
enough to hold either failure.
"""

import warnings

import pytest
from build123d import Box, Compound, Pos, Rectangle, Solid

from solid123d._common import (
    bodies_overlap,
    fuse_bodies,
    gained_bodies,
    pieces_of,
)


def test_a_union_of_overlapping_boxes_is_one_body():
    fused = fuse_bodies([Box(10, 10, 10), Pos(5, 0, 0) * Box(10, 10, 10)])
    assert len(fused.solids()) == 1
    assert fused.volume == pytest.approx(1500)


def test_disjoint_parts_stay_separate_and_are_not_flagged():
    parts = [Pos(20 * i, 0, 0) * Box(10, 10, 10) for i in range(4)]
    fused = fuse_bodies(parts)
    assert len(fused.solids()) == 4
    assert fused.volume == pytest.approx(4000)
    assert not bodies_overlap(fused)
    assert not gained_bodies(parts, fused)


def test_touching_parts_do_not_count_as_overlapping():
    """A part sitting exactly in a pocket cut for it shares a face with
    its neighbour. Sharing a surface is not sharing material, and the
    check must not confuse the two."""
    left = Box(10, 10, 10)
    right = Pos(10, 0, 0) * Box(10, 10, 10)
    assert not bodies_overlap(Compound([left, right]))


def test_overlapping_bodies_are_detected():
    """What OCCT hands back when a fuse quietly does nothing: the operands
    themselves, still on top of each other."""
    unfused = Compound([Box(10, 10, 10), Pos(5, 0, 0) * Box(10, 10, 10)])
    assert bodies_overlap(unfused)


def test_a_result_in_more_pieces_than_it_was_given_is_detected():
    given = [Box(10, 10, 10), Pos(5, 0, 0) * Box(10, 10, 10)]
    shattered = Compound([Pos(30 * i, 0, 0) * Box(1, 1, 1) for i in range(3)])
    assert gained_bodies(given, shattered)
    assert not gained_bodies(given, Compound(given))


def test_one_body_can_never_violate_either_check():
    single = Box(10, 10, 10)
    assert not bodies_overlap(single)
    assert not gained_bodies([single, single], single)


class TestRetry:
    """A violation is retried with a fuzzy tolerance before it is believed."""

    @staticmethod
    def operands():
        return [Box(10, 10, 10), Pos(5, 0, 0) * Box(10, 10, 10)]

    def test_a_failed_fuse_is_retried_and_the_good_result_adopted(self, monkeypatch):
        import solid123d._common as common

        real = common._fuse
        seen: list[float | None] = []

        def flaky(bodies, fuzz=None):
            seen.append(fuzz)
            if fuzz is None:  # the exact pass "fails": operands unmerged
                return Compound(list(bodies))
            return real(bodies, fuzz=fuzz)

        monkeypatch.setattr(common, "_fuse", flaky)
        fused = fuse_bodies(self.operands())
        assert fused.volume == pytest.approx(1500)
        assert len(fused.solids()) == 1
        assert seen[0] is None and seen[1] is not None

    def test_a_fuse_that_cannot_be_repaired_warns_rather_than_hiding(self, monkeypatch):
        import solid123d._common as common

        def always_unmerged(bodies, fuzz=None):
            return Compound(list(bodies))

        monkeypatch.setattr(common, "_fuse", always_unmerged)
        with pytest.warns(UserWarning, match="more pieces than it was given"):
            fused = fuse_bodies(self.operands())
        assert len(fused.solids()) == 2  # kept, but announced

    def test_a_correct_fuse_is_never_retried(self, monkeypatch):
        import solid123d._common as common

        real = common._fuse
        calls = [0]

        def counting(bodies, fuzz=None):
            calls[0] += 1
            return real(bodies, fuzz=fuzz)

        monkeypatch.setattr(common, "_fuse", counting)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            fuse_bodies(self.operands())
        assert calls[0] == 1

    def test_a_rung_that_throws_does_not_end_the_ladder(self, monkeypatch):
        """OCCT throws on some fuzzy values (a null shape, an empty
        sequence). That rung does not apply; the next one may still work."""
        import solid123d._common as common

        real = common._fuse
        attempts = [0]

        def throwing(bodies, fuzz=None):
            attempts[0] += 1
            if fuzz is None:
                return Compound(list(bodies))
            if attempts[0] == 2:
                raise ValueError("Null TopoDS_Shape object")
            return real(bodies, fuzz=fuzz)

        monkeypatch.setattr(common, "_fuse", throwing)
        fused = fuse_bodies(self.operands())
        assert fused.volume == pytest.approx(1500)
        assert attempts[0] == 3


def test_union_of_a_solid_with_an_exact_duplicate_of_itself():
    """The corpus case in miniature: a loop that lays its last copy on its
    first. The duplicate adds nothing and must not be left lying on top."""
    bar = Solid.make_box(2, 20, 2)
    fused = fuse_bodies([bar, Solid.make_box(2, 20, 2)])
    assert fused.volume == pytest.approx(80)
    assert not bodies_overlap(fused)


class TestTwoDimensional:
    """A 2D union can come back unmerged just as a 3D one can, and a sheet
    has no solids for the checks to look at -- so both work on faces when
    there are none, the same distinction ``extent`` draws.

    Defensive rather than corpus-driven: no model in the CodeCAD corpus
    currently exercises it. The stack of circles that looked like a 2D
    union failure turned out to be a transform bug instead.
    """

    @staticmethod
    def squares():
        return [Rectangle(10, 10), Pos(5, 0, 0) * Rectangle(10, 10)]

    def test_overlapping_coplanar_faces_are_detected(self):
        from build123d import Compound

        unmerged = Compound(self.squares())
        assert bodies_overlap(unmerged)

    def test_a_correct_2d_union_is_one_face_and_passes(self):
        fused = fuse_bodies(self.squares())
        assert len(fused.faces()) == 1
        assert fused.area == pytest.approx(150)
        assert not bodies_overlap(fused)

    def test_faces_side_by_side_are_not_overlapping(self):
        from build123d import Compound

        apart = Compound([Rectangle(10, 10), Pos(20, 0, 0) * Rectangle(10, 10)])
        assert not bodies_overlap(apart)

    def test_faces_sharing_only_an_edge_are_not_overlapping(self):
        from build123d import Compound

        touching = Compound([Rectangle(10, 10), Pos(10, 0, 0) * Rectangle(10, 10)])
        assert not bodies_overlap(touching)

    def test_more_faces_out_than_in_is_detected(self):
        from build123d import Compound

        given = self.squares()
        shattered = Compound([Pos(30 * i, 0, 0) * Rectangle(1, 1) for i in range(3)])
        assert gained_bodies(given, shattered)

    def test_a_crescent_is_sampled_from_inside(self):
        """A face whose centre falls outside it -- the centre of a crescent
        is in the bite. Sampling only the centre would miss the overlap."""
        from build123d import Circle, Compound

        crescent = Circle(10) - Pos(6, 0, 0) * Circle(8)
        assert crescent.faces()
        assert bodies_overlap(Compound([crescent, Circle(10)]))

    def test_3d_results_still_use_solids(self):
        """A solid has faces too; they must not be mistaken for pieces."""
        box = Box(10, 10, 10)
        assert len(pieces_of(box)) == 1


class TestJunkBodies:
    """OCCT signals an outright failure by including a body whose bounding
    box spans 1e100 and which holds no volume.

    Such a body is not merely wrong, it is contagious: the funnel in the
    corpus fused a cone with a thin angled rib, got one of these plus a
    cone that measured correctly but was internally damaged, and the
    difference that followed returned 14,014 where the shell is 4,059.
    """

    @staticmethod
    def junk():
        """A stand-in with the signature of one: an absurd extent."""
        return Solid.make_box(1e60, 1e60, 1e60)

    def test_an_absurd_extent_counts_as_damage(self):
        from build123d import Compound

        from solid123d._common import damaged

        assert damaged(Compound([Box(10, 10, 10), self.junk()]))

    def test_ordinary_geometry_is_not_damaged(self):
        from build123d import Compound

        from solid123d._common import damaged

        assert not damaged(Compound([Box(10, 10, 10), Pos(50, 0, 0) * Box(2, 2, 2)]))
        assert not damaged(Box(10, 10, 10))

    def test_a_union_that_cannot_be_repaired_returns_its_operands(self, monkeypatch):
        """The operands are still the right material in the right places,
        only unjoined, and every boolean downstream decomposes its operands
        anyway. That beats handing back a damaged shape."""
        import solid123d._common as common

        def always_damaged(bodies, fuzz=None):
            from build123d import Compound

            return Compound([*bodies, TestJunkBodies.junk()])

        monkeypatch.setattr(common, "_fuse", always_damaged)
        given = [Box(10, 10, 10), Pos(5, 0, 0) * Box(10, 10, 10)]
        with pytest.warns(UserWarning, match="returned unjoined"):
            result = fuse_bodies(given)
        kept = [s for s in result.solids() if s.bounding_box().diagonal < 1e50]
        assert len(kept) == 2
        assert sum(s.volume for s in kept) == pytest.approx(2000)
