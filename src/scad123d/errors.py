"""Exceptions raised by scad123d."""


class Scad123dError(Exception):
    """Base class for scad123d errors."""


class OpenSCADNotFoundError(Scad123dError):
    """The OpenSCAD binary could not be located."""


class OpenSCADRunError(Scad123dError):
    """The OpenSCAD binary ran but failed."""


class UnsupportedNodeError(Scad123dError):
    """A CSG node has no build123d mapping and no mesh fallback is available."""


class UndeclaredModuleError(Scad123dError):
    """The requested module was not declared (as a ``module``) in the file."""


class MissingArgumentError(Scad123dError, TypeError):
    """A required OpenSCAD module parameter was not supplied.

    Subclasses TypeError, matching what Python itself raises for a missing
    required argument -- so this still looks like a normal call-time error
    to anything that catches TypeError, while giving the OpenSCAD parameter
    name in the message instead of a generic positional-argument count.
    """
