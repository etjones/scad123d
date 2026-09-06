"""mesh_import: exact BRep topology from an indexed triangle mesh.

Tier 1 builds meshes by hand. The one needs_openscad test is the case that
motivated the module: OpenSCAD's Manifold backend triangulates a slab with
a through-hole in a way build123d's sewing-based import gets wrong.
"""

import pytest

from scad123d.errors import MeshImportError
from scad123d.mesh_import import signed_volume, solid_from_triangles

# A 2x2x2 cube centered at the origin, triangles wound outward.
CUBE_VERTS = [
    (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
    (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
]  # fmt: skip
CUBE_TRIS = [
    (0, 2, 1), (0, 3, 2),  # bottom (z=-1), normal -z
    (4, 5, 6), (4, 6, 7),  # top
    (0, 1, 5), (0, 5, 4),  # front (y=-1)
    (2, 3, 7), (2, 7, 6),  # back
    (1, 2, 6), (1, 6, 5),  # right (x=1)
    (3, 0, 4), (3, 4, 7),  # left
]  # fmt: skip


def _scaled(verts, factor):  # type: ignore[no-untyped-def]
    return [(x * factor, y * factor, z * factor) for x, y, z in verts]


def test_signed_volume_of_an_outward_cube_is_positive():
    assert signed_volume(CUBE_VERTS, CUBE_TRIS) == pytest.approx(8)


def test_builds_a_valid_solid_with_shared_topology():
    solid = solid_from_triangles(CUBE_VERTS, CUBE_TRIS)
    assert solid.is_valid
    assert solid.volume == pytest.approx(8)
    assert len(solid.faces()) == 12
    # Shared edges: a cube of 12 triangles has 18 distinct edges, not 36.
    assert len(solid.edges()) == 18
    assert len(solid.vertices()) == 8


def test_inward_winding_is_corrected():
    inward = [(a, c, b) for a, b, c in CUBE_TRIS]
    assert signed_volume(CUBE_VERTS, inward) == pytest.approx(-8)
    solid = solid_from_triangles(CUBE_VERTS, inward)
    assert solid.is_valid
    assert solid.volume == pytest.approx(8)


def test_duplicate_vertex_positions_are_merged():
    # Manifold lists one position under several indices. Give the cube a
    # second copy of vertex 6 and route half its triangles through it.
    verts = list(CUBE_VERTS) + [CUBE_VERTS[6]]
    tris = [
        tuple(8 if i == 6 and n % 2 else i for i in t) for n, t in enumerate(CUBE_TRIS)
    ]
    solid = solid_from_triangles(verts, tris)
    assert solid.is_valid
    assert solid.volume == pytest.approx(8)
    assert len(solid.vertices()) == 8


def test_inner_shell_becomes_a_void():
    inner_verts = _scaled(CUBE_VERTS, 0.5)
    inner_tris = [(a + 8, c + 8, b + 8) for a, b, c in CUBE_TRIS]  # wound inward
    solid = solid_from_triangles(CUBE_VERTS + inner_verts, CUBE_TRIS + inner_tris)
    assert solid.is_valid
    assert solid.volume == pytest.approx(8 - 1)
    assert len(solid.shells()) == 2


def test_degenerate_triangles_are_dropped():
    tris = CUBE_TRIS + [(0, 0, 1), (2, 2, 2)]
    solid = solid_from_triangles(CUBE_VERTS, tris)
    assert solid.volume == pytest.approx(8)


def test_open_mesh_raises_rather_than_returning_a_wrong_solid():
    with pytest.raises(MeshImportError):
        solid_from_triangles(CUBE_VERTS, CUBE_TRIS[:-2])  # a face missing


@pytest.mark.needs_openscad
def test_manifold_through_hole_matches_cgal(tmp_path, monkeypatch):
    """The motivating case: identical triangles from both backends, but
    build123d's sewing produced an invalid 3143.6 solid from Manifold's
    file where 3071.9 is right. Both must now import to the same volume."""
    from scad123d.mesh_import import read_mesh_file
    from scad123d.openscad import export_mesh

    source = (
        "difference() {\n"
        "\tcube(size = [30, 30, 4], center = true);\n"
        "\thull() {\n"
        "\t\tcylinder($fn = 0, $fa = 12, $fs = 2, h = 20, r1 = 3, r2 = 3, center = true);\n"
        "\t\tmultmatrix([[1,0,0,10],[0,1,0,0],[0,0,1,0],[0,0,0,1]]) {\n"
        "\t\t\tcylinder($fn = 0, $fa = 12, $fs = 2, h = 20, r1 = 5, r2 = 5, center = true);\n"
        "\t\t}\n\t}\n}"
    )
    from scad123d.openscad import _supports_backend_flag

    if not _supports_backend_flag():
        pytest.skip("this OpenSCAD has no --backend flag")
    volumes = {}
    for backend in ("CGAL", "Manifold"):
        monkeypatch.setenv("SCAD123D_BACKEND", backend)
        path = export_mesh(source, suffix=".3mf")
        shapes = read_mesh_file(path)
        assert len(shapes) == 1 and shapes[0].is_valid
        volumes[backend] = shapes[0].volume
    assert volumes["Manifold"] == pytest.approx(volumes["CGAL"], rel=1e-6)
    assert volumes["CGAL"] == pytest.approx(3071.94, abs=0.01)


def _shift(verts, dx):  # type: ignore[no-untyped-def]
    return [(x + dx, y, z) for x, y, z in verts]


def test_separate_bodies_become_a_compound_not_voids():
    # Two cubes side by side: the second is not inside the first, so it is
    # a body of its own. (Treating every non-largest shell as a cavity made
    # every multi-part model import as an invalid solid.)
    verts = CUBE_VERTS + _shift(CUBE_VERTS, 10)
    tris = CUBE_TRIS + [(a + 8, b + 8, c + 8) for a, b, c in CUBE_TRIS]
    shape = solid_from_triangles(verts, tris)
    assert shape.is_valid
    assert len(shape.solids()) == 2
    assert shape.volume == pytest.approx(16)


def test_inside_out_separate_body_is_still_a_body():
    # A user polyhedron listed inside-out comes through Manifold inside-out
    # (CGAL would have repaired it). It is a body, not a cavity: winding is
    # normalized, containment decides.
    verts = CUBE_VERTS + _shift(CUBE_VERTS, 10)
    tris = CUBE_TRIS + [(a + 8, c + 8, b + 8) for a, b, c in CUBE_TRIS]
    shape = solid_from_triangles(verts, tris)
    assert shape.is_valid
    assert len(shape.solids()) == 2
    assert shape.volume == pytest.approx(16)


def test_outward_wound_cavity_is_still_a_cavity():
    inner_verts = _scaled(CUBE_VERTS, 0.5)
    inner_tris = [(a + 8, b + 8, c + 8) for a, b, c in CUBE_TRIS]  # wound outward
    shape = solid_from_triangles(CUBE_VERTS + inner_verts, CUBE_TRIS + inner_tris)
    assert shape.is_valid
    assert len(shape.solids()) == 1
    assert shape.volume == pytest.approx(8 - 1)


def test_island_inside_a_cavity_is_a_body_again():
    mid = _scaled(CUBE_VERTS, 0.75)  # cavity
    core = _scaled(CUBE_VERTS, 0.25)  # island inside it
    verts = CUBE_VERTS + mid + core
    tris = (
        CUBE_TRIS
        + [(a + 8, c + 8, b + 8) for a, b, c in CUBE_TRIS]
        + [(a + 16, b + 16, c + 16) for a, b, c in CUBE_TRIS]
    )
    shape = solid_from_triangles(verts, tris)
    assert shape.is_valid
    assert len(shape.solids()) == 2
    assert shape.volume == pytest.approx(8 - 0.75**3 * 8 + 0.25**3 * 8)


@pytest.mark.needs_openscad
def test_full_multi_body_render_imports_valid():
    """The fixture that exposed the void bug: several separate primitives in
    one render, both backends."""
    from scad123d.mesh_import import read_mesh_file
    from scad123d.openscad import _supports_backend_flag, export_csg, export_mesh

    csg = export_csg("tests/fixtures/scad/primitives.scad")
    backends = ["CGAL", "Manifold"] if _supports_backend_flag() else [None]
    for backend in backends:
        env = {"SCAD123D_BACKEND": backend} if backend else {}
        with pytest.MonkeyPatch.context() as mp:
            for k, v in env.items():
                mp.setenv(k, v)
            shapes = read_mesh_file(export_mesh(csg, suffix=".3mf"))
        total = sum(s.volume for s in shapes)
        assert all(s.is_valid for s in shapes)
        assert total == pytest.approx(8097.28, abs=0.01)


def test_mesh_volume_is_brep_free_and_handles_cavities_and_bodies(tmp_path):
    # Write a 3MF with build123d's own Mesher, then read its volume back
    # with pure arithmetic: an outer cube with a cavity plus a separate
    # inside-out body.
    from build123d import Box, Mesher, Pos

    from scad123d.mesh_import import mesh_volume

    hollow = Box(2, 2, 2) - Box(1, 1, 1)
    other = Pos(10, 0, 0) * Box(2, 2, 2)
    mesher = Mesher()
    mesher.add_shape([hollow, other])
    mesher.write(str(tmp_path / "m.3mf"))
    assert mesh_volume(tmp_path / "m.3mf") == pytest.approx(7 + 8, rel=1e-9)


def test_mesh_volume_does_not_mistake_a_nested_body_for_a_cavity(tmp_path):
    # A ball inside a ring's bounding box is not inside the ring: winding
    # says body, and only winding decides. (A ball bearing came out with
    # a negative volume under the old bounding-box rule.)
    from build123d import Box, Cylinder, Mesher, Pos, Sphere

    from scad123d.mesh_import import mesh_volume

    ring = Cylinder(10, 4) - Cylinder(6, 4)
    ball = Sphere(2)  # at the origin: inside the ring's box, in its hole
    hollow = Box(30, 30, 30).moved(Pos(50, 0, 0)) - Box(10, 10, 10).moved(Pos(50, 0, 0))
    mesher = Mesher()
    mesher.add_shape([ring, ball, hollow])
    mesher.write(str(tmp_path / "m.3mf"))
    expected = ring.volume + ball.volume + hollow.volume
    assert mesh_volume(tmp_path / "m.3mf") == pytest.approx(expected, rel=1e-3)
