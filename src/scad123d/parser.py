"""Parse OpenSCAD's flattened .csg output into a CsgNode tree."""

from pathlib import Path
from typing import Any

from lark import Lark, Token, Tree

from .nodes import CsgNode

_GRAMMAR_PATH = Path(__file__).parent / "csg.lark"
_parser: Lark | None = None


def _get_parser() -> Lark:
    global _parser
    if _parser is None:
        _parser = Lark(
            _GRAMMAR_PATH.read_text(),
            parser="lalr",
            propagate_positions=False,
            maybe_placeholders=True,
        )
    return _parser


def _value(tree: Tree | Token) -> Any:
    if isinstance(tree, Token):
        raise ValueError(f"unexpected bare token {tree!r}")
    kind = tree.data
    if kind == "number":
        text = str(tree.children[0])
        number = float(text)
        return int(number) if number.is_integer() and "." not in text and "e" not in text.lower() else number
    if kind == "string":
        raw = str(tree.children[0])[1:-1]
        return raw.encode().decode("unicode_escape")
    if kind == "true":
        return True
    if kind == "false":
        return False
    if kind == "undef":
        return None
    if kind == "vector":
        return [_value(c) for c in tree.children if c is not None]
    raise ValueError(f"unhandled value node {kind!r}")


def _node(tree: Tree) -> CsgNode:
    modifier: str | None = None
    name: str = ""
    args: dict[str, Any] = {}
    children: list[CsgNode] = []
    positional = 0

    for child in tree.children:
        if child is None:
            continue
        if isinstance(child, Token):
            if child.type == "MODIFIER":
                modifier = str(child)
            elif child.type == "NAME":
                name = str(child)
            continue
        if child.data == "arglist":
            for arg in child.children:
                if arg.data == "named":
                    args[str(arg.children[0])] = _value(arg.children[1])
                else:
                    args[f"_{positional}"] = _value(arg.children[0])
                    positional += 1
        elif child.data == "block":
            children = [_node(c) for c in child.children if c is not None]
        elif child.data == "leaf":
            children = []

    return CsgNode(name=name, args=args, children=children, modifier=modifier)


def parse_csg(text: str) -> CsgNode:
    """Parse .csg source into a single root node.

    Multiple top-level nodes are wrapped in an implicit ``group`` -- the same
    implicit union OpenSCAD applies to a file's top level.
    """
    tree = _get_parser().parse(text)
    roots = [_node(c) for c in tree.children if c is not None]
    if len(roots) == 1:
        return roots[0]
    return CsgNode(name="group", children=roots)


def parse_csg_file(path: str | Path) -> CsgNode:
    return parse_csg(Path(path).read_text())
