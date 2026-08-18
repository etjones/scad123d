"""Import a single OpenSCAD module and call it like a Python function.

``import_scad()`` imports a whole file's top-level geometry. Sometimes what
you actually want is one parameterized module from a library -- a gear, a
threaded insert, a specific bracket shape -- called with different arguments
each time, the way you'd call a Python function.

There is no OpenSCAD-level "just run this one module" operation, so this
builds a small temporary wrapper file per call --

    include <the library>
    the_module(args);

-- and runs it through the same CSG pipeline as ``import_scad()``.
"""

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from build123d import Shape

from .build import BuildOptions, build
from .errors import UnsupportedNodeError
from .facets import DEFAULT_FACET_THRESHOLD
from .openscad import export_csg_with_warnings, scad_literal
from .parser import parse_csg

_IMPORT_STYLES = ("include", "use")


def _reference(path: str | Path) -> str:
    """The text to put inside ``include <...>`` / ``use <...>``.

    A path that exists locally is resolved to an absolute path: the
    generated wrapper file lives in a fresh temp directory on every call, so
    a relative include would resolve against the wrong directory otherwise.
    A path that doesn't exist locally (``"BOSL2/std.scad"``) is passed
    through untouched, for OpenSCAD's own library-path search
    (``$OPENSCADPATH`` and the default library folders) to resolve exactly
    the way it would in a `.scad` file you wrote by hand.
    """
    candidate = Path(path)
    if candidate.exists():
        return str(candidate.resolve())
    return str(path)


def import_module(
    path: str | Path,
    module_name: str,
    *,
    import_style: str = "include",
    facet_threshold: int = DEFAULT_FACET_THRESHOLD,
    mesh_scope: str = "minimal",
    timeout: float = 600,
) -> Callable[..., Shape]:
    """Return a callable that runs one OpenSCAD module and returns its result.

    ``path`` is either a real local ``.scad`` file, or a library-style
    reference such as ``"BOSL2/std.scad"``, resolved the same way OpenSCAD
    resolves an ``include <BOSL2/std.scad>`` written by hand: via
    ``$OPENSCADPATH`` and the default library folders.

    ``import_style`` controls how the module's file is brought into the
    generated wrapper:

    - ``"include"`` (default) brings in everything -- module and function
      definitions, variables, and any of the file's own top-level code. This
      is the right choice for libraries whose modules depend on file-level
      constants, which is common enough (BOSL2 is documented and used this
      way) that it is the safer default.
    - ``"use"`` brings in only module and function *definitions* -- no
      variables, no top-level code. This is how some libraries (MCAD) are
      conventionally brought in, and avoids accidentally re-running any
      demo/test geometry a library's file might render at its own top level.

    The returned callable accepts the same positional and keyword arguments
    as the OpenSCAD module itself, and returns a build123d ``Shape``::

        cuboid = scad123d.import_module("BOSL2/std.scad", "cuboid")
        box = cuboid(size=[20, 15, 10], rounding=3)

    Every call re-invokes OpenSCAD -- there is no OpenSCAD-level operation
    for "just run this module", so this generates and runs a small temporary
    wrapper file each time. That also means a bad module or argument name is
    not a Python-level error at ``import_module()`` time; OpenSCAD treats
    both as warnings, not failures, so it is only caught when the resulting
    call produces no geometry (see the raised error for what to check).
    """
    if import_style not in _IMPORT_STYLES:
        raise ValueError(f"import_style must be 'include' or 'use', got {import_style!r}")
    reference = _reference(path)

    def call(*args: Any, **kwargs: Any) -> Shape:
        parts = [scad_literal(a) for a in args]
        parts += [f"{name} = {scad_literal(value)}" for name, value in kwargs.items()]
        call_text = f"{module_name}({', '.join(parts)})"
        source = f"{import_style} <{reference}>\n{call_text};\n"

        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "wrapper.scad"
            wrapper.write_text(source)
            csg_text, warnings = export_csg_with_warnings(wrapper, timeout=timeout)

        tree = parse_csg(csg_text)
        options = BuildOptions(facet_threshold=facet_threshold, mesh_scope=mesh_scope, timeout=timeout)
        shape = build(tree, options)

        if shape is None:
            hint = f"\nOpenSCAD reported:\n{warnings.strip()}" if warnings.strip() else ""
            raise UnsupportedNodeError(
                f"{call_text} produced no geometry -- check the module name "
                f"and argument names against {path}.{hint}"
            )
        return shape

    call.__name__ = module_name
    call.__qualname__ = module_name
    call.__doc__ = f"Calls the OpenSCAD module {module_name!r} from {path!r}."
    return call
