"""Two test tiers.

Tier 1 needs no OpenSCAD binary: it parses committed .csg fixtures and checks
geometry against committed reference metrics. Tier 2 is marked
``needs_openscad`` and regenerates from .scad, comparing against OpenSCAD's own
mesh output.
"""

import json
import pathlib
from pathlib import Path

import pytest

from scad123d.openscad import find_openscad, openscad_version

FIXTURES = Path(__file__).parent / "fixtures"
METRICS_PATH = FIXTURES / "metrics.json"


def pytest_collection_modifyitems(config, items):
    if find_openscad() is not None:
        return
    skip = pytest.mark.skip(reason="OpenSCAD binary not found")
    for item in items:
        if "needs_openscad" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def metrics() -> dict:
    if not METRICS_PATH.exists():
        pytest.skip("no reference metrics; run `just fixtures`")
    return json.loads(METRICS_PATH.read_text())


def require_fixture_openscad_version(metrics: dict) -> None:
    """Skip a test that assumes byte- or tessellation-exact agreement with
    the OpenSCAD version the committed fixtures were generated with.

    Different OpenSCAD versions can legitimately reformat .csg output or
    tessellate curves slightly differently -- CI's own differential job
    commonly runs an older apt-packaged OpenSCAD (2021.01 vs. the 2025.07.18
    fixtures were generated with), where this is expected, not a regression.
    Call this from any test whose assertion is that exact rather than the
    usual convergence-based comparison.
    """
    recorded = metrics.get("_openscad_version")
    current = openscad_version()
    if recorded is not None and current != recorded:
        pytest.skip(
            f"fixtures were generated with {recorded!r}; this machine has "
            f"{current!r}. Exact byte/tessellation agreement doesn't hold "
            f"across OpenSCAD versions -- run `just fixtures` to regenerate "
            f"and verify against this version instead."
        )


def shape_metrics(shape) -> dict:
    bbox = shape.bounding_box()
    centre = shape.center()
    return {
        "volume": shape.volume,
        "bbox": [bbox.size.X, bbox.size.Y, bbox.size.Z],
        "centroid": [centre.X, centre.Y, centre.Z],
    }


def assert_close(got: dict, want: dict, rel: float = 1e-6, abs_: float = 1e-6):
    assert got["volume"] == pytest.approx(want["volume"], rel=rel, abs=abs_)
    for g, w in zip(got["bbox"], want["bbox"]):
        assert g == pytest.approx(w, rel=rel, abs=abs_)
    for g, w in zip(got["centroid"], want["centroid"]):
        assert g == pytest.approx(w, rel=rel, abs=abs_)


def _read_stl_triangles(path) -> list[list[float]]:
    import re
    import struct

    data = pathlib.Path(path).read_bytes()
    if data[:5] == b"solid" and b"facet" in data[:2000]:
        nums = [
            float(v)
            for m in re.findall(
                rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data
            )
            for v in m
        ]
        return [nums[i : i + 9] for i in range(0, len(nums), 9)]
    count = struct.unpack("<I", data[80:84])[0]
    return [
        list(struct.unpack("<12f", data[84 + i * 50 : 84 + i * 50 + 48])[3:12])
        for i in range(count)
    ]


def stl_volume(path) -> float:
    """Total volume of a binary or ASCII STL, via the divergence theorem.

    Computed per connected component, not as one global signed sum: an STL
    with multiple disjoint solids (e.g. two separate polyhedron() calls in
    one .scad file) can have each solid wound independently, and OpenSCAD
    versions differ on whether they normalize that -- confirmed directly:
    the same fixture's implicit union gave the second solid *positive*
    signed volume on one installed OpenSCAD version and *negative* on
    another. A single global signed sum lets two oppositely-wound solids
    partially cancel; abs() on that sum hides the cancellation instead of
    fixing it. Grouping into components and taking abs() per component
    before summing is correct regardless of any given version's winding
    convention.
    """
    tris = _read_stl_triangles(path)

    # Union-Find over triangles, keyed by rounded vertex position so
    # coincident vertices from independently-emitted triangles match despite
    # tiny floating-point differences.
    parent: dict[tuple, tuple] = {}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def vkey(v):
        return (round(v[0], 6), round(v[1], 6), round(v[2], 6))

    tri_vertex_keys = []
    for t in tris:
        keys = [vkey(t[0:3]), vkey(t[3:6]), vkey(t[6:9])]
        tri_vertex_keys.append(keys)
        for k in keys:
            parent.setdefault(k, k)
        union(keys[0], keys[1])
        union(keys[1], keys[2])

    component_totals: dict[tuple, float] = {}
    for t, keys in zip(tris, tri_vertex_keys):
        a, b, c = t[0:3], t[3:6], t[6:9]
        signed = (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        ) / 6.0
        root = find(keys[0])
        component_totals[root] = component_totals.get(root, 0.0) + signed

    return sum(abs(v) for v in component_totals.values())
