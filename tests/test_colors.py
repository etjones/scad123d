"""Per-color volume cross-check against OpenSCAD's own colored render.

The 3MF parsing and closure test run on hand-written model XML, so they
need no OpenSCAD; two needs_openscad tests confirm the real render agrees
for disjoint colors and reports itself undefined for overlapping ones.
"""

import io
import zipfile

import pytest

from scad123d.colors import (
    MODEL_PATH,
    NS,
    UNCOLORED,
    compare,
    openscad_color_volumes,
    parse_3mf_color_volumes,
)

CUBE_VERTICES = [
    (0, 0, 0),
    (1, 0, 0),
    (1, 1, 0),
    (0, 1, 0),
    (0, 0, 1),
    (1, 0, 1),
    (1, 1, 1),
    (0, 1, 1),
]
# The 12 triangles of a unit cube, wound outward.
CUBE_TRIANGLES = [
    (0, 2, 1),
    (0, 3, 2),
    (4, 5, 6),
    (4, 6, 7),
    (0, 1, 5),
    (0, 5, 4),
    (1, 2, 6),
    (1, 6, 5),
    (2, 3, 7),
    (2, 7, 6),
    (3, 0, 4),
    (3, 4, 7),
]


def model_3mf(objects: list[dict], palette: list[str]) -> bytes:
    """A minimal 3MF holding hand-written meshes, as OpenSCAD writes them:
    one basematerials palette, triangles tagged with an index into it."""
    bases = "".join(
        f'<base name="Color {i}" displaycolor="{c}"/>' for i, c in enumerate(palette)
    )
    parts = []
    for n, obj in enumerate(objects, start=2):
        verts = "".join(
            f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in obj["vertices"]
        )
        tris = "".join(
            f'<triangle v1="{a}" v2="{b}" v3="{c}" pid="1" p1="{p}"/>'
            for a, b, c, p in obj["triangles"]
        )
        parts.append(
            f'<object id="{n}" type="model" pid="1" pindex="0">'
            f"<mesh><vertices>{verts}</vertices>"
            f"<triangles>{tris}</triangles></mesh></object>"
        )
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="millimeter" xmlns="{NS["m"]}"><resources>'
        f'<basematerials id="1">{bases}</basematerials>'
        f"{''.join(parts)}</resources><build/></model>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MODEL_PATH, xml)
    return buffer.getvalue()


def scaled(factor: float, offset: float = 0.0) -> list[tuple[float, float, float]]:
    return [(x * factor + offset, y * factor, z * factor) for x, y, z in CUBE_VERTICES]


def test_disjoint_colors_measure_their_own_volumes():
    data = model_3mf(
        [
            {
                "vertices": scaled(2),
                "triangles": [(*t, 1) for t in CUBE_TRIANGLES],
            },
            {
                "vertices": scaled(3, offset=10),
                "triangles": [(*t, 2) for t in CUBE_TRIANGLES],
            },
        ],
        palette=["#f9d72c", "#ff0000", "#0000ff"],
    )
    assert parse_3mf_color_volumes(data) == {"red": 8.0, "blue": 27.0}


def test_openscads_default_color_reads_as_uncolored():
    data = model_3mf(
        [{"vertices": scaled(2), "triangles": [(*t, 0) for t in CUBE_TRIANGLES]}],
        palette=["#f9d72c"],
    )
    assert parse_3mf_color_volumes(data) == {UNCOLORED: 8.0}


def test_two_colors_on_one_mesh_are_measured_separately():
    """One object whose triangles carry different colors: still fine as
    long as each color's triangles close a volume of their own."""
    both = [(*t, 1) for t in CUBE_TRIANGLES]
    data = model_3mf(
        [
            {"vertices": scaled(2), "triangles": both},
            {
                "vertices": scaled(2, offset=10),
                "triangles": [(*t, 2) for t in CUBE_TRIANGLES],
            },
        ],
        palette=["#f9d72c", "#ff0000", "#008000"],
    )
    assert parse_3mf_color_volumes(data) == {"red": 8.0, "green": 8.0}


def test_an_open_color_group_is_reported_as_undefined():
    """Overlapping colors leave each group open -- OpenSCAD removed the
    interface triangles -- and an open surface encloses no volume."""
    open_cube = [(*t, 1) for t in CUBE_TRIANGLES[:-2]]  # drop a face
    data = model_3mf(
        [{"vertices": scaled(2), "triangles": open_cube}],
        palette=["#f9d72c", "#ff0000"],
    )
    assert parse_3mf_color_volumes(data) is None


def test_compare_is_quiet_when_every_color_agrees():
    assert (
        compare({"red": 100.0, "blue": 50.0}, {"red": 100.005, "blue": 50.0}, 0.01)
        is None
    )


def test_compare_names_the_worst_disagreement():
    message = compare({"red": 100.0, "blue": 50.0}, {"red": 90.0, "blue": 10.0}, 0.01)
    assert message is not None
    assert message.startswith("color volume off by blue")
    assert "80.0%" in message


def test_compare_flags_a_color_only_one_side_has():
    message = compare({"red": 100.0}, {"blue": 100.0}, 0.01)
    assert message is not None and "blue 0 vs OpenSCAD 100" in message


@pytest.mark.needs_openscad
def test_real_render_measures_disjoint_colors():
    csg = (
        "group() {\n"
        "\tcolor([1, 0, 0, 1]) { cube(size = [10, 10, 10], center = false); }\n"
        "\tcolor([0, 0, 1, 1]) {\n"
        "\t\tmultmatrix([[1, 0, 0, 20], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
        "\t\t\tcube(size = [5, 5, 5], center = false);\n\t\t}\n\t}\n}"
    )
    volumes = openscad_color_volumes(csg, timeout=120)
    assert volumes is not None
    assert volumes["red"] == pytest.approx(1000, rel=1e-6)
    assert volumes["blue"] == pytest.approx(125, rel=1e-6)


@pytest.mark.needs_openscad
def test_real_render_of_overlapping_colors_is_undefined():
    csg = (
        "group() {\n"
        "\tcolor([1, 0, 0, 1]) { cube(size = [10, 10, 10], center = false); }\n"
        "\tcolor([0, 0, 1, 1]) {\n"
        "\t\tmultmatrix([[1, 0, 0, 5], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
        "\t\t\tcube(size = [10, 10, 10], center = false);\n\t\t}\n\t}\n}"
    )
    assert openscad_color_volumes(csg, timeout=120) is None
