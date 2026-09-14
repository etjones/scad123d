"""Gated workarounds for upstream OCCT/build123d defects.

Every OCCT workaround in solid123d, and where it lives. Two are
monkeypatches installed by importing this module, so no call site reveals
them; this list is the only place all five are visible.

In this module, as monkeypatches:

1. ``Shape.clean`` -- ``clean()`` deletes real geometry when the result
   holds a face crossed by a curved surface's parametric seam
   (gumyr/build123d#1428). The guarded version keeps the unclean shape
   when the clean would not conserve volume. Detailed below.

2. ``Shape._bool_op`` -- a boolean returns a *valid* shape missing most of
   its material when an operand nearly coincides with the accumulated
   result (a loop laying its last copy on its first, off by
   rotation-matrix noise: 17 mm^3 for what should have been 207). Such a
   result falls outside the bounds its inputs imply, and the same
   operation with a fuzzy tolerance gets it right. The retry climbs a
   short ladder of tolerances, each a fraction of the operands' own size;
   if no rung helps, it warns rather than pretending -- and asks
   ``why_occt_struggled`` whether an operand was something a boolean is
   defined on at all.

In ``_common``, as ordinary functions called from the operations that need
them, so these two *are* visible from their call sites:

3. ``bodies_of`` (used by ``boolean``) -- a boolean handed a compound of
   touching or overlapping bodies as one operand returns nonsense: a cut
   came back *larger* than its argument, and a fuse of a two-face sketch
   with a square returned a face of area 8e100. Passing every body as its
   own operand is equivalent by OCCT's own definition of the operation,
   and works.

4. ``fuse_bodies`` (used by ``group``, so by every union) -- a fuse can
   return more separate pieces than it was given, or pieces that overlap
   each other. A union joins material, so it can do neither: 6 bodies came
   back as 14 solids holding 20,393 of the 28,072 they should, 7 came back
   as 21 summing to negative zero, and 13 bars came back as 3 solids
   holding 637.74 where 616.37 was right. ``gained_bodies`` and
   ``bodies_overlap`` state the two invariants, and a violation retries on
   the same fraction-of-the-model ladder. The bounds check above cannot
   see any of it, because the interval from the largest input to the sum
   of the inputs is wide enough to hold all three answers.

5. ``cut_all`` (used by ``booleans.difference``) -- a cut can keep
   material inside the very shapes it cut with. OCCT split a sphere
   against a box across its middle and then kept the half the box
   covered; separately, one Cut taking every subtrahend at once returned
   the argument untouched where any four of the five cut correctly.
   ``material_left_in_tools`` states the invariant both violate, and only
   when it is violated does anything retry: a fuzzy value scaled to the
   model, then a fold tool by tool.

The split between this module and those three is deliberate. Here the
question is "is this result valid?", a property of one operation's output.
There it is "how should this operation be handed to OCCT?", a strategy the
call site owns. They also measure differently: the bounds check here uses
volume alone, which is 0 for 2D geometry, while ``cut_all`` falls back to
area -- so folding the no-op detection into the bounds check would either
lose 2D coverage or change the bounds semantics for every operation.

OCCT_SPHERE_SEAM_BUG_IS_UNFIXED gates the clean patch below. While
True (the current state of upstream), importing solid123d replaces
``build123d.Shape.clean`` with a volume-guarded version; set it to False
-- or delete the workaround entirely -- once upstream ships a fix, and
every call site reverts to stock build123d behavior. The canary test in
tests/test_occt_workarounds.py fails the moment an upgraded
build123d/OCCT no longer exhibits the bug, which is the signal to flip
this flag.

The bug (https://github.com/gumyr/build123d/issues/1428): ``clean()``
-- the ShapeUpgrade_UnifySameDomain pass build123d's boolean operators
run automatically -- deletes real geometry when the result contains a
face crossed by a curved surface's parametric seam. ``sphere(5) -
cube(8, center=True)`` loses the spherical cap its seam meridian
crosses (73.30 mm^3 instead of 87.96); the matching fuse loses the same
material and exports to STEP as faces only, which slicers read as zero
volume. The maintainer notes OCCT's seam handling as the underlying
culprit ("degenerate treatment of valid geometry containing curved
seams").

Patching ``Shape.clean`` -- rather than routing solid123d's own
booleans through raw BRepAlgoAPI calls -- is deliberate: it is the one
chokepoint every affected path shares. With it in place, solid123d's
``union()``/``difference()``/``intersection()``, build123d's native
``+``/``-``/``&`` on the shapes we hand out, and everything in scad123d
(which imports solid123d) all behave consistently. A call-site-only fix
would leave ``union()`` correct while a user's own ``a + b`` on the
same shapes silently lost material -- a confusing split.

The guarded clean preserves clean()'s contract exactly: it mutates
``self`` in place and returns it. The trial clean runs on a deepcopy
(clean() mutates as well as returns, so an in-place trial would destroy
the original), and its result is adopted only when volume is conserved.
Cost: one geometry copy and two mass computations per clean(); measured
cheaper than the unconditional clean it replaces (16.3 vs 22.9 ms per
sphere/cube fuse), since most cleans are accepted and the pathological
ones skip the expensive rebuild.

Caveat, stated in the upstream issue too: a volume guard protects
solids. A hypothetical clean() failure that conserved volume while
mangling faces would pass the guard.
"""

import copy
import math

from build123d import Shape
from build123d.topology.shape_core import SkipClean
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse

OCCT_SPHERE_SEAM_BUG_IS_UNFIXED = True

# How much the volume may move before a clean() is rejected. The two
# volumes are integrated over different face sets (fragmented vs. unified),
# and that alone differs by 1e-8 to 2e-7 relative on real boolean results
# -- measured on a 52-wedge gear union from the CodeCAD corpus. At the
# original 1e-9 that noise rejected 20 of 21 cleans, so every boolean ran
# on the previous one's unmerged fragments (26 -> 822 faces) until OCCT's
# fuse failed outright and returned an inverted 8-face shape. The defect
# this guards against loses ~17% (73.30 vs 87.96 mm^3), so 1e-5 is forty
# times above the noise and four orders of magnitude below the bug.
CLEAN_VOLUME_RTOL = 1e-5

# Relative slack on the input-implied volume bounds before a boolean
# result is declared implausible (tessellation and integration noise are
# orders of magnitude below this), and OCCT's fuzzy tolerance, in model
# units, used for the retry. 1e-5 mm merges the near-coincident faces that
# defeat the exact algorithm without moving any real geometry.
BOOLEAN_VOLUME_SLACK = 1e-4

# Fractions of the operands' own size tried as OCCT fuzzy values when a
# boolean's volume comes back implausible. Fractions, not fixed
# distances: OCCT asks for a value measured against the geometry in
# question, and the 1e-5 mm this used to pass is far too small for a
# 130 mm part -- a spike array needed 1e-4 of its diagonal, 0.013 mm, and
# returned exactly zero at everything below that. Several rungs, because
# one is a guess and the ladder is cheap next to a wrong answer.
BOOLEAN_RETRY_FRACTIONS = (1e-7, 1e-6, 1e-5, 1e-4)

_original_clean = Shape.clean
_original_bool_op = Shape._bool_op


def _volume_guarded_clean(self: Shape) -> Shape:
    """Drop-in Shape.clean: adopt the clean only if volume is conserved."""
    try:
        before = _leaf_volume(self)
    except Exception:  # noqa: BLE001 -- any OCP failure computing mass
        # properties (Standard_Failure etc.): fall back to stock clean,
        # which is strictly no worse than pre-workaround behavior
        return _original_clean(self)
    trial = copy.deepcopy(self)
    _original_clean(trial)
    if math.isclose(
        _leaf_volume(trial), before, rel_tol=CLEAN_VOLUME_RTOL, abs_tol=1e-9
    ):
        self.wrapped = trial.wrapped
    return self


def _leaf_volume(shape: Shape) -> float:
    """Volume summed over the shape's solids, whatever the nesting.

    Not ``shape.volume``: build123d's Compound.volume sums only the
    compound's *direct* Solid children, and the color-partitioned union
    in _common.group() returns a Compound of per-color Compounds -- which
    reads as 0. Signed on purpose: an inside-out result sums negative and
    is thereby implausible too.
    """
    solids = shape.solids()
    if not solids:
        return shape.volume  # a Solid, or 2D geometry (0)
    return sum(s.volume for s in solids)


def _volume_or_none(shape: Shape) -> float | None:
    try:
        return _leaf_volume(shape)
    except Exception:  # noqa: BLE001 -- OCP mass properties can throw
        return None


def _volume_bounds(operation, args, tools) -> tuple[float, float] | None:
    """The interval a boolean's volume must fall in, from its inputs.

    A union holds at least its largest input and at most their sum; a cut
    keeps at most the minuend and at least minuend minus tool; a common
    part is at most the smallest input. Anything outside is not a
    different-but-valid answer -- it is OCCT having failed silently.
    """
    va = [v for v in (_volume_or_none(a) for a in args) if v is not None]
    vt = [v for v in (_volume_or_none(t) for t in tools) if v is not None]
    if not va:
        return None
    if isinstance(operation, BRepAlgoAPI_Fuse):
        return max(va + vt), sum(va) + sum(vt)
    if isinstance(operation, BRepAlgoAPI_Cut):
        return max(sum(va) - sum(vt), 0.0), sum(va)
    if isinstance(operation, BRepAlgoAPI_Common):
        # arguments form one group and tools another; the common part is
        # at most the smaller group
        return 0.0, min(sum(va), sum(vt))
    return None


def _plausible(volume: float, bounds: tuple[float, float]) -> bool:
    lo, hi = bounds
    slack = BOOLEAN_VOLUME_SLACK * max(hi, 1.0)
    return lo - slack <= volume <= hi + slack


def _guarded_bool_op(self: Shape, args, tools, operation) -> Shape:
    """Drop-in Shape._bool_op: raw boolean, sanity-checked, then the
    volume-guarded clean.

    _bool_op runs ShapeUpgrade_UnifySameDomain inline (not via
    Shape.clean), gated by build123d's own SkipClean flag -- so the
    boolean executes under SkipClean, and the unify pass is reapplied
    afterwards through the guarded clean.

    The sanity check is the second workaround this module carries: OCCT's
    fuse can return a *valid* shape with most of the material missing when
    an operand nearly coincides with part of the accumulated result (a
    ``for (i = [0 : n])`` loop that lays its last copy on its first, off
    by rotation-matrix noise; found on a servo horn from the CodeCAD
    corpus, 17 mm^3 for what should be 207). Such a result lies outside
    the bounds its inputs imply, and the same operation with a fuzzy
    tolerance gets it right.
    """
    args = list(args)
    tools = list(tools)
    bounds = _volume_bounds(operation, args, tools)
    with SkipClean():
        result = _original_bool_op(self, args, tools, operation)
    if result is None or result.wrapped is None:
        return result
    volume = _volume_or_none(result)
    if bounds is not None and volume is not None and not _plausible(volume, bounds):
        candidate = _fuzzy_retry(self, args, tools, operation, bounds)
        if candidate is not None:
            result = candidate
        else:
            from ._common import why_occt_struggled
            from .errors import BooleanFailed

            raise BooleanFailed(
                "OCCT boolean returned an implausible volume "
                f"({volume:.6g}, inputs imply {bounds[0]:.6g}..{bounds[1]:.6g}) "
                "and no fuzzy retry helped" + why_occt_struggled([*args, *tools])
            )
    return _volume_guarded_clean(result)


def _model_diagonal(args, tools) -> float:
    sizes = []
    for shape in list(args) + list(tools):
        try:
            sizes.append(shape.bounding_box().diagonal)
        except Exception:  # noqa: BLE001, S112 -- an empty or broken shape
            continue
    return max(sizes, default=1.0)


def _fuzzy_retry(self, args, tools, operation, bounds):
    """Rerun the boolean with growing fuzzy tolerances, returning the first
    result whose volume its inputs allow, or None if none does."""
    diagonal = _model_diagonal(args, tools)
    for fraction in BOOLEAN_RETRY_FRACTIONS:
        retry = type(operation)()
        retry.SetFuzzyValue(diagonal * fraction)
        try:
            with SkipClean():
                candidate = _original_bool_op(self, args, tools, retry)
        except Exception:  # noqa: BLE001, S112 -- OCCT throws on some fuzzy
            # values; that rung does not apply, the next one may
            continue
        if candidate is None or candidate.wrapped is None:
            continue
        candidate_volume = _volume_or_none(candidate)
        if candidate_volume is not None and _plausible(candidate_volume, bounds):
            return candidate
    return None


def install() -> None:
    """Activate the gated workarounds (idempotent). Called on import of
    solid123d."""
    if OCCT_SPHERE_SEAM_BUG_IS_UNFIXED:
        Shape.clean = _volume_guarded_clean
        Shape._bool_op = _guarded_bool_op


def uninstall() -> None:
    """Restore stock build123d behavior (used by the canary test)."""
    Shape.clean = _original_clean
    Shape._bool_op = _original_bool_op
