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
    openscad_channel,
    openscad_color_volumes,
    openscad_key,
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
    assert parse_3mf_color_volumes(data) == {"#ff0000": 8.0, "#0000ff": 27.0}


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
    assert parse_3mf_color_volumes(data) == {"#ff0000": 8.0, "#008000": 8.0}


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
    assert volumes["#ff0000"] == pytest.approx(1000, rel=1e-6)
    assert volumes["#0000ff"] == pytest.approx(125, rel=1e-6)


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


def test_the_export_convention_matches_openscad_for_every_channel():
    """The whole point: OpenSCAD's byte is reproduced, not approximated.
    Ground truth came from rendering all 256 values through OpenSCAD; these
    are the cases where truncation, the 32-bit narrowing, or both matter."""
    assert openscad_channel(1.0) == 255
    assert openscad_channel(0.0) == 0
    assert openscad_channel(0.933333) == 237  # lightgreen's 238, truncated
    assert openscad_channel(0.501961) == 128  # green's 128, exact
    assert openscad_channel(0.0117647) == 2  # 3, truncated
    # 0.972549 * 255 is 247.999995 as a double but 248.00000x once narrowed
    # to 32 bits, which is what OpenSCAD stores. Rounding gets this right by
    # luck; truncating the double does not.
    assert openscad_channel(0.972549) == 248


def test_a_key_is_openscads_bytes_not_ours():
    assert openscad_key((0.564706, 0.933333, 0.564706)) == "#90ed90"
    assert openscad_key((0.823529, 0.411765, 0.117647)) == "#d1691d"
    assert openscad_key((1.0, 0.0, 0.0, 1.0)) == "#ff0000"


def test_a_color_openscad_truncated_still_compares_equal():
    """Keyed OpenSCAD's way, the two sides are the same string."""
    assert compare({"#90ed90": 100.0}, {"#90ed90": 100.0}, 0.001) is None


def test_a_genuinely_different_color_still_fails():
    assert compare({"#ff0000": 100.0}, {"#0000ff": 100.0}, 0.001) is not None
    # One unit apart is a different color, not a tolerance to absorb.
    assert compare({"#ff0000": 100.0}, {"#fe0000": 100.0}, 0.001) is not None


def test_the_message_names_the_color_a_reader_knows():
    message = compare({"#90ed90": 100.0}, {"#90ed90": 50.0}, 0.001)
    assert message is not None and message.startswith("color volume off by lightgreen")


def test_uncolored_never_absorbs_a_colored_bucket():
    assert compare({UNCOLORED: 100.0}, {"#ff0000": 100.0}, 0.001) is not None
