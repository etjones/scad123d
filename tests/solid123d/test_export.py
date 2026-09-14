"""STEP export: one colored product per region body, read back through
OCCT's XDE reader; the author's tree by default, one group per color on
request; moved trees land where they should."""

from pathlib import Path

import pytest
from build123d import Solid
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.Quantity import Quantity_ColorRGBA
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import (
    XCAFDoc_ColorGen,
    XCAFDoc_ColorSurf,
    XCAFDoc_ColorTool,
    XCAFDoc_DocumentTool,
    XCAFDoc_ShapeTool,
)

import solid123d as s
from solid123d.export import export_step, region_bodies

RED = (1.0, 0.0, 0.0, 1.0)
BLUE = (0.0, 0.0, 1.0, 1.0)


def _volume(shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return round(props.Mass(), 6)


def _color_of(label: TDF_Label, shapes, colors) -> tuple | None:
    """A body's color as the reader stored it.

    A solid's color comes back on its own label. A sheet's (2D geometry)
    comes back as a surface style on its face, which the reader files
    under a subshape label -- so look there too before giving up.
    """
    candidates = [label]
    subs = TDF_LabelSequence()
    XCAFDoc_ShapeTool.GetSubShapes_s(label, subs)
    candidates += [subs.Value(i) for i in range(1, subs.Length() + 1)]
    color = Quantity_ColorRGBA()
    for candidate in candidates:
        for kind in (XCAFDoc_ColorGen, XCAFDoc_ColorSurf):
            if XCAFDoc_ColorTool.GetColor_s(candidate, kind, color):
                rgb = color.GetRGB()
                return (
                    round(rgb.Red(), 6),
                    round(rgb.Green(), 6),
                    round(rgb.Blue(), 6),
                    round(color.Alpha(), 6),
                )
    return None


def _name(label: TDF_Label) -> str:
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return attr.Get().ToExtString()
    return ""


def read_step(path: Path) -> list[tuple[tuple[str, ...], tuple | None, float]]:
    """Every body in the file as (path of names, rgba, volume), where the
    path runs from the top assembly down to the body's own name."""
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    XCAFApp_Application.GetApplication_s().NewDocument(
        TCollection_ExtendedString("MDTV-XCAF"), doc
    )
    reader = STEPCAFControl_Reader()
    reader.SetColorMode(True)
    reader.SetNameMode(True)
    assert reader.ReadFile(str(path)) == 1
    assert reader.Transfer(doc)
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    colors = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    out: list[tuple[tuple[str, ...], tuple | None, float]] = []

    def walk(label: TDF_Label, names: tuple[str, ...]) -> None:
        target = TDF_Label()
        if XCAFDoc_ShapeTool.IsReference_s(label):
            XCAFDoc_ShapeTool.GetReferredShape_s(label, target)
        else:
            target = label
        names = (*names, _name(target))
        if XCAFDoc_ShapeTool.IsAssembly_s(target):
            components = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(target, components)
            for i in range(1, components.Length() + 1):
                walk(components.Value(i), names)
            return
        rgba = _color_of(target, shapes, colors)
        out.append((names, rgba, _volume(XCAFDoc_ShapeTool.GetShape_s(label))))

    free = TDF_LabelSequence()
    shapes.GetFreeShapes(free)
    for i in range(1, free.Length() + 1):
        walk(free.Value(i), ())
    return sorted(out)


def red_blue_overlap():
    return s.union()(
        s.color("red")(s.cube(10)),
        s.color("blue")(s.translate([5, 0, 0])(s.cube(10))),
    )


def test_region_bodies_are_world_placed_solids_with_colors():
    bodies = region_bodies(s.translate([0, 0, 100])(red_blue_overlap()))
    assert sorted((b.label, b.rgba) for b in bodies) == [("blue", BLUE), ("red", RED)]
    for body in bodies:
        assert body.shape.Location().IsIdentity()
        assert Solid(body.shape).bounding_box().min.Z == pytest.approx(100)
    assert sorted(round(b.volume) for b in bodies) == [500, 1000]


def test_each_body_is_its_own_colored_product(tmp_path):
    path = export_step(red_blue_overlap(), tmp_path / "rb.step")
    assert read_step(path) == [
        (("model", "blue"), BLUE, 1000.0),
        (("model", "red"), RED, 500.0),
    ]


def test_authors_tree_is_mirrored_by_default(tmp_path):
    model = s.union()(
        red_blue_overlap(),
        s.color("red")(s.translate([0, 30, 0])(s.cube(4))),
    )
    model.label = "widget"
    rows = read_step(export_step(model, tmp_path / "tree.step"))
    assert [r[0][0] for r in rows] == ["widget"] * 3
    assert sorted(r[1:] for r in rows) == [(BLUE, 1000.0), (RED, 64.0), (RED, 500.0)]


def test_group_by_color_buckets_bodies_under_one_group_per_color(tmp_path):
    model = s.union()(
        red_blue_overlap(),
        s.color("red")(s.translate([0, 30, 0])(s.cube(4))),
        s.translate([0, 60, 0])(s.cube(2)),
    )
    rows = read_step(export_step(model, tmp_path / "grouped.step", group_by_color=True))
    groups = sorted({r[0][1] for r in rows})
    assert groups == ["blue", "red", "uncolored"]
    red = [r for r in rows if r[0][1] == "red"]
    assert sorted(v for _, _, v in red) == [64.0, 500.0]
    assert next(r for r in rows if r[0][1] == "uncolored")[1] is None


def test_single_solid_is_a_free_colored_shape(tmp_path):
    rows = read_step(export_step(s.color("red")(s.cube(10)), tmp_path / "one.step"))
    assert rows == [(("red",), RED, 1000.0)]


def test_uncolored_single_solid_has_no_color(tmp_path):
    rows = read_step(export_step(s.cube(10), tmp_path / "plain.step"))
    assert rows == [(("solid",), None, 1000.0)]


def test_moved_tree_lands_in_world_coordinates(tmp_path):
    model = s.rotate([0, 0, 90])(s.translate([0, 0, 100])(red_blue_overlap()))
    path = export_step(model, tmp_path / "moved.step")
    rows = read_step(path)
    assert sorted(v for _, _, v in rows) == [500.0, 1000.0]
    from build123d import import_step

    bb = import_step(str(path)).bounding_box()
    assert (bb.min.Z, bb.max.Z) == pytest.approx((100, 110))
    assert bb.min.X == pytest.approx(-10)


def test_alpha_survives(tmp_path):
    rows = read_step(export_step(s.color("red", 0.5)(s.cube(10)), tmp_path / "a.step"))
    assert rows[0][1] == (1.0, 0.0, 0.0, 0.5)


def test_render_uses_the_body_exporter(tmp_path):
    out = s.scad_render_to_file(red_blue_overlap(), tmp_path / "r.step")
    assert len(read_step(Path(out))) == 2


def test_two_d_geometry_exports_as_a_sheet(tmp_path):
    from build123d import import_step

    path = export_step(s.color("red")(s.square(10)), tmp_path / "flat.step")
    imported = import_step(str(path))
    assert not imported.solids()
    assert sum(f.area for f in imported.faces()) == pytest.approx(100)
    assert read_step(path) == [(("red",), RED, 0.0)]
