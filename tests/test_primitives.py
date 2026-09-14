"""Primitives: the face and fill rules OpenSCAD applies."""

import pytest

from solid123d import polygon, polyhedron


class TestNonPlanarPolyhedronFaces:
    """OpenSCAD's polyhedron() accepts a face whose vertices are not
    coplanar and tessellates it; OCCT will not build a planar face from a
    bent wire. Such a model produced no STEP at all -- 259 of the 274
    corpus models failing with "wires not planar" are polyhedra."""

    def test_a_bent_quad_face_builds(self):
        # A unit cube with one top corner lifted, so two side quads bend.
        points = [
            [0, 0, 0],
            [1, 0, 0],
            [1, 1, 0],
            [0, 1, 0],
            [0, 0, 1],
            [1, 0, 1],
            [1, 1, 1.5],
            [0, 1, 1],
        ]
        faces = [
            [0, 1, 2, 3],
            [4, 5, 1, 0],
            [7, 6, 5, 4],
            [5, 6, 2, 1],
            [6, 7, 3, 2],
            [7, 4, 0, 3],
        ]
        solid = polyhedron(points, faces)
        assert solid.volume > 0
        assert solid.is_valid

    def test_a_flat_face_is_still_one_face(self):
        """The fan is only for bent loops: a cube keeps six faces, not
        twelve, so nothing that already worked changes shape."""
        points = [
            [0, 0, 0],
            [1, 0, 0],
            [1, 1, 0],
            [0, 1, 0],
            [0, 0, 1],
            [1, 0, 1],
            [1, 1, 1],
            [0, 1, 1],
        ]
        faces = [
            [0, 1, 2, 3],
            [4, 5, 1, 0],
            [7, 6, 5, 4],
            [5, 6, 2, 1],
            [6, 7, 3, 2],
            [7, 4, 0, 3],
        ]
        solid = polyhedron(points, faces)
        assert len(solid.faces()) == 6
        assert solid.volume == pytest.approx(1.0)

    def test_a_bent_ngon_still_raises(self):
        """A fan is only verified against OpenSCAD for a quad. Beyond that
        OpenSCAD's constrained Delaunay picks different diagonals -- a bent
        pentagon came out 11% different -- and on a non-convex face a fan
        lays triangles outside the polygon. Raising keeps a wrong answer
        from being believed."""
        points = [
            [0, 0, 0], [10, 0, 0], [13, 8, 0], [5, 13, 0], [-3, 8, 0],
            [0, 0, 5], [10, 0, 5], [13, 8, 9], [5, 13, 5], [-3, 8, 5],
        ]
        faces = [
            [0, 1, 2, 3, 4], [9, 8, 7, 6, 5],
            [5, 6, 1, 0], [6, 7, 2, 1], [7, 8, 3, 2], [8, 9, 4, 3], [9, 5, 0, 4],
        ]
        with pytest.raises(ValueError, match="not planar"):
            polyhedron(points, faces)


class TestPolygonEvenOdd:
    """OpenSCAD tessellates 2D geometry with libtess2 under
    TESS_WINDING_ODD, so a path is solid when an odd number of paths
    enclose it. Treating paths[0] as the outline and subtracting the rest
    got everything but the simple ring wrong."""

    @staticmethod
    def square(half, cx=0.0):
        return [
            [cx - half, -half],
            [cx + half, -half],
            [cx + half, half],
            [cx - half, half],
        ]

    def area(self, points, paths):
        return polygon(points, paths).area

    def test_a_plain_ring_is_unchanged(self):
        pts = self.square(10) + self.square(5)
        assert self.area(pts, [[0, 1, 2, 3], [4, 5, 6, 7]]) == pytest.approx(300)

    def test_a_path_nested_in_a_hole_is_solid_again(self):
        """Three nested squares measured 500 against OpenSCAD's 600."""
        pts = self.square(15) + self.square(10) + self.square(5)
        area = self.area(pts, [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]])
        assert area == pytest.approx(900 - 400 + 100)

    def test_nesting_keeps_alternating(self):
        pts = self.square(20) + self.square(15) + self.square(10) + self.square(5)
        area = self.area(
            pts, [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11], [12, 13, 14, 15]]
        )
        assert area == pytest.approx(1600 - 900 + 400 - 100)

    def test_two_paths_side_by_side_are_both_solid(self):
        """Neither encloses the other, so neither is a hole: 100 against
        OpenSCAD's 200, where the second was subtracted and cancelled
        nothing."""
        pts = self.square(5) + self.square(5, cx=20)
        assert self.area(pts, [[0, 1, 2, 3], [4, 5, 6, 7]]) == pytest.approx(200)
