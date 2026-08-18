"""Tier 2: compare against OpenSCAD itself. Requires the OpenSCAD binary.

Two independent checks:

1. CSG export is deterministic, so re-exporting a fixture must reproduce the
   committed .csg byte for byte (modulo whitespace). This is what makes tier 1
   trustworthy.
2. Volume agreement against OpenSCAD's own mesh. Where scad123d keeps exact
   BRep curves, its solid *circumscribes* OpenSCAD's inscribed faceted mesh, so
   the comparison is deliberately one-sided: our volume must be greater than or
   equal to OpenSCAD's, and converge as $fn rises.
"""

import math

import pytest

import scad123d
from scad123d.openscad import export_csg, export_mesh

from .conftest import FIXTURES, stl_volume
from .test_build import _CYLINDER_CENTERED, _SPHERE, _hull_of, _translated

SCAD_DIR = FIXTURES / "scad"
SCAD_NAMES = sorted(p.stem for p in SCAD_DIR.glob("*.scad"))

pytestmark = pytest.mark.needs_openscad


def _normalise(text: str) -> str:
    return "".join(text.split())


@pytest.mark.parametrize("name", SCAD_NAMES)
def test_csg_export_is_deterministic(name):
    """Committed fixtures must still match what OpenSCAD produces today."""
    committed = FIXTURES / f"{name}.csg"
    if not committed.exists():
        pytest.skip(f"{name}.csg not committed")
    assert _normalise(export_csg(SCAD_DIR / f"{name}.scad")) == _normalise(
        committed.read_text()
    ), f"{name}.csg is stale -- run `just fixtures`"


@pytest.mark.parametrize("name", SCAD_NAMES)
def test_csg_input_round_trips(name):
    """.csg must be valid OpenSCAD input -- the mesh fallback depends on it."""
    once = export_csg(SCAD_DIR / f"{name}.scad")
    path = export_mesh(once, suffix=".csg")
    try:
        assert _normalise(path.read_text()) == _normalise(once)
    finally:
        import shutil

        shutil.rmtree(path.parent, ignore_errors=True)


def _openscad_volume(scad, fn):
    import shutil

    mesh = export_mesh(export_csg(scad, {"$fn": fn}), suffix=".stl")
    try:
        return stl_volume(mesh)
    finally:
        shutil.rmtree(mesh.parent, ignore_errors=True)


# Fixtures with no curved surfaces at all, where agreement must be exact.
_POLYHEDRAL = {"polyhedron"}

# Fixtures whose geometry is fixed independent of $fn -- either set at call
# sites (which -D cannot override) or baked into literal point data (a
# polyhedron ball kernel, matching how BOSL2 emits one) -- so the convergence
# test has nothing to vary. Checked exactly below instead.
_CALL_SITE_FN = {"facets", "minkowski_polyhedron"}


@pytest.mark.parametrize("name", SCAD_NAMES)
def test_volume_agrees_with_openscad(name):
    """scad123d's exact geometry must be the limit OpenSCAD converges to.

    Total volume is NOT bounded on one side: an exact cylindrical hole removes
    *more* material than OpenSCAD's inscribed faceted one, so a subtracted curve
    pushes our volume below OpenSCAD's while an added curve pushes it above.
    The invariant that does hold in both directions is convergence -- raising
    $fn must move OpenSCAD's volume toward ours.
    """
    if name in _CALL_SITE_FN:
        pytest.skip("call-site $fn cannot be varied with -D; see the exact test")

    scad = SCAD_DIR / f"{name}.scad"
    ours = scad123d.import_scad(scad).volume

    if name in _POLYHEDRAL:
        assert ours == pytest.approx(_openscad_volume(scad, 128), rel=1e-9)
        return

    coarse = abs(ours - _openscad_volume(scad, 16))
    fine = abs(ours - _openscad_volume(scad, 128))
    assert fine < coarse, (
        f"{name}: raising $fn did not move OpenSCAD toward scad123d "
        f"(coarse err {coarse:.5f}, fine err {fine:.5f})"
    )
    assert fine / ours < 0.005, (
        f"{name}: still {fine / ours:.2%} apart at $fn=128 "
        f"(scad123d {ours:.4f})"
    )


def test_minkowski_beats_openscad_and_converges():
    """Rung 1 is analytically exact, so it should exceed OpenSCAD at any $fn."""
    scad = SCAD_DIR / "minkowski_sphere.scad"
    ours = scad123d.import_scad(scad).volume

    import shutil

    volumes = {}
    for fn in (16, 64):
        mesh = export_mesh(export_csg(scad, {"$fn": fn}), suffix=".stl")
        try:
            volumes[fn] = stl_volume(mesh)
        finally:
            shutil.rmtree(mesh.parent, ignore_errors=True)

    assert volumes[16] < volumes[64] <= ours * (1 + 1e-6)
    assert ours == pytest.approx(volumes[64], rel=0.02)


def test_overrides_change_the_geometry():
    scad = SCAD_DIR / "params.scad"
    base = scad123d.import_scad(scad)
    wider = scad123d.import_scad(scad, width=40)
    assert wider.bounding_box().size.X == pytest.approx(40, rel=1e-9)
    assert wider.volume > base.volume

    more_holes = scad123d.import_scad(scad, holes=6)
    assert more_holes.volume < base.volume


def test_hull_falls_back_to_a_mesh_rather_than_failing():
    """Decision: hull() must never hard-fail -- BOSL2 rounding depends on it."""
    source = (
        "hull() {\n"
        "\tcylinder($fn = 16, $fa = 12, $fs = 2, h = 2, r1 = 3, r2 = 3, center = true);\n"
        "\tmultmatrix([[1, 0, 0, 14], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
        "\t\tcylinder($fn = 16, $fa = 12, $fs = 2, h = 2, r1 = 3, r2 = 3, center = true);\n"
        "\t}\n"
        "}"
    )
    with pytest.warns(UserWarning, match="no BRep equivalent"):
        shape = scad123d.import_csg(source)
    assert shape.volume > 0
    # a 14-long slot of radius 3 and height 2, faceted
    assert shape.volume == pytest.approx(2 * (14 * 6 + math.pi * 9), rel=0.05)


def test_mesh_scope_hoist_meshes_everything():
    """A hull() of unequal radii has no analytic path (rung 2 only covers
    equal-radius spheres/cylinders), so this still genuinely needs the mesh
    fallback -- unlike a single-child or equal-radius hull, which rung 2 now
    handles without ever touching OpenSCAD.
    """
    source = (
        "difference() {\n"
        "\tcube(size = [30, 30, 4], center = true);\n"
        "\thull() {\n"
        "\t\tcylinder($fn = 16, $fa = 12, $fs = 2, h = 20, r1 = 3, r2 = 3, center = true);\n"
        "\t\tmultmatrix([[1,0,0,10],[0,1,0,0],[0,0,1,0],[0,0,0,1]]) {\n"
        "\t\t\tcylinder($fn = 16, $fa = 12, $fs = 2, h = 20, r1 = 5, r2 = 5, center = true);\n"
        "\t\t}\n"
        "\t}\n"
        "}"
    )
    with pytest.warns(UserWarning, match="no BRep equivalent"):
        minimal = scad123d.import_csg(source, mesh_scope="minimal")
    with pytest.warns(UserWarning, match="no BRep equivalent"):
        hoisted = scad123d.import_csg(source, mesh_scope="hoist")
    assert minimal.volume == pytest.approx(hoisted.volume, rel=0.02)


def test_faceted_sphere_takes_the_mesh_path():
    with pytest.warns(UserWarning, match="faceted sphere"):
        shape = scad123d.import_csg("sphere($fn = 6, $fa = 12, $fs = 2, r = 10);")
    assert shape.volume > 0
    assert shape.volume < (4 / 3) * math.pi * 1000  # inscribed in the true sphere


def test_facets_differ_from_openscad_by_exactly_the_circle_gap():
    """facets.scad pins $fn at every call site, so the gap is deterministic.

    Everything below the threshold is faceted identically to OpenSCAD; the one
    shape at or above it (cylinder r=8 h=10 $fn=64) is an exact BRep cylinder
    for us and a 64-gon prism for OpenSCAD. The whole difference is that gap.
    """
    scad = SCAD_DIR / "facets.scad"
    ours = scad123d.import_scad(scad).volume
    theirs = _openscad_volume(scad, 128)

    r, h, sides = 8.0, 10.0, 64
    exact_disc = math.pi * r**2
    ngon_disc = 0.5 * sides * r**2 * math.sin(2 * math.pi / sides)
    assert ours - theirs == pytest.approx((exact_disc - ngon_disc) * h, rel=1e-4)


def test_minkowski_polyhedron_kernel_matches_steiner_formula():
    """minkowski_polyhedron.scad bakes its ball as literal polyhedron point
    data (a 128-vertex faceted sphere, radius 2, mirroring how BOSL2's own
    cuboid(rounding=) emits its kernel -- see design notes), so there is no
    $fn to vary against OpenSCAD. Checked against the closed-form Steiner
    volume instead, which is the real ground truth here.
    """
    scad = SCAD_DIR / "minkowski_polyhedron.scad"
    ours = scad123d.import_scad(scad).volume

    a, b, c, r = 10.0, 8.0, 6.0, 2.0
    edges = [(a, math.pi / 2)] * 4 + [(b, math.pi / 2)] * 4 + [(c, math.pi / 2)] * 4
    area = 2 * (a * b + b * c + c * a)
    exact = a * b * c + area * r + r * r / 2 * sum(l * t for l, t in edges) + (4 / 3) * math.pi * r**3
    assert ours == pytest.approx(exact, rel=1e-5)


# The four cases below all exercise rung 2's "not analytic" path, which means
# they exercise the mesh fallback -- which itself invokes OpenSCAD. That
# makes them tier-2 tests by nature, not tier-1: a tier-1 test claims to need
# no binary, but these can't complete without one either way.


def test_unequal_radius_hull_falls_back_to_mesh():
    source = _hull_of(
        _SPHERE.format(r=3),
        _translated(10, 0, 0, _SPHERE.format(r=5)),
    )
    with pytest.warns(UserWarning, match="no BRep equivalent"):
        shape = scad123d.import_csg(source)
    assert shape.is_valid
    assert shape.volume > 0


def test_mixed_sphere_and_cylinder_hull_falls_back_to_mesh():
    source = _hull_of(
        _SPHERE.format(r=3),
        _translated(10, 0, 0, _CYLINDER_CENTERED.format(r=3)),
    )
    with pytest.warns(UserWarning, match="no BRep equivalent"):
        shape = scad123d.import_csg(source)
    assert shape.is_valid


def test_coplanar_noncollinear_sphere_hull_falls_back_to_mesh():
    """A real limitation, not a bug: qhull's 3D ConvexHull raises on
    degenerate (flat) input, so a hull of spheres with coplanar centers takes
    the mesh path rather than crashing. See ROADMAP.md.
    """
    centers = [(-10, -10, 0), (10, -10, 0), (10, 10, 0), (-10, 10, 0)]
    source = _hull_of(*(_translated(x, y, z, _SPHERE.format(r=2)) for x, y, z in centers))
    with pytest.warns(UserWarning, match="no BRep equivalent"):
        shape = scad123d.import_csg(source)
    assert shape.is_valid
    assert shape.volume > 0


def test_differing_cylinder_spans_fall_back_to_mesh():
    """A real limitation: cylinders that don't share one axial span have no
    simple extrude-shaped hull, so this takes the mesh path.
    """
    source = _hull_of(
        _translated(-10, 0, 0, _CYLINDER_CENTERED.format(r=3)),
        _translated(
            10, 0, 0,
            "multmatrix([[1,0,0,0],[0,1,0,0],[0,0,1,-5],[0,0,0,1]]) "
            "{ cylinder($fn = 0, $fa = 12, $fs = 2, h = 10, r1 = 3, r2 = 3, center = false); }",
        ),
    )
    with pytest.warns(UserWarning, match="no BRep equivalent"):
        shape = scad123d.import_csg(source)
    assert shape.is_valid
