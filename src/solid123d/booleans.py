"""OpenSCAD boolean operations: ``union()(a, b)``, ``difference()(a, b)``.

Note that because primitives are native build123d shapes, the algebra
operators also work directly: ``a + b`` (union), ``a - b`` (difference),
``a & b`` (intersection). SolidPython's ``a * b`` intersection operator
is NOT available — use ``a & b`` or ``intersection()(a, b)``.

``hull()`` and ``minkowski()`` evaluate every case that has a closed-form
BRep answer (see hull.py and minkowski.py) and raise NotImplementedError
for the rest — build123d has no general convex-hull or Minkowski operator,
and a faceted approximation would defeat the point of a BRep kernel.
"""

import warnings
from collections.abc import Callable

from build123d import Shape
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

from ._common import (
    VOLUME_EPS,
    _carries_color,
    _recolored,
    boolean,
    checked,
    cut_all,
    flatten,
    group,
    own_rgba,
    total_volume,
    world_leaves,
)
from .hull import analytic_hull
from .minkowski import analytic_minkowski

Applier = Callable[..., Shape]


def union() -> Applier:
    def apply(*children: Shape) -> Shape:
        return group(children)

    return apply


def difference() -> Applier:
    def apply(*children: Shape) -> Shape:
        shapes = flatten(children)
        if not shapes:
            raise ValueError("difference() requires at least one shape")
        base, *cutters = shapes
        if not cutters:
            return base
        # OCCT's Cut takes every subtrahend at once: A - (B u C) is
        # (A - B) - C, in one pass over A instead of one per subtrahend.
        if not _carries_color(base):
            return cut_all([base], cutters)
        return _cut_regions(base, cutters)

    return apply


def _cut_regions(base: Shape, cutters: list[Shape]) -> Shape:
    """Difference that keeps the retained material's colors.

    Each region body of *base* is cut on its own and gets its color back;
    the cutters' colors are irrelevant (a cutter is a hole, not material).
    """
    regions = world_leaves(base)
    plain = cut_all(regions, cutters)
    kept: list[Shape] = []
    for region in regions:
        piece = cut_all([region], cutters)
        if total_volume(piece) > VOLUME_EPS:
            kept.append(_recolored(piece, own_rgba(region), region.label))
    return checked(kept, plain, "difference")


def intersection() -> Applier:
    def apply(*children: Shape) -> Shape:
        shapes = flatten(children)
        if not shapes:
            raise ValueError("intersection() requires at least one shape")
        if len(shapes) == 1 or not any(_carries_color(s) for s in shapes):
            return _intersect_plain(shapes)
        return _intersect_regions(shapes)

    return apply


def _intersect_plain(shapes: list[Shape]) -> Shape:
    """Intersection of every operand, each decomposed into its own bodies.

    OCCT's Common of an argument list and a tool list is the common part of
    their unions, so folding operand by operand keeps OpenSCAD's semantics
    while never handing a compound over whole.
    """
    result = shapes[0]
    for other in shapes[1:]:
        result = boolean([result], [other], BRepAlgoAPI_Common())
    return result


def _intersect_regions(shapes: list[Shape]) -> Shape:
    """Intersection under the union's precedence rule: the shared material
    takes the later operand's color when it has one, else the earlier's."""
    current = world_leaves(shapes[0])
    plain: Shape | None = None
    for other in shapes[1:]:
        others = world_leaves(other)
        plain = boolean(
            world_leaves(plain) if plain else current, others, BRepAlgoAPI_Common()
        )
        following: list[Shape] = []
        for a in current:
            for b in others:
                piece = boolean([a], [b], BRepAlgoAPI_Common())
                if total_volume(piece) <= VOLUME_EPS:
                    continue
                winner = b if own_rgba(b) is not None else a
                following.append(_recolored(piece, own_rgba(winner), winner.label))
        current = following
    assert plain is not None
    return checked(current, plain, "intersection")


def _warn_new_material(operation: str, shapes: list[Shape]) -> None:
    """hull() and minkowski() create material that belonged to no child, so
    no child's color can own it; say so rather than guess. An enclosing
    color() still applies to the whole result."""
    if any(_carries_color(s) for s in shapes):
        warnings.warn(
            f"solid123d: {operation}() creates new material; the children's "
            "color() assignments are dropped (an enclosing color() applies to "
            "the whole result)",
            stacklevel=3,
        )


def hull() -> Applier:
    def apply(*children: Shape) -> Shape:
        shapes = flatten(children)
        if not shapes:
            raise ValueError("hull() requires at least one shape")
        result = analytic_hull(shapes)
        if result is not None:
            _warn_new_material("hull", shapes)
            return result
        raise NotImplementedError(
            "hull() of these children has no closed-form BRep answer. "
            "Supported exactly: equal-radius spheres (any count/positions), "
            "equal-radius parallel cylinders sharing one span, exactly two "
            "spheres of any radii, exactly two 2D circles, and any "
            "collection of purely flat-faced (polyhedral) children. "
            "Notably NOT supported: three or more spheres of unequal radii "
            "(needs tritangent planes / power-diagram combinatorics) and "
            "mixed curved children -- model those explicitly (loft/sweep), "
            "or import through scad123d, which renders unsupported hulls "
            "as meshes via OpenSCAD"
        )

    return apply


def minkowski() -> Applier:
    def apply(*children: Shape) -> Shape:
        shapes = flatten(children)
        if not shapes:
            raise ValueError("minkowski() requires at least one shape")
        result = analytic_minkowski(shapes)
        if result is not None:
            _warn_new_material("minkowski", shapes)
            return result
        raise NotImplementedError(
            "minkowski() has no general build123d equivalent; the common "
            "case of rounding a shape, minkowski()(A, sphere(r)) or "
            "minkowski()(A, circle(r)) -- including a sphere tessellated as "
            "a polyhedron, as BOSL2 rounding kernels are -- is computed "
            "exactly as offset(A, r) and works automatically. For anything "
            "else, use offset() or fillet/chamfer on the build123d object "
            "instead"
        )

    return apply
