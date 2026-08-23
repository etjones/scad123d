"""The in-memory mesh-fallback cache: one OpenSCAD render per distinct
emitted subtree, with every caller receiving an independent copy."""

import pytest

import scad123d
from scad123d import mesh

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
