"""Parse OpenSCAD module/function *declarations* -- names and parameter
lists -- without evaluating the language.

This is deliberately not a full OpenSCAD parser. It tokenizes the source,
looks for ``module NAME(...)`` and ``function NAME(...) = ...`` at the top
level, and otherwise just skips over statements by tracking bracket
depth -- enough to tell where one declaration's body ends and the next
top-level statement begins, without caring what the body actually does.
Nested declarations (a ``module`` written inside another module's body) are
not collected, matching SolidPython's ``py_scadparser``, the prior art this
follows.

A file's own ``use``/``include`` targets are also collected (not parsed
recursively by this module -- see ``find_module`` for that), since a
library's entry point commonly just gathers its real definitions from other
files (BOSL2's ``std.scad`` is exactly this: zero declarations of its own,
just a wall of ``include <...>``).
"""

import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<linecomment>//[^\n]*)
    | (?P<blockcomment>/\*.*?\*/)
    | (?P<string>"(?:\\.|[^"\\])*")
    | (?P<number>\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)
    | (?P<ident>\$?[A-Za-z_][A-Za-z_0-9]*)
    # "\" isn't meaningful OpenSCAD syntax outside strings (already atomic
    # above), but it must still tokenize to *something* rather than being
    # silently dropped -- a use<>/include<> target gets reconstructed by
    # concatenating tokens between "<" and ">", and a Windows absolute path
    # is full of backslashes.
    | (?P<punct>[(){}\[\];,=?:+\-*/%<>!&|.#\\])
    """,
    re.VERBOSE | re.DOTALL,
)

_OPEN = {"(", "[", "{"}
_CLOSE = {")", "]", "}"}
_MODIFIER_CHARS = {"%", "#", "!", "*"}


def tokenize(source: str) -> list[str]:
    tokens = []
    pos = 0
    n = len(source)
    while pos < n:
        m = _TOKEN_RE.match(source, pos)
        if not m:
            pos += 1  # stray/unsupported character -- skip rather than fail
            continue
        pos = m.end()
        if m.lastgroup in ("ws", "linecomment", "blockcomment"):
            continue
        tokens.append(m.group())
    return tokens


def _skip_balanced(tokens: list[str], i: int) -> int:
    """``tokens[i]`` is one of ``( [ {``; return the index just past its match."""
    depth = 0
    n = len(tokens)
    while i < n:
        if tokens[i] in _OPEN:
            depth += 1
        elif tokens[i] in _CLOSE:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced brackets in OpenSCAD source")


def _skip_statement(tokens: list[str], i: int) -> int:
    n = len(tokens)
    while i < n and tokens[i] in _MODIFIER_CHARS:
        i += 1
    if i >= n:
        raise ValueError("unexpected end of input while skipping a statement")
    tok = tokens[i]
    if tok == "{":
        return _skip_balanced(tokens, i)
    if tok == ";":
        return i + 1
    if tok == "if":
        i = _skip_balanced(tokens, i + 1)  # the "(...)" condition
        i = _skip_statement(tokens, i)
        if i < n and tokens[i] == "else":
            i = _skip_statement(tokens, i + 1)
        return i
    if tok in ("for", "intersection_for", "let"):
        i = _skip_balanced(tokens, i + 1)
        return _skip_statement(tokens, i)
    # Anything else -- a call, an assignment, a use/include directive -- runs
    # to its own top-level ";", skipping over any bracketed sub-expressions.
    while i < n and tokens[i] != ";":
        i = _skip_balanced(tokens, i) if tokens[i] in _OPEN else i + 1
    return i + 1


@dataclass(frozen=True)
class ScadParameter:
    name: str
    required: bool


@dataclass(frozen=True)
class ScadDeclaration:
    kind: str  # "module" or "function"
    name: str
    parameters: tuple[ScadParameter, ...]


def _parse_parameters(tokens: list[str], i: int) -> tuple[tuple[ScadParameter, ...], int]:
    """``tokens[i]`` is the token just after the opening ``(``."""
    params = []
    if tokens[i] == ")":
        return tuple(params), i + 1
    while True:
        name = tokens[i]
        i += 1
        required = True
        if tokens[i] == "=":
            required = False
            i += 1
            while tokens[i] not in (",", ")"):
                i = _skip_balanced(tokens, i) if tokens[i] in _OPEN else i + 1
        params.append(ScadParameter(name=name, required=required))
        if tokens[i] == ",":
            i += 1
            if tokens[i] == ")":  # trailing comma, e.g. BOSL2's own strings.scad
                i += 1
                break
            continue
        if tokens[i] == ")":
            i += 1
            break
    return tuple(params), i


_SAFE_TOP_LEVEL_CALLS = {"echo", "assert"}


def _looks_like_geometry_statement(tokens: list[str], i: int) -> bool:
    """Does the statement starting at ``tokens[i]`` look like it could
    produce geometry, as opposed to a plain variable assignment or a known
    side-effect-free call (``echo``/``assert``)?

    Used to decide whether bringing a file in via ``include`` risks
    re-rendering something the caller didn't ask for -- see
    ``suggest_import_style``. Deliberately conservative: an ``if``/``for``/
    ``intersection_for``/``let`` at top level counts as geometry even though
    it might not be (properly telling would mean recursing into its body),
    and anything unrecognized counts as geometry too. Getting this wrong in
    the "counts as geometry" direction just means defaulting to ``use``
    instead of ``include``, a loud, fixable-by-override failure (missing
    global constants) rather than the silent, wrong-answer failure the other
    direction risks (unwanted geometry silently unioned into the result).
    """
    n = len(tokens)
    j = i
    while j < n and tokens[j] in _MODIFIER_CHARS:
        j += 1
    if j >= n:
        return False
    tok = tokens[j]
    if tok in ("if", "for", "intersection_for", "let"):
        return True
    if j + 1 < n and tokens[j + 1] == "=":
        return False  # "ident = expr ;" -- a plain assignment
    if j + 1 < n and tokens[j + 1] == "(":
        return tok not in _SAFE_TOP_LEVEL_CALLS
    return True


def _scan(tokens: list[str]) -> tuple[list[ScadDeclaration], list[str], bool]:
    n = len(tokens)
    declarations: list[ScadDeclaration] = []
    references: list[str] = []
    has_top_level_geometry = False
    i = 0
    while i < n:
        tok = tokens[i]
        if tok in ("module", "function") and i + 2 < n and tokens[i + 2] == "(":
            kind, name = tok, tokens[i + 1]
            params, j = _parse_parameters(tokens, i + 3)
            declarations.append(ScadDeclaration(kind=kind, name=name, parameters=params))
            if kind == "module":
                i = _skip_statement(tokens, j)
            else:
                # "function NAME(params) = expression ;"
                if j < n and tokens[j] == "=":
                    j += 1
                while j < n and tokens[j] != ";":
                    j = _skip_balanced(tokens, j) if tokens[j] in _OPEN else j + 1
                i = j + 1
            continue
        if tok in ("use", "include") and i + 1 < n and tokens[i + 1] == "<":
            # Unlike every other statement, "use <...>"/"include <...>" take
            # no trailing ";" -- skip_statement would misread whatever comes
            # right after (very often another include) as the start of the
            # *current* statement and swallow it hunting for a semicolon
            # that isn't coming.
            j = i + 2
            parts = []
            while j < n and tokens[j] != ">":
                parts.append(tokens[j])
                j += 1
            if j < n:
                references.append("".join(parts))
                j += 1
            i = j
            continue
        if _looks_like_geometry_statement(tokens, i):
            has_top_level_geometry = True
        i = _skip_statement(tokens, i)
    return declarations, references, has_top_level_geometry


def parse_declarations(source: str) -> list[ScadDeclaration]:
    """Top-level ``module``/``function`` declarations in ``source``."""
    declarations, _, _ = _scan(tokenize(source))
    return declarations


def parse_declarations_file(path: str | Path) -> list[ScadDeclaration]:
    return parse_declarations(Path(path).read_text())


@dataclass(frozen=True)
class ParsedScadFile:
    path: Path
    declarations: tuple[ScadDeclaration, ...]
    references: tuple[str, ...]  # raw use<>/include<> targets, in file order
    has_top_level_geometry: bool


def parse_file(path: str | Path) -> ParsedScadFile:
    resolved = resolve_scad_path(path)
    declarations, references, has_top_level_geometry = _scan(tokenize(resolved.read_text()))
    return ParsedScadFile(
        path=resolved,
        declarations=tuple(declarations),
        references=tuple(references),
        has_top_level_geometry=has_top_level_geometry,
    )


def suggest_import_style(path: str | Path) -> str:
    """Guess whether ``path`` should be brought into a wrapper via
    ``"include"`` or ``"use"``, so ``import_module()`` doesn't need to be
    told explicitly.

    ``"use"`` if ``path`` itself has any top-level statement that isn't a
    declaration, a ``use``/``include`` directive, or a plain variable
    assignment -- i.e. something that looks like it could render geometry on
    its own, which ``include`` would silently union into whatever module the
    caller actually asked for. ``"include"`` otherwise, since that's needed
    whenever a module's body depends on the file's own top-level variables
    (BOSL2's modules on its `$anchor_override`-style config variables, for
    instance) and, empirically, that's the overwhelmingly common shape for
    real library files (BOSL2, MCAD) -- pure declarations and directives, no
    stray top-level geometry.

    Only ``path``'s own top-level content is examined, not anything it
    transitively ``use``s/``include``s -- that's the file whose content
    literally get spliced into the wrapper, so it's the only thing whose
    top-level statements this choice actually controls.
    """
    return "use" if parse_file(path).has_top_level_geometry else "include"


_graph_cache: dict[Path, tuple[float, dict[str, tuple[ScadDeclaration, Path]]]] = {}


def _resolve_reference(parsed: ParsedScadFile, ref: str) -> Path | None:
    # A relative reference inside a library file is relative to *that
    # file's own directory*, same as OpenSCAD resolves it -- not the
    # caller's cwd or the library search paths, which is what
    # resolve_scad_path alone would try. BOSL2's std.scad saying
    # `include <version.scad>` means the version.scad next to std.scad, not
    # one hypothetically sitting at the library root. Only fall back to the
    # normal search if that's not it (e.g. the reference names a different
    # top-level library).
    sibling = parsed.path.parent / ref
    if sibling.exists():
        return sibling.resolve()
    try:
        return resolve_scad_path(ref)
    except FileNotFoundError:
        return None


def _scan_module_graph(root: Path) -> dict[str, tuple[ScadDeclaration, Path]]:
    """Every ``module`` reachable from ``root`` via ``use``/``include``,
    however deep, as ``{name: (declaration, the file that declares it)}``.

    Visits depth-first, recording a file's own declarations *after*
    recursing into what it references -- so a name declared directly in a
    file overrides a same-named one pulled in transitively, approximating
    OpenSCAD's real "last definition wins" behavior (true textual-order
    resolution, since ``include`` is literally textual substitution) for
    the overwhelmingly common shape of real library files: `use`/`include`
    directives gathered at the top, declarations after. A file that
    interleaves its own declarations *between* includes is not modeled
    exactly right, but name collisions across a library's own include graph
    are rare to begin with -- most libraries treat that as a bug in
    themselves.

    A file reached transitively that this scanner can't parse (this is a
    declaration scanner, not a full OpenSCAD parser -- confirmed against
    real BOSL2 source: unusual-but-legal constructs like a trailing comma in
    a parameter list can still trip it) is skipped, same as a missing one --
    its own declarations are lost, but the rest of the graph still resolves.
    ``root`` itself is the one file the caller explicitly named, so a parse
    failure there is not swallowed the same way.
    """
    mapping: dict[str, tuple[ScadDeclaration, Path]] = {}
    visited: set[Path] = set()

    def visit(path: Path, *, required: bool) -> None:
        if path in visited:
            return
        visited.add(path)
        if required:
            parsed = parse_file(path)
        else:
            try:
                parsed = parse_file(path)
            except (IndexError, ValueError):
                return
        for ref in parsed.references:
            resolved = _resolve_reference(parsed, ref)
            if resolved is not None:
                visit(resolved, required=False)
        for decl in parsed.declarations:
            if decl.kind == "module":
                mapping[decl.name] = (decl, parsed.path)

    visit(root, required=True)
    return mapping


def module_map(path: str | Path) -> dict[str, tuple[ScadDeclaration, Path]]:
    """The cached ``{name: (declaration, declaring file)}`` map for every
    ``module`` reachable from ``path``.

    Cached on ``path``'s resolved location and its modification time, so
    repeated calls (e.g. from repeated ``import_module()`` calls) don't
    re-tokenize and re-walk the include graph. Only ``path`` itself is
    watched for changes -- a file it transitively ``use``s/``include``s
    being edited without ``path`` itself changing won't invalidate the
    cache. That's the right tradeoff for third-party libraries (static) and
    covers the common case of iterating on your own single file directly;
    call ``clear_cache()`` if you're editing something reached only
    transitively and need a fresh scan.
    """
    root = resolve_scad_path(path)
    mtime = root.stat().st_mtime
    cached = _graph_cache.get(root)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    mapping = _scan_module_graph(root)
    _graph_cache[root] = (mtime, mapping)
    return mapping


def clear_cache() -> None:
    """Forget every cached module map, forcing the next lookup to rescan."""
    _graph_cache.clear()


def find_module(path: str | Path, module_name: str) -> tuple[ScadDeclaration, Path]:
    """Find a ``module`` declaration named ``module_name``, searching ``path``
    and everything it ``use``s/``include``s, however deep that goes -- since
    a library's entry point commonly has no declarations of its own and
    just gathers them from other files. See ``module_map`` for caching.

    Returns ``(declaration, the file that actually declares it)``. Raises
    ``FileNotFoundError`` immediately if ``path`` itself can't be located,
    and ``LookupError`` if ``module_name`` isn't anywhere in the reachable
    graph (message lists every module name that was found instead).
    """
    mapping = module_map(path)
    entry = mapping.get(module_name)
    if entry is not None:
        return entry
    hint = (
        f" Modules found in {path} and what it use<>s/include<>s: {', '.join(sorted(mapping))}."
        if mapping
        else f" No module declarations found anywhere reachable from {path}."
    )
    raise LookupError(f"no module {module_name!r} found from {path!r}.{hint}")


def _openscad_library_paths() -> list[Path]:
    """Where OpenSCAD itself looks up ``use <...>``/``include <...>`` targets.

    Mirrors OpenSCAD's own search order: ``$OPENSCADPATH`` (``:`` or
    ``;``-separated), then the platform's default user/system library
    folders. Needed here because, unlike the old blind wrapper-file
    approach, parsing a library's declarations means *we* have to locate its
    file ourselves rather than leaving that to OpenSCAD.
    """
    paths = [Path(".")]
    env = os.environ.get("OPENSCADPATH")
    if env:
        paths += [Path(s) for s in re.split(r"\s*[;:]\s*", env) if s]
    system = platform.system()
    if system == "Darwin":
        paths += [
            Path.home() / "Documents/OpenSCAD/libraries",
            Path("/Applications/OpenSCAD.app/Contents/Resources/libraries"),
        ]
    elif system == "Windows":
        paths += [Path(PureWindowsPath(r"C:\Program Files\OpenSCAD\libraries"))]
    else:
        paths += [
            Path.home() / ".local/share/OpenSCAD/libraries",
            Path("/usr/share/openscad/libraries"),
        ]
    return paths


def resolve_scad_path(path: str | Path) -> Path:
    """Locate a ``.scad`` file the same way ``include <...>``/``use <...>``
    would: as given, absolute or relative to the cwd, or under a library
    search path. Raises ``FileNotFoundError`` immediately if it's nowhere --
    the whole point of parsing up front rather than deferring to OpenSCAD.
    """
    candidate = Path(path)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    if candidate.exists():
        return candidate.resolve()
    for base in _openscad_library_paths():
        found = base / candidate
        if found.exists():
            return found.resolve()
    searched = ", ".join(str(p) for p in _openscad_library_paths())
    raise FileNotFoundError(
        f"no .scad file found for {str(path)!r} -- looked in the current "
        f"directory and library paths ({searched}). Set $OPENSCADPATH or "
        f"pass an absolute/relative path to an existing file."
    )
