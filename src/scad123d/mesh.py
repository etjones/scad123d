"""Rung 0: the mesh fallback.

Nodes with no BRep equivalent (hull, non-spherical minkowski, projection,
surface, mesh import) are written back out as .csg -- which OpenSCAD accepts as
input -- rendered to a mesh, and spliced into the tree.

The result is exactly as accurate as OpenSCAD, because it *is* OpenSCAD. What
is lost is analytic surfaces in that region: fillets there are not meaningful
and selectors return triangles.
"""

import shutil
import warnings

from build123d import Mesher, Shape

from .emit import emit
from .nodes import CsgNode
from .openscad import export_mesh


def mesh_subtree(node: CsgNode, timeout: float = 600) -> Shape | None:
    """Render one CSG subtree via OpenSCAD and import it as a build123d Shape.

    3MF is used rather than STL: it carries manifold information, so the
    imported solid needs less repair.
    """
    source = emit(node)
    path = export_mesh(source, suffix=".3mf", timeout=timeout)
    try:
        shapes = Mesher().read(str(path))
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)

    if not shapes:
        return None
    result = shapes[0]
    for extra in shapes[1:]:
        result = result + extra
    return result


def warn_meshed(node_name: str, reason: str) -> None:
    warnings.warn(
        f"scad123d: {node_name}() has no BRep equivalent ({reason}); "
        f"rendered to a mesh via OpenSCAD. Fillets and face selectors will "
        f"not behave analytically on this region.",
        stacklevel=3,
    )
