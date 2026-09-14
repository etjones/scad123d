"""OpenSCAD primitive shapes, emitted as build123d objects.

``segments`` / ``$fn`` style arguments are accepted and ignored:
build123d is a BRep kernel, so curves are exact.
"""

from collections.abc import Sequence

from build123d import (
    Align,
    Box,
    Cone,
    Cylinder,
    Face,
    FontStyle,
    Rectangle,
    Shape,
    Shell,
    Solid,
    TextAlign,
    Vector,
    Wire,
)
from build123d import Circle as _BdCircle
from build123d import Polygon as _BdPolygon
from build123d import Sphere as _BdSphere
from build123d import Text as _BdText

from ._common import vec3
from .fonts import (
    fallback_font_path,
    find_font_path,
    isolate_family,
    known_family,
    parse_font_spec,
)

_CENTERED = (Align.CENTER, Align.CENTER, Align.CENTER)
_CORNER = (Align.MIN, Align.MIN, Align.MIN)


def cube(size: float | Sequence[float] = 1, center: bool = False) -> Shape:
    x, y, z = vec3(size)
    return Box(x, y, z, align=_CENTERED if center else _CORNER)


def sphere(
    r: float | None = None,
    d: float | None = None,
    segments: int | None = None,
) -> Shape:
    radius = r if r is not None else (d / 2 if d is not None else 1.0)
    return _BdSphere(radius)


def cylinder(
    r: float | None = None,
    h: float | None = None,
    r1: float | None = None,
    r2: float | None = None,
    center: bool = False,
    d: float | None = None,
    d1: float | None = None,
    d2: float | None = None,
    segments: int | None = None,
) -> Shape:
    base = r if r is not None else (d / 2 if d is not None else None)
    bottom = r1 if r1 is not None else (d1 / 2 if d1 is not None else base)
    top = r2 if r2 is not None else (d2 / 2 if d2 is not None else base)
    bottom = 1.0 if bottom is None else float(bottom)
    top = 1.0 if top is None else float(top)
    height = 1.0 if h is None else float(h)
    z_align = Align.CENTER if center else Align.MIN
    align = (Align.CENTER, Align.CENTER, z_align)
    if bottom == top:
        return Cylinder(bottom, height, align=align)
    return Cone(bottom_radius=bottom, top_radius=top, height=height, align=align)


def square(size: float | Sequence[float] = 1, center: bool = False) -> Shape:
    x, y, _ = vec3(size)
    align = (Align.CENTER, Align.CENTER) if center else (Align.MIN, Align.MIN)
    return Rectangle(x, y, align=align)


def circle(
    r: float | None = None,
    d: float | None = None,
    segments: int | None = None,
) -> Shape:
    radius = r if r is not None else (d / 2 if d is not None else 1.0)
    return _BdCircle(radius)


def polyhedron(
    points: Sequence[Sequence[float]],
    faces: Sequence[Sequence[int]] | None = None,
    convexity: int | None = None,
    triangles: Sequence[Sequence[int]] | None = None,
) -> Shape:
    """Build a solid from explicit points and index faces (OpenSCAD polyhedron).

    Winding is not trusted: if the result encloses negative volume it is
    reversed, matching OpenSCAD's tolerance for either orientation.
    ``convexity`` is accepted and ignored (a preview hint in OpenSCAD);
    ``triangles`` is OpenSCAD's deprecated pre-2014 spelling of ``faces``.
    """
    if faces is None:
        faces = triangles
    if faces is None:
        raise ValueError("polyhedron() requires faces")
    verts = [Vector(float(p[0]), float(p[1]), float(p[2])) for p in points]
    built: list[Face] = []
    for face in faces:
        if len(face) < 3:
            continue
        loop = [verts[i] for i in face]
        built.extend(_faces_of_loop(loop))
    if not built:
        raise ValueError("polyhedron() needs at least one face")

    solid = Solid(Shell(built))
    if solid.volume < 0:
        solid = Solid(solid.wrapped.Complemented())
    return solid


def _faces_of_loop(loop: list[Vector]) -> list[Face]:
    """One face for a loop OCCT accepts; two triangles for a bent quad.

    OpenSCAD's polyhedron() accepts a face whose vertices are not coplanar
    and tessellates it, where OCCT refuses to build a planar face from a
    bent wire and raises "wires not planar" -- so the model produced no
    STEP at all, and 259 of the 274 corpus models failing that way are
    polyhedra.

    OCCT decides, not a tolerance of ours: what counts as flat enough is
    its own criterion, and a model whose faces sit just inside it was
    still being rejected when we guessed at the threshold ourselves.

    Only a quad is split here. OpenSCAD tessellates with libtess2 asking
    for TESS_CONSTRAINED_DELAUNAY_TRIANGLES, which chooses a diagonal by
    the Delaunay criterion rather than by vertex order; for a quad the two
    agree, verified against OpenSCAD for every winding, and beyond a quad
    they do not -- a bent pentagon came out 11% different, and on a
    non-convex face a fan is not merely a different triangulation but an
    invalid one, laying triangles outside the polygon. A larger bent face
    keeps raising until that tessellation is implemented properly: a
    missing STEP is a worse result than a wrong one only until the wrong
    one is believed.
    """
    try:
        return [Face(Wire.make_polygon(loop, close=True))]
    except ValueError:
        if len(loop) != 4:
            raise
        return [
            Face(Wire.make_polygon([loop[0], loop[1], loop[2]], close=True)),
            Face(Wire.make_polygon([loop[0], loop[2], loop[3]], close=True)),
        ]


def polygon(
    points: Sequence[Sequence[float]],
    paths: Sequence[Sequence[int]] | None = None,
    convexity: int | None = None,
) -> Shape:
    pts = [(float(p[0]), float(p[1])) for p in points]
    if paths is None:
        return _BdPolygon(*pts, align=None)
    faces = [_BdPolygon(*[pts[i] for i in path], align=None) for path in paths]
    return _even_odd(faces)


def _even_odd(faces: list[Shape]) -> Shape:
    """Fill the paths the way OpenSCAD does: a point is inside when an odd
    number of paths enclose it.

    OpenSCAD tessellates 2D geometry with libtess2 under TESS_WINDING_ODD,
    so a path's role follows from how deeply it nests, not from its
    position in the list. Treating paths[0] as the outline and subtracting
    every other path got three cases wrong: a third path nested inside a
    hole is solid again (three nested squares measured 500 against
    OpenSCAD's 600), a fourth is a hole again, and two paths side by side
    are both solid (100 against 200) rather than one cancelling the other.

    Even-odd over a set of closed paths is their symmetric difference,
    which needs no containment test: nested paths alternate and disjoint
    ones simply add.
    """
    result = faces[0]
    for face in faces[1:]:
        result = (result + face) - (result & face)
    return result


def _is_collection(path: object) -> bool:
    """Is this a font collection, holding several faces in one file?"""
    return str(path).lower().endswith((".ttc", ".otc"))


_FONT_STYLES = {
    "bold": FontStyle.BOLD,
    "italic": FontStyle.ITALIC,
    "bold italic": FontStyle.BOLDITALIC,
}

# build123d has two alignment ideas and only one of them is OpenSCAD's.
# ``align`` moves the finished bounding box; ``text_align`` positions the
# text within its own layout, which is where the pen and the baseline
# live. Aligning the box put the *ink* at the origin, so "H" began at 0
# where OpenSCAD begins at 2.279 -- its left side bearing -- and "Wg" sat
# wholly above the axis where OpenSCAD lets the g descend below it.
#
# OpenSCAD's halign measures the layout box, from the pen origin to the
# advance, not the ink: that is why left leaves a bearing's worth of gap.
# text_align means the same thing, so the three map straight across, and
# all three were checked against OpenSCAD to the last printed digit.
_HALIGN = {
    "left": TextAlign.LEFT,
    "center": TextAlign.CENTER,
    "right": TextAlign.RIGHT,
}

# OCCT's "bottom" is the baseline of the last line, which is OpenSCAD's
# baseline, so that one needs no adjustment. The other three are measured
# on the ink -- the manual says the tallest character, the lowest-reaching
# character, and the centre of the bounding box -- so they are applied
# here as a shift off the baseline rather than by asking OCCT, whose own
# TOP and CENTER follow the font's line metrics instead.
_INK_OFFSET = {
    "baseline": lambda lo, hi: 0.0,
    "top": lambda lo, hi: -hi,
    "bottom": lambda lo, hi: -lo,
    "center": lambda lo, hi: -(lo + hi) / 2,
}


# OpenSCAD's text() size is the em square in model units; build123d's
# font_size goes to OCCT, which measures a font the typographic way, at 72
# points to the inch against a 100-unit em. Asking for 20 therefore drew a
# glyph 14.35 tall where OpenSCAD draws 19.93 -- every string in the
# corpus came out 28% short in each direction, and 48% short in area.
# Measured against OpenSCAD on Helvetica and on Arial: scaling by this
# reproduces its glyph to within 0.02% of area.
EM_PER_POINT = 100 / 72


def text(
    text: str,
    size: float = 10,
    font: str | None = None,
    halign: str = "left",
    valign: str = "baseline",
    spacing: float = 1,
    direction: str = "ltr",
    language: str | None = None,
    script: str | None = None,
    segments: int | None = None,
) -> Shape:
    kwargs: dict[str, object] = {
        "align": None,
        "text_align": (_HALIGN[halign], TextAlign.BOTTOM),
    }
    if font is None:
        # OpenSCAD's default is Liberation Sans, and drawing in anything
        # else is a silent 6.7% volume error on every unstyled text().
        # A path, not a family: a family lookup would take whatever face
        # OCCT has cached under that name (see fonts.isolate_family).
        default = fallback_font_path()
        if default is not None:
            kwargs["font_path"] = str(default)
    else:
        family, style = parse_font_spec(font)
        font_path = find_font_path(font)
        if font_path is not None and not _is_collection(font_path):
            kwargs["font_path"] = font_path
        elif known_family(family):
            # Either the family is installed and only this style lacks a
            # file of its own, or its file is a collection. A path names
            # a file, not a face, so handing OCCT a .ttc always draws the
            # first weight in it -- Helvetica.ttc holds six and would
            # always come back Regular. Asking by family and style gets
            # the right one: Helvetica Bold is 44% larger in area than
            # its regular weight, and matches OpenSCAD.
            isolate_family(family, style)
            kwargs["font"] = family
            if style is not None:
                kwargs["font_style"] = _FONT_STYLES.get(
                    style.lower(), FontStyle.REGULAR
                )
        else:
            # Nothing in the font directories matches the family, which is
            # where OpenSCAD looks too -- so it would not find it either,
            # and draws its own fallback rather than failing. Following it
            # there is what makes such a model agree.
            fallback = fallback_font_path()
            if fallback is not None:
                kwargs["font_path"] = str(fallback)
            else:
                isolate_family(family, style)
                kwargs["font"] = family
                if style is not None:
                    kwargs["font_style"] = _FONT_STYLES.get(
                        style.lower(), FontStyle.REGULAR
                    )
    shape = _BdText(text, font_size=size * EM_PER_POINT, **kwargs)
    if valign == "baseline" or not shape.faces():
        return shape
    box = shape.bounding_box()
    return shape.translate((0, _INK_OFFSET[valign](box.min.Y, box.max.Y), 0))
