"""Errors solid123d raises."""


class Solid123dError(Exception):
    """Base class for solid123d errors."""


class NeedsTessellation(Solid123dError, ValueError):
    """Geometry OpenSCAD tessellates and OCCT will not build directly.

    OpenSCAD hands such a shape to libtess2, which computes the planar
    arrangement of its edges -- splitting them at every crossing and
    inserting vertices -- and then keeps the regions an odd winding number
    marks as inside (GeometryUtils.cc, TESS_WINDING_ODD). OCCT has no
    equivalent: it wants a wire that is planar and simple, and refuses
    anything else.

    Guessing at the tessellation ourselves is what this exception exists to
    avoid. Splitting a bent quad on its first diagonal looked right on
    every case tried and was still 0.016% away from OpenSCAD on the model
    it was written for, because libtess2 refines its triangulation toward
    Delaunay afterwards and no choice of diagonal reproduces that. A
    caller that can reach OpenSCAD should catch this and render the
    subtree there, which is exact by construction; one that cannot should
    report it rather than return a shape that is merely close.

    Subclasses ValueError, which is what OCCT's own failure arrives as.
    """


class BooleanFailed(Solid123dError, ValueError):
    """A boolean OCCT could not compute, after every retry we have.

    Raised only where the result is known to be wrong, not merely
    suspicious: a cut whose material is still sitting inside the tool that
    was supposed to remove it, or a result whose volume falls outside what
    its own operands allow. Both conditions are checked after the fuzzy
    retry and the one-tool-at-a-time fold have already failed.

    Until now these warned and returned the bad shape anyway, which is the
    worst of the options: the caller gets a confident answer that a printer
    will happily make. tyrant180-motorguard.scad measured 19,622 where
    OpenSCAD renders 4,483, and said so only in a warning nobody reads.

    A caller that can reach OpenSCAD should render the subtree there, which
    is exact; one that cannot should report the failure. Either beats
    returning a shape we have already proved wrong.
    """
