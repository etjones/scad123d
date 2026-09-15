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
from .occt_workarounds import BOOLEAN_RETRY_FRACTIONS

# Faces on an operand before an empty intersection is worth a second
# look. Well above anything a handful of primitives produces, well
# below the thousands a fused thread or lattice carries.
SLIVER_FACE_COUNT = 200

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
        result = _common_or_retry(result, other)
    return result


def _boxes_overlap(a: Shape, b: Shape) -> bool:
    """Do these two shapes' bounding boxes share any volume?

    Conservative on purpose: boxes that overlap say nothing about whether
    the shapes do, so this can only ever raise a suspicion. Boxes that do
    *not* overlap are proof the shapes cannot, which is the direction that
    matters here.
    """
    one, two = a.bounding_box(), b.bounding_box()
    return (
        one.min.X < two.max.X
        and two.min.X < one.max.X
        and one.min.Y < two.max.Y
        and two.min.Y < one.max.Y
        and one.min.Z < two.max.Z
        and two.min.Z < one.max.Z
    )


def _sliver_prone(a: Shape, b: Shape) -> bool:
    """Is either operand complex enough to carry the slivers that make OCCT
    give up?

    The retries are not free -- four more booleans on shapes that may be
    large -- so they have to be earned. Slivers come from fusing many small
    bodies: the thread that prompted this is 288 polyhedra unioned into
    5,496 faces, 1,218 of them under a millionth of a square millimetre.
    Two boxes have twelve faces between them and no such hazard, and an
    empty intersection of simple shapes is simply an empty intersection.

    Without this gate a model whose intersections are legitimately empty
    pays for the retries on every one: infinitycube.scad went from 31
    seconds to 138, straight past the timeout.
    """
    return max(len(a.faces()), len(b.faces())) >= SLIVER_FACE_COUNT


def _common_or_retry(a: Shape, b: Shape) -> Shape:
    """The common part of *a* and *b*, retried fuzzily if it comes back
    empty from shapes whose bounding boxes overlap.

    An empty intersection of two shapes that cannot possibly meet is the
    right answer, and the bounding boxes settle that for free. An empty one
    from shapes that *do* share a box is a claim worth checking, because
    OCCT will return it for geometry it merely found hard.

    A thread built the way every OpenSCAD thread library builds one -- 288
    small polyhedra unioned, then trimmed to length by intersecting a box
    -- came back empty. The thread was valid, the box was valid, and
    BOPAlgo_ArgumentAnalyzer flagged nothing, because the 1,218 sliver
    faces the union left behind are all just above Precision::Confusion.
    OCCT is not indifferent to them the way a mesh kernel is. A fuzzy value
    coarser than the slivers and finer than the real features returns the
    answer: 39.53 against the zero the plain call gave, and the model it
    came from went from 18% short to 0.1%.
    """
    result = boolean([a], [b], BRepAlgoAPI_Common())
    # Topology, not mass properties: OCCT recomputes those from scratch on
    # every access, and this runs on every intersection in every model. An
    # empty result has nothing in it to find, which is the only question
    # being asked here.
    if result.solids() or result.faces():
        return result
    if not _sliver_prone(a, b) or not _boxes_overlap(a, b):
        return result
    diagonal = max(a.bounding_box().diagonal, b.bounding_box().diagonal)
    for fraction in BOOLEAN_RETRY_FRACTIONS:
        operation = BRepAlgoAPI_Common()
        operation.SetFuzzyValue(diagonal * fraction)
        try:
            candidate = boolean([a], [b], operation)
        except Exception:  # noqa: BLE001, S112 -- a coarser value may still work
            continue
        if candidate.solids() or candidate.faces():
            return candidate
    # Every retry still says empty. It may genuinely be -- overlapping
    # boxes are not overlapping shapes -- so this is not an error, and the
    # empty result stands.
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
