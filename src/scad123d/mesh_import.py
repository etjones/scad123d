"""Turn a mesh file OpenSCAD rendered into build123d solids -- exactly.

build123d's ``Mesher.read`` builds one free face per triangle and asks
``BRepBuilderAPI_Sewing`` to rediscover the shared edges. Sewing is a
heuristic, and on some perfectly good meshes (OpenSCAD's Manifold backend
triangulates planar faces in a way that trips it; found on a slab with a
hull-shaped through-hole) it returns a shell with a mis-oriented patch:
``is_valid`` is False and the volume is off by a few percent, silently.

A mesh from OpenSCAD already *is* consistent topology -- each triangle
lists its vertices by index -- so there is nothing to rediscover. This
module builds the BRep the way the mesh describes it: one vertex per
distinct position, one edge per vertex pair, each triangle a wire of those
shared edges reversed as its winding dictates. Edge-connected components
become shells. Which shells are bodies and which are cavities is decided
geometrically (a shell inside another is that body's void), not from
winding: OpenSCAD's own output winds voids inward, but a user polyhedron
listed inside-out passes through Manifold still inside-out, and CGAL
repairs it -- so winding is normalized per component first, then ignored.

Every result is checked against the divergence-theorem volume of the
triangles themselves, and a disagreement raises ``MeshImportError``
instead of returning a wrong shape.
"""

import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from build123d import Compound, Mesher, Pos, Shape, Shell, Solid
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Sewing,
)
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.gp import gp_Pnt
from OCP.TopAbs import TopAbs_ShapeEnum, TopAbs_State
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shell

from .errors import MeshImportError

Point = tuple[float, float, float]
Triangle = tuple[int, int, int]

# Relative disagreement between the solid's volume and the triangles' own
# before the import is declared wrong. Both are computed from the same
# planar facets, so the true difference is floating-point noise.
_VOLUME_RTOL = 1e-6


def read_mesh_file(path: str | Path) -> list[Shape]:
    """Read every mesh object in a 3MF file as a build123d shape."""
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


class _Component:
    """One closed shell, wound outward, with a point known to lie on it."""

    def __init__(self, shell: TopoDS_Shell, volume: float, probe: Point) -> None:
        self.shell = shell
        self.volume = volume  # positive: the shell has been normalized outward
        self.probe = probe
        self.solid = Solid(BRepBuilderAPI_MakeSolid(shell).Solid())
        self.bbox = self.solid.bounding_box()


def _shells_topologically(
    points: list[Point], triangles: list[Triangle]
) -> list[_Component]:
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

    components: list[_Component] = []
    for members in _components(triangles):
        tris = [triangles[ti] for ti in members]
        volume = signed_volume(points, tris)
        if volume < 0:  # wound inward (a void, or an inside-out polyhedron)
            tris = [(i, k, j) for i, j, k in tris]
            volume = -volume
        builder = BRep_Builder()
        shell = TopoDS_Shell()
        builder.MakeShell(shell)
        for i, j, k in tris:
            wire = BRepBuilderAPI_MakeWire(edge(i, j), edge(j, k), edge(k, i)).Wire()
            builder.Add(shell, BRepBuilderAPI_MakeFace(wire, True).Face())
        shell.Closed(True)
        i, j, k = tris[0]
        probe = tuple(
            (points[i][d] + points[j][d] + points[k][d]) / 3 for d in range(3)
        )
        components.append(_Component(shell, volume, probe))  # type: ignore[arg-type]
    return components


def _shells_by_sewing(
    points: list[Point], triangles: list[Triangle]
) -> list[_Component]:
    """build123d's own approach, kept as the second attempt for meshes whose
    edge sharing is not clean (an edge used by more than two triangles)."""
    sewing = BRepBuilderAPI_Sewing()
    for i, j, k in triangles:
        wire = BRepBuilderAPI_MakeWire(
            BRepBuilderAPI_MakeEdge(gp_Pnt(*points[i]), gp_Pnt(*points[j])).Edge(),
            BRepBuilderAPI_MakeEdge(gp_Pnt(*points[j]), gp_Pnt(*points[k])).Edge(),
            BRepBuilderAPI_MakeEdge(gp_Pnt(*points[k]), gp_Pnt(*points[i])).Edge(),
        ).Wire()
        sewing.Add(BRepBuilderAPI_MakeFace(wire, True).Face())
    sewing.Perform()
    components: list[_Component] = []
    explorer = TopExp_Explorer(sewing.SewedShape(), TopAbs_ShapeEnum.TopAbs_SHELL)
    while explorer.More():
        shell = TopoDS.Shell_s(explorer.Current())
        volume = Solid(BRepBuilderAPI_MakeSolid(shell).Solid()).volume
        if volume < 0:
            shell = TopoDS.Shell_s(shell.Reversed())
            volume = -volume
        probe = Shell(shell).faces()[0].center()
        components.append(_Component(shell, volume, (probe.X, probe.Y, probe.Z)))
        explorer.Next()
    if not components:
        raise MeshImportError("sewing produced no shell")
    return components


def _contains(outer: _Component, inner: _Component) -> bool:
    """Whether a point on ``inner``'s surface lies strictly inside ``outer``."""
    bb, ib = outer.bbox, inner.bbox
    if not (
        bb.min.X <= ib.min.X
        and bb.min.Y <= ib.min.Y
        and bb.min.Z <= ib.min.Z
        and ib.max.X <= bb.max.X
        and ib.max.Y <= bb.max.Y
        and ib.max.Z <= bb.max.Z
    ):
        return False
    classifier = BRepClass3d_SolidClassifier(
        outer.solid.wrapped, gp_Pnt(*inner.probe), 1e-7
    )
    return classifier.State() == TopAbs_State.TopAbs_IN


def _assemble(components: list[_Component]) -> tuple[Shape, float]:
    """Nest shells into solids with voids; returns the shape and the volume
    the nesting implies (bodies minus cavities), for the caller to verify.

    Each shell's parent is the smallest shell enclosing it. Even nesting
    depth is material, odd is a cavity -- so an island inside a cavity is a
    body again.
    """
    order = sorted(range(len(components)), key=lambda i: components[i].volume)
    parent: list[int | None] = [None] * len(components)
    for rank, i in enumerate(order):
        for j in order[rank + 1 :]:  # candidates larger than i, smallest first
            if _contains(components[j], components[i]):
                parent[i] = j
                break

    def depth(i: int) -> int:
        d = 0
        while parent[i] is not None:
            i = parent[i]  # type: ignore[assignment]
            d += 1
        return d

    depths = [depth(i) for i in range(len(components))]
    solids: list[Solid] = []
    expected = 0.0
    for i, comp in enumerate(components):
        if depths[i] % 2:
            expected -= comp.volume
            continue
        expected += comp.volume
        maker = BRepBuilderAPI_MakeSolid(comp.shell)
        for j, other in enumerate(components):
            if parent[j] == i:
                maker.Add(TopoDS.Shell_s(other.shell.Reversed()))
        solids.append(Solid(maker.Solid()))
    shape: Shape = solids[0] if len(solids) == 1 else Compound(solids)
    return shape, expected


def _agrees(shape: Shape, expected: float) -> bool:
    if not shape.is_valid:
        return False
    return math.isclose(shape.volume, expected, rel_tol=_VOLUME_RTOL, abs_tol=1e-9)


def solid_from_triangles(vertices: list[Point], triangles: list[Triangle]) -> Shape:
    """Build a Solid (or a Compound of them) from an indexed triangle mesh,
    verified by volume.

    Raises MeshImportError if neither the topological build nor sewing
    yields a valid shape enclosing the volume the triangles do.
    """
    points, tris = _dedupe(vertices, triangles)
    if not tris:
        raise MeshImportError("mesh has no non-degenerate triangles")

    attempts: list[str] = []
    for name, build in (
        ("topology", _shells_topologically),
        ("sewing", _shells_by_sewing),
    ):
        try:
            shape, expected = _assemble(build(points, tris))
        except Exception as exc:  # noqa: BLE001 -- OCCT raises many types
            attempts.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if _agrees(shape, expected):
            return shape
        attempts.append(
            f"{name}: volume {shape.volume:.6g} vs triangles {expected:.6g}, "
            f"valid={shape.is_valid}"
        )
    raise MeshImportError(
        "could not build a valid solid from OpenSCAD's mesh "
        f"({len(points)} vertices, {len(tris)} triangles): " + "; ".join(attempts)
    )


def _nested(bboxes: list[tuple[Point, Point]], i: int, j: int) -> bool:
    (imin, imax), (jmin, jmax) = bboxes[i], bboxes[j]
    return all(jmin[d] <= imin[d] and imax[d] <= jmax[d] for d in range(3))


SHREDDED_FRACTION = 0.02


@dataclass(frozen=True)
class MeshReport:
    """What OpenSCAD's own render measures, and whether it can be trusted
    to measure anything.

    A closed, consistently oriented surface encloses a definite volume.
    One with a boundary edge (used by a single triangle), a non-manifold
    edge (used by three or more), or two triangles walking an edge the
    same way has no well-defined inside, so the number the divergence
    theorem returns over it is not a measurement. Neither is a negative
    one: an outward-facing closed surface cannot enclose less than
    nothing.
    """

    volume: float
    triangles: int
    boundary_edges: int
    nonmanifold_edges: int
    flipped_edges: int

    @property
    def usable(self) -> bool:
        """Does this mesh bound a definite region?

        An open surface has no inside. A closed one that encloses negative
        volume is inside out. A render that is *mostly* self-intersections
        is not a surface anyone can measure. But a handful of non-manifold
        edges is ordinary: two solids touching along an edge exports as an
        edge with four triangles on it, and the volume is still exact.

        The threshold matters more than it looks. Measured over the
        corpus, 303 of the 512 references a stricter rule rejected had
        under 1% non-manifold edges -- real disagreements, wrongly set
        aside -- while the renders that are visibly shredded run from 4%
        to 69%.
        """
        if self.boundary_edges or self.volume < 0:
            return False
        return self.nonmanifold_edges <= self.edges * SHREDDED_FRACTION

    @property
    def edges(self) -> int:
        return max(self.triangles * 3 // 2, 1)

    def fault(self) -> str:
        """Why this mesh cannot be measured, in the order worth reporting."""
        if self.boundary_edges:
            return (
                f"it is an open surface ({self.boundary_edges:,} edges have "
                "only one triangle)"
            )
        if self.volume < 0:
            return f"it encloses a negative volume ({self.volume:.6g})"
        return (
            f"it is {100 * self.nonmanifold_edges / self.edges:.0f}% "
            f"self-intersecting ({self.nonmanifold_edges:,} edges shared by "
            "three or more triangles)"
        )


def _edge_faults(triangles: list[Triangle]) -> tuple[int, int, int]:
    """(boundary, non-manifold, contradictory) edge counts of a mesh."""
    undirected: Counter = Counter()
    directed: Counter = Counter()
    for a, b, c in triangles:
        for u, v in ((a, b), (b, c), (c, a)):
            undirected[frozenset((u, v))] += 1
            directed[(u, v)] += 1
    return (
        sum(1 for n in undirected.values() if n == 1),
        sum(1 for n in undirected.values() if n > 2),
        sum(1 for n in directed.values() if n > 1),
    )


def soup_report(points: list, triangles: list) -> MeshReport:
    """A MeshReport for a triangle soup that is already indexed."""
    boundary, nonmanifold, flipped = _edge_faults(triangles)
    return MeshReport(
        signed_volume(points, triangles),
        len(triangles),
        boundary,
        nonmanifold,
        flipped,
    )


def mesh_volume(path: str | Path) -> float:
    """Volume enclosed by every mesh in a 3MF file (see ``mesh_report``)."""
    return mesh_report(path).volume


def mesh_report(path: str | Path) -> MeshReport:
    """Volume enclosed by every mesh in a 3MF file, without building a BRep,
    with the evidence for whether that volume means anything.

    For checking a build against OpenSCAD's own render, where the render
    may have tens of thousands of facets: pure arithmetic over the
    triangles, no OCCT.

    Whether a component is a body or a cavity comes from its winding:
    OpenSCAD winds bodies outward (positive signed volume) and cavities
    inward. Bounding boxes are *not* a containment test -- the balls of a
    ball bearing sit inside the ring's box without being inside the ring,
    and calling them cavities made a real model's volume come out negative.
    The box is consulted only to rescue the one case winding gets wrong: a
    user polyhedron listed inside-out passes through Manifold still
    inside-out, and if nothing could enclose it, it is a body.
    """
    mesher = Mesher()
    reader = mesher.model.QueryReader("3mf")
    reader.ReadFromFile(str(path))
    iterator = mesher.model.GetMeshObjects()
    total = 0.0
    faults = [0, 0, 0]
    facets = 0
    for _ in range(iterator.Count()):
        iterator.MoveNext()
        mesh = iterator.GetCurrentMeshObject()
        vertices = [tuple(v.Coordinates[0:3]) for v in mesh.GetVertices()]
        triangles = [tuple(t.Indices[0:3]) for t in mesh.GetTriangleIndices()]
        points, tris = _dedupe(vertices, triangles)  # type: ignore[arg-type]
        facets += len(tris)
        for i, count in enumerate(_edge_faults(tris)):
            faults[i] += count
        volumes: list[float] = []
        bboxes: list[tuple[Point, Point]] = []
        for members in _components(tris):
            comp = [tris[ti] for ti in members]
            volumes.append(signed_volume(points, comp))
            used = {v for t in comp for v in t}
            xs = [points[v][0] for v in used]
            ys = [points[v][1] for v in used]
            zs = [points[v][2] for v in used]
            bboxes.append(((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))))
        for i, volume in enumerate(volumes):
            if volume >= 0:
                total += volume
                continue
            enclosable = any(
                j != i and abs(volumes[j]) > -volume and _nested(bboxes, i, j)
                for j in range(len(volumes))
            )
            total += volume if enclosable else -volume
    return MeshReport(total, facets, faults[0], faults[1], faults[2])


def unit_extrusion(csg_source: str) -> str:
    """The CSG source wrapped in a 1 mm ``linear_extrude`` -- the way to get
    OpenSCAD to render 2D geometry, which it will not export to 3MF."""
    return f"linear_extrude(height = 1, center = false, convexity = 10) {{\n{csg_source}\n}}"


def profile_from_unit_extrusion(shapes: list[Shape]) -> Shape | None:
    """Recover the 2D region a unit extrusion was made from: its top faces,
    merged from the import's triangles into one face per island (holes
    kept), and moved back to z = 0 with +Z normals, matching how every
    other 2D shape in the pipeline is built."""
    faces = [
        f
        for s in shapes
        for f in s.faces()
        if abs(f.center().Z - 1.0) < 1e-6 and f.normal_at().Z > 0.5
    ]
    if not faces:
        return None
    merged = Compound(faces).clean()
    return Pos(0, 0, -1) * merged
