"""STEP export with one product per region body, colored on the body.

build123d's exporter colors *tree nodes* and treats a flat Compound as one
leaf, so color and structure are welded together there. Here the XDE
document is built directly: every leaf body becomes its own STEP product
carrying its resolved color and name, and the assembly tree above the
bodies is a separate choice -- the author's own grouping by default, or
one group per color for consumers that read structure and nothing else
(slicers). Regrouping never touches a body.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from os import PathLike, fsdecode
from pathlib import Path

from build123d import Compound, Location, Shape
from OCP.APIHeaderSection import APIHeaderSection_MakeHeader
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.Interface import Interface_Static
from OCP.Message import Message, Message_Gravity
from OCP.Quantity import Quantity_ColorRGBA
from OCP.STEPCAFControl import STEPCAFControl_Controller, STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Controller
from OCP.TCollection import TCollection_ExtendedString, TCollection_HAsciiString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import (
    XCAFDoc_ColorGen,
    XCAFDoc_ColorSurf,
    XCAFDoc_ColorTool,
    XCAFDoc_DocumentTool,
    XCAFDoc_ShapeTool,
)

from ._common import _rgba, baked_topods, color_label

UNCOLORED = "uncolored"


@dataclass
class Body:
    """One region: a solid in world coordinates with its resolved color."""

    shape: TopoDS_Shape
    rgba: tuple | None
    label: str

    @property
    def key(self) -> tuple | None:
        """Bucket key: colors equal to display precision are one color."""
        return None if self.rgba is None else tuple(round(v, 3) for v in self.rgba)

    @property
    def volume(self) -> float:
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(self.shape, props)
        return abs(props.Mass())


def region_bodies(shape: Shape) -> list[Body]:
    """Every leaf solid of *shape*'s tree, placed in world coordinates."""
    bodies: list[Body] = []

    def walk(node: Shape, acc: Location) -> None:
        if node.children:
            here = acc * node.location
            for child in node.children:
                walk(child, here)
            return
        placed = node.moved(acc)
        rgba = _rgba(node)
        label = node.label or (color_label(rgba) if rgba else "solid")
        # A 2D leaf (a sketch, an unextruded profile) has no solids; it is
        # exported whole, as the sheet it is.
        pieces = placed.solids() or [placed]
        for piece in pieces:
            bodies.append(Body(baked_topods(piece.wrapped), rgba, label))

    walk(shape, Location())
    return bodies


def _new_document() -> TDocStd_Document:
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    XCAFApp_Application.GetApplication_s().NewDocument(
        TCollection_ExtendedString("MDTV-XCAF"), doc
    )
    return doc


def _name(label: TDF_Label, text: str) -> None:
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(text))


def _add_body(
    shapes: XCAFDoc_ShapeTool,
    colors: XCAFDoc_ColorTool,
    parent: TDF_Label | None,
    body: Body,
) -> TDF_Label:
    part = shapes.AddShape(body.shape, False)
    _name(part, body.label)
    if body.rgba is not None:
        r, g, b, a = (*body.rgba, 1.0)[:4] if len(body.rgba) == 3 else body.rgba
        rgba = Quantity_ColorRGBA(r, g, b, a)
        # Generic is what a solid carries and what CAD importers read off
        # the body. A sheet (2D geometry) has no volume to color, so OCCT
        # writes its color as a face style instead and reads it back on a
        # subshape label -- setting both is what makes 2D round-trip.
        colors.SetColor(part, rgba, XCAFDoc_ColorGen)
        colors.SetColor(part, rgba, XCAFDoc_ColorSurf)
    if parent is not None:
        shapes.AddComponent(parent, part, TopLoc_Location())
    return part


def _add_tree(
    shapes: XCAFDoc_ShapeTool,
    colors: XCAFDoc_ColorTool,
    parent: TDF_Label,
    node: Shape,
    acc: Location,
) -> None:
    """Mirror the author's tree: a group per Compound-with-children, a
    product per leaf solid."""
    if node.children:
        group = shapes.NewShape()
        _name(group, node.label or "group")
        here = acc * node.location
        for child in node.children:
            _add_tree(shapes, colors, group, child, here)
        shapes.AddComponent(parent, group, TopLoc_Location())
        return
    for body in region_bodies(node.moved(acc)):
        _add_body(shapes, colors, parent, body)


def _add_by_color(
    shapes: XCAFDoc_ShapeTool,
    colors: XCAFDoc_ColorTool,
    root: TDF_Label,
    bodies: Iterable[Body],
) -> None:
    """One group per resolved color, in order of first appearance, with an
    ``uncolored`` group for the rest. The author's grouping is discarded."""
    buckets: dict[tuple | None, list[Body]] = {}
    for body in bodies:
        buckets.setdefault(body.key, []).append(body)
    for key, members in buckets.items():
        group = shapes.NewShape()
        _name(group, color_label(key) if key else UNCOLORED)
        for body in members:
            _add_body(shapes, colors, group, body)
        shapes.AddComponent(root, group, TopLoc_Location())


def _quiet() -> None:
    for printer in Message.DefaultMessenger_s().Printers():
        printer.SetTraceLevel(Message_Gravity.Message_Fail)


def export_step(
    shape: Shape,
    path: str | PathLike[str],
    *,
    group_by_color: bool = False,
) -> Path:
    """Write *shape* to STEP with a colored product per region body.

    By default the assembly tree mirrors the author's grouping. With
    ``group_by_color`` the bodies are bucketed under one group per color
    instead (plus ``uncolored``), which is what slicers read -- at the cost
    of any grouping the author made.
    """
    _quiet()
    doc = _new_document()
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colors = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    bodies = region_bodies(shape)
    if not bodies or shape.wrapped is None:
        raise ValueError("nothing to export: the shape is empty")
    if len(bodies) == 1 and not (group_by_color or shape.children):
        _add_body(shapes, colors, None, bodies[0])
    else:
        root = shapes.NewShape()
        _name(root, shape.label or "model")
        if group_by_color:
            _add_by_color(shapes, colors, root, bodies)
        elif isinstance(shape, Compound) and shape.children:
            here = shape.location
            for child in shape.children:
                _add_tree(shapes, colors, root, child, here)
        else:
            for body in bodies:
                _add_body(shapes, colors, root, body)
    shapes.UpdateAssemblies()

    STEPCAFControl_Controller.Init_s()
    STEPControl_Controller.Init_s()
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer = STEPCAFControl_Writer()
    writer.SetColorMode(True)
    writer.SetNameMode(True)
    writer.SetLayerMode(True)
    header = APIHeaderSection_MakeHeader(writer.Writer().Model())
    if header.IsDone():
        header.SetOriginatingSystem(TCollection_HAsciiString("solid123d"))
        if shape.label:
            header.SetName(TCollection_HAsciiString(shape.label))
    writer.Transfer(doc, STEPControl_AsIs)
    status = writer.Write(fsdecode(path))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(f"failed to write STEP file {path}")
    return Path(path)
