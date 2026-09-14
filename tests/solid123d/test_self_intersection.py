"""Naming what OCCT will not do, once a boolean has already failed.

A solid that passes BRepCheck can still intersect itself, and OCCT's
booleans are only defined on arguments that do not. `is_valid` does not
test for it, so such a shape calls itself valid right up to the point
where every cut of it comes back inside out.

`BOPAlgo_ArgumentAnalyzer` is the checker OCCT ships for the question. It
is a full intersection pass, as expensive as the boolean itself, so it
runs only after an invariant has failed and no repair has helped --
never as a precondition.
"""

import warnings

import pytest
from build123d import Box, Pos, Solid

from solid123d._common import cut_all, why_occt_struggled


class TestHealthyShapesSaySoNothing:
    """The diagnostic must find nothing to report on ordinary geometry,
    or it would turn every failure into a false accusation."""

    @pytest.mark.parametrize(
        "shape",
        [
            Box(10, 10, 10),
            Box(10, 10, 10) - Pos(2, 0, 0) * Box(4, 4, 20),
            Box(10, 10, 10) + Pos(5, 0, 0) * Box(10, 10, 10),
            Solid.make_cylinder(3, 10),
            Solid.make_sphere(5),
        ],
    )
    def test_nothing_to_report(self, shape):
        assert why_occt_struggled([shape]) == ""

    def test_several_at_once(self):
        assert why_occt_struggled([Box(1, 1, 1), Solid.make_sphere(2)]) == ""

    def test_an_empty_shape_is_skipped_not_crashed_on(self):
        class Empty:
            wrapped = None

        assert why_occt_struggled([Empty()]) == ""


class TestItRunsOnlyAfterAFailure:
    """The gate: a cut that satisfies its invariant must never pay for
    the analyzer."""

    def test_a_good_cut_never_asks(self, monkeypatch):
        import solid123d._common as common

        asked = []
        monkeypatch.setattr(
            common, "why_occt_struggled", lambda shapes: asked.append(1) or ""
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            result = cut_all([Box(10, 10, 10)], [Pos(2, 0, 0) * Box(4, 4, 20)])
        assert result.volume == pytest.approx(1000 - 160)
        assert asked == []

    def test_an_unrepairable_cut_does_ask(self, monkeypatch):
        import solid123d._common as common

        asked = []

        def explain(shapes):
            asked.append(len(list(shapes)))
            return " -- because of reasons"

        monkeypatch.setattr(common, "why_occt_struggled", explain)
        # a cut whose result always keeps material inside its tool
        monkeypatch.setattr(common, "material_left_in_tools", lambda r, t: True)
        from solid123d.errors import BooleanFailed

        with pytest.raises(BooleanFailed, match="because of reasons"):
            cut_all([Box(10, 10, 10)], [Pos(2, 0, 0) * Box(4, 4, 20)])
        assert asked, "the analyzer should have been consulted"


class TestWhatItSays:
    def test_it_names_the_defect_it_found(self, monkeypatch):
        """Phrased from the analyzer's own status codes."""

        class FakeResult:
            def __init__(self, status):
                self._status = status

            def GetCheckStatus(self):
                return self._status

        class FakeAnalyzer:
            SelfInterMode = SmallEdgeMode = False

            def SetShape1(self, s): ...
            def SetShape2(self, s): ...
            def Perform(self): ...
            def GetCheckResult(self):
                return [FakeResult("BOPAlgo_CheckStatus.BOPAlgo_SelfIntersect")]

        import OCP.BOPAlgo as bop

        monkeypatch.setattr(bop, "BOPAlgo_ArgumentAnalyzer", FakeAnalyzer)
        said = why_occt_struggled([Box(1, 1, 1)])
        assert "intersects itself" in said
        assert "BRepCheck does not test" in said
