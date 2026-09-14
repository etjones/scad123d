"""OpenSCAD 2D -> 3D operations: linear_extrude and rotate_extrude."""

from collections.abc import Callable

from build123d import Axis, Plane, Pos, Shape
from build123d import extrude as _bd_extrude
from build123d import loft as _bd_loft
from build123d import revolve as _bd_revolve
from build123d import scale as _bd_scale

from ._common import group

Applier = Callable[..., Shape]


def linear_extrude(
    height: float = 100,
    center: bool = False,
    convexity: int | None = None,
    twist: float = 0,
    slices: int | None = None,
    scale: float | tuple[float, float] = 1.0,
    segments: int | None = None,
) -> Applier:
    if twist:
        raise NotImplementedError(
            "linear_extrude(twist=...) is not supported; use build123d "
            "sweep() along a helix instead"
        )
    scale_xy = (
        (float(scale), float(scale))
        if isinstance(scale, (int, float))
        else (float(scale[0]), float(scale[1]))
    )

    def apply(*children: Shape) -> Shape:
        face = group(children)
        if scale_xy == (1.0, 1.0):
            # OpenSCAD ignores a polygon's point-winding order entirely --
            # linear_extrude always sweeps toward +Z. build123d's extrude()
            # instead extrudes each face along its own face-plane normal,
            # which depends on that face's winding; a profile assembled
            # from faces with mixed winding (e.g. a BOSL2 stroke() ribbon
            # unioned with round_corners() joint fragments) then partially
            # extrudes backward. Pin the direction so every face sweeps the
            # same way regardless of how it happened to wind.
            solid = _bd_extrude(face, amount=height, dir=(0, 0, 1))
        else:
            # OCCT's loft accepts one face per section, so a disconnected
            # profile (a compound of several faces) must be lofted face by
            # face. about=(0,0,0) is load-bearing: OpenSCAD scales the whole
            # cross-section about the global Z axis, so an off-axis island's
            # top must migrate toward the axis, not shrink in place (which is
            # what build123d's default -- each object's own location -- does).
            solids = []
            for f in face.faces():
                top = _bd_scale(f, by=(scale_xy[0], scale_xy[1], 1.0), about=(0, 0, 0))
                solids.append(_bd_loft([f, Pos(0, 0, height) * top]))
            solid = solids[0] if len(solids) == 1 else group(solids)
        if center:
            solid = Pos(0, 0, -height / 2) * solid
        return solid

    return apply


def rotate_extrude(
    angle: float = 360,
    convexity: int | None = None,
    segments: int | None = None,
) -> Applier:
    def apply(*children: Shape) -> Shape:
        # OpenSCAD takes an XY profile (x >= 0) and spins it about Z;
        # map the profile onto the XZ plane, then revolve.
        profile = Plane.XZ * group(children)
        return _bd_revolve(profile, axis=Axis.Z, revolution_arc=angle)

    return apply
