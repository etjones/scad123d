import math
from pathlib import Path

import pytest
from build123d import GeomType, Shape

from solid123d import (
    circle,
    cube,
    cylinder,
    difference,
    hull,
    intersection,
    linear_extrude,
    minkowski,
    mirror,
    polygon,
    rotate,
    rotate_extrude,
    scad_render_to_file,
    scale,
    sphere,
    square,
    translate,
    union,
)
from solid123d.utils import up


def bbox(shape: Shape) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    bb = shape.bounding_box()
    return (
        (bb.min.X, bb.min.Y, bb.min.Z),
        (bb.max.X, bb.max.Y, bb.max.Z),
    )


def assert_close(a: tuple, b: tuple, tol: float = 1e-6) -> None:
    for x, y in zip(a, b):
        assert x == pytest.approx(y, abs=tol)


class TestPrimitives:
    def test_cube_corner_at_origin(self) -> None:
        lo, hi = bbox(cube(10))
        assert_close(lo, (0, 0, 0))
        assert_close(hi, (10, 10, 10))

    def test_cube_centered(self) -> None:
        lo, hi = bbox(cube(10, center=True))
        assert_close(lo, (-5, -5, -5))
        assert_close(hi, (5, 5, 5))

    def test_cube_vector_size(self) -> None:
        _lo, hi = bbox(cube([1, 2, 3]))
        assert_close(hi, (1, 2, 3))

    def test_sphere_volume(self) -> None:
        s = sphere(r=5)
        assert s.volume == pytest.approx(4 / 3 * math.pi * 125, rel=1e-6)
        assert sphere(d=10).volume == pytest.approx(s.volume, rel=1e-9)

    def test_cylinder_sits_on_z0(self) -> None:
        c = cylinder(r=2, h=10)
        lo, hi = bbox(c)
        assert lo[2] == pytest.approx(0)
        assert hi[2] == pytest.approx(10)
        assert c.volume == pytest.approx(math.pi * 4 * 10, rel=1e-6)

    def test_cylinder_centered(self) -> None:
        lo, hi = bbox(cylinder(r=2, h=10, center=True))
        assert lo[2] == pytest.approx(-5)
        assert hi[2] == pytest.approx(5)

    def test_cone_r1_r2(self) -> None:
        c = cylinder(r1=4, r2=0, h=9)
        assert c.volume == pytest.approx(math.pi * 16 * 9 / 3, rel=1e-6)

    def test_cylinder_openscad_keyword_style(self) -> None:
        c = cylinder(h=10, d=4)
        assert c.volume == pytest.approx(math.pi * 4 * 10, rel=1e-6)

    def test_square_and_circle(self) -> None:
        lo, hi = bbox(square([4, 2]))
        assert_close(lo, (0, 0, 0))
        assert_close(hi, (4, 2, 0))
        assert circle(r=3).area == pytest.approx(math.pi * 9, rel=1e-6)

    def test_polygon(self) -> None:
        tri = polygon([[0, 0], [10, 0], [0, 10]])
        assert tri.area == pytest.approx(50)

    def test_polygon_with_hole(self) -> None:
        shape = polygon(
            points=[[0, 0], [10, 0], [10, 10], [0, 10], [2, 2], [8, 2], [8, 8], [2, 8]],
            paths=[[0, 1, 2, 3], [4, 5, 6, 7]],
        )
        assert shape.area == pytest.approx(100 - 36)


class TestTransforms:
    def test_translate(self) -> None:
        lo, _ = bbox(translate([10, 0, 0])(cube(10, center=True)))
        assert_close(lo, (5, -5, -5))

    def test_translate_multiple_children_unions(self) -> None:
        shape = translate([0, 0, 5])(cube(2), cube(4))
        assert shape.volume == pytest.approx(64)
        lo, _ = bbox(shape)
        assert lo[2] == pytest.approx(5)

    def test_rotate_vector_openscad_order(self) -> None:
        # rotate([90, 0, 90]) applied to a unit-ish box off-axis:
        # X-rot first, then Z-rot, matching OpenSCAD's fixed-axis order.
        shape = rotate([90, 0, 90])(cube([1, 2, 3]))
        lo, hi = bbox(shape)
        assert_close(lo, (0, 0, 0), tol=1e-6)
        assert_close(hi, (3, 1, 2), tol=1e-6)

    def test_rotate_axis_angle(self) -> None:
        shape = rotate(a=45, v=[0, 0, 1])(cube(1, center=True))
        _, hi = bbox(shape)
        assert hi[0] == pytest.approx(math.sqrt(2) / 2, rel=1e-6)

    def test_rotate_scalar_is_about_z(self) -> None:
        shape = rotate(90)(translate([5, 0, 0])(cube(1, center=True)))
        lo, hi = bbox(shape)
        assert (lo[1] + hi[1]) / 2 == pytest.approx(5, abs=1e-6)

    def test_scale(self) -> None:
        shape = scale([2, 1, 0.5])(cube(10))
        _, hi = bbox(shape)
        assert_close(hi, (20, 10, 5))

    def test_scale_2d_vector_keeps_z(self) -> None:
        # OpenSCAD: scale([x, y]) implies z factor 1, not 0
        shape = scale([2, 3])(cube(10))
        _, hi = bbox(shape)
        assert_close(hi, (20, 30, 10))
        assert shape.volume == pytest.approx(6000)

    def test_mirror(self) -> None:
        shape = mirror([1, 0, 0])(translate([5, 0, 0])(cube(2)))
        lo, hi = bbox(shape)
        assert hi[0] == pytest.approx(-5)
        assert lo[0] == pytest.approx(-7)


class TestBooleans:
    def test_union(self) -> None:
        shape = union()(cube(10), translate([5, 0, 0])(cube(10)))
        assert shape.volume == pytest.approx(1500)

    def test_difference(self) -> None:
        shape = difference()(cube(10), translate([5, 0, 0])(cube(10)))
        assert shape.volume == pytest.approx(500)

    def test_intersection(self) -> None:
        shape = intersection()(cube(10), translate([5, 0, 0])(cube(10)))
        assert shape.volume == pytest.approx(500)

    def test_native_operators(self) -> None:
        a, b = cube(10), translate([5, 0, 0])(cube(10))
        assert (a + b).volume == pytest.approx(1500)
        assert (a - b).volume == pytest.approx(500)
        assert (a & b).volume == pytest.approx(500)

    def test_hull_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            hull()(cube(1), sphere(1))


class TestMinkowski:
    """minkowski()(A, sphere(r)) / minkowski()(A, circle(r)) -- a Minkowski
    sum with a ball -- is exactly offset(A, r), computed natively by build123d.
    This is the common real-world use of minkowski() (rounding a shape), so it
    is handled exactly instead of raising NotImplementedError.
    """

    @staticmethod
    def _steiner(
        volume: float, area: float, edges: list[tuple[float, float]], r: float
    ) -> float:
        return (
            volume
            + area * r
            + r * r / 2 * sum(length * angle for length, angle in edges)
            + (4 / 3) * math.pi * r**3
        )

    def test_box_plus_sphere_matches_steiner_formula(self) -> None:
        box = cube([20, 15, 10], center=True)
        shape = minkowski()(box, sphere(3))
        edges = (
            [(20, math.pi / 2)] * 4 + [(15, math.pi / 2)] * 4 + [(10, math.pi / 2)] * 4
        )
        expected = self._steiner(20 * 15 * 10, 2 * (300 + 200 + 150), edges, 3)
        assert shape.volume == pytest.approx(expected, rel=1e-9)

    def test_result_is_analytic_not_a_mesh(self) -> None:
        shape = minkowski()(cube([20, 15, 10], center=True), sphere(3))
        kinds = {f.geom_type for f in shape.faces()}
        assert kinds == {GeomType.PLANE, GeomType.CYLINDER, GeomType.SPHERE}

    def test_ball_may_come_first(self) -> None:
        """minkowski() is commutative; the ball need not be the last argument."""
        forward = minkowski()(cube([20, 15, 10], center=True), sphere(3))
        backward = minkowski()(sphere(3), cube([20, 15, 10], center=True))
        assert backward.volume == pytest.approx(forward.volume, rel=1e-9)

    def test_2d_square_plus_circle_is_a_2d_offset(self) -> None:
        """The 2D Steiner formula: area + perimeter*r + pi*r^2."""
        shape = minkowski()(square([10, 10], center=True), circle(2))
        expected = 100 + 40 * 2 + math.pi * 2**2
        assert shape.area == pytest.approx(expected, rel=1e-9)

    def test_non_ball_minkowski_still_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            minkowski()(cube(10), cube(2))

    def test_translated_sphere_does_not_match(self) -> None:
        """Only a bare, untransformed ball is recognized -- matching how the
        rounding idiom is actually written. A translated sphere is a
        different (and much rarer) Minkowski sum, so it correctly falls
        through to NotImplementedError rather than silently ignoring the
        translation.
        """
        with pytest.raises(NotImplementedError):
            minkowski()(cube(10), translate([5, 0, 0])(sphere(2)))


class TestExtrusions:
    def test_linear_extrude(self) -> None:
        shape = linear_extrude(height=5)(square([4, 3]))
        assert shape.volume == pytest.approx(60)
        lo, hi = bbox(shape)
        assert lo[2] == pytest.approx(0)
        assert hi[2] == pytest.approx(5)

    def test_linear_extrude_center(self) -> None:
        lo, hi = bbox(linear_extrude(height=4, center=True)(square(2)))
        assert lo[2] == pytest.approx(-2)
        assert hi[2] == pytest.approx(2)

    def test_linear_extrude_scale_tapers(self) -> None:
        shape = linear_extrude(height=6, scale=0.5)(square(4, center=True))
        _, hi = bbox(shape)
        assert hi[2] == pytest.approx(6)
        full = linear_extrude(height=6)(square(4, center=True))
        assert shape.volume < full.volume

    def test_linear_extrude_scale_disconnected_profile(self) -> None:
        # A profile of several disjoint faces (common in real models: pockets,
        # standoffs) must loft each island separately -- OCCT's loft rejects a
        # multi-face compound as a section with "BRep_API: command not done".
        profile = union()(
            translate([-6, 0])(square(2, center=True)),
            translate([6, 0])(square(2, center=True)),
        )
        shape = linear_extrude(height=4, scale=0.5)(profile)
        # each island is a frustum tapering about the global Z axis:
        # volume = h/3 * (A1 + A2 + sqrt(A1*A2)) with A1=4, A2=1 -> 28/3 each
        assert shape.volume == pytest.approx(2 * 28 / 3, rel=1e-6)

    def test_linear_extrude_ignores_face_winding_direction(self) -> None:
        # OpenSCAD's polygon()/linear_extrude() never look at point-winding
        # order -- it always sweeps toward +Z. build123d's own extrude()
        # instead sweeps each face along that face's own plane normal, which
        # IS winding-dependent. A profile assembled from faces with mixed
        # winding (e.g. a real model's BOSL2 stroke() ribbon unioned with
        # round_corners() joint fragments -- see scad123d's
        # ultimate-junction-box-cover repro) then partially extrudes
        # backward into -Z, splitting what should be one solid in two.
        ccw = polygon([[0, 0], [4, 0], [4, 4], [0, 4]])  # +Z normal
        cw = polygon([[6, 0], [6, 4], [10, 4], [10, 0]])  # -Z normal
        shape = linear_extrude(height=5)(union()(ccw, cw))
        lo, hi = bbox(shape)
        assert lo[2] == pytest.approx(0)
        assert hi[2] == pytest.approx(5)
        assert shape.volume == pytest.approx(2 * 4 * 4 * 5)

    def test_linear_extrude_scale_off_axis_scales_about_origin(self) -> None:
        # OpenSCAD scales the cross-section about the global Z axis, so an
        # off-axis island's top face migrates toward the axis rather than
        # shrinking in place.
        shape = linear_extrude(height=4, scale=0.5)(
            translate([6, 0])(square(2, center=True))
        )
        lo, hi = bbox(shape)
        # top square spans x in [2.5, 3.5]; bottom spans [5, 7]
        assert lo[0] == pytest.approx(2.5)
        assert hi[0] == pytest.approx(7)

    def test_rotate_extrude_torus(self) -> None:
        shape = rotate_extrude()(translate([10, 0, 0])(circle(r=2)))
        # torus volume: 2 * pi^2 * R * r^2
        assert shape.volume == pytest.approx(2 * math.pi**2 * 10 * 4, rel=1e-6)

    def test_rotate_extrude_partial(self) -> None:
        shape = rotate_extrude(angle=180)(translate([10, 0, 0])(circle(r=2)))
        assert shape.volume == pytest.approx(math.pi**2 * 10 * 4, rel=1e-6)


class TestRender:
    def test_scad_filename_becomes_step(self, tmp_path: Path) -> None:
        target = tmp_path / "c.scad"
        with pytest.warns(UserWarning):
            out = scad_render_to_file(cube(10), filename=str(target))
        assert out == str(tmp_path / "c.step")
        assert Path(out).exists()

    def test_stl_export(self, tmp_path: Path) -> None:
        out = scad_render_to_file(sphere(5), filepath=str(tmp_path / "s.stl"))
        assert Path(out).stat().st_size > 0

    def test_readme_example(self, tmp_path: Path) -> None:
        c = translate([10, 0, 0])(cube(10, center=True))
        with pytest.warns(UserWarning):
            scad_render_to_file(c, filename=str(tmp_path / "c.scad"))
        assert (tmp_path / "c.step").exists()


class TestTypingAliases:
    def test_aliases_are_shape(self) -> None:
        from solid123d import OpenSCADObject, OpenSCADObjectPlus

        assert OpenSCADObject is Shape
        assert OpenSCADObjectPlus is Shape
        assert isinstance(cube(1), OpenSCADObject)


class TestUtils:
    def test_up(self) -> None:
        lo, _ = bbox(up(3)(cube(1)))
        assert lo[2] == pytest.approx(3)


class TestSeamRobustBooleans:
    """build123d's plain +/- silently lose geometry when a curved face's
    parametric seam lies in the work region (gumyr/build123d#1428); the
    gated patch in occt_workarounds.py guards the clean step that causes
    it, everywhere in the process.
    """

    def test_difference_keeps_all_six_caps(self) -> None:
        # plain `-` drops the +X cap (the sphere's seam meridian) -> 73.30
        d = difference()(sphere(5), cube(8, center=True))
        assert d.volume == pytest.approx(87.9646, rel=1e-4)
        assert len([x for x in d.solids() if x.volume > 1e-6]) == 6

    def test_union_keeps_the_seam_side_material(self) -> None:
        # plain `+` loses the same material -> 551.64 and a non-solid export
        u = union()(sphere(5), cube(8, center=True))
        sphere_vol = 4 / 3 * math.pi * 125
        inter = intersection()(sphere(5), cube(8, center=True)).volume
        assert u.volume == pytest.approx(sphere_vol + 512 - inter, rel=1e-6)
        assert u.volume == pytest.approx(599.9646, rel=1e-4)
        assert len(u.solids()) == 1

    def test_union_output_stays_clean(self) -> None:
        # the raw fuzzy op leaves splitter faces; clean() merges them so
        # output matches what build123d's operators produce
        u = union()(cube(10), translate([5, 0, 0])(cube(10)))
        assert len(u.faces()) == 6


class TestNaryBooleans:
    """union()/difference() hand every operand to OCCT in one operation.
    A pairwise reduce re-processes the growing result at each step --
    quadratic once curved faces stop merging (200 overlapping cylinders:
    19.5 s pairwise, 1.1 s N-ary); every timeout in the CodeCAD corpus
    runs sat in that loop."""

    @staticmethod
    def _count_ops(monkeypatch) -> list[int]:
        from solid123d import occt_workarounds

        real = occt_workarounds._original_bool_op
        tools_per_call: list[int] = []

        def counting(self, args, tools, operation):
            tools_per_call.append(len(list(tools)))
            return real(self, args, list(tools), operation)

        monkeypatch.setattr(occt_workarounds, "_original_bool_op", counting)
        return tools_per_call

    def test_union_of_many_is_one_operation(self, monkeypatch) -> None:
        calls = self._count_ops(monkeypatch)
        parts = [translate([i * 8, 0, 0])(cube(10)) for i in range(6)]
        fused = union()(*parts)
        assert calls == [5]  # one fuse, five tools
        assert fused.volume == pytest.approx(10 * 10 * (8 * 5 + 10))

    def test_difference_of_many_is_one_operation(self, monkeypatch) -> None:
        calls = self._count_ops(monkeypatch)
        holes = [
            translate([x, 0, 0])(cylinder(r=1, h=30, center=True)) for x in (-6, 0, 6)
        ]
        plate = difference()(cube([20, 10, 4], center=True), *holes)
        assert calls == [3]
        assert plate.volume == pytest.approx(800 - 3 * math.pi * 4, rel=1e-6)

    def test_nary_union_matches_pairwise_on_overlapping_cylinders(self) -> None:
        from functools import reduce
        from operator import add

        parts = [
            translate([i * 7, (i % 2) * 3, 0])(cylinder(r=5, h=10)) for i in range(40)
        ]
        assert union()(*parts).volume == pytest.approx(
            reduce(add, parts).volume, rel=1e-6
        )
