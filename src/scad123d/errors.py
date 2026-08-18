"""Exceptions raised by scad123d."""


class Scad123dError(Exception):
    """Base class for scad123d errors."""


class OpenSCADNotFoundError(Scad123dError):
    """The OpenSCAD binary could not be located."""


class OpenSCADRunError(Scad123dError):
    """The OpenSCAD binary ran but failed."""


class UnsupportedNodeError(Scad123dError):
    """A CSG node has no build123d mapping and no mesh fallback is available."""
