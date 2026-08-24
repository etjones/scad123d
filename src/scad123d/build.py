"""Walk a CSG tree and build native build123d geometry.

Primitives, grouping (including color()-preserving union), and the analytic
hull()/minkowski() cores are delegated to solid123d -- the shared
OpenSCAD-semantics layer, where they are also usable by pure-Python
SolidPython-style code. What stays here is everything that needs the
OpenSCAD binary or the CSG pipeline: nodes solid123d has no concept of
(multmatrix) live in .solids, and nodes with no BRep equivalent take the
mesh fallback.
"""

import warnings
from dataclasses import dataclass, field
from functools import reduce
from operator import and_, sub

import solid123d as s1
from build123d import Shape
from solid123d import polyhedron
from solid123d.hull import analytic_hull
from solid123d.minkowski import analytic_minkowski

from .facets import (
    DEFAULT_FACET_THRESHOLD,
    faceted_circle,
    faceted_cylinder,
    should_facet,
)
from .mesh import mesh_subtree, warn_meshed
from .nodes import CsgNode
from .solids import apply_matrix

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


def _children_positional(node: CsgNode, options: BuildOptions) -> list[Shape | None]:
    """Like _children, but keeps an empty child as None in its position.

    Unions can drop empties freely; difference and intersection cannot --
    their semantics depend on which operand was empty (see the boolean
    branches in _build).
    """
    out: list[Shape | None] = []
    kept: list[CsgNode] = []
    for child in node.children:
        if child.modifier == "%":
            continue
        shape = _build(child, options)
        if shape is not None and shape._wrapped is None:
            shape = None
        out.append(shape)
        kept.append(child)
    marked = [s for s, c in zip(out, kept) if c.modifier == "!"]
    return marked if marked else out


def _union(shapes: list[Shape]) -> Shape | None:
    """Union children the way an OpenSCAD block does.

    Delegates to solid123d's union() applier -- the shared implicit-union
    with the color()-preserving Compound-vs-fuse logic and the 2D/3D
    mixing warning. Only the empty case differs: an empty OpenSCAD group
    is legal and encloses nothing, so it maps to None here, where
    solid123d (matching SolidPython) raises.
    """
    if not shapes:
        return None
    return s1.union()(shapes)


def _fallback(node: CsgNode, options: BuildOptions, reason: str) -> Shape | None:
    warn_meshed(node.name, reason)
    options.meshed_nodes.append(node.name)
    return mesh_subtree(node, options.timeout)


def _build(node: CsgNode, options: BuildOptions) -> Shape | None:
    name = node.name
    a = node.args

    # Degenerate primitives -- a zero (or negative) critical dimension --
    # produce no geometry in OpenSCAD's render, but OCCT raises
    # Standard_Failure on them. Real code hits this constantly: a library
    # that disables an optional feature by collapsing a dimension to zero
    # (Gridfinity emits cube([42, 42, 0]) for a disabled lip, for example).
    # Returning None matches OpenSCAD: the node contributes nothing and the
    # rest of the model builds normally (_children drops Nones).

    # --- leaves: 3D -----------------------------------------------------
    if name == "cube":
        size = [float(v) for v in a.get("size", [1, 1, 1])]
        if any(v <= 0 for v in size):
            return None
        return s1.cube(size, center=bool(a.get("center", False)))

    if name == "sphere":
        radius = float(a.get("r", 1))
        if radius <= 0:
            return None
        if should_facet(a.get("$fn"), options.facet_threshold):
            # OpenSCAD's sphere tessellation is a ring construction; not worth
            # reproducing for a case this rare, so let OpenSCAD build it.
            return _fallback(node, options, f"faceted sphere, $fn={int(a['$fn'])}")
        return s1.sphere(r=radius)

    if name == "cylinder":
        h = float(a.get("h", 1))
        r1, r2 = float(a.get("r1", 1)), float(a.get("r2", 1))
        if h <= 0 or (r1 <= 0 and r2 <= 0):
            return None  # r1 or r2 alone may be 0: that's a cone
        center = bool(a.get("center", False))
        if should_facet(a.get("$fn"), options.facet_threshold):
            return faceted_cylinder(max(r1, 0), max(r2, 0), h, int(a["$fn"]), center)
        return s1.cylinder(h=h, r1=max(r1, 0), r2=max(r2, 0), center=center)

    if name == "polyhedron":
        return polyhedron(a.get("points", []), a.get("faces", []))

    # --- leaves: 2D -----------------------------------------------------
    if name == "square":
        size = [float(v) for v in a.get("size", [1, 1])]
        if any(v <= 0 for v in size[:2]):
            return None
        return s1.square(size, center=bool(a.get("center", False)))

    if name == "circle":
        radius = float(a.get("r", 1))
        if radius <= 0:
            return None
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

    # difference and intersection care WHERE an empty child sat, so they
    # cannot use _children's empties-dropped list: an empty first
    # difference child empties the result (the minuend is gone -- dropping
    # it would silently promote the first subtrahend to minuend), and any
    # empty intersection operand annihilates the whole result. OpenSCAD
    # agrees on both; found via a real model whose disabled feature left
    # an empty group inside an intersection.
    if name == "difference":
        shapes = _children_positional(node, options)
        if not shapes or shapes[0] is None:
            return None
        rest = [s for s in shapes[1:] if s is not None]
        return reduce(sub, rest, shapes[0])

    if name == "intersection":
        shapes = _children_positional(node, options)
        if not shapes or any(s is None for s in shapes):
            return None
        return reduce(and_, shapes)

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
            # s1.color() also labels the shape (CSS name or hex) when it
            # has no label yet -- no separate labeling step needed here.
            shape = s1.color(
                list(rgba)[:3], alpha=float(list(rgba)[3]) if len(rgba) > 3 else 1.0
            )(shape)
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
        # Real code disables features by leaving a subtree empty; OpenSCAD
        # treats minkowski()/hull() of nothing as nothing, and asking it to
        # mesh an empty subtree is an error, not a fallback.
        if not built:
            return None
        analytic = analytic_minkowski(built)
        if analytic is not None:
            return analytic
        return _fallback(node, options, "second operand is not a sphere/circle")

    if name == "hull":
        built = _children(node, options)
        if not built:
            return None
        analytic = analytic_hull(built)
        if analytic is not None:
            return analytic
        return _fallback(
            node,
            options,
            "children fit none of the closed-form hull cases",
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
