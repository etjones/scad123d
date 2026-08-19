"""Import a single OpenSCAD module and call it like a Python function.

``import_scad()`` imports a whole file's top-level geometry. Sometimes what
you actually want is one parameterized module from a library -- a gear, a
threaded insert, a specific bracket shape -- called with different arguments
each time, the way you'd call a Python function.

There is no OpenSCAD-level "just run this one module" operation, so this
builds a small temporary wrapper file per call --

    include <the library>
    the_module(args);

-- and runs it through the same CSG pipeline as ``import_scad()``. Unlike a
plain ``*args, **kwargs`` wrapper, the returned callable's signature is built
from the module's actual declared parameters (see ``scad_declarations``):
calling it with a bad argument name is a ``TypeError`` from Python itself,
a missing required argument is caught before OpenSCAD ever runs, and a
missing file or module is an error at ``import_module()`` time rather than
a confusing empty result at call time.
"""

import keyword
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from build123d import Shape

from .build import BuildOptions, build
from .errors import MissingArgumentError, UndeclaredModuleError, UnsupportedNodeError
from .facets import DEFAULT_FACET_THRESHOLD
from .openscad import export_csg_with_warnings, scad_literal
from .parser import parse_csg
from .scad_declarations import find_module, resolve_scad_path

_IMPORT_STYLES = ("include", "use")

_UNSET = object()


def _python_identifier(name: str, used: set[str]) -> str:
    """A valid, non-colliding Python identifier for an OpenSCAD parameter name.

    ``$fn`` -> ``fn``; a Python keyword gets a trailing underscore; a name
    that would still collide with an earlier parameter (rare, but nothing in
    OpenSCAD's grammar forbids e.g. ``$x`` and ``x`` in the same parameter
    list) gets a numeric suffix.
    """
    ident = name.lstrip("$")
    if not ident or not (ident[0].isalpha() or ident[0] == "_"):
        ident = "_" + ident
    if keyword.iskeyword(ident):
        ident += "_"
    base, n = ident, 2
    while ident in used:
        ident = f"{base}_{n}"
        n += 1
    used.add(ident)
    return ident


def _build_callable(
    module_name: str, params: tuple, dispatch: Callable[[dict[str, Any]], Shape]
) -> Callable[..., Shape]:
    """A real Python function with one parameter per declared OpenSCAD
    parameter, in the same order -- so positional calls, keyword calls, and
    ``help()``/autocomplete all reflect the module's actual signature.

    Every parameter defaults to a private "not supplied" sentinel rather
    than e.g. ``None``, and only parameters the caller actually set are
    forwarded to ``dispatch`` -- ``None`` is a real OpenSCAD value
    (``undef``), distinct from "let the module's own default apply".
    """
    used: set[str] = set()
    py_names = [_python_identifier(p.name, used) for p in params]
    params_src = ", ".join(f"{py} = _UNSET" for py in py_names)
    pairs_src = ", ".join(f"{p.name!r}: {py}" for p, py in zip(params, py_names))
    func_name = _python_identifier(module_name, set())
    source = f"def {func_name}({params_src}):\n    return _dispatch({{{pairs_src}}})\n"

    namespace: dict[str, Any] = {}
    exec(source, {"_UNSET": _UNSET, "_dispatch": dispatch}, namespace)
    func = namespace[func_name]
    func.__name__ = module_name
    func.__qualname__ = module_name
    return func


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
    ``$OPENSCADPATH`` and the default library folders. Unlike OpenSCAD's own
    resolution, a file that can't be found raises immediately, here, rather
    than surfacing later as an empty result.

    ``path`` is parsed for its top-level ``module`` declarations (not
    evaluated) -- and, if ``module_name`` isn't declared there directly, so
    is everything ``path`` itself ``use``s/``include``s, breadth-first,
    however deep that goes. This matters because plenty of real libraries'
    entry points have no declarations of their own and just gather them from
    other files (BOSL2's ``std.scad`` is exactly this). A typo or genuinely
    missing module raises immediately, listing every module name seen along
    the way.

    A parameter with no ``= default`` in the declaration is required; the
    returned callable enforces that itself, before OpenSCAD ever runs, with
    a message naming the missing parameter. Note this is the *declaration's*
    defaults, not necessarily a module's effective behavior -- some libraries
    (BOSL2 included) declare a parameter bare and assign its real default
    inside the body (``size = size == undef ? [1,1,1] : size;``), which this
    has no way to see; such a parameter will be required here even though
    calling it from the module's own file could omit it.

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
    wrapper file each time.
    """
    if import_style not in _IMPORT_STYLES:
        raise ValueError(f"import_style must be 'include' or 'use', got {import_style!r}")

    try:
        decl, _declaring_file = find_module(path, module_name)
    except LookupError as exc:
        raise UndeclaredModuleError(str(exc)) from exc
    # The wrapper includes/uses the file the caller asked for, not
    # necessarily the one that turned out to declare the module -- a
    # library's entry point (e.g. BOSL2's std.scad) reaching the real
    # definition transitively is the normal case, not an edge case, and
    # `path` is what the caller actually wrote and expects to see honored.
    resolved = resolve_scad_path(path)
    required = {p.name for p in decl.parameters if p.required}

    def dispatch(kwargs: dict[str, Any]) -> Shape:
        supplied = {name: value for name, value in kwargs.items() if value is not _UNSET}
        missing = required - supplied.keys()
        if missing:
            name = sorted(missing)[0]
            raise MissingArgumentError(
                f"argument {name!r} is required in call to {module_name!r}"
            )
        parts = [f"{name} = {scad_literal(value)}" for name, value in supplied.items()]
        call_text = f"{module_name}({', '.join(parts)})"
        source = f"{import_style} <{resolved}>\n{call_text};\n"

        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "wrapper.scad"
            wrapper.write_text(source)
            csg_text, warnings = export_csg_with_warnings(wrapper, timeout=timeout)

        tree = parse_csg(csg_text)
        options = BuildOptions(facet_threshold=facet_threshold, mesh_scope=mesh_scope, timeout=timeout)
        shape = build(tree, options)

        if shape is None:
            hint = f"\nOpenSCAD reported:\n{warnings.strip()}" if warnings.strip() else ""
            raise UnsupportedNodeError(f"{call_text} produced no geometry.{hint}")
        return shape

    func = _build_callable(module_name, decl.parameters, dispatch)
    param_list = ", ".join(p.name if p.required else f"{p.name}=..." for p in decl.parameters)
    func.__doc__ = f"Calls the OpenSCAD module {module_name}({param_list}) from {path!r}."
    return func
