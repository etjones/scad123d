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


@pytest.mark.needs_openscad
class TestImportStyleAutoDetection:
    def test_auto_detected_use_avoids_polluting_the_result(self, tmp_path):
        # The scenario this exists for: a file with a demo call left at its
        # own top level (extremely common in a file you're actively
        # developing). Defaulting to "include" would silently union that
        # demo geometry into the result of the module you actually asked
        # for -- wrong volume, no error. Auto-detection should pick "use"
        # here and avoid that.
        lib = tmp_path / "lib.scad"
        lib.write_text("module a_box(size) cube(size);\ntranslate([200, 0, 0]) a_box(100);\n")

        a_box = scad123d.import_module(lib, "a_box")
        box = a_box(size=5)
        assert box.volume == pytest.approx(125, rel=1e-9)  # not the size=100 demo call

    def test_explicit_include_overrides_the_auto_detected_use(self, tmp_path):
        # Proving the override actually reaches OpenSCAD: forcing "include"
        # on the same file now *does* pull in the demo call, polluting the
        # result -- the opposite of the test above.
        lib = tmp_path / "lib.scad"
        lib.write_text("module a_box(size) cube(size);\ntranslate([200, 0, 0]) a_box(100);\n")

        a_box = scad123d.import_module(lib, "a_box", import_style="include")
        box = a_box(size=5)
        assert box.volume == pytest.approx(125 + 100**3, rel=1e-9)

    def test_scad_library_uses_the_same_auto_detected_style(self, tmp_path):
        lib = tmp_path / "lib.scad"
        lib.write_text("module a_box(size) cube(size);\ntranslate([200, 0, 0]) a_box(100);\n")

        library = scad123d.import_module(lib)
        box = library.a_box(size=5)
        assert box.volume == pytest.approx(125, rel=1e-9)


def test_missing_file_raises_immediately(tmp_path):
    with pytest.raises(FileNotFoundError):
        scad123d.import_module(tmp_path / "does_not_exist.scad", "whatever")


def test_unknown_module_raises_immediately_with_a_helpful_message():
    with pytest.raises(
        scad123d.UndeclaredModuleError, match="this_module_does_not_exist"
    ):
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
    middle.write_text(f"include <{leaf}>\n")
    root = tmp_path / "root.scad"
    root.write_text(f"include <{middle}>\n")

    leaf_box = scad123d.import_module(root, "leaf_box")
    assert list(inspect.signature(leaf_box).parameters) == ["size"]


class TestScadLibrary:
    """import_module(path), no module_name -- a namespace of every module."""

    def test_returns_a_scad_library(self):
        lib = scad123d.import_module(MODULE_LIB)
        assert isinstance(lib, scad123d.ScadLibrary)

    def test_dir_lists_declared_modules(self):
        lib = scad123d.import_module(MODULE_LIB)
        assert dir(lib) == ["sized_box"]

    def test_attribute_access_gives_a_real_signature(self):
        lib = scad123d.import_module(MODULE_LIB)
        assert list(inspect.signature(lib.sized_box).parameters) == ["size", "rounded"]

    def test_repeated_attribute_access_returns_the_same_callable(self):
        # Proves __getattr__ only builds once -- the second access finds the
        # function already sitting in the instance __dict__ and never calls
        # __getattr__ (a fresh exec()'d function each time would fail this).
        lib = scad123d.import_module(MODULE_LIB)
        assert lib.sized_box is lib.sized_box

    def test_unknown_attribute_raises_attribute_error(self):
        lib = scad123d.import_module(MODULE_LIB)
        with pytest.raises(AttributeError, match="this_module_does_not_exist"):
            _ = lib.this_module_does_not_exist

    def test_hasattr_works_normally(self):
        lib = scad123d.import_module(MODULE_LIB)
        assert hasattr(lib, "sized_box")
        assert not hasattr(lib, "this_module_does_not_exist")

    def test_functions_are_parsed_but_not_exposed(self, tmp_path):
        lib_file = tmp_path / "lib.scad"
        lib_file.write_text(
            "module a_module(x) { cube(x); }\nfunction a_function(x) = x + 1;\n"
        )
        lib = scad123d.import_module(lib_file)
        assert dir(lib) == ["a_module"]
        with pytest.raises(AttributeError):
            _ = lib.a_function

    def test_repr_lists_modules(self):
        lib = scad123d.import_module(MODULE_LIB)
        assert "sized_box" in repr(lib)

    @pytest.mark.needs_openscad
    def test_calling_a_namespace_module_builds_real_geometry(self):
        lib = scad123d.import_module(MODULE_LIB)
        box = lib.sized_box(size=[20, 15, 10], rounded=False)
        assert box.volume == pytest.approx(20 * 15 * 10, rel=1e-9)


def test_repeated_import_module_calls_do_not_require_a_fresh_file_read(tmp_path):
    # Black-box: caching is scad_declarations.module_map's job (see
    # test_scad_declarations.py for the cache-identity/mtime-invalidation
    # tests) -- this just confirms import_module() itself keeps working
    # correctly across repeated calls for the same file, whatever caching
    # happens underneath.
    lib = tmp_path / "lib.scad"
    lib.write_text("module a(x) cube(x);\nmodule b(y) sphere(y);")

    a1 = scad123d.import_module(lib, "a")
    a2 = scad123d.import_module(lib, "a")
    assert list(inspect.signature(a1).parameters) == list(
        inspect.signature(a2).parameters
    )

    library = scad123d.import_module(lib)
    assert dir(library) == ["a", "b"]


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
        exact = (
            a * b * c
            + area * r
            + r * r / 2 * sum(l * t for l, t in edges)
            + (4 / 3) * math.pi * r**3
        )
        assert box.volume == pytest.approx(exact, rel=1e-6)

    def test_same_callable_reused_with_different_arguments(self):
        sized_box = scad123d.import_module(MODULE_LIB, "sized_box")
        small = sized_box(size=[5, 5, 5])
        large = sized_box(size=[20, 20, 20])
        assert small.volume == pytest.approx(125, rel=1e-9)
        assert large.volume == pytest.approx(8000, rel=1e-9)


def _try_library_module(
    path: str, name: str, *, import_style: str | None = None, **kwargs
):
    """Call a library module, skipping the test if the library isn't
    present, or isn't in a state this can actually use, rather than failing
    -- these are optional, real-world confidence checks, not a correctness
    gate (the self-contained MODULE_LIB fixture above is that). A CI runner
    can have no copy of the library at all (FileNotFoundError), a module
    genuinely missing from whatever copy it does have
    (UndeclaredModuleError), or -- seen in practice -- a copy just old or
    mismatched enough to fail during real geometry construction; that last
    one isn't any single exception type, so it's caught broadly here on
    purpose.
    """
    try:
        fn = scad123d.import_module(path, name, import_style=import_style)
        return fn(**kwargs)
    except (scad123d.Scad123dError, FileNotFoundError) as exc:
        pytest.skip(f"{path} not available on this machine: {exc}")
    except Exception as exc:  # broad and deliberate -- see docstring # noqa: BLE001
        pytest.skip(f"{path} not usable on this machine: {exc}")


@pytest.mark.needs_openscad
def test_bosl2_cuboid_with_auto_detected_import_style():
    # No import_style passed -- BOSL2/std.scad is pure declarations and
    # include<> directives (no top-level geometry), so this should
    # auto-detect "include", which cuboid() genuinely needs (its modules
    # read BOSL2's own file-level config variables).
    #
    # cuboid() also declares several parameters (p1, p2, chamfer,
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
def test_mcad_gear_with_auto_detected_import_style():
    # No import_style passed here either -- MCAD's involute_gears.scad has
    # no top-level geometry (its demo/test code all lives inside test_*
    # modules, never called at the top level), so "include" vs "use" makes
    # no difference to the result; auto-detection picks "include".
    part = _try_library_module(
        "MCAD/involute_gears.scad",
        "gear",
        number_of_teeth=12,
        circular_pitch=8,
        gear_thickness=6,
        bore_diameter=5,
    )
    # Not exactly gear_thickness=6: MCAD's gear() adds its own hub/backing
    # along Z. 9 was confirmed directly against this exact call.
    assert part.bounding_box().size.Z == pytest.approx(9, rel=1e-6)
