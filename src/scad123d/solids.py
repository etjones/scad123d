"""Geometry helpers with no solid123d equivalent."""

import math
from collections.abc import Sequence

from build123d import (
    Compound,
    Face,
    Location,
    Matrix,
    Shape,
    Shell,
    Solid,
    Vector,
    Wire,
)
from build123d import scale as _bd_scale
from OCP.gp import gp_Trsf
from OCP.TopAbs import TopAbs_ShapeEnum

# OpenSCAD writes CSG matrix entries at 6 significant figures, so a rotation
# arrives only orthonormal to ~3e-7 (cos 45 deg is emitted as 0.707107). The
# rigidity test must tolerate that, and the matrix is re-orthonormalized before
# use so the transform is exactly rigid rather than approximately.
_RIGID_TOL = 1e-5
_ZERO_TOL = 1e-12

_WRAPPERS = {
    TopAbs_ShapeEnum.TopAbs_COMPOUND: Compound,
    TopAbs_ShapeEnum.TopAbs_SOLID: Solid,
    TopAbs_ShapeEnum.TopAbs_SHELL: Shell,
    TopAbs_ShapeEnum.TopAbs_FACE: Face,
}


def _rewrap(topods) -> Shape:
    """Wrap a raw TopoDS_Shape in the matching build123d class.

    Shape.cast() returns None when called on the abstract base, so dispatch on
    the OCCT shape type instead.
    """
    return _WRAPPERS.get(topods.ShapeType(), Compound)(topods)


def _determinant(m) -> float:
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def polyhedron(
    points: Sequence[Sequence[float]], faces: Sequence[Sequence[int]]
) -> Shape:
    """Build a solid from explicit points and index faces (OpenSCAD polyhedron).

    Winding is not trusted: if the result encloses negative volume it is
    reversed, matching OpenSCAD's tolerance for either orientation.
    """
    verts = [Vector(float(p[0]), float(p[1]), float(p[2])) for p in points]
    built: list[Face] = []
    for face in faces:
        if len(face) < 3:
            continue
        loop = [verts[i] for i in face]
        built.append(Face(Wire.make_polygon(loop, close=True)))
    if not built:
        raise ValueError("polyhedron() needs at least one face")

    solid = Solid(Shell(built))
    if solid.volume < 0:
        solid = Solid(solid.wrapped.Complemented())
    return solid


def _orthonormalized(m: Sequence[Sequence[float]]) -> list[list[float]]:
    """Snap a near-rigid 4x4 to an exactly rigid one, preserving handedness.

    Gram-Schmidt on the columns, with the uniform scale factor restored as the
    mean of the original column norms. Without this, the ~1e-6 error in
    OpenSCAD's 6-significant-figure matrices would leak a spurious tiny scale
    into every rotated shape.
    """
    cols = [[m[r][c] for r in range(3)] for c in range(3)]
    norms = [math.sqrt(sum(v * v for v in col)) for col in cols]
    scale = sum(norms) / 3

    ortho: list[list[float]] = []
    for col in cols:
        vec = list(col)
        for prior in ortho:
            dot = sum(a * b for a, b in zip(vec, prior))
            vec = [v - dot * p for v, p in zip(vec, prior)]
        length = math.sqrt(sum(v * v for v in vec))
        if length < _ZERO_TOL:
            return [list(row) for row in m]
        ortho.append([v / length for v in vec])

    return [
        [scale * ortho[0][r], scale * ortho[1][r], scale * ortho[2][r], m[r][3]]
        for r in range(3)
    ] + [[0.0, 0.0, 0.0, 1.0]]


def _decompose(m: Sequence[Sequence[float]]) -> tuple[float, list[list[float]]] | None:
    """Split a near-rigid 3x3 into (uniform scale, exact rotation/reflection).

    Returns None when the matrix has non-uniform scale or shear. Gram-Schmidt
    on the columns yields an exactly orthonormal basis; handedness is preserved
    (not forced right-handed) so reflections survive.
    """
    cols = [[m[r][c] for r in range(3)] for c in range(3)]
    norms = [math.sqrt(sum(v * v for v in col)) for col in cols]
    if any(n < _ZERO_TOL for n in norms):
        return None
    if max(norms) - min(norms) > _RIGID_TOL * max(norms):
        return None

    ortho: list[list[float]] = []
    for col in cols:
        vec = list(col)
        for prior in ortho:
            dot = sum(a * b for a, b in zip(vec, prior))
            vec = [v - dot * p for v, p in zip(vec, prior)]
        length = math.sqrt(sum(v * v for v in vec))
        if length < _ZERO_TOL:
            return None
        ortho.append([v / length for v in vec])

    # Columns must have been close to orthogonal for this to be a rotation.
    for i in range(3):
        if sum(a * b for a, b in zip(ortho[i], cols[i])) < norms[i] * (1 - _RIGID_TOL):
            return None

    scale = sum(norms) / 3
    rotation = [[ortho[c][r] for c in range(3)] for r in range(3)]
    return scale, rotation


def apply_matrix(shape: Shape, m: Sequence[Sequence[float]]) -> Shape:
    """Apply an OpenSCAD 4x4 multmatrix to a shape.

    The matrix maps ``v -> A v + t``. When ``A`` is a uniform scale times a
    rotation, that is applied as an origin-based scale followed by a
    *unit-scale* rigid transform, which keeps analytic surfaces: a sphere stays
    a GeomType.SPHERE. Non-uniform scale and shear go through
    transform_geometry, producing B-spline surfaces -- the correct result, since
    a non-uniformly scaled sphere really is an ellipsoid.

    Three OCCT/build123d hazards are worked around here:

    * ``transform_shape()`` routes through ``gp_GTrsf.Trsf()``, and OCCT never
      sets the form flag on an element-wise GTrsf, so even a pure translation
      raises "non-orthogonal GTrsf".
    * A ``gp_Trsf`` carrying any scale factor other than 1 yields a shape that
      fails ``BRepCheck`` -- the geometry scales but its tolerances do not. So
      scaling is done separately, never folded into the gp_Trsf.
    * OpenSCAD emits matrix entries at 6 significant figures, so a rotation
      arrives orthonormal only to ~3e-7 (cos 45 deg is written 0.707107). That
      residual is enough to invalidate the shape, hence the re-orthonormalizing
      decomposition rather than using the matrix as given.
    """
    if shape._wrapped is None:
        # An intersection/difference upstream can legitimately produce nothing
        # (e.g. a corner cylinder trimmed away entirely). OpenSCAD just drops
        # such a node from the tree; build123d's moved()/transform_geometry()
        # instead raise on an empty shape, so short-circuit here rather than
        # letting that propagate as a crash.
        return shape

    rows = [[float(v) for v in row] for row in m]
    while len(rows) < 4:
        rows.append([0.0, 0.0, 0.0, 1.0])
    for row in rows:
        while len(row) < 4:
            row.append(0.0)
    rows = [row[:4] for row in rows[:4]]
    rows[3] = [0.0, 0.0, 0.0, 1.0]

    decomposed = _decompose(rows)
    if decomposed is not None:
        scale, rotation = decomposed
        result = shape
        # v -> A v + t == rotate_translate(scale_about_origin(v)); order matters.
        if abs(scale - 1.0) > _RIGID_TOL:
            result = _bd_scale(result, by=scale, about=(0, 0, 0))
        trsf = gp_Trsf()
        trsf.SetValues(
            rotation[0][0], rotation[0][1], rotation[0][2], rows[0][3],
            rotation[1][0], rotation[1][1], rotation[1][2], rows[1][3],
            rotation[2][0], rotation[2][1], rotation[2][2], rows[2][3],
        )
        result = result.moved(Location(trsf))
    else:
        result = shape.transform_geometry(Matrix(rows))

    # A reflection (negative determinant, e.g. OpenSCAD mirror()) inverts face
    # orientation, leaving a solid that encloses negative volume.
    if _determinant(rows) < 0:
        result = _rewrap(result.wrapped.Reversed())
    return result
