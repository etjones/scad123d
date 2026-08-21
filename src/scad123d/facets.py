"""The $fn policy, and analytic faceted primitives.

OpenSCAD's $fn is ambiguous. Set globally it is a complexity switch and you
want exact BRep curves; set at a call site it is intentional geometry
(``circle(r=10, $fn=6)`` *is* a hexagon). The CSG export records only the
effective value at each node, so the two cases are indistinguishable -- see
README. We therefore discriminate on magnitude via ``facet_threshold``.
"""

import math

from build123d import Polygon, Shape

DEFAULT_FACET_THRESHOLD = 20


def should_facet(fn: float | None, threshold: int) -> bool:
    """True when an explicit, small $fn should be honored as real geometry.

    $fn == 0 means unset ($fa/$fs driving), which is never an intentional
    polygon, so it always yields exact curves.
    """
    if not fn or threshold <= 0:
        return False
    count = int(fn)
    return 3 <= count < threshold


def ngon_points(radius: float, count: int) -> list[tuple[float, float]]:
    """Vertices of an OpenSCAD n-gon: angle i*360/n, first vertex on +X."""
    step = 2 * math.pi / count
    return [
        (radius * math.cos(i * step), radius * math.sin(i * step)) for i in range(count)
    ]


def faceted_circle(radius: float, count: int) -> Shape:
    return Polygon(*ngon_points(radius, count), align=None)


def faceted_cylinder(
    r1: float, r2: float, height: float, count: int, center: bool
) -> Shape:
    """An n-gon prism, frustum, or cone, matching OpenSCAD's tessellation."""
    z0 = -height / 2 if center else 0.0
    z1 = z0 + height

    if r1 <= 0 and r2 <= 0:
        raise ValueError("cylinder needs a positive radius")

    bottom = [(x, y, z0) for x, y in ngon_points(r1, count)] if r1 > 0 else [(0, 0, z0)]
    top = [(x, y, z1) for x, y in ngon_points(r2, count)] if r2 > 0 else [(0, 0, z1)]

    points: list[tuple[float, float, float]] = bottom + top
    nb, nt = len(bottom), len(top)
    faces: list[list[int]] = []

    if nb > 1:
        faces.append(list(range(nb - 1, -1, -1)))
    if nt > 1:
        faces.append([nb + i for i in range(nt)])

    for i in range(count):
        j = (i + 1) % count
        if nb > 1 and nt > 1:
            faces.append([i, j, nb + j, nb + i])
        elif nb > 1:  # cone narrowing to a point at the top
            faces.append([i, j, nb])
        else:  # cone widening from a point at the bottom
            faces.append([0, 1 + j, 1 + i])

    from solid123d import polyhedron

    return polyhedron(points, faces)
