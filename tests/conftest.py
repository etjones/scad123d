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


def stl_volume(path) -> float:
    """Signed volume of a binary or ASCII STL, via the divergence theorem."""
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
        tris = [nums[i : i + 9] for i in range(0, len(nums), 9)]
    else:
        count = struct.unpack("<I", data[80:84])[0]
        tris = [
            list(struct.unpack("<12f", data[84 + i * 50 : 84 + i * 50 + 48])[3:12])
            for i in range(count)
        ]
    total = 0.0
    for t in tris:
        a, b, c = t[0:3], t[3:6], t[6:9]
        total += (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        ) / 6.0
    return abs(total)
