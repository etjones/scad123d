"""Tier 1: the CSG grammar and parser. No OpenSCAD binary needed."""

import pytest

from scad123d.emit import emit
from scad123d.nodes import CsgNode
from scad123d.parser import parse_csg


def test_leaf_with_named_args():
    node = parse_csg("cube(size = [1, 2, 3], center = false);")
    assert node.name == "cube"
    assert node.args == {"size": [1, 2, 3], "center": False}
    assert node.children == []


def test_dollar_args_are_ordinary_names():
    node = parse_csg("sphere($fn = 6, $fa = 12, $fs = 2, r = 1.5);")
    assert node.args["$fn"] == 6
    assert node.args["r"] == 1.5


def test_multmatrix_is_positional():
    node = parse_csg(
        "multmatrix([[1, 0, 0, 4], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) "
        "{ cube(size = [1, 1, 1], center = false); }"
    )
    assert node.name == "multmatrix"
    assert node.args["_0"][0] == [1, 0, 0, 4]
    assert len(node.children) == 1


def test_negative_zero_and_decimals():
    node = parse_csg(
        "multmatrix([[-1, -0, -0, 0], [-0, 1, -0, 0], "
        "[-0, -0, 1, 0.001], [0, 0, 0, 1]]) { cube(size = [1, 1, 1]); }"
    )
    assert node.args["_0"][0][0] == -1
    assert node.args["_0"][2][3] == pytest.approx(0.001)


def test_empty_group_is_a_leaf():
    node = parse_csg("group();")
    assert node.name == "group"
    assert node.children == []


def test_multiple_top_level_nodes_wrap_in_group():
    node = parse_csg("cube(size = [1, 1, 1]); sphere(r = 2);")
    assert node.name == "group"
    assert [c.name for c in node.children] == ["cube", "sphere"]


def test_modifiers_are_captured():
    node = parse_csg("%cube(size = [1, 1, 1]); #sphere(r = 1);")
    assert [c.modifier for c in node.children] == ["%", "#"]


def test_string_and_undef_values():
    node = parse_csg(
        'text(text = "Hi \\"there\\"", font = "Helvetica", halign = "default");'
    )
    assert node.args["text"] == 'Hi "there"'
    node = parse_csg("polygon(points = [[0, 0]], paths = undef, convexity = 1);")
    assert node.args["paths"] is None


def test_nested_vectors_and_polyhedron():
    node = parse_csg(
        "polyhedron(points = [[0, 0, 0], [5, 0, 0], [0, 5, 0], [0, 0, 5]], "
        "faces = [[0, 2, 1], [0, 1, 3]], convexity = 1);"
    )
    assert len(node.args["points"]) == 4
    assert node.args["faces"][0] == [0, 2, 1]


def test_unsupported_nodes_are_reported():
    node = parse_csg("difference() { hull() { sphere(r = 1); } cube(size = [1, 1, 1]); }")
    assert node.unsupported_nodes() == ["hull"]


@pytest.mark.parametrize(
    "source",
    [
        "cube(size = [1, 2, 3], center = false);",
        "group() {\n\tsphere($fn = 6, r = 1.5);\n}",
        'text(text = "a", font = "X", halign = "default");',
        "multmatrix([[1, 0, 0, 4], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) {\n\tcube(size = [1, 1, 1]);\n}",
    ],
)
def test_emit_round_trips(source):
    """emit() must produce text that OpenSCAD (and we) can re-read.

    This is what makes the mesh fallback possible: .csg is valid OpenSCAD input.
    """
    once = parse_csg(source)
    twice = parse_csg(emit(once))
    assert twice == once
