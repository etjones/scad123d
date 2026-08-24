"""heal_small_edges: boolean sliver edges are dropped before export.

The reproduction is the analytic-meets-meshed seam from Gridfinity's lip:
an exact cylinder fused with a faceted prism inscribed in the same circle,
stacked so their circular/polygonal boundaries share one plane. The exact
arc and the inscribed polygon cross at every facet chord, and OCCT's fuse
leaves a micro sliver edge at each crossing -- watertight as a BRep, but
reported as open edges by slicers that tessellate each face independently.
"""

import math

import pytest
from build123d import Circle, Polygon, Pos, extrude

from scad123d.heal import heal_small_edges


def test_sliver_seam_is_healed():
    # Polygon vertices poke 2e-6 outside the circle, so each chord crosses
    # the arc twice and the fuse leaves a micro sliver edge (~4e-5 long) at
    # every crossing -- the same count-per-facet, same-scale pattern the
    # real analytic/meshed Gridfinity lip seam produced (80 edges of
    # 6.3e-5, one pair per facet chord, at z=39.39).
    r, n = 2.55, 60
    q = r + 2e-6
    disc = extrude(Circle(r), amount=1)
    pts = [
        (q * math.cos(2 * math.pi * i / n), q * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]
    prism = Pos(0, 0, 1) * extrude(Polygon(*pts, align=None), amount=1)
    fused = disc + prism

    micro_before = [e for e in fused.edges() if e.length < 1e-4]
    assert micro_before, "expected the seam to produce sliver edges"

    healed = heal_small_edges(fused)
    assert not [e for e in healed.edges() if e.length < 1e-4]
    assert healed.is_valid
    assert healed.volume == pytest.approx(fused.volume, rel=1e-4)


def test_clean_shape_is_returned_unchanged():
    disc = extrude(Circle(3), amount=2)
    assert heal_small_edges(disc) is disc
