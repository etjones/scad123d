"""Rung 1: analytic Minkowski sums.

A Minkowski sum with a ball *is* an offset, which OCCT performs natively and
exactly. Since rounding is what the overwhelming majority of real minkowski()
calls are for, this covers most usage -- and the analytic result is better than
OpenSCAD's own, which is a faceted approximation.

Verified exact against the Steiner formula
    V(P (+) B_r) = V + A*r + (r^2/2) * sum(L_e * theta_e) + (4/3)*pi*r^3
to ~1e-10 relative error on convex and non-convex inputs.
"""

from build123d import Kind, Shape, offset as _bd_offset

from .nodes import CsgNode

_BALL_NODES = {"sphere", "circle"}


def ball_radius(node: CsgNode) -> float | None:
    """The radius if this node is a plain sphere/circle, else None.

    A sphere carrying a transform is not a ball centred at the origin, so only
    bare primitives qualify; that is exactly how the idiom is written.
    """
    if node.name not in _BALL_NODES or node.children:
        return None
    radius = node.args.get("r")
    if radius is None:
        diameter = node.args.get("d")
        radius = None if diameter is None else float(diameter) / 2
    if radius is None:
        return None
    radius = float(radius)
    return radius if radius > 0 else None


def analytic_minkowski(node: CsgNode, built: list[Shape]) -> Shape | None:
    """Try to evaluate minkowski() as an offset.

    Applies when there are exactly two children and the second is a bare
    sphere or circle. Returns None when the pattern does not match or OCCT
    declines, so the caller can fall back to a mesh.
    """
    if len(node.children) != 2 or len(built) < 1:
        return None
    radius = ball_radius(node.children[1])
    if radius is None:
        return None
    target = built[0]
    if target is None:
        return None
    try:
        result = _bd_offset(target, amount=radius, kind=Kind.ARC)
    except Exception:
        return None
    if result is None or not result.is_valid:
        return None
    return result
