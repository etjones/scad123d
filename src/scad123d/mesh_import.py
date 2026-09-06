"""Turn a mesh file OpenSCAD rendered into build123d solids -- exactly.

build123d's ``Mesher.read`` builds one free face per triangle and asks
``BRepBuilderAPI_Sewing`` to rediscover the shared edges. Sewing is a
heuristic, and on some perfectly good meshes (OpenSCAD's Manifold backend
triangulates planar faces in a way that trips it; found on a slab with a
hull-shaped through-hole) it returns a shell with a mis-oriented patch:
``is_valid`` is False and the volume is off by a few percent, silently.

A mesh from OpenSCAD already *is* consistent topology -- each triangle
lists its vertices by index, wound outward -- so there is nothing to
rediscover. This module builds the BRep the way the mesh describes it:
one vertex per distinct position, one edge per vertex pair, each triangle
a wire of those shared edges reversed as its winding dictates. Connected
components become shells; the largest is the outer boundary, the rest are
voids. Every result is checked against the divergence-theorem volume of
the triangles themselves, and a disagreement raises ``MeshImportError``
instead of returning a wrong shape.
"""

import math
from pathlib import Path

from build123d import Mesher, Shape, Shell, Solid
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_MakeWire,
)
from OCP.gp import gp_Pnt
from OCP.TopoDS import TopoDS, TopoDS_Shell

from .errors import MeshImportError

Point = tuple[float, float, float]
Triangle = tuple[int, int, int]

# Relative disagreement between the solid's volume and the triangles' own
# before the import is declared wrong. Both are computed from the same
# planar facets, so the true difference is floating-point noise.
_VOLUME_RTOL = 1e-6


def read_mesh_file(path: str | Path) -> list[Shape]:
    """Read every mesh object in a 3MF file as a build123d Solid."""
    mesher = Mesher()
    reader = mesher.model.QueryReader("3mf")
    reader.ReadFromFile(str(path))
    iterator = mesher.model.GetMeshObjects()
    shapes: list[Shape] = []
    for _ in range(iterator.Count()):
        iterator.MoveNext()
        mesh = iterator.GetCurrentMeshObject()
        vertices = [tuple(v.Coordinates[0:3]) for v in mesh.GetVertices()]
        triangles = [tuple(t.Indices[0:3]) for t in mesh.GetTriangleIndices()]
        if not triangles:
            continue
        shapes.append(solid_from_triangles(vertices, triangles))
    return shapes


def signed_volume(vertices: list[Point], triangles: list[Triangle]) -> float:
    """Volume enclosed by an outward-wound triangle mesh (divergence theorem)."""
    total = 0.0
    for i, j, k in triangles:
        a, b, c = vertices[i], vertices[j], vertices[k]
        total += (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        )
    return total / 6.0


def _dedupe(
    vertices: list[Point], triangles: list[Triangle]
) -> tuple[list[Point], list[Triangle]]:
    """Merge vertices at identical positions and drop collapsed triangles.

    Exporters (Manifold's among them) may list one position under several
    indices; topology built from indices must see one vertex per position.
    """
    index: dict[Point, int] = {}
    points: list[Point] = []
    remap: list[int] = []
    for p in vertices:
        key = (round(p[0], 9), round(p[1], 9), round(p[2], 9))
        if key not in index:
            index[key] = len(points)
            points.append(p)
        remap.append(index[key])
    remapped = [(remap[i], remap[j], remap[k]) for i, j, k in triangles]
    return points, [t for t in remapped if len(set(t)) == 3]


def _components(triangles: list[Triangle]) -> list[list[int]]:
    """Group triangle indices into edge-connected components (union-find)."""
    parent = list(range(len(triangles)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner: dict[tuple[int, int], int] = {}
    for ti, (i, j, k) in enumerate(triangles):
        for a, b in ((i, j), (j, k), (k, i)):
            key = (a, b) if a < b else (b, a)
            if key in owner:
                parent[find(ti)] = find(owner[key])
            else:
                owner[key] = ti
    groups: dict[int, list[int]] = {}
    for ti in range(len(triangles)):
        groups.setdefault(find(ti), []).append(ti)
    return list(groups.values())


def _build_topologically(points: list[Point], triangles: list[Triangle]) -> Solid:
    vertices = [BRepBuilderAPI_MakeVertex(gp_Pnt(*p)).Vertex() for p in points]
    edges: dict[tuple[int, int], object] = {}

    def edge(a: int, b: int):  # type: ignore[no-untyped-def]
        key = (a, b) if a < b else (b, a)
        if key not in edges:
            edges[key] = BRepBuilderAPI_MakeEdge(
                vertices[key[0]], vertices[key[1]]
            ).Edge()
        e = edges[key]
        return e if (a, b) == key else TopoDS.Edge_s(e.Reversed())

    shells: list[Shell] = []
    for members in _components(triangles):
        builder = BRep_Builder()
        shell = TopoDS_Shell()
        builder.MakeShell(shell)
        for ti in members:
            i, j, k = triangles[ti]
            wire = BRepBuilderAPI_MakeWire(edge(i, j), edge(j, k), edge(k, i)).Wire()
            builder.Add(shell, BRepBuilderAPI_MakeFace(wire, True).Face())
        shell.Closed(True)
        shells.append(Shell(shell))

    outer = max(shells, key=lambda s: math.prod(s.bounding_box().size))
    maker = BRepBuilderAPI_MakeSolid(outer.wrapped)
    for inner in shells:
        if inner is not outer:
            maker.Add(inner.wrapped)
    solid = Solid(maker.Solid())
    if solid.volume < 0:
        solid = Solid(solid.wrapped.Complemented())
    return solid


def _build_by_sewing(points: list[Point], triangles: list[Triangle]) -> Solid:
    """build123d's own approach, kept as the second attempt for meshes whose
    edge sharing is not clean (an edge used by more than two triangles)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer

    sewing = BRepBuilderAPI_Sewing()
    for i, j, k in triangles:
        wire = BRepBuilderAPI_MakeWire(
            BRepBuilderAPI_MakeEdge(gp_Pnt(*points[i]), gp_Pnt(*points[j])).Edge(),
            BRepBuilderAPI_MakeEdge(gp_Pnt(*points[j]), gp_Pnt(*points[k])).Edge(),
            BRepBuilderAPI_MakeEdge(gp_Pnt(*points[k]), gp_Pnt(*points[i])).Edge(),
        ).Wire()
        sewing.Add(BRepBuilderAPI_MakeFace(wire, True).Face())
    sewing.Perform()
    sewn = sewing.SewedShape()
    shells: list[Shell] = []
    explorer = TopExp_Explorer(sewn, TopAbs_ShapeEnum.TopAbs_SHELL)
    while explorer.More():
        shells.append(Shell(TopoDS.Shell_s(explorer.Current())))
        explorer.Next()
    if not shells:
        raise MeshImportError("sewing produced no shell")
    outer = max(shells, key=lambda s: math.prod(s.bounding_box().size))
    maker = BRepBuilderAPI_MakeSolid(outer.wrapped)
    for inner in shells:
        if inner is not outer:
            maker.Add(inner.wrapped)
    solid = Solid(maker.Solid())
    if solid.volume < 0:
        solid = Solid(solid.wrapped.Complemented())
    return solid


def _agrees(solid: Solid, expected: float) -> bool:
    if not solid.is_valid:
        return False
    return math.isclose(solid.volume, expected, rel_tol=_VOLUME_RTOL, abs_tol=1e-9)


def solid_from_triangles(vertices: list[Point], triangles: list[Triangle]) -> Solid:
    """Build a Solid from an indexed triangle mesh, verified by volume.

    Raises MeshImportError if neither the topological build nor sewing
    yields a valid solid enclosing the volume the triangles do.
    """
    points, tris = _dedupe(vertices, triangles)
    if not tris:
        raise MeshImportError("mesh has no non-degenerate triangles")
    expected = abs(signed_volume(points, tris))

    attempts: list[str] = []
    for name, build in (
        ("topology", _build_topologically),
        ("sewing", _build_by_sewing),
    ):
        try:
            solid = build(points, tris)
        except Exception as exc:  # noqa: BLE001 -- OCCT raises many types
            attempts.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if _agrees(solid, expected):
            return solid
        attempts.append(
            f"{name}: volume {solid.volume:.6g} vs triangles {expected:.6g}, "
            f"valid={solid.is_valid}"
        )
    raise MeshImportError(
        "could not build a valid solid from OpenSCAD's mesh "
        f"({len(points)} vertices, {len(tris)} triangles): " + "; ".join(attempts)
    )
