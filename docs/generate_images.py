"""Builds the shapes shown in the README's illustrations and exports them as
glTF, ready to screenshot with docs/viewer.html.

    uv run python docs/generate_images.py

This only does the "build the geometry" half. Turning a .glb into a .png
still needs a browser: serve the docs/ directory (e.g. `python -m http.server
8743` from docs/, or the "docs-viewer" entry in .claude/launch.json), open
viewer.html?model=models/<name>.glb&recolor=1&color=<hex>&az=<degrees>&el=<
degrees>, and save the canvas (`canvas.toDataURL('image/png')`) once the page
title reads "ready". The README images were produced this way via Claude's
browser tooling; there's no one-shot CLI for the screenshot half.

Add &edges=1 to highlight edges. For a glTF exported via
export_gltf_with_edges(), the viewer draws the model's real BRep edges from a
JSON sidecar (see that function's docstring for why a mesh-based dihedral-
angle heuristic doesn't work once a fillet is involved). For an STL, which
has no edge topology, the viewer falls back to the angle heuristic, since
that's the only thing available.
"""

import json
import shutil
from pathlib import Path

from build123d import Axis, Mesher, Shape, export_gltf, fillet

import scad123d
from scad123d.openscad import export_csg, export_mesh

HERE = Path(__file__).parent
MODELS = HERE / "models"
MODELS.mkdir(exist_ok=True)

SCAD_DIR = HERE / "scad"
SCAD_DIR.mkdir(exist_ok=True)


def write(name: str, source: str) -> Path:
    path = SCAD_DIR / name
    path.write_text(source)
    return path


def export_gltf_with_edges(
    shape: Shape, glb_path: str, samples_per_edge: int = 24
) -> None:
    """Export a shape to glTF, plus a JSON sidecar of its real BRep edges.

    viewer.html's edges=1 draws these instead of guessing edges from mesh
    dihedral angles -- necessary for anything with a tangent (G1-continuous)
    fillet, where adjacent facets meet at a real but nearly-zero angle. That
    residual angle is mesh tessellation noise, not signal: it varies facet to
    facet, so an angle threshold either misses the fillet's true boundary
    entirely or, at a low enough threshold to catch it, also lights up
    spurious noise scattered across the curved surface. There is no dihedral
    angle that separates the two cases, because for a truly tangent fillet
    there isn't one -- the only reliable source for "where are the separate
    faces" is the BRep topology itself, which is exactly what this exports.

    Coordinates are converted to match export_gltf's own conventions (mm to
    m, and the Z-up -> Y-up rotation build123d bakes into every glTF export)
    so the edges line up with the mesh without the viewer needing to know
    which convention which file uses.
    """
    export_gltf(shape, glb_path)
    t_values = [i / samples_per_edge for i in range(samples_per_edge + 1)]
    polylines = []
    for edge in shape.edges():
        polylines.append(
            [[p.X / 1000, p.Z / 1000, -p.Y / 1000] for p in edge.positions(t_values)]
        )
    Path(glb_path).with_suffix(".edges.json").write_text(json.dumps(polylines))


def bosl2_tube_filleted() -> None:
    """Import a BOSL2 tube, then fillet its rims -- real BRep edges, not
    triangles, survive the import.
    """
    scad = write(
        "tube_example.scad",
        "include <BOSL2/std.scad>\n$fn = 32;\ntube(h=20, or=15, ir=10);\n",
    )
    tube = scad123d.import_scad(scad)
    edges = tube.edges().group_by(Axis.Z)
    filleted = fillet(edges[0] + edges[-1], radius=1.5)
    export_gltf_with_edges(filleted, str(MODELS / "bosl2_tube.glb"))


def minkowski_before_and_after() -> None:
    """The same minkowski() rounded box, rendered OpenSCAD's way (a coarse
    facsimile of a sphere, $fn=10) and scad123d's way (an exact offset).
    """
    scad = write(
        "round_box.scad",
        "minkowski() {\n    cube([20, 15, 10], center = true);\n    sphere(r = 3);\n}\n",
    )

    mesh_path = export_mesh(export_csg(scad, {"$fn": 10}), suffix=".3mf")
    try:
        before = Mesher().read(str(mesh_path))[0]
    finally:
        shutil.rmtree(mesh_path.parent, ignore_errors=True)
    export_gltf(before, str(MODELS / "minkowski_before.glb"))

    after = scad123d.import_scad(scad)
    export_gltf(after, str(MODELS / "minkowski_after.glb"))


def hull_analytic() -> None:
    """hull() of 8 equal-radius corner spheres -> exact offset (rung 2)."""
    scad = write(
        "hull_corners.scad",
        "hull() {\n"
        + "\n".join(
            f"    translate([{x},{y},{z}]) sphere(r=3);"
            for x in (-10, 10)
            for y in (-7.5, 7.5)
            for z in (-5, 5)
        )
        + "\n}\n",
    )
    ok = scad123d.import_scad(scad)
    export_gltf(ok, str(MODELS / "hull_ok.glb"))


def boss_stl_vs_step() -> None:
    """A cylinder emerging from a cube: OpenSCAD's own STL export (a coarse
    $fn=48 facet approximation) next to scad123d's STEP-equivalent geometry
    (an exact cylindrical face). Screenshot both with viewer.html's edges=1.
    The STL has no real edge topology -- it's just triangles -- so the
    viewer's dihedral-angle heuristic is the only option there, and reveals
    every one of the ~48 facet boundaries. The STEP-equivalent side uses its
    real BRep edges instead (see export_gltf_with_edges), showing only the
    edges the shape actually has: the cube's 12 edges and the two circular
    rims.
    """
    source = (
        "union() {\n"
        "    cube([20, 20, 10], center = true);\n"
        "    translate([0, 0, 5]) cylinder(h = 8, r = 6, $fn = 48, center = false);\n"
        "}\n"
    )
    scad = write("boss.scad", source)

    stl_path = export_mesh(source, suffix=".stl")
    try:
        shutil.copy(stl_path, MODELS / "boss_openscad.stl")
    finally:
        shutil.rmtree(stl_path.parent, ignore_errors=True)

    part = scad123d.import_scad(scad)
    export_gltf_with_edges(part, str(MODELS / "boss_step.glb"))


def mcad_gear() -> None:
    """An MCAD involute gear -- a second library, to show this isn't
    BOSL2-specific.
    """
    scad = write(
        "mcad_gear.scad",
        "use <MCAD/involute_gears.scad>\n"
        "gear(number_of_teeth=12, circular_pitch=8, gear_thickness=6, bore_diameter=5);\n",
    )
    gear = scad123d.import_scad(scad)
    export_gltf(gear, str(MODELS / "mcad_gear.glb"))


def hull_two_vs_three_spheres() -> None:
    """The pairwise tangent-cone rung next to its mathematical boundary.

    Two unequal spheres: exact -- two spherical caps sewn to the external
    tangent cone, with the two tangency seams as the shape's only real
    edges (export_gltf_with_edges shows exactly those two circles).
    Three non-collinear unequal spheres: no closed form (tritangent
    planes, power-diagram combinatorics), so the mesh fallback renders it
    -- visibly faceted.
    """
    two = write(
        "hull_two_spheres.scad",
        "hull() {\n"
        "    translate([-8, 0, 0]) sphere(r = 5);\n"
        "    translate([8, 0, 0]) sphere(r = 9);\n"
        "}\n",
    )
    smooth = scad123d.import_scad(two)
    export_gltf_with_edges(smooth, str(MODELS / "hull_two_spheres.glb"))

    three = write(
        "hull_three_spheres.scad",
        "$fn = 24;\nhull() {\n"
        "    translate([-8, 0, 0]) sphere(r = 5);\n"
        "    translate([8, 0, 0]) sphere(r = 9);\n"
        "    translate([0, 16, 0]) sphere(r = 7);\n"
        "}\n",
    )
    faceted = scad123d.import_scad(three)
    export_gltf(faceted, str(MODELS / "hull_three_spheres.glb"))


if __name__ == "__main__":
    bosl2_tube_filleted()
    minkowski_before_and_after()
    boss_stl_vs_step()
    mcad_gear()
    hull_analytic()
    hull_two_vs_three_spheres()
    print(f"models written to {MODELS}")
