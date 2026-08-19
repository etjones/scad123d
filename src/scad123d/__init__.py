"""scad123d: import OpenSCAD files as native build123d BRep geometry.

    import scad123d
    part = scad123d.import_scad("bracket.scad", width=40)

OpenSCAD itself evaluates the language (via its flattened CSG export); scad123d
maps the resulting tree of analytic primitives onto build123d. The OpenSCAD
binary is required.
"""

from pathlib import Path
from typing import Any

from build123d import Shape

from .build import BuildOptions, build
from .errors import (
    MissingArgumentError,
    OpenSCADNotFoundError,
    OpenSCADRunError,
    Scad123dError,
    UndeclaredModuleError,
    UnsupportedNodeError,
)
from .facets import DEFAULT_FACET_THRESHOLD
from .module_import import import_module
from .nodes import CsgNode
from .openscad import export_csg, find_openscad, openscad_version
from .parser import parse_csg, parse_csg_file

__all__ = [
    "DEFAULT_FACET_THRESHOLD",
    "CsgNode",
    "MissingArgumentError",
    "OpenSCADNotFoundError",
    "OpenSCADRunError",
    "Scad123dError",
    "UndeclaredModuleError",
    "UnsupportedNodeError",
    "find_openscad",
    "import_csg",
    "import_module",
    "import_scad",
    "openscad_version",
    "parse_csg",
    "parse_csg_file",
    "to_csg",
]


def import_scad(
    path: str | Path,
    *,
    facet_threshold: int = DEFAULT_FACET_THRESHOLD,
    mesh_scope: str = "minimal",
    timeout: float = 600,
    **overrides: Any,
) -> Shape:
    """Import an OpenSCAD file as a build123d Shape.

    Args:
        path: the ``.scad`` file to import.
        facet_threshold: honor ``$fn`` as real geometry below this value; use
            exact BRep curves at or above it. See README -- the CSG export
            cannot distinguish a global ``$fn`` from a call-site one, so
            magnitude is the discriminator.
        mesh_scope: ``"minimal"`` meshes only subtrees with no BRep equivalent;
            ``"hoist"`` meshes the whole model if any such node is present,
            which is more robust but keeps no analytic geometry.
        timeout: seconds allowed for each OpenSCAD invocation.
        **overrides: top-level variable overrides, as OpenSCAD's ``-D``.

    Raises:
        OpenSCADNotFoundError: the OpenSCAD binary was not found.
        OpenSCADRunError: OpenSCAD failed on the input.

    Security:
        This runs OpenSCAD on ``path``, and a ``.scad`` file can ``include``
        and read arbitrary paths. Do not call this on untrusted input.
    """
    tree = parse_csg(export_csg(path, overrides or None, timeout))
    return import_csg(
        tree,
        facet_threshold=facet_threshold,
        mesh_scope=mesh_scope,
        timeout=timeout,
    )


def import_csg(
    tree: CsgNode | str | Path,
    *,
    facet_threshold: int = DEFAULT_FACET_THRESHOLD,
    mesh_scope: str = "minimal",
    timeout: float = 600,
) -> Shape:
    """Build geometry from an already-exported CSG tree, source text, or path.

    A ``Path`` is always read as a file; a ``str`` containing CSG punctuation
    is treated as source text, otherwise as a path.

    Useful for testing and caching: CSG export is deterministic, so a ``.csg``
    file can be committed as a fixture and rebuilt without the binary (unless
    a subtree needs the mesh fallback).
    """
    if isinstance(tree, CsgNode):
        node = tree
    elif isinstance(tree, Path):
        node = parse_csg_file(tree)
    else:
        # A path never contains CSG punctuation; source text always does.
        text = str(tree)
        is_source = any(ch in text for ch in "();")
        node = parse_csg(text) if is_source else parse_csg_file(text)

    options = BuildOptions(
        facet_threshold=facet_threshold, mesh_scope=mesh_scope, timeout=timeout
    )
    shape = build(node, options)
    if shape is None:
        raise UnsupportedNodeError("the CSG tree produced no geometry")
    return shape


def to_csg(path: str | Path, **overrides: Any) -> str:
    """Return OpenSCAD's flattened CSG export for a .scad file, as text."""
    return export_csg(path, overrides or None)
