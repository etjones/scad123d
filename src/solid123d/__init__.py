"""solid123d: run SolidPython code on the build123d kernel.

Change ``from solid import ...`` to ``from solid123d import ...`` and the
same code produces native build123d shapes, which can be mixed freely
with regular build123d code and exported to STEP/STL.
"""

from build123d import Shape

from . import occt_workarounds
from ._common import color_label

# Gated on occt_workarounds.OCCT_SPHERE_SEAM_BUG_IS_UNFIXED: while
# upstream loses seam-crossed geometry in clean() (gumyr/build123d#1428),
# patch Shape.clean process-wide so union() and a user's own `a + b`
# behave identically. Flip the flag off once upstream is fixed.
occt_workarounds.install()
from .booleans import difference, hull, intersection, minkowski, union
from .export import export_step, region_bodies
from .extrusions import linear_extrude, rotate_extrude
from .hull import analytic_hull
from .minkowski import analytic_minkowski
from .primitives import (
    circle,
    cube,
    cylinder,
    polygon,
    polyhedron,
    sphere,
    square,
    text,
)
from .render import scad_render, scad_render_to_file
from .transforms import (
    color,
    mirror,
    offset,
    resize,
    rotate,
    scale,
    translate,
)

# Typing aliases: SolidPython signatures like `-> OpenSCADObject` stay
# correct, since every solid123d function returns a build123d Shape.
OpenSCADObject = Shape
OpenSCADObjectPlus = Shape

__all__ = [
    "OpenSCADObject",
    "OpenSCADObjectPlus",
    "analytic_hull",
    "analytic_minkowski",
    "circle",
    "color",
    "color_label",
    "cube",
    "cylinder",
    "difference",
    "export_step",
    "hull",
    "intersection",
    "linear_extrude",
    "minkowski",
    "mirror",
    "offset",
    "polygon",
    "polyhedron",
    "region_bodies",
    "resize",
    "rotate",
    "rotate_extrude",
    "scad_render",
    "scad_render_to_file",
    "scale",
    "sphere",
    "square",
    "text",
    "translate",
    "union",
]
