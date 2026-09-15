"""The gated Shape.clean patch (see src/solid123d/occt_workarounds.py)
and its upstream canary."""

import pytest
from build123d import Box, Pos, Shape, Sphere

from solid123d import occt_workarounds
from solid123d._common import total_volume

EXPECTED_CUT = 87.9646
EXPECTED_FUSE = 599.9646
BROKEN_CUT = 73.3038


class TestPatchInstalled:
    def test_importing_solid123d_installs_the_guarded_clean(self) -> None:
        assert occt_workarounds.OCCT_SPHERE_SEAM_BUG_IS_UNFIXED
        assert Shape.clean is occt_workarounds._volume_guarded_clean

    def test_native_operators_are_seam_safe(self) -> None:
        # the whole point of patching the chokepoint: a user's own +/-
        # behave exactly like solid123d's union()/difference()
        assert (Sphere(5) - Box(8, 8, 8)).volume == pytest.approx(
            EXPECTED_CUT, rel=1e-4
        )
        assert (Sphere(5) + Box(8, 8, 8)).volume == pytest.approx(
            EXPECTED_FUSE, rel=1e-4
        )

    def test_guarded_clean_still_merges_splitter_faces(self) -> None:
        # clean()'s real benefit is preserved when it is safe
        fused = Box(10, 10, 10) + (Pos(5, 0, 0) * Box(10, 10, 10))
        assert len(fused.faces()) == 6

    def test_guard_accepts_integration_noise_but_not_lost_geometry(
        self, monkeypatch
    ) -> None:
        """Volumes integrated over a fragmented and a unified face set differ
        by ~1e-7 relative -- measured on a 52-wedge servo-horn gear from the
        CodeCAD corpus. At the original 1e-9 tolerance that noise rejected
        20 of 21 cleans, fragments piled up (26 -> 822 faces) and OCCT's
        fuse finally returned an inverted 8-face shape. The guard must let
        noise through and still catch the seam bug's ~17% loss."""
        import copy

        shape = Box(10, 10, 10)
        true_volume = shape.volume
        applied: list[float] = []

        def fake_clean(target: Shape, factor: float) -> Shape:
            # "clean" that returns a scaled copy: volume moves by factor^3
            scaled = copy.deepcopy(target).scale(factor)
            target.wrapped = scaled.wrapped
            applied.append(factor)
            return target

        for factor, expect_adopted in ((1 + 3e-8, True), (0.94, False)):
            monkeypatch.setattr(
                occt_workarounds,
                "_original_clean",
                lambda t, f=factor: fake_clean(t, f),
            )
            probe = Box(10, 10, 10)
            occt_workarounds._volume_guarded_clean(probe)
            moved = probe.volume != pytest.approx(true_volume, rel=1e-12)
            assert moved is expect_adopted, (factor, probe.volume)
        assert len(applied) == 2


class TestUpstreamCanary:
    def test_upstream_bug_still_present(self) -> None:
        """ATTENTION on failure: this test asserts the upstream bug still
        EXISTS. If it fails after a build123d/OCCT upgrade, upstream has
        fixed gumyr/build123d#1428 -- flip
        occt_workarounds.OCCT_SPHERE_SEAM_BUG_IS_UNFIXED to False (or
        delete the workaround) and delete this test.
        """
        occt_workarounds.uninstall()
        try:
            broken = (Sphere(5) - Box(8, 8, 8)).volume
        finally:
            occt_workarounds.install()
        assert broken == pytest.approx(BROKEN_CUT, rel=1e-4), (
            "Upstream clean() no longer loses seam-crossed geometry! "
            "Flip OCCT_SPHERE_SEAM_BUG_IS_UNFIXED to False and retire "
            "this workaround."
        )


class TestImplausibleBooleanRetry:
    def test_fuse_that_loses_material_is_retried_with_fuzzy_tolerance(
        self, monkeypatch
    ) -> None:
        """OCCT can return a valid-looking fuse with most of the material
        gone (an operand nearly coincident with the accumulated result).
        Simulate it: the exact fuse returns a sliver, the fuzzy retry the
        real union. The guarded op must notice from the input-implied
        bounds and adopt the retry."""
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

        a, b = Box(10, 10, 10), Pos(5, 0, 0) * Box(10, 10, 10)
        real = occt_workarounds._original_bool_op
        calls: list[float] = []

        def flaky(self, args, tools, operation):
            calls.append(operation.FuzzyValue())
            # build123d's own + already carries a small fuzzy value (1e-6);
            # only the guard's larger retry value counts as the retry.
            if isinstance(
                operation, BRepAlgoAPI_Fuse
            ) and operation.FuzzyValue() not in _retry_fuzz(a, b):
                return Box(1, 1, 1)  # "valid", but 1 mm^3 of a 1500 mm^3 union
            return real(self, args, tools, operation)

        monkeypatch.setattr(occt_workarounds, "_original_bool_op", flaky)
        fused = a + b
        assert fused.volume == pytest.approx(1500)
        # the exact pass, then the first rung of the ladder
        assert len(calls) == 2 and calls[-1] == _retry_fuzz(a, b)[0]

    def test_plausible_results_are_not_retried(self, monkeypatch) -> None:
        real = occt_workarounds._original_bool_op
        calls: list[float] = []

        def counting(self, args, tools, operation):
            calls.append(operation.FuzzyValue())
            return real(self, args, tools, operation)

        monkeypatch.setattr(occt_workarounds, "_original_bool_op", counting)
        a, b = Box(10, 10, 10), Box(4, 4, 4)
        assert (a - b).volume == pytest.approx(1000 - 64)
        assert (a & b).volume == pytest.approx(64)
        assert len(calls) == 2
        assert not set(calls) & set(_retry_fuzz(a, b))

    def test_unfixable_result_raises_rather_than_hiding(self, monkeypatch) -> None:
        """A sliver where the operands imply 1500 is not a near miss, and
        returning it hands the caller a shape a printer will make. The
        caller renders the subtree in OpenSCAD instead."""
        from solid123d.errors import BooleanFailed

        def always_sliver(self, args, tools, operation):
            return Box(1, 1, 1)

        monkeypatch.setattr(occt_workarounds, "_original_bool_op", always_sliver)
        with pytest.raises(BooleanFailed, match="implausible volume"):
            Box(10, 10, 10) + Pos(5, 0, 0) * Box(10, 10, 10)


def _retry_fuzz(*shapes) -> list[float]:
    """The fuzzy values the guard's ladder would try for these operands."""
    diagonal = max(s.bounding_box().diagonal for s in shapes)
    return [diagonal * f for f in occt_workarounds.BOOLEAN_RETRY_FRACTIONS]


class TestNestedCompoundOperands:
    def test_bounds_see_through_nested_compounds(self, monkeypatch) -> None:
        """A color-partitioned union is a Compound of Compounds, whose
        build123d .volume is 0. Bounds computed from that would flag every
        correct result as implausible (spurious fuzzy retries, warnings).
        Measured by leaf solids, a nested operand is just its solids."""
        from build123d import Compound

        nested = Compound(
            [Compound([Box(10, 10, 10)]), Compound([Pos(20, 0, 0) * Box(10, 10, 10)])]
        )
        assert nested.volume == 0  # the blind spot
        assert occt_workarounds._leaf_volume(nested) == pytest.approx(2000)
        real = occt_workarounds._original_bool_op
        calls: list[float] = []

        def counting(self, args, tools, operation):
            calls.append(operation.FuzzyValue())
            return real(self, args, tools, operation)

        monkeypatch.setattr(occt_workarounds, "_original_bool_op", counting)
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any "implausible" warning fails
            fused = nested + Pos(5, 0, 0) * Box(10, 10, 10)
        assert not set(calls) & set(_retry_fuzz(nested))  # no retry
        assert occt_workarounds._leaf_volume(fused) == pytest.approx(2500)

    def test_inverted_result_is_implausible(self) -> None:
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

        a, b = Box(10, 10, 10), Pos(5, 0, 0) * Box(10, 10, 10)
        bounds = occt_workarounds._volume_bounds(BRepAlgoAPI_Fuse(), [a], [b])
        assert bounds == pytest.approx((1000, 2000))
        assert not occt_workarounds._plausible(-1500, bounds)


class TestEmptyIntersectionIsChecked:
    """OCCT returns an empty Common for geometry it merely found hard. A
    thread built the way every OpenSCAD thread library builds one -- small
    polyhedra unioned, then trimmed by intersecting a box -- came back
    empty, and the model it came from was 18% short with no warning
    anywhere: the shapes were valid and BOPAlgo_ArgumentAnalyzer flagged
    nothing, because the sliver faces the union left are just above
    Precision::Confusion.
    """

    def test_shapes_that_cannot_meet_are_not_retried(self):
        """An empty answer from separated shapes is the right answer, and
        the bounding boxes settle it without paying for four retries."""
        from solid123d import booleans

        calls = []
        real = booleans.boolean

        def counted(args, tools, op):
            calls.append(op)
            return real(args, tools, op)

        original, booleans.boolean = booleans.boolean, counted
        try:
            out = booleans.intersection()(Box(1, 1, 1), Pos(50, 0, 0) * Box(1, 1, 1))
        finally:
            booleans.boolean = original
        assert total_volume(out) == 0
        assert len(calls) == 1, "a separated pair must not be retried"

    def test_overlapping_boxes_that_are_genuinely_empty_still_return_empty(self):
        """Overlapping bounding boxes are not overlapping shapes. Two
        L-shapes can share a box and touch nothing; the retries run and the
        empty answer stands, rather than becoming an error."""
        from solid123d import booleans

        a = Box(10, 2, 2)
        b = Pos(0, 8, 8) * Box(2, 10, 2)
        out = booleans.intersection()(a, b)
        assert total_volume(out) == pytest.approx(0, abs=1e-9)

    def test_an_ordinary_intersection_is_unchanged(self):
        from solid123d import booleans

        out = booleans.intersection()(Box(10, 10, 10), Pos(5, 0, 0) * Box(10, 10, 10))
        assert total_volume(out) == pytest.approx(500, rel=1e-9)
