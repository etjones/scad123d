"""Walk a CSG tree and build native build123d geometry.

Primitives are delegated to solid123d, which already renders OpenSCAD shapes as
build123d objects. Nodes solid123d has no concept of (multmatrix, polyhedron)
live in .solids; nodes with no BRep equivalent take the mesh fallback.
"""

import math
import warnings
from dataclasses import dataclass, field
from functools import reduce
from operator import add, and_, sub

import solid123d as s1
import webcolors
from build123d import Compound, Shape

from .facets import (
    DEFAULT_FACET_THRESHOLD,
    faceted_circle,
    faceted_cylinder,
    should_facet,
)
from .hull import analytic_hull
from .mesh import mesh_subtree, warn_meshed
from .minkowski import analytic_minkowski
from .nodes import CsgNode
from .solids import apply_matrix, polyhedron

# CSG text() emits halign/valign="default"; solid123d expects OpenSCAD's names.
_HALIGN = {"default": "left", "left": "left", "center": "center", "right": "right"}
_VALIGN = {
    "default": "baseline",
    "baseline": "baseline",
    "bottom": "bottom",
    "center": "center",
    "top": "top",
}


@dataclass
class BuildOptions:
    facet_threshold: int = DEFAULT_FACET_THRESHOLD
    mesh_scope: str = "minimal"
    timeout: float = 600
    meshed_nodes: list[str] = field(default_factory=list)


def build(node: CsgNode, options: BuildOptions | None = None) -> Shape | None:
    """Build a CSG tree into a build123d Shape (None if it encloses nothing)."""
    options = options or BuildOptions()
    if options.mesh_scope == "hoist" and node.unsupported_nodes():
        names = sorted(set(node.unsupported_nodes()))
        warn_meshed(", ".join(names), "mesh_scope='hoist'")
        options.meshed_nodes.extend(names)
        return mesh_subtree(node, options.timeout)
    return _build(node, options)


def _children(node: CsgNode, options: BuildOptions) -> list[Shape]:
    out: list[Shape] = []
    for child in node.children:
        if child.modifier == "%":  # background: drawn but not part of the model
            continue
        shape = _build(child, options)
        # A boolean op can legitimately produce nothing (e.g. an intersection
        # that doesn't overlap); OpenSCAD just drops it from the tree, so treat
        # an empty-but-not-None shape the same as None everywhere downstream.
        if shape is not None and shape._wrapped is not None:
            out.append(shape)
    # `!` (show only) discards every sibling of the marked child.
    marked = [
        s
        for s, c in zip(out, [c for c in node.children if c.modifier != "%"])
        if c.modifier == "!"
    ]
    return marked or out


def _color_label(rgba: list[float] | tuple[float, ...]) -> str:
    """A human-readable name for an rgba value: the exact CSS color name
    when there is one (OpenSCAD's color names are the CSS/SVG names, so
    ``color("red")`` round-trips back to ``"red"`` even though the CSG
    export only records ``[1, 0, 0, 1]``), otherwise the hex string.

    Used to label the build123d shapes that ``color()`` produces, so parts
    show up in STEP viewers and slicers under a recognizable name instead
    of OCCT's auto-generated ``COMPOUND``/``SOLID``.
    """
    r, g, b = (round(float(v) * 255) for v in list(rgba)[:3])
    try:
        return webcolors.rgb_to_name((r, g, b))
    except ValueError:
        return f"#{r:02x}{g:02x}{b:02x}"


def _carries_color(shape: Shape) -> bool:
    """Does this shape, or any shape nested under it, have an explicit color?

    Checks the private ``_color`` rather than the ``color`` property: the
    property walks *up* through parents and caches what it finds, so it
    reports inherited color, not authored color -- and it's authored color
    (an explicit ``color()`` in the .scad) that signals "these are distinct
    parts". Descends through ``children`` because a nested disjoint colored
    group arrives here as a Compound whose own color is unset but whose
    children carry theirs.
    """
    if shape._color is not None:
        return True
    return any(_carries_color(child) for child in shape.children)


def _union(shapes: list[Shape]) -> Shape | None:
    """Union children the way an OpenSCAD block does.

    OpenSCAD refuses to mix 2D and 3D in one group and warns; adding a Face to
    a Solid in build123d silently degenerates instead, so filter explicitly and
    keep the 3D geometry.
    """
    if not shapes:
        return None
    solid = [s for s in shapes if s.solids()]
    if solid and len(solid) != len(shapes):
        warnings.warn(
            "scad123d: a group mixes 2D and 3D children, which OpenSCAD does "
            "not support; the 2D children were dropped",
            stacklevel=3,
        )
        shapes = solid
    if len(shapes) == 1:
        return shapes[0]

    fused = reduce(add, shapes)

    # A real boolean fuse can't tell us which color survives once it's
    # merged overlapping material away, so it only ever keeps one color for
    # the whole result -- losing distinctly-colored children (see color()
    # below) the moment they're grouped with anything else. Detect when
    # nothing was actually merged -- the fused volume matches the naive sum
    # of the children's volumes -- and in that case a Compound of the
    # children, kept as separate bodies, is a legitimate alternative to the
    # fuse: same total volume, but grouping (unlike fusing) doesn't touch
    # each child's own color/label/material.
    #
    # Volume alone can't distinguish *disjoint* children from children that
    # touch along a shared surface without overlapping (a part designed to
    # sit exactly in a cavity cut for it -- zero shared volume, real shared
    # face). A fuse glues that shared face away, merging the parts into one
    # solid with a smaller total surface area (confirmed directly: two
    # touching unit cubes fuse to 1 solid, unchanged volume, smaller area),
    # so the area comparison below tells the two cases apart:
    #
    # - Truly disjoint (volume AND area both match the naive sums): always
    #   return the Compound. A fuse of disjoint bodies keeps them as
    #   separate solids anyway, so nothing is lost and colors survive.
    # - Touching (volume matches, area doesn't): the two results genuinely
    #   differ -- OpenSCAD's union semantics say one merged solid, but
    #   merging is exactly what destroys per-part color. Color is the tie
    #   breaker: children that carry colors are evidence the author means
    #   them as distinct parts (a multi-material print, an assembly), so
    #   keep them separate; uncolored children get the faithful OpenSCAD
    #   merge, preserving longstanding behavior for plain geometry.
    #
    # `children=` (not a flat `Compound(shapes)`) matters too: it's what
    # makes this an assembly the STEP exporter's PreOrderIter walks node by
    # node, applying each child's own .color -- a flat Compound with no
    # parent/child tree is treated as one leaf and gets a single color
    # splashed across every solid inside it instead.
    total_volume = sum(s.volume for s in shapes)
    if math.isclose(fused.volume, total_volume, rel_tol=1e-9, abs_tol=1e-9):
        total_area = sum(s.area for s in shapes)
        areas_match = math.isclose(
            fused.area, total_area, rel_tol=1e-9, abs_tol=1e-9
        )
        if areas_match or any(_carries_color(s) for s in shapes):
            return Compound(children=list(shapes))

    # Real overlap: which color the merged region should be is genuinely
    # undefined without OCCT-level boolean history tracking, but a real
    # fuse doesn't even keep the *first* child's color -- confirmed
    # directly, fusing two colored, overlapping shapes gives a result with
    # no color at all. No color is a worse default than picking one, so
    # fall back to the first child that had one, same as OpenSCAD's own
    # rendering of overlapping colors effectively does (one object's color
    # wins the ambiguous region, not neither).
    if fused.color is None:
        for s in shapes:
            if s.color is not None:
                fused.color = s.color
                break
    if fused.color is not None and not fused.label:
        fused.label = _color_label(tuple(fused.color))
    return fused


def _fallback(node: CsgNode, options: BuildOptions, reason: str) -> Shape | None:
    warn_meshed(node.name, reason)
    options.meshed_nodes.append(node.name)
    return mesh_subtree(node, options.timeout)


def _build(node: CsgNode, options: BuildOptions) -> Shape | None:
    name = node.name
    a = node.args

    # --- leaves: 3D -----------------------------------------------------
    if name == "cube":
        size = a.get("size", [1, 1, 1])
        return s1.cube(list(size), center=bool(a.get("center", False)))

    if name == "sphere":
        radius = float(a.get("r", 1))
        if should_facet(a.get("$fn"), options.facet_threshold):
            # OpenSCAD's sphere tessellation is a ring construction; not worth
            # reproducing for a case this rare, so let OpenSCAD build it.
            return _fallback(node, options, f"faceted sphere, $fn={int(a['$fn'])}")
        return s1.sphere(r=radius)

    if name == "cylinder":
        h = float(a.get("h", 1))
        r1, r2 = float(a.get("r1", 1)), float(a.get("r2", 1))
        center = bool(a.get("center", False))
        if should_facet(a.get("$fn"), options.facet_threshold):
            return faceted_cylinder(r1, r2, h, int(a["$fn"]), center)
        return s1.cylinder(h=h, r1=r1, r2=r2, center=center)

    if name == "polyhedron":
        return polyhedron(a.get("points", []), a.get("faces", []))

    # --- leaves: 2D -----------------------------------------------------
    if name == "square":
        size = a.get("size", [1, 1])
        return s1.square(list(size), center=bool(a.get("center", False)))

    if name == "circle":
        radius = float(a.get("r", 1))
        if should_facet(a.get("$fn"), options.facet_threshold):
            return faceted_circle(radius, int(a["$fn"]))
        return s1.circle(r=radius)

    if name == "polygon":
        return s1.polygon(a.get("points", []), paths=a.get("paths"))

    if name == "text":
        return s1.text(
            a.get("text", ""),
            size=float(a.get("size", 10)),
            font=a.get("font") or None,
            halign=_HALIGN.get(str(a.get("halign", "default")), "left"),
            valign=_VALIGN.get(str(a.get("valign", "default")), "baseline"),
            spacing=float(a.get("spacing", 1)),
        )

    # --- booleans and grouping ------------------------------------------
    if name in ("group", "union", "render"):
        return _union(_children(node, options))

    if name == "difference":
        shapes = _children(node, options)
        return reduce(sub, shapes) if shapes else None

    if name == "intersection":
        shapes = _children(node, options)
        return reduce(and_, shapes) if shapes else None

    # --- transforms -----------------------------------------------------
    if name == "multmatrix":
        shape = _union(_children(node, options))
        matrix = a.get("_0")
        if shape is None or matrix is None:
            return shape
        return apply_matrix(shape, matrix)

    if name == "color":
        shape = _union(_children(node, options))
        rgba = a.get("_0") or a.get("c")
        if shape is not None and rgba:
            shape = s1.color(
                list(rgba)[:3], alpha=float(list(rgba)[3]) if len(rgba) > 3 else 1.0
            )(shape)
            if not shape.label:
                shape.label = _color_label(rgba)
        return shape

    if name == "resize":
        shape = _union(_children(node, options))
        if shape is None:
            return None
        auto = a.get("auto", False)
        return s1.resize(
            list(a.get("newsize", [0, 0, 0])),
            auto=[bool(x) for x in auto] if isinstance(auto, list) else bool(auto),
        )(shape)

    if name == "offset":
        shape = _union(_children(node, options))
        if shape is None:
            return None
        r, delta = a.get("r"), a.get("delta")
        if r in (None, 0) and delta in (None, 0):
            return shape
        return s1.offset(
            r=float(r) if r else None,
            delta=float(delta) if delta else None,
            chamfer=bool(a.get("chamfer", False)),
        )(shape)

    # --- 2D -> 3D -------------------------------------------------------
    if name == "linear_extrude":
        if float(a.get("twist", 0) or 0):
            return _fallback(node, options, "linear_extrude(twist=...)")
        shape = _union(_children(node, options))
        if shape is None:
            return None
        scale = a.get("scale", 1)
        return s1.linear_extrude(
            height=float(a.get("height", 100)),
            center=bool(a.get("center", False)),
            scale=tuple(scale) if isinstance(scale, list) else float(scale),
        )(shape)

    if name == "rotate_extrude":
        shape = _union(_children(node, options))
        if shape is None:
            return None
        return s1.rotate_extrude(angle=float(a.get("angle", 360)))(shape)

    # --- no BRep equivalent ---------------------------------------------
    if name == "minkowski":
        built = _children(node, options)
        analytic = analytic_minkowski(built)
        if analytic is not None:
            return analytic
        return _fallback(node, options, "second operand is not a sphere/circle")

    if name == "hull":
        built = _children(node, options)
        analytic = analytic_hull(built)
        if analytic is not None:
            return analytic
        return _fallback(
            node, options, "not all children are equal-radius spheres/cylinders"
        )

    if name in ("projection", "surface", "import"):
        reasons = {
            "projection": "not implemented",
            "surface": "not implemented",
            "import": "imports a mesh",
        }
        return _fallback(node, options, reasons[name])

    warnings.warn(f"scad123d: unrecognised CSG node {name!r}; ignored", stacklevel=3)
    return None
