"""Tests for scad123d.scad_declarations -- the declaration-only parser
behind import_module()'s real signatures. No OpenSCAD binary needed: this
never evaluates the language, just tokenizes and skips over it.
"""

import pytest

from scad123d.scad_declarations import (
    clear_cache,
    find_module,
    module_map,
    parse_declarations,
    resolve_scad_path,
    suggest_import_style,
)

from .conftest import FIXTURES

MODULE_LIB = FIXTURES / "modules" / "module_lib.scad"


def _names(decls, kind=None):
    return [d.name for d in decls if kind is None or d.kind == kind]


def test_brace_bodied_module():
    decls = parse_declarations("module foo(a, b=1) { cube(a); }")
    assert len(decls) == 1
    assert decls[0].kind == "module"
    assert decls[0].name == "foo"
    assert [(p.name, p.required) for p in decls[0].parameters] == [("a", True), ("b", False)]


def test_bare_statement_bodied_module():
    decls = parse_declarations("module foo() cube(1); module bar() sphere(1);")
    assert _names(decls) == ["foo", "bar"]


def test_if_else_bodied_module_without_braces():
    decls = parse_declarations(
        "module foo(r) if (r > 0) sphere(r); else cube(1); module after() cube(1);"
    )
    assert _names(decls) == ["foo", "after"]


def test_function_declaration():
    decls = parse_declarations("function bar(x, y=2) = x + y;")
    assert decls[0].kind == "function"
    assert [(p.name, p.required) for p in decls[0].parameters] == [("x", True), ("y", False)]


def test_default_value_with_nested_brackets_and_commas():
    decls = parse_declarations("module foo(c=[1,2,min(3,4)]) { cube(c); }")
    assert decls[0].parameters[0].required is False


def test_trailing_comma_in_parameter_list():
    # Real, live example: BOSL2's own strings.scad declares
    # `function _substr_match_recurse(str,sindex,pattern,plen,pindex=0,)`.
    decls = parse_declarations("module foo(a, b=1,) { cube(a); }")
    assert [(p.name, p.required) for p in decls[0].parameters] == [("a", True), ("b", False)]


class TestSuggestImportStyle:
    def test_declarations_and_assignments_only_suggests_include(self, tmp_path):
        f = tmp_path / "lib.scad"
        f.write_text("x = 5;\n$fn = 10;\nmodule foo(a) cube(a);\n")
        assert suggest_import_style(f) == "include"

    def test_bare_top_level_call_suggests_use(self, tmp_path):
        f = tmp_path / "lib.scad"
        f.write_text("module foo(a) cube(a);\nfoo(10);\n")
        assert suggest_import_style(f) == "use"

    def test_top_level_echo_and_assert_do_not_trigger_use(self, tmp_path):
        f = tmp_path / "lib.scad"
        f.write_text('module foo(a) cube(a);\necho("loaded");\nassert(true);\n')
        assert suggest_import_style(f) == "include"

    def test_top_level_if_suggests_use(self, tmp_path):
        f = tmp_path / "lib.scad"
        f.write_text("module foo(a) cube(a);\nif (true) foo(1);\n")
        assert suggest_import_style(f) == "use"

    def test_top_level_for_suggests_use(self, tmp_path):
        f = tmp_path / "lib.scad"
        f.write_text("module foo(a) cube(a);\nfor (i=[0:2]) foo(i);\n")
        assert suggest_import_style(f) == "use"

    def test_use_and_include_directives_do_not_trigger_use(self, tmp_path):
        f = tmp_path / "lib.scad"
        f.write_text('include <BOSL2/std.scad>\nmodule foo(a) cube(a);\n')
        assert suggest_import_style(f) == "include"

    def test_real_bosl2_std_suggests_include(self):
        try:
            std = resolve_scad_path("BOSL2/std.scad")
        except FileNotFoundError:
            pytest.skip("BOSL2 not installed on this machine")
        assert suggest_import_style(std) == "include"

    def test_real_mcad_involute_gears_suggests_include(self):
        try:
            gears = resolve_scad_path("MCAD/involute_gears.scad")
        except FileNotFoundError:
            pytest.skip("MCAD not installed on this machine")
        assert suggest_import_style(gears) == "include"


def test_use_and_include_directives_are_skipped_not_misparsed():
    decls = parse_declarations(
        'use <BOSL2/std.scad>;\ninclude <MCAD/involute_gears.scad>;\nmodule foo() cube(1);'
    )
    assert _names(decls) == ["foo"]


def test_top_level_assignment_and_call_are_skipped():
    decls = parse_declarations("x = 5;\ntranslate([1,2,3]) cube(x);\nmodule foo() cube(1);")
    assert _names(decls) == ["foo"]


def test_nested_module_inside_a_body_is_not_collected():
    # Matches the prior art (SolidPython's py_scadparser): only top-level
    # declarations are collected, not ones nested inside another body.
    decls = parse_declarations("module outer() { module inner() cube(1); }")
    assert _names(decls) == ["outer"]


def test_comments_do_not_confuse_the_scanner():
    decls = parse_declarations(
        "// module fake() {}\n/* module also_fake() {} */\nmodule real() { cube(1); }"
    )
    assert _names(decls) == ["real"]


def test_resolve_scad_path_finds_an_existing_local_file(tmp_path):
    local = tmp_path / "lib.scad"
    local.write_text("module x() { cube(1); }")
    assert resolve_scad_path(local) == local.resolve()


def test_resolve_scad_path_raises_immediately_when_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_scad_path(tmp_path / "does_not_exist.scad")


def test_find_module_direct():
    decl, path = find_module(MODULE_LIB, "sized_box")
    assert decl.name == "sized_box"
    assert path.name == "module_lib.scad"


def test_find_module_transitive_through_include(tmp_path):
    leaf = tmp_path / "leaf.scad"
    leaf.write_text("module leaf_box(size) { cube(size); }")
    root = tmp_path / "root.scad"
    root.write_text(f"include <{leaf}>\n")

    decl, path = find_module(root, "leaf_box")
    assert decl.name == "leaf_box"
    assert path == leaf.resolve()


def test_find_module_raises_and_lists_what_it_did_find(tmp_path):
    lib = tmp_path / "lib.scad"
    lib.write_text("module present() { cube(1); }")
    with pytest.raises(LookupError, match="present"):
        find_module(lib, "absent")


def test_find_module_does_not_loop_forever_on_a_cycle(tmp_path):
    a = tmp_path / "a.scad"
    b = tmp_path / "b.scad"
    a.write_text(f"include <{b}>\nmodule from_a() cube(1);\n")
    b.write_text(f"include <{a}>\nmodule from_b() cube(1);\n")

    decl, path = find_module(a, "from_b")
    assert decl.name == "from_b"
    assert path == b.resolve()


def test_a_files_own_declaration_overrides_one_pulled_in_transitively(tmp_path):
    leaf = tmp_path / "leaf.scad"
    leaf.write_text("module shared(from_leaf) cube(from_leaf);")
    root = tmp_path / "root.scad"
    root.write_text(f"include <{leaf}>\nmodule shared(from_root) cube(from_root);\n")

    mapping = module_map(root)
    decl, declaring_path = mapping["shared"]
    assert declaring_path == root.resolve()
    assert [p.name for p in decl.parameters] == ["from_root"]


def test_a_file_this_scanner_cannot_parse_is_skipped_not_fatal(tmp_path):
    # A trailing comma is handled (see above); this uses a genuinely
    # unbalanced construct to stand in for "some construct this simplified
    # grammar doesn't handle" without relying on that staying broken.
    broken = tmp_path / "broken.scad"
    broken.write_text("module broken(a")  # truncated -- no closing ")" at all
    root = tmp_path / "root.scad"
    root.write_text(f"include <{broken}>\nmodule fine() cube(1);\n")

    mapping = module_map(root)
    assert set(mapping) == {"fine"}


def test_module_map_root_itself_unparseable_raises(tmp_path):
    broken = tmp_path / "broken.scad"
    broken.write_text("module broken(a")  # truncated -- no closing ")" at all
    with pytest.raises((IndexError, ValueError)):
        module_map(broken)


def test_module_map_is_cached_by_path_and_mtime(tmp_path):
    lib = tmp_path / "lib.scad"
    lib.write_text("module a() cube(1);")
    assert module_map(lib) is module_map(lib)


def test_clear_cache_forces_a_rescan(tmp_path):
    lib = tmp_path / "lib.scad"
    lib.write_text("module a() cube(1);")
    first = module_map(lib)
    clear_cache()
    second = module_map(lib)
    assert first is not second
    assert first == second
