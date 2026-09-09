"""The in-memory mesh-fallback cache: one OpenSCAD render per distinct
emitted subtree, with every caller receiving an independent copy."""

import pytest

import scad123d
from scad123d import mesh

pytestmark = pytest.mark.needs_openscad

# hull() of three unequal-radius spheres has no analytic rung -> mesh fallback.
_FALLBACK_HULL = """\
hull() {
    sphere($fn = 0, $fa = 12, $fs = 2, r = 2);
    multmatrix([[1,0,0,10],[0,1,0,0],[0,0,1,0],[0,0,0,1]]) {
        sphere($fn = 0, $fa = 12, $fs = 2, r = 3);
    }
    multmatrix([[1,0,0,0],[0,1,0,10],[0,0,1,0],[0,0,0,1]]) {
        sphere($fn = 0, $fa = 12, $fs = 2, r = 4);
    }
}"""


def _translated(x: float, body: str) -> str:
    return f"multmatrix([[1,0,0,{x}],[0,1,0,0],[0,0,1,0],[0,0,0,1]]) {{\n{body}\n}}"


@pytest.fixture(autouse=True)
def _fresh_cache():
    mesh.clear_cache()
    yield
    mesh.clear_cache()


@pytest.fixture()
def render_count(monkeypatch):
    calls = []
    real = mesh._render

    def counting(source: str, timeout: float):
        calls.append(source)
        return real(source, timeout)

    monkeypatch.setattr(mesh, "_render", counting)
    return calls


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_identical_subtrees_render_once(render_count):
    source = (
        "union() {\n"
        + "\n".join(_translated(x, _FALLBACK_HULL) for x in (0, 50))
        + "\n}"
    )
    shape = scad123d.import_csg(source)
    assert len(render_count) == 1
    # Both placements survived as independent, correctly-located geometry:
    # the parent multmatrix moved each copy without disturbing the other.
    solids = shape.solids()
    assert len(solids) == 2
    xs = sorted(s.center().X for s in solids)
    assert xs[1] - xs[0] == pytest.approx(50, abs=1e-6)
    assert solids[0].volume == pytest.approx(solids[1].volume, rel=1e-9)


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_distinct_subtrees_render_separately(render_count):
    bigger = _FALLBACK_HULL.replace("r = 4", "r = 5")
    source = (
        "union() {\n"
        + _translated(0, _FALLBACK_HULL)
        + "\n"
        + _translated(50, bigger)
        + "\n}"
    )
    scad123d.import_csg(source)
    assert len(render_count) == 2


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_cache_survives_across_imports(render_count):
    scad123d.import_csg(_FALLBACK_HULL)
    scad123d.import_csg(_FALLBACK_HULL)
    assert len(render_count) == 1


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_clear_cache_forces_a_rerender(render_count):
    scad123d.import_csg(_FALLBACK_HULL)
    mesh.clear_cache()
    scad123d.import_csg(_FALLBACK_HULL)
    assert len(render_count) == 2


# --- the imported mesh is normalised before anyone builds on it -------------


BAUBLE = """intersection() {
\tlinear_extrude(height = 55, center = true, twist = 120, slices = 55, $fn = 0, $fa = 12, $fs = 2) {
\t\tmultmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {
\t\t\tsquare(size = [4, 29.5], center = false);
\t\t}
\t}
\tsphere($fn = 0, $fa = 12, $fs = 2, r = 27.5);
}"""


def _volume(shape) -> float:
    return sum(abs(s.volume) for s in shape.solids())


@pytest.mark.needs_openscad
def test_a_meshed_subtree_builds_the_same_cold_and_warm():
    """The invariant that was broken: a model's result must not depend on
    whether the same meshed subtree was already built in this process.

    copy.copy is shallow, so every copy handed out of the cache shares one
    TopoDS shape. The first boolean to touch it improved the shared shape
    in place, and every later build of the same subtree inherited that --
    so the answer depended on history, and a batch worker only ever sees
    the first, worst version. A bauble measured 87,144 cold and 2,766 warm.
    Cleaning the mesh once at import makes the first use behave like the
    rest.
    """
    mesh.clear_cache()
    cold = _volume(scad123d.import_csg(BAUBLE))
    warm = _volume(scad123d.import_csg(BAUBLE))
    assert cold == pytest.approx(warm, rel=1e-9)

    mesh.clear_cache()
    again = _volume(scad123d.import_csg(BAUBLE))
    assert again == pytest.approx(cold, rel=1e-9)


@pytest.mark.needs_openscad
def test_the_cached_mesh_is_already_normalised():
    """Cleaning the cached shape again changes nothing, which is what makes
    the first use behave like every later one."""
    mesh.clear_cache()
    scad123d.import_csg(BAUBLE)
    cached = [s for s in mesh._cache.values() if s is not None]
    assert cached, "the twisted extrude should have been meshed"
    for shape in cached:
        assert len(shape.clean().faces()) == len(shape.faces())
