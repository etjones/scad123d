"""Post-boolean healing: drop micro-edge slivers before handing shapes out.

A fuse between an analytic region and a meshed one -- an exact corner arc
against the faceted circle a mesh fallback inscribes in it -- crosses once
per facet chord, leaving a sliver edge (~1e-5 long) at every crossing. The
BRep stays topologically closed, but slicers tessellate each face
independently and report the slivers as open edges. Both representations
were meshes before scad123d grew analytic hull rungs, tessellated
identically by the same OpenSCAD render, so their seam vertices matched
exactly and the problem could not occur.
"""

from build123d import Compound, Shape
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.ShapeFix import ShapeFix_FixSmallFace, ShapeFix_Shape, ShapeFix_Wireframe

# Far below any real model feature (0.1 micron-scale slivers at mm scale),
# far above OCCT's linear precision (1e-7).
_SMALL_EDGE = 1e-4


def _heal_one(shape: Shape) -> Shape:
    # Small FACES first: a sliver face's edges cannot be dropped while the
    # face still needs them, so edge fixing alone leaves one micro-edge per
    # sliver face behind. Then the wireframe pass drops the remaining
    # micro-edges and closes the wire gaps that removal opens.
    small_face = ShapeFix_FixSmallFace()
    small_face.Init(shape.wrapped)
    small_face.SetPrecision(_SMALL_EDGE)
    small_face.Perform()
    wireframe = ShapeFix_Wireframe(small_face.FixShape())
    wireframe.SetPrecision(_SMALL_EDGE)
    wireframe.ModeDropSmallEdges = True
    wireframe.FixSmallEdges()
    wireframe.FixWireGaps()
    fix = ShapeFix_Shape(wireframe.Shape())
    fix.Perform()
    healed = fix.Shape()
    if not BRepCheck_Analyzer(healed).IsValid():
        return shape
    # Shape.cast is abstract; any concrete subclass's cast dispatches on the
    # actual TopoDS type and returns the right wrapper.
    result = Compound.cast(healed)
    # Slivers carry near-zero volume, so healing must not move the total;
    # a bigger change means ShapeFix rebuilt something it shouldn't have.
    if abs(result.volume - shape.volume) > 1e-4 * abs(shape.volume):
        return shape
    # ShapeFix does not carry display metadata; keep the original's.
    result.color = shape.color
    result.label = shape.label
    return result


def heal_small_edges(shape: Shape) -> Shape:
    """Drop boolean sliver edges shorter than 1e-4; a no-op on clean shapes.

    Cost model (measured on a 2.5k-face Gridfinity tray, 14s build): the
    detection scan every import pays is tens of milliseconds -- edge count
    is small even for models whose *tessellations* run to 100k triangles,
    because BRep edges are analytic. The ShapeFix passes only run when the
    scan finds a sub-1e-4 edge, and cost seconds (~5s on that tray, mostly
    inside OCCT); that price is paid exactly by the shapes that would
    otherwise fail in a slicer, once per import.

    Recurses into a Compound's children instead of healing it whole so an
    authored-color assembly keeps its structure (ShapeFix would rebuild the
    tree and orphan each child's color and label). If healing ever leaves a
    shape invalid or moves its volume, the original is returned unchanged --
    slivers render fine everywhere except mesh diagnostics, so keeping them
    beats corrupting geometry.
    """
    if not any(e.length < _SMALL_EDGE for e in shape.edges()):
        return shape
    if isinstance(shape, Compound) and shape.children:
        rebuilt = Compound(children=[heal_small_edges(c) for c in shape.children])
        rebuilt.color = shape.color
        rebuilt.label = shape.label
        return rebuilt
    return _heal_one(shape)
