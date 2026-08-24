"""Rung 0: the mesh fallback.

Nodes with no BRep equivalent (hull, non-spherical minkowski, projection,
surface, mesh import) are written back out as .csg -- which OpenSCAD accepts as
input -- rendered to a mesh, and spliced into the tree.

The result is exactly as accurate as OpenSCAD, because it *is* OpenSCAD. What
is lost is analytic surfaces in that region: fillets there are not meaningful
and selectors return triangles.
"""

import copy
import shutil
import warnings

from build123d import Mesher, Shape

from .emit import emit
from .errors import MeshFallbackWarning
from .nodes import CsgNode
from .openscad import export_mesh

# Meshed subtrees keyed by their emitted CSG source. The source text is a
# complete cache key: emit() bakes every transform into multmatrix rows and
# every $fn/$fa/$fs into the primitive calls, so identical text renders to
# identical geometry (per OpenSCAD binary -- which cannot change within one
# process). Repetition is the norm in real models: a Gridfinity tray stamps
# the same base-pad hull once per grid cell, 100+ identical OpenSCAD runs
# without this. In-memory only, deliberately: a persistent cache would need
# the key to also cover the OpenSCAD version and the content of any
# path-referenced inputs (import(), surface(), fonts for text()).
_cache: dict[str, Shape | None] = {}


def clear_cache() -> None:
    """Drop all memoized meshes (for tests and long-lived processes)."""
    _cache.clear()


def mesh_subtree(node: CsgNode, timeout: float = 600) -> Shape | None:
    """Render one CSG subtree via OpenSCAD and import it as a build123d Shape.

    3MF is used rather than STL: it carries manifold information, so the
    imported solid needs less repair.

    Results are memoized on the emitted source. Every return -- including
    the first -- is a copy, so no caller ever holds the cached original:
    downstream code is free to move()/locate()/recolor its shape in place
    without corrupting what the next identical subtree receives.
    """
    source = emit(node)
    if source not in _cache:
        _cache[source] = _render(source, timeout)
    result = _cache[source]
    return None if result is None else copy.copy(result)


def _render(source: str, timeout: float) -> Shape | None:
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
        MeshFallbackWarning,
        stacklevel=3,
    )
