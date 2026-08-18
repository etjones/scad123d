"""Serialise a CsgNode tree back to .csg text.

Used by the mesh fallback: a subtree is written out and handed back to
OpenSCAD, which accepts .csg as input.
"""

from .nodes import CsgNode


def _literal(value: object) -> str:
    if value is None:
        return "undef"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    if isinstance(value, float):
        return repr(round(value, 10))
    return repr(value)


def emit(node: CsgNode, indent: int = 0) -> str:
    """Render one node (and its subtree) as .csg source."""
    pad = "\t" * indent
    args = ", ".join(
        _literal(v) if k.startswith("_") else f"{k} = {_literal(v)}"
        for k, v in node.args.items()
    )
    head = f"{pad}{node.modifier or ''}{node.name}({args})"
    if not node.children:
        return f"{head};"
    inner = "\n".join(emit(c, indent + 1) for c in node.children)
    return f"{head} {{\n{inner}\n{pad}}}"
