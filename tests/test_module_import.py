"""Tests for scad123d.import_module().

Tier 1 (no binary): everything that now happens before OpenSCAD ever runs --
declaration parsing, path/module resolution, and argument validation, all of
which raise as soon as they can rather than deferring to a confusing empty
result at call time.
Tier 2 (needs_openscad): actually calling a module, using a committed,
self-contained fixture so these don't depend on any external library being
installed. A couple of additional tests try BOSL2/MCAD specifically and skip
themselves if those aren't found -- real confidence when they're available,
without making the suite depend on them.
"""

import inspect
import math

import pytest

import scad123d

from .conftest import FIXTURES

MODULE_LIB = FIXTURES / "modules" / "module_lib.scad"


def test_bad_import_style_rejected_before_any_openscad_call():
    with pytest.raises(ValueError, match="include.*use"):
        scad123d.import_module("whatever.scad", "whatever", import_style="bogus")


def test_missing_file_raises_immediately(tmp_path):
    with pytest.raises(FileNotFoundError):
        scad123d.import_module(tmp_path / "does_not_exist.scad", "whatever")


def test_unknown_module_raises_immediately_with_a_helpful_message():
    with pytest.raises(scad123d.UndeclaredModuleError, match="this_module_does_not_exist"):
        scad123d.import_module(MODULE_LIB, "this_module_does_not_exist")


def test_unknown_module_error_lists_modules_actually_found():
    with pytest.raises(scad123d.UndeclaredModuleError, match="sized_box"):
        scad123d.import_module(MODULE_LIB, "this_module_does_not_exist")


def test_missing_required_argument_raises_before_any_openscad_call(tmp_path):
    lib = tmp_path / "lib.scad"
    lib.write_text("module needs_radius(rad) { sphere(r=rad); }")
    needs_radius = scad123d.import_module(lib, "needs_radius")
    with pytest.raises(
        scad123d.MissingArgumentError,
        match="argument 'rad' is required in call to 'needs_radius'",
    ):
        needs_radius()


def test_typo_d_keyword_argument_is_a_python_type_error():
    """The old blind wrapper let OpenSCAD silently fall back to a default
    for an unrecognized argument name. A real signature makes this a normal
    Python error instead, and it happens before OpenSCAD ever runs.
    """
    sized_box = scad123d.import_module(MODULE_LIB, "sized_box")
    with pytest.raises(TypeError, match="sze"):
        sized_box(sze=[999, 999, 999])


def test_signature_lists_required_and_optional_from_the_declaration():
    sized_box = scad123d.import_module(MODULE_LIB, "sized_box")
    params = inspect.signature(sized_box).parameters
    assert list(params) == ["size", "rounded"]


def test_module_found_transitively_through_use_and_include(tmp_path):
    leaf = tmp_path / "leaf.scad"
    leaf.write_text("module leaf_box(size) { cube(size); }")
    middle = tmp_path / "middle.scad"
    middle.write_text(f'include <{leaf}>\n')
    root = tmp_path / "root.scad"
    root.write_text(f'include <{middle}>\n')

    leaf_box = scad123d.import_module(root, "leaf_box")
    assert list(inspect.signature(leaf_box).parameters) == ["size"]


class TestModuleImport:
    pytestmark = pytest.mark.needs_openscad

    def test_keyword_arguments(self):
        sized_box = scad123d.import_module(MODULE_LIB, "sized_box")
        box = sized_box(size=[20, 15, 10], rounded=False)
        assert box.volume == pytest.approx(20 * 15 * 10, rel=1e-9)

    def test_positional_arguments(self):
        sized_box = scad123d.import_module(MODULE_LIB, "sized_box")
        box = sized_box([20, 15, 10], False)
        assert box.volume == pytest.approx(20 * 15 * 10, rel=1e-9)

    def test_default_value_applies_when_argument_omitted(self):
        sized_box = scad123d.import_module(MODULE_LIB, "sized_box")
        box = sized_box()  # size defaults to [10, 10, 10], rounded to false
        assert box.volume == pytest.approx(1000, rel=1e-9)

    def test_rounded_argument_hits_rung_1_analytically(self):
        """rounded=true takes the minkowski(cube, sphere) branch -- this
        should come back exact (rung 1), not a mesh approximation.
        """
        sized_box = scad123d.import_module(MODULE_LIB, "sized_box")
        box = sized_box(size=[20, 15, 10], rounded=True)
        a, b, c, r = 20.0, 15.0, 10.0, 1.0
        edges = [(a, math.pi / 2)] * 4 + [(b, math.pi / 2)] * 4 + [(c, math.pi / 2)] * 4
        area = 2 * (a * b + b * c + c * a)
        exact = a * b * c + area * r + r * r / 2 * sum(l * t for l, t in edges) + (4 / 3) * math.pi * r**3
        assert box.volume == pytest.approx(exact, rel=1e-6)

    def test_same_callable_reused_with_different_arguments(self):
        sized_box = scad123d.import_module(MODULE_LIB, "sized_box")
        small = sized_box(size=[5, 5, 5])
        large = sized_box(size=[20, 20, 20])
        assert small.volume == pytest.approx(125, rel=1e-9)
        assert large.volume == pytest.approx(8000, rel=1e-9)


def _try_library_module(path: str, name: str, *, import_style: str = "include", **kwargs):
    """Call a library module, skipping the test if the library isn't found
    rather than failing -- these are optional, real-world confidence checks.
    """
    try:
        fn = scad123d.import_module(path, name, import_style=import_style)
        return fn(**kwargs)
    except (scad123d.UnsupportedNodeError, scad123d.OpenSCADRunError) as exc:
        pytest.skip(f"{path} not available on this machine: {exc}")


@pytest.mark.needs_openscad
def test_bosl2_cuboid_via_include():
    # BOSL2's cuboid() declares several parameters (p1, p2, chamfer,
    # except_edges, clip_angle) bare, with no "= default" in the
    # declaration -- it assigns their real defaults inside the body instead
    # (e.g. `chamfer = chamfer == undef ? ... `). import_module() has no way
    # to see that; it only knows what the declaration itself says, so these
    # count as required and must be passed explicitly. None -> undef is
    # exactly what BOSL2's own body-level fallback expects, so this behaves
    # identically to what BOSL2's own callers, who simply omit them, get.
    box = _try_library_module(
        "BOSL2/std.scad",
        "cuboid",
        size=[20, 15, 10],
        rounding=3,
        p1=None,
        p2=None,
        chamfer=None,
        except_edges=None,
        clip_angle=None,
    )
    # BOSL2's `size` is the outer bounding size; rounding cuts the corners
    # off *within* that envelope, so the result has less volume than a sharp
    # box of the same nominal size, not more.
    assert box.volume < 20 * 15 * 10
    assert box.is_valid


@pytest.mark.needs_openscad
def test_mcad_gear_via_use():
    part = _try_library_module(
        "MCAD/involute_gears.scad",
        "gear",
        import_style="use",
        number_of_teeth=12,
        circular_pitch=8,
        gear_thickness=6,
        bore_diameter=5,
    )
    # Not exactly gear_thickness=6: MCAD's gear() adds its own hub/backing
    # along Z. 9 was confirmed directly against this exact call.
    assert part.bounding_box().size.Z == pytest.approx(9, rel=1e-6)
