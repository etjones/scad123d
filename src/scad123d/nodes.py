"""The CSG tree: a node name, literal arguments, and children."""

from dataclasses import dataclass, field
from typing import Any

# Nodes that combine or transform children rather than producing geometry alone.
CONTAINERS = frozenset(
    {
        "group",
        "union",
        "difference",
        "intersection",
        "hull",
        "minkowski",
        "render",
        "multmatrix",
        "color",
        "resize",
        "offset",
        "projection",
        "linear_extrude",
        "rotate_extrude",
    }
)

# Nodes with no BRep equivalent, handled by the mesh fallback.
UNSUPPORTED = frozenset({"hull", "minkowski", "projection", "surface", "import"})


@dataclass
class CsgNode:
    """One node of a flattened OpenSCAD CSG tree.

    ``args`` holds only literals (numbers, strings, bools, None for ``undef``,
    and nested lists) -- the CSG format has no expressions.
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    children: list["CsgNode"] = field(default_factory=list)
    modifier: str | None = None

    def get(self, key: str, default: Any = None) -> Any:
        value = self.args.get(key, default)
        return default if value is None and key not in self.args else value

    def walk(self):
        """Depth-first traversal, self first."""
        yield self
        for child in self.children:
            yield from child.walk()

    def unsupported_nodes(self) -> list[str]:
        """Names of nodes in this subtree that need the mesh fallback."""
        return [n.name for n in self.walk() if n.name in UNSUPPORTED]
