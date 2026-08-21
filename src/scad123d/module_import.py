"""Import OpenSCAD modules and call them like Python functions.

``import_scad()`` imports a whole file's top-level geometry. Sometimes what
you actually want is one parameterized module from a library -- a gear, a
threaded insert, a specific bracket shape -- called with different arguments
each time, the way you'd call a Python function. ``import_module(path, name)``
does that; ``import_module(path)`` (no name) returns a namespace of every
module in ``path``, so ``gears = import_module("BOSL2/gears.scad")`` then
``gears.gear(...)``.

There is no OpenSCAD-level "just run this one module" operation, so calling
a module builds a small temporary wrapper file per call --

    include <the library>
    the_module(args);

-- and runs it through the same CSG pipeline as ``import_scad()``. Unlike a
plain ``*args, **kwargs`` wrapper, every returned callable's signature is
built from the module's actual declared parameters (see
``scad_declarations``): calling it with a bad argument name is a
``TypeError`` from Python itself, a missing required argument is caught
before OpenSCAD ever runs, and a missing file or module is an error at
``import_module()`` time rather than a confusing empty result at call time.
"""

import keyword
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, overload

from build123d import Shape

from .build import BuildOptions, build
from .errors import MissingArgumentError, UndeclaredModuleError, UnsupportedNodeError
from .facets import DEFAULT_FACET_THRESHOLD
from .openscad import export_csg_with_warnings, scad_literal
from .parser import parse_csg
from .scad_declarations import (
    ScadDeclaration,
    find_module,
    module_map,
    resolve_scad_path,
    suggest_import_style,
)

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
    exec(source, {"_UNSET": _UNSET, "_dispatch": dispatch}, namespace)  # noqa: S102
    func = namespace[func_name]
    func.__name__ = module_name
    func.__qualname__ = module_name
    return func


def _build_module_callable(
    module_name: str,
    decl: ScadDeclaration,
    resolved: Path,
    *,
    import_style: str,
    facet_threshold: int,
    mesh_scope: str,
    timeout: float,
) -> Callable[..., Shape]:
    """The callable for one declared module: wraps it in a temporary
    ``include``/``use`` + call, runs it through the CSG pipeline, and
    enforces its required parameters before any of that happens.
    """
    required = {p.name for p in decl.parameters if p.required}

    def dispatch(kwargs: dict[str, Any]) -> Shape:
        supplied = {
            name: value for name, value in kwargs.items() if value is not _UNSET
        }
        missing = required - supplied.keys()
        if missing:
            name = min(missing)
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
        options = BuildOptions(
            facet_threshold=facet_threshold, mesh_scope=mesh_scope, timeout=timeout
        )
        shape = build(tree, options)

        if shape is None:
            hint = (
                f"\nOpenSCAD reported:\n{warnings.strip()}" if warnings.strip() else ""
            )
            raise UnsupportedNodeError(f"{call_text} produced no geometry.{hint}")
        # Name the result after the module that made it, so it shows up in
        # STEP viewers/slicers as e.g. "gear" rather than OCCT's
        # auto-generated "COMPOUND". Colored children keep their own
        # color-derived labels (see build.py).
        if not shape.label:
            shape.label = module_name
        return shape

    func = _build_callable(module_name, decl.parameters, dispatch)
    param_list = ", ".join(
        p.name if p.required else f"{p.name}=..." for p in decl.parameters
    )
    func.__doc__ = f"Calls the OpenSCAD module {module_name}({param_list})."
    return func


class ScadLibrary:
    """A namespace of every ``module`` declared in (or reachable from) a
    ``.scad`` file, built by ``import_module(path)`` with no ``module_name``.

    Each attribute is built lazily, on first access, from the same real
    signature machinery as the single-module form of ``import_module()`` --
    accessing a name that turns out not to be a declared module raises
    ``AttributeError`` (so ``hasattr()``/``dir()`` behave normally), not a
    scad123d-specific error. Only ``module`` declarations are exposed;
    OpenSCAD ``function``s return plain values (numbers, lists, strings),
    which this CSG-based pipeline has no way to extract, so they're parsed
    (to detect name collisions) but never surfaced here.
    """

    def __init__(
        self,
        path: str | Path,
        mapping: dict[str, tuple[ScadDeclaration, Path]],
        resolved: Path,
        *,
        import_style: str,
        facet_threshold: int,
        mesh_scope: str,
        timeout: float,
    ) -> None:
        self.__path = path
        self.__mapping = mapping
        self.__resolved = resolved
        self.__import_style = import_style
        self.__facet_threshold = facet_threshold
        self.__mesh_scope = mesh_scope
        self.__timeout = timeout

    def __getattr__(self, name: str) -> Callable[..., Shape]:
        # this method enables lazy loading of OpenSCAD modules;
        # the first time a module is accessed, it is built and cached,
        # subsequent accesses return the cached function
        entry = self.__mapping.get(name)
        if entry is None:
            available = ", ".join(sorted(self.__mapping))
            hint = (
                f" Modules declared: {available}."
                if available
                else " No modules were declared anywhere reachable from this file."
            )
            raise AttributeError(f"no module {name!r} in {self.__path!r}.{hint}")
        decl, _declaring_file = entry
        func = _build_module_callable(
            name,
            decl,
            self.__resolved,
            import_style=self.__import_style,
            facet_threshold=self.__facet_threshold,
            mesh_scope=self.__mesh_scope,
            timeout=self.__timeout,
        )
        func.__doc__ += f" From {self.__path!r}."
        # The whole point: store it as a normal instance attribute, so this
        # __getattr__ (only invoked on lookup *miss*) never runs again for
        # this name -- no separate cache dict needed.
        setattr(self, name, func)
        return func

    def __dir__(self) -> list[str]:
        # Just the declared modules, not this object's own dunder/private
        # machinery -- this reads as a namespace of modules, not an
        # instance of some class, so that's what dir()/autocomplete should
        # show.
        return sorted(self.__mapping)

    def __repr__(self) -> str:
        return (
            f"<ScadLibrary {str(self.__path)!r}: {', '.join(sorted(self.__mapping))}>"
        )


@overload
def import_module(
    path: str | Path,
    module_name: str,
    *,
    import_style: str | None = None,
    facet_threshold: int = DEFAULT_FACET_THRESHOLD,
    mesh_scope: str = "minimal",
    timeout: float = 600,
) -> Callable[..., Shape]: ...


@overload
def import_module(
    path: str | Path,
    module_name: None = None,
    *,
    import_style: str | None = None,
    facet_threshold: int = DEFAULT_FACET_THRESHOLD,
    mesh_scope: str = "minimal",
    timeout: float = 600,
) -> ScadLibrary: ...


def import_module(
    path: str | Path,
    module_name: str | None = None,
    *,
    import_style: str | None = None,
    facet_threshold: int = DEFAULT_FACET_THRESHOLD,
    mesh_scope: str = "minimal",
    timeout: float = 600,
) -> Callable[..., Shape] | ScadLibrary:
    """Call one OpenSCAD module like a Python function, or import a whole
    file's modules as a namespace.

    With ``module_name``, returns a callable for just that module. Without
    it, returns a ``ScadLibrary``: a namespace with one lazily-built
    attribute per module declared in (or reachable from) ``path`` --
    ``gears = import_module("BOSL2/gears.scad"); gears.gear(...)``. Each
    attribute is built on first access, not up front, so importing a file
    with dozens of declarations doesn't build callables you never call.

    ``path`` is either a real local ``.scad`` file, or a library-style
    reference such as ``"BOSL2/std.scad"``, resolved the same way OpenSCAD
    resolves an ``include <BOSL2/std.scad>`` written by hand: via
    ``$OPENSCADPATH`` and the default library folders. Unlike OpenSCAD's own
    resolution, a file that can't be found raises immediately, here, rather
    than surfacing later as an empty result.

    ``path`` is parsed for its top-level ``module`` declarations (not
    evaluated) -- and, if a requested module isn't declared there directly,
    so is everything ``path`` itself ``use``s/``include``s, however deep
    that goes. This matters because plenty of real libraries' entry points
    have no declarations of their own and just gather them from other files
    (BOSL2's ``std.scad`` is exactly this). A typo or genuinely missing
    module raises immediately (``UndeclaredModuleError`` with
    ``module_name``, ``AttributeError`` on a ``ScadLibrary``), listing every
    module name found instead. This scan is cached on ``path``'s resolved
    location and modification time -- see ``scad_declarations.module_map``
    -- so repeated ``import_module()`` calls for the same file don't
    re-parse it.

    A parameter with no ``= default`` in the declaration is required; the
    returned callable enforces that itself, before OpenSCAD ever runs, with
    a message naming the missing parameter. Note this is the *declaration's*
    defaults, not necessarily a module's effective behavior -- some libraries
    (BOSL2 included) declare a parameter bare and assign its real default
    inside the body (``size = size == undef ? [1,1,1] : size;``), which this
    has no way to see; such a parameter will be required here even though
    calling it from the module's own file could omit it.

    ``import_style`` controls how ``path`` is brought into the generated
    wrapper, and you shouldn't normally need to think about it -- leave it
    unset and it's guessed from ``path``'s own content:

    - ``"include"`` brings in everything -- module and function
      definitions, variables, and any of the file's own top-level code. This
      is what a module's body needs whenever it depends on file-level
      constants (BOSL2's modules on its own config variables, for instance),
      and it's the overwhelmingly common shape for real library files (BOSL2,
      MCAD): pure declarations, nothing that renders on its own.
    - ``"use"`` brings in only module and function *definitions* -- no
      variables, no top-level code -- which matters when ``path`` has a
      top-level statement that would itself render something (a demo call
      left at the bottom of a file you're actively developing, say);
      ``include`` would silently union that into whatever module you
      actually asked for.

    The guess (``scad_declarations.suggest_import_style``) is: ``"use"`` if
    ``path`` has any top-level statement that isn't a declaration, a
    ``use``/``include`` directive, or a plain variable assignment --
    ``"include"`` otherwise. Pass ``import_style`` explicitly to override it;
    see docs/REFERENCE.md for the corner cases where the guess can be wrong
    in either direction.

    Each returned callable accepts the same positional and keyword arguments
    as the OpenSCAD module itself, and returns a build123d ``Shape``::

        cuboid = scad123d.import_module("BOSL2/std.scad", "cuboid")
        box = cuboid(size=[20, 15, 10], rounding=3)

        gears = scad123d.import_module("MCAD/involute_gears.scad")
        part = gears.gear(number_of_teeth=12, circular_pitch=8)

    Every call re-invokes OpenSCAD -- there is no OpenSCAD-level operation
    for "just run this module", so this generates and runs a small temporary
    wrapper file each time.
    """
    if import_style is not None and import_style not in _IMPORT_STYLES:
        raise ValueError(
            f"import_style must be 'include' or 'use', got {import_style!r}"
        )

    resolved = resolve_scad_path(path)
    style = import_style if import_style is not None else suggest_import_style(resolved)

    if module_name is None:
        return ScadLibrary(
            path,
            module_map(path),
            resolved,
            import_style=style,
            facet_threshold=facet_threshold,
            mesh_scope=mesh_scope,
            timeout=timeout,
        )

    try:
        decl, _declaring_file = find_module(path, module_name)
    except LookupError as exc:
        raise UndeclaredModuleError(str(exc)) from exc
    func = _build_module_callable(
        module_name,
        decl,
        resolved,
        import_style=style,
        facet_threshold=facet_threshold,
        mesh_scope=mesh_scope,
        timeout=timeout,
    )
    func.__doc__ += f" From {path!r}."
    return func
