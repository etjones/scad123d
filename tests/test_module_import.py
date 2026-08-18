"""Tests for scad123d.import_module().

Tier 1 (no binary): the pure path-resolution logic and the argument
validation that happens before any OpenSCAD invocation.
Tier 2 (needs_openscad): actually calling a module, using a committed,
self-contained fixture so these don't depend on any external library being
installed. A couple of additional tests try BOSL2/MCAD specifically and skip
themselves if those aren't found -- real confidence when they're available,
without making the suite depend on them.
"""

from pathlib import Path

import pytest

import scad123d
from scad123d.module_import import _reference

from .conftest import FIXTURES

MODULE_LIB = FIXTURES / "modules" / "module_lib.scad"


def test_reference_resolves_an_existing_local_file(tmp_path):
    local = tmp_path / "lib.scad"
    local.write_text("module x() { cube(1); }")
    assert _reference(local) == str(local.resolve())


def test_reference_passes_through_a_library_style_path():
    # "BOSL2/std.scad" is not a real path relative to the cwd running the
    # test, so this must be left untouched for OpenSCAD's own $OPENSCADPATH
    # / library-folder search to resolve -- exactly as it would for an
    # include<> written by hand in a real .scad file.
    assert _reference("BOSL2/std.scad") == "BOSL2/std.scad"


def test_bad_import_style_rejected_before_any_openscad_call():
    with pytest.raises(ValueError, match="include.*use"):
        scad123d.import_module("whatever.scad", "whatever", import_style="bogus")


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
        import math

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

    def test_unknown_module_raises_with_a_helpful_message(self):
        bad = scad123d.import_module(MODULE_LIB, "this_module_does_not_exist")
        with pytest.raises(scad123d.UnsupportedNodeError, match="this_module_does_not_exist"):
            bad(size=1)

    def test_unknown_module_error_includes_openscad_warning(self):
        bad = scad123d.import_module(MODULE_LIB, "this_module_does_not_exist")
        with pytest.raises(scad123d.UnsupportedNodeError, match="Ignoring unknown module"):
            bad(size=1)

    def test_typo_d_argument_falls_back_to_the_default_not_an_error(self):
        """OpenSCAD treats an unrecognized argument name as a warning, not a
        failure -- the module still runs with its default for that
        parameter. Documented behavior, not a scad123d limitation.
        """
        sized_box = scad123d.import_module(MODULE_LIB, "sized_box")
        box = sized_box(sze=[999, 999, 999])  # typo: "sze" instead of "size"
        assert box.volume == pytest.approx(1000, rel=1e-9)  # the [10,10,10] default


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
    box = _try_library_module("BOSL2/std.scad", "cuboid", size=[20, 15, 10], rounding=3)
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
