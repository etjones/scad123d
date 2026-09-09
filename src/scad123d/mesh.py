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

from build123d import Shape

from .emit import emit
from .errors import MeshFallbackWarning, OpenSCADRunError
from .mesh_import import profile_from_unit_extrusion, read_mesh_file, unit_extrusion
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

    3MF is used rather than STL: it carries indexed (shared-vertex)
    triangles, which mesh_import.py turns into exact BRep topology without
    any sewing heuristics.

    Results are memoized on the emitted source. Every return -- including
    the first -- is a copy, so no caller ever holds the cached original:
    downstream code is free to move()/locate()/recolor its shape in place
    without corrupting what the next identical subtree receives.
    """
    source = emit(node)
    if source not in _cache:
        _cache[source] = _normalised(_render(source, timeout))
    result = _cache[source]
    return None if result is None else copy.copy(result)


def _normalised(shape: Shape | None) -> Shape | None:
    """Clean an imported mesh before anyone builds on it.

    OCCT's booleans handle a raw import badly: intersecting one against a
    sphere returned the sphere. Cleaning it first is what makes them work
    -- not by merging faces, which a twisted surface has none to merge
    (3302 before and after), but by leaving the shape in the state a
    boolean expects. Until this ran here, whether a model got a cleaned
    mesh depended on *history*. The cached shape is shared by
    every copy handed out (copy.copy is shallow, so the TopoDS is common),
    so the first boolean to touch it updated the shared shape in place and
    every later use of the same subtree behaved better than the first.

    A bauble whose two twisted extrudes are meshed measured 87,144 built on
    its own and 2,766 built in a process that had already built the same
    subtree -- the same code, the same mesh, three orders of magnitude
    apart, and the batch only ever sees the first. Cleaning once at import
    makes the first use behave like the rest.

    clean() is the guarded one from solid123d, which keeps the unclean
    shape if the unify would not conserve volume, so this cannot lose
    material to the seam defect it works around.
    """
    return None if shape is None else shape.clean()


def _render(source: str, timeout: float) -> Shape | None:
    try:
        path = export_mesh(source, suffix=".3mf", timeout=timeout)
    except OpenSCADRunError as exc:
        # An empty subtree is legal OpenSCAD (a disabled feature, an
        # intersection of disjoint parts); rendering it standalone is the
        # only thing that errors. Empty means "encloses nothing", not
        # "failed" -- same contract as an empty boolean.
        if "Current top level object is empty" in str(exc):
            return None
        # OpenSCAD only exports 3D to 3MF. A 2D subtree -- hull() of
        # circles inside a linear_extrude is the everyday case -- is
        # rendered as a 1 mm extrusion instead, and its top face is the
        # profile. (15 of the first 500 corpus models needed this.)
        if "not a 3D object" in str(exc):
            return _render_2d(source, timeout)
        raise
    try:
        shapes = read_mesh_file(path)
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)

    if not shapes:
        return None
    result = shapes[0]
    for extra in shapes[1:]:
        result = result + extra
    return result


def _render_2d(source: str, timeout: float) -> Shape | None:
    path = export_mesh(unit_extrusion(source), suffix=".3mf", timeout=timeout)
    try:
        shapes = read_mesh_file(path)
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)
    return profile_from_unit_extrusion(shapes) if shapes else None


def warn_meshed(node_name: str, reason: str) -> None:
    warnings.warn(
        f"scad123d: {node_name}() has no BRep equivalent ({reason}); "
        f"rendered to a mesh via OpenSCAD. Fillets and face selectors will "
        f"not behave analytically on this region.",
        MeshFallbackWarning,
        stacklevel=3,
    )
