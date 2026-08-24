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

from .conftest import FIXTURES, require_fixture_openscad_version, stl_volume
from .test_build import _CYLINDER_CENTERED, _SPHERE, _hull_of, _translated

SCAD_DIR = FIXTURES / "scad"
SCAD_NAMES = sorted(p.stem for p in SCAD_DIR.glob("*.scad"))

pytestmark = pytest.mark.needs_openscad


def _normalise(text: str) -> str:
    return "".join(text.split())


@pytest.mark.parametrize("name", SCAD_NAMES)
def test_csg_export_is_deterministic(name, metrics):
    """Committed fixtures must still match what OpenSCAD produces today."""
    require_fixture_openscad_version(metrics)
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
    """Decision: hull() must never hard-fail -- BOSL2 rounding depends on it.

    Three non-collinear spheres of unequal radii: no rung covers this (the
    tangent-cone rung is strictly pairwise -- three needs tritangent planes
    with power-diagram combinatorics), so it genuinely still needs the mesh
    fallback. This test's example has been upgraded twice as rungs landed:
    faceted cylinders (now exact via the polyhedral rung), then an unequal
    sphere *pair* (now exact via the tangent-cone rung).
    """
    source = (
        "hull() {\n"
        "\tsphere($fn = 0, $fa = 12, $fs = 2, r = 3);\n"
        "\tmultmatrix([[1, 0, 0, 14], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
        "\t\tsphere($fn = 0, $fa = 12, $fs = 2, r = 5);\n"
        "\t}\n"
        "\tmultmatrix([[1, 0, 0, 7], [0, 1, 0, 12], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
        "\t\tsphere($fn = 0, $fa = 12, $fs = 2, r = 4);\n"
        "\t}\n"
        "}"
    )
    with pytest.warns(UserWarning, match="no BRep equivalent"):
        shape = scad123d.import_csg(source)
    # Loose sandwich: contains the largest sphere; inside the bounding box.
    assert shape.volume > (4 / 3) * math.pi * 125
    assert shape.volume < shape.bounding_box().size.X * shape.bounding_box().size.Y * shape.bounding_box().size.Z


def test_mesh_scope_hoist_meshes_everything():
    """A hull() of unequal-radius *curved* cylinders has no analytic path
    (rung 2 needs equal radii, rung 3 needs all-planar faces), so this still
    genuinely needs the mesh fallback -- unlike a single-child, equal-radius,
    or all-polyhedral hull, which the rungs now handle without ever touching
    OpenSCAD. ($fn = 0 matters: at $fn = 16 these cylinders would be faceted
    prisms, which rung 3 hulls exactly.)
    """
    source = (
        "difference() {\n"
        "\tcube(size = [30, 30, 4], center = true);\n"
        "\thull() {\n"
        "\t\tcylinder($fn = 0, $fa = 12, $fs = 2, h = 20, r1 = 3, r2 = 3, center = true);\n"
        "\t\tmultmatrix([[1,0,0,10],[0,1,0,0],[0,0,1,0],[0,0,0,1]]) {\n"
        "\t\t\tcylinder($fn = 0, $fa = 12, $fs = 2, h = 20, r1 = 5, r2 = 5, center = true);\n"
        "\t\t}\n"
        "\t}\n"
        "}"
    )
    with pytest.warns(UserWarning, match="no BRep equivalent"):
        minimal = scad123d.import_csg(source, mesh_scope="minimal")
    with pytest.warns(UserWarning, match="no BRep equivalent"):
        hoisted = scad123d.import_csg(source, mesh_scope="hoist")
    assert minimal.volume == pytest.approx(hoisted.volume, rel=0.02)


def test_two_sphere_hull_converges_to_openscad():
    """The tangent-cone pair rung differentially: OpenSCAD's inscribed
    faceted hull must converge from below toward our exact volume as $fn
    rises -- the same convergence invariant the generic volume test uses
    for curved shapes.
    """
    from tests.test_build import _pair_hull_volume  # closed form, tested tier-1

    def source(fn: int) -> str:
        return (
            "hull() {\n"
            f"\tsphere($fn = {fn}, $fa = 12, $fs = 2, r = 3);\n"
            "\tmultmatrix([[1, 0, 0, 14], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
            f"\t\tsphere($fn = {fn}, $fa = 12, $fs = 2, r = 5);\n"
            "\t}\n"
            "}"
        )

    import shutil

    ours = _pair_hull_volume(3, 5, 14)
    volumes = {}
    for fn in (32, 128):
        mesh = export_mesh(source(fn), suffix=".stl")
        try:
            volumes[fn] = stl_volume(mesh)
        finally:
            shutil.rmtree(mesh.parent, ignore_errors=True)
    assert volumes[32] < ours
    assert volumes[128] < ours
    assert abs(ours - volumes[128]) < abs(ours - volumes[32])
    assert (ours - volumes[128]) / ours < 0.005


def test_polyhedral_hull_matches_openscad_exactly():
    """Rung 3 differential check: the hull of polyhedral children is the
    same exact polytope OpenSCAD itself computes, so volumes agree to float
    precision -- not just the convergence bound curved shapes get.
    """
    c = s = math.sqrt(2) / 2
    source = (
        "hull() {\n"
        "\tcube(size = [10, 10, 10], center = true);\n"
        f"\tmultmatrix([[{c}, {-s}, 0, 30], [{s}, {c}, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) "
        "{ cube(size = [10, 10, 10], center = true); }\n"
        "}"
    )
    ours = scad123d.import_csg(source).volume
    mesh = export_mesh(source, suffix=".stl")
    try:
        theirs = stl_volume(mesh)
    finally:
        import shutil

        shutil.rmtree(mesh.parent, ignore_errors=True)
    assert ours == pytest.approx(theirs, rel=1e-6)


def test_hull_of_a_mesh_fallback_child_is_still_a_polyhedral_hull():
    """A faceted sphere child takes the mesh path (that's the child's own,
    unchanged behavior) -- but its triangles are planar, so the *hull*
    around it qualifies for rung 3 and comes back as clean BRep planes
    rather than falling back to a second OpenSCAD mesh render.
    """
    import warnings as _warnings

    from build123d import GeomType

    source = (
        "hull() {\n"
        "\tsphere($fn = 8, $fa = 12, $fs = 2, r = 5);\n"
        "\tmultmatrix([[1, 0, 0, 20], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n"
        "\t\tcube(size = [4, 4, 4], center = true);\n\t}\n"
        "}"
    )
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        shape = scad123d.import_csg(source)
    messages = [str(w.message) for w in caught]
    assert any("faceted sphere" in m for m in messages)
    assert not any("hull()" in m for m in messages)
    assert all(f.geom_type == GeomType.PLANE for f in shape.faces())


def test_faceted_sphere_takes_the_mesh_path():
    with pytest.warns(UserWarning, match="faceted sphere"):
        shape = scad123d.import_csg("sphere($fn = 6, $fa = 12, $fs = 2, r = 10);")
    assert shape.volume > 0
    assert shape.volume < (4 / 3) * math.pi * 1000  # inscribed in the true sphere


def test_facets_differ_from_openscad_by_exactly_the_circle_gap(metrics):
    """facets.scad pins $fn at every call site, so the gap is deterministic.

    Everything below the threshold is faceted identically to OpenSCAD; the one
    shape at or above it (cylinder r=8 h=10 $fn=64) is an exact BRep cylinder
    for us and a 64-gon prism for OpenSCAD. The whole difference is that gap
    -- assuming OpenSCAD's own circle tessellation matches the version the
    exact formula below was checked against.
    """
    require_fixture_openscad_version(metrics)
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


def test_three_unequal_sphere_hull_falls_back_to_mesh():
    """A *pair* of unequal spheres is exact (the tangent-cone rung), but
    three non-collinear unequal spheres need tritangent planes with
    power-diagram combinatorics -- no rung covers that, so it's the
    permanent honest example of the mesh fallback.
    """
    source = _hull_of(
        _SPHERE.format(r=3),
        _translated(14, 0, 0, _SPHERE.format(r=5)),
        _translated(7, 12, 0, _SPHERE.format(r=4)),
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


def test_coplanar_noncollinear_sphere_hull_is_analytic():
    """Formerly a documented limitation (qhull's 3D ConvexHull raises on
    flat point sets, so this meshed): solid123d 0.3.1's identical-
    translates rung handles coplanar centers analytically -- a ball is a
    revolution solid, so this is conv(centers square) (+) ball, a rounded
    slab: V = 2r*A0 + (pi r^2/2)*P0 + 4/3 pi r^3.
    """
    r, side = 2.0, 20.0
    centers = [(-10, -10, 0), (10, -10, 0), (10, 10, 0), (-10, 10, 0)]
    source = _hull_of(*(_translated(x, y, z, _SPHERE.format(r=2)) for x, y, z in centers))
    shape = scad123d.import_csg(source)
    expected = (
        2 * r * side * side
        + math.pi * r * r / 2 * 4 * side
        + 4 / 3 * math.pi * r**3
    )
    assert shape.volume == pytest.approx(expected, rel=1e-6)


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
