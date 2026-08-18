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
"""

import shutil
from pathlib import Path

from build123d import Axis, Mesher, export_gltf, fillet

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
    export_gltf(filleted, str(MODELS / "bosl2_tube.glb"))


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
    (an exact cylindrical face). Screenshot both with viewer.html's
    edges=1 -- it draws a line at every dihedral angle over ~5 degrees, which
    reveals every one of the STL's ~48 facet boundaries but only the real
    edges (cube edges, the two circular rims) on the analytic version, since
    build123d's fine tessellation keeps neighboring facets nearly coplanar.
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
    export_gltf(part, str(MODELS / "boss_step.glb"))


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


def hull_fallback() -> None:
    """hull() of unequal-radius spheres -> no analytic path, mesh fallback.
    $fn=14 (below facet_threshold) also facets the spheres themselves, so
    the whole render is visibly a mesh.
    """
    scad = write(
        "hull_fallback.scad",
        "$fn = 14;\nhull() {\n"
        "    translate([-8, 0, 0]) sphere(r = 5);\n"
        "    translate([8, 0, 0]) sphere(r = 9);\n"
        "}\n",
    )
    bad = scad123d.import_scad(scad)
    export_gltf(bad, str(MODELS / "hull_fallback.glb"))


if __name__ == "__main__":
    bosl2_tube_filleted()
    minkowski_before_and_after()
    boss_stl_vs_step()
    mcad_gear()
    hull_analytic()
    hull_fallback()
    print(f"models written to {MODELS}")
