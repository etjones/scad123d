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
