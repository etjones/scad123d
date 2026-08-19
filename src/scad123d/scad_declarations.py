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
    | (?P<punct>[(){}\[\];,=?:+\-*/%<>!&|.#])
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
            continue
        if tokens[i] == ")":
            i += 1
            break
    return tuple(params), i


def _scan(tokens: list[str]) -> tuple[list[ScadDeclaration], list[str]]:
    n = len(tokens)
    declarations: list[ScadDeclaration] = []
    references: list[str] = []
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
        i = _skip_statement(tokens, i)
    return declarations, references


def parse_declarations(source: str) -> list[ScadDeclaration]:
    """Top-level ``module``/``function`` declarations in ``source``."""
    declarations, _ = _scan(tokenize(source))
    return declarations


def parse_declarations_file(path: str | Path) -> list[ScadDeclaration]:
    return parse_declarations(Path(path).read_text())


@dataclass(frozen=True)
class ParsedScadFile:
    path: Path
    declarations: tuple[ScadDeclaration, ...]
    references: tuple[str, ...]  # raw use<>/include<> targets, in file order


def parse_file(path: str | Path) -> ParsedScadFile:
    resolved = resolve_scad_path(path)
    declarations, references = _scan(tokenize(resolved.read_text()))
    return ParsedScadFile(path=resolved, declarations=tuple(declarations), references=tuple(references))


def find_module(path: str | Path, module_name: str) -> tuple[ScadDeclaration, Path]:
    """Find a ``module`` declaration named ``module_name``, searching ``path``
    and then, breadth-first, whatever it ``use``s/``include``s -- however
    deep that goes -- since a library's entry point commonly has no
    declarations of its own and just gathers them from other files.

    Returns ``(declaration, the file that actually declares it)``. Raises
    ``FileNotFoundError`` immediately if ``path`` itself can't be located,
    and ``LookupError`` if the search space is exhausted without finding
    ``module_name`` (message lists every module name seen along the way).
    A referenced file that can't be found is skipped rather than treated as
    fatal -- only ``path`` itself, the file the caller asked for directly,
    must exist.
    """
    root = resolve_scad_path(path)
    visited: set[Path] = set()
    queue: list[Path] = [root]
    seen_modules: set[str] = set()
    while queue:
        candidate = queue.pop(0)
        if candidate in visited:
            continue
        visited.add(candidate)
        parsed = parse_file(candidate)
        for decl in parsed.declarations:
            if decl.kind != "module":
                continue
            seen_modules.add(decl.name)
            if decl.name == module_name:
                return decl, parsed.path
        for ref in parsed.references:
            # A relative reference inside a library file is relative to
            # *that file's own directory*, same as OpenSCAD resolves it --
            # not the caller's cwd or the library search paths, which is
            # what resolve_scad_path alone would try. BOSL2's std.scad
            # saying `include <version.scad>` means the version.scad next
            # to std.scad, not one hypothetically sitting at the library
            # root. Only fall back to the normal search if that's not it
            # (e.g. the reference names a *different* top-level library).
            sibling = parsed.path.parent / ref
            if sibling.exists():
                queue.append(sibling.resolve())
                continue
            try:
                queue.append(resolve_scad_path(ref))
            except FileNotFoundError:
                continue
    hint = f" Modules found in {path} and what it use<>s/include<>s: {', '.join(sorted(seen_modules))}." if seen_modules else f" No module declarations found anywhere reachable from {path}."
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
