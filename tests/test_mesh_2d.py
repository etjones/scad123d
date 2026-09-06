"""The 2D mesh fallback: OpenSCAD will not export a 2D object to 3MF, so a
2D subtree with no analytic form (hull() beyond solid123d's closed forms,
projection(), a 2D import()) is rendered as a 1 mm extrusion and its top
face taken as the profile. All tier 2: every case needs OpenSCAD."""

import pytest

import scad123d
from scad123d.mesh_import import mesh_volume
from scad123d.openscad import export_mesh

pytestmark = pytest.mark.needs_openscad


def _at(x: float, body: str) -> str:
    return f"multmatrix([[1, 0, 0, {x}], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {{ {body} }}"


def _circle(r: float) -> str:
    return f"circle($fn = 0, $fa = 12, $fs = 2, r = {r});"


# Two circles are hulled analytically; three are not (rung 2 is pairwise),
# nor is a circle with a square -- so these genuinely take the fallback.
THREE_CIRCLES = (
    f"hull() {{\n{_circle(3)}\n{_at(10, _circle(5))}\n{_at(5, _circle(2))}\n}}"
)
CIRCLE_AND_SQUARE = (
    f"hull() {{\n{_circle(3)}\n{_at(10, 'square(size = [6, 6], center = true);')}\n}}"
)


def _extruded(height: float, body: str) -> str:
    return f"linear_extrude(height = {height}, center = false, convexity = 1) {{\n{body}\n}}"


@pytest.mark.parametrize("profile", [THREE_CIRCLES, CIRCLE_AND_SQUARE])
def test_2d_hull_inside_an_extrude_matches_openscad_exactly(profile):
    source = _extruded(4, profile)
    with pytest.warns(UserWarning, match="hull\\(\\) has no BRep equivalent"):
        shape = scad123d.import_csg(source)
    assert shape.is_valid and len(shape.solids()) == 1
    # Same facets as OpenSCAD's own render of the whole thing: identical
    # volume, not merely close.
    reference = mesh_volume(export_mesh(source, suffix=".3mf"))
    assert shape.volume == pytest.approx(reference, rel=1e-6)


def test_2d_fallback_profile_is_one_face_per_island_with_holes():
    holes = f"{_circle(1.5)}\n{_at(10, _circle(2))}\n"  # a hole in each lobe
    profile = f"difference() {{\n{THREE_CIRCLES}\n{holes}\n}}"
    with pytest.warns(UserWarning):
        face = scad123d.import_csg(profile)
    assert not face.solids()
    assert len(face.faces()) == 1  # merged from the render's triangles
    assert len(face.faces()[0].inner_wires()) == 2
    # The hull comes back faceted (it is OpenSCAD's render) but the holes
    # are cut as exact circles, which remove more than OpenSCAD's inscribed
    # 9-gon and hexagon: a little smaller than the reference, by design.
    solid = scad123d.import_csg(_extruded(2, profile))
    reference = mesh_volume(export_mesh(_extruded(2, profile), suffix=".3mf"))
    assert reference * 0.95 < solid.volume < reference


def test_projection_falls_back_to_a_2d_profile():
    source = _extruded(
        2,
        "projection(cut = false) {\n"
        "\tmultmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 3], [0, 0, 0, 1]]) {\n"
        "\t\tcube(size = [10, 6, 4], center = false);\n"
        "\t}\n}",
    )
    with pytest.warns(UserWarning, match="projection"):
        shape = scad123d.import_csg(source)
    assert shape.volume == pytest.approx(10 * 6 * 2, rel=1e-9)
    assert shape.bounding_box().min.Z == pytest.approx(0)


def test_top_level_2d_fallback_result_sits_at_z0_facing_up():
    with pytest.warns(UserWarning):
        face = scad123d.import_csg(CIRCLE_AND_SQUARE)
    assert not face.solids()
    bb = face.bounding_box()
    assert bb.min.Z == pytest.approx(0) and bb.max.Z == pytest.approx(0)
    assert all(f.normal_at().Z > 0.99 for f in face.faces())
    assert face.area == pytest.approx(
        mesh_volume(export_mesh(_extruded(1, CIRCLE_AND_SQUARE), suffix=".3mf")),
        rel=1e-6,
    )
