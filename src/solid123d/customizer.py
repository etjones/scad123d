"""Read OpenSCAD customizer parameter-set files.

The OpenSCAD customizer (Window > Customizer) saves named parameter sets
to a JSON file next to the .scad file (the same file its CLI consumes
via ``-p file.json -P set_name``):

    {
        "fileFormatVersion": "1",
        "parameterSets": {
            "big_thin": {"coin_diam": "55", "engrave": "true", ...}
        }
    }

Values are stored as strings in OpenSCAD literal syntax. ``load_params``
returns them coerced to Python values (int/float/bool/list/str), so the
workflow is: tune in OpenSCAD's customizer UI, save a set, then build
the same design with solid123d using the chosen values.

``customize()`` makes this the default behavior, mirroring how OpenSCAD
itself applies a saved set: call it once after your module-level
parameter defaults, and if a JSON file named like the running script
exists beside it, the chosen parameter set overrides those defaults,
with a confirmation line printed to stderr so nobody is surprised by
values coming from outside the script. Command line switches (consumed
from ``sys.argv`` so they
don't disturb the script's own argument parsing):

    -P NAME / --parameter-set NAME   choose a set (like OpenSCAD's -P)
    -p FILE / --parameter-file FILE  use a specific JSON file
    -D name=value                    override one variable (like
                                     OpenSCAD's -D; repeatable)
    --no-customizer                  ignore any parameter file
"""

import json
import sys
import warnings
from collections.abc import MutableMapping
from pathlib import Path

ParamValue = int | float | bool | str | list
ParamSet = dict[str, ParamValue]


def _coerce(value: str) -> ParamValue:
    """Convert an OpenSCAD literal string to a Python value.

    OpenSCAD numeric, boolean, and vector literals are also valid JSON;
    anything unparseable is a plain string (e.g. "S", "Academy Engraved
    LET").
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _resolve_json_path(path: str | Path) -> Path:
    """Accept a .json path, or a .scad path with a sibling .json file."""
    path = Path(path)
    if path.suffix.lower() == ".scad":
        sibling = path.with_suffix(".json")
        if not sibling.is_file():
            raise FileNotFoundError(
                f"no parameter file found next to {path}; expected {sibling}"
            )
        return sibling
    return path


def parameter_sets(path: str | Path) -> list[str]:
    """List the parameter-set names in a customizer JSON file."""
    with open(_resolve_json_path(path)) as f:
        data = json.load(f)
    return list(data.get("parameterSets", {}))


def load_params(path: str | Path, set_name: str | None = None) -> ParamSet:
    """Load one named parameter set, with values coerced to Python types.

    Args:
        path: customizer .json file, or the .scad file it sits beside
        set_name: which set to load; may be omitted if the file contains
            exactly one set

    Raises:
        KeyError: set_name not present, or omitted with several sets
    """
    with open(_resolve_json_path(path)) as f:
        data = json.load(f)
    sets = data.get("parameterSets", {})
    if set_name is None:
        if len(sets) != 1:
            raise KeyError(
                f"set_name required; file contains {len(sets)} parameter "
                f"sets: {sorted(sets)}"
            )
        set_name = next(iter(sets))
    if set_name not in sets:
        raise KeyError(f"no parameter set {set_name!r}; available: {sorted(sets)}")
    return {key: _coerce(value) for key, value in sets[set_name].items()}


def _extract_flag(argv: list[str], *names: str, takes_value: bool) -> str | None:
    """Find and remove one command line flag from argv.

    Returns the flag's value (or "" for a bare switch), or None if absent.
    Supports both ``--flag value`` and ``--flag=value`` spellings.
    """
    for i, arg in enumerate(argv):
        key, eq, inline = arg.partition("=")
        if key not in names:
            continue
        if not takes_value:
            del argv[i]
            return ""
        if eq:
            del argv[i]
            return inline
        if i + 1 < len(argv):
            value = argv[i + 1]
            del argv[i : i + 2]
            return value
        raise SystemExit(f"{key} requires a value")
    return None


def _pick_set_name(sets: list[str], requested: str | None, source: Path) -> str:
    """Choose which parameter set to apply."""
    if requested is not None:
        return requested
    if "default" in sets:
        return "default"
    if len(sets) == 1:
        return sets[0]
    raise KeyError(
        f"{source} contains {len(sets)} parameter sets {sorted(sets)} and "
        f'none is named "default"; choose one with -P NAME'
    )


def resolve_param_set(
    path: str | Path, set_name: str | None = None
) -> tuple[str, ParamSet]:
    """Pick and load one parameter set: name and coerced values.

    Selection order: ``set_name`` if given, else the set named
    "default", else the file's only set; several sets with no "default"
    raise ``KeyError`` naming the candidates. This is the shared
    selection logic behind ``customize()``, public so other tools (e.g.
    scad123d's converter) apply parameter files with identical rules.
    """
    json_path = _resolve_json_path(path)
    chosen = _pick_set_name(parameter_sets(json_path), set_name, json_path)
    return chosen, load_params(json_path, chosen)


def customize(
    target: MutableMapping[str, object] | None = None,
    path: str | Path | None = None,
    set_name: str | None = None,
    argv: list[str] | None = None,
) -> ParamSet:
    """Apply an OpenSCAD customizer parameter set to a script's variables.

    Call once after defining module-level parameter defaults::

        coin_diam = 35.0
        engrave = True
        customize()

    If a JSON file named like the running script (``coin.py`` ->
    ``coin.json``) exists beside it, the chosen set overrides the
    matching module-level variables — OpenSCAD semantics: only names
    that already exist are set; unknown names warn and are skipped. With
    no parameter file present, defaults stand and nothing happens.

    Set selection: ``-P NAME`` on the command line, else a set named
    "default", else the file's only set; several sets with no "default"
    raise ``KeyError`` naming the candidates. ``--no-customizer``
    ignores the parameter file entirely; ``-p FILE`` reads a different
    file (then required to exist). ``-D name=value`` overrides a single
    variable, as OpenSCAD's -D: repeatable, applied on top of any
    parameter set, and honored even with ``--no-customizer``. Recognized
    flags are removed from ``sys.argv`` so downstream argument parsing
    is undisturbed.

    Args:
        target: namespace to update; defaults to the caller's globals
        path: parameter .json (or .scad beside it); defaults to the
            sibling JSON of the calling script
        set_name: parameter set to use, overriding the command line
        argv: argument list to scan; defaults to (and mutates) sys.argv

    Returns:
        The coerced parameters that were applied ({} if none were).
    """
    caller = sys._getframe(1)
    if target is None:
        target = caller.f_globals

    argv = sys.argv if argv is None else argv
    skip = _extract_flag(argv, "--no-customizer", takes_value=False) is not None
    flag_path = _extract_flag(argv, "-p", "--parameter-file", takes_value=True)
    flag_set = _extract_flag(argv, "-P", "--parameter-set", takes_value=True)
    defines: ParamSet = {}
    while (define := _extract_flag(argv, "-D", takes_value=True)) is not None:
        name, sep, raw = define.partition("=")
        if not sep:
            raise SystemExit(f"-D expects name=value, got {define!r}")
        defines[name] = _coerce(raw)

    params: ParamSet = {}
    chosen = None
    if not skip:
        explicit = path is not None or flag_path is not None
        if path is None:
            if flag_path is not None:
                path = flag_path
            else:
                script = target.get("__file__") or caller.f_globals.get("__file__")
                if script is None and not defines:
                    raise ValueError(
                        "customize() could not determine the calling script; "
                        "pass path= explicitly"
                    )
                if script is not None:
                    path = Path(script).with_suffix(".json")

        if path is not None:
            json_path = _resolve_json_path(path)
            if json_path.is_file():
                chosen, params = resolve_param_set(json_path, set_name or flag_set)
                print(
                    f"Applying customizer parameter set {chosen!r} from "
                    f"{json_path} (use --no-customizer to ignore it)",
                    file=sys.stderr,
                )
            elif explicit:
                raise FileNotFoundError(f"parameter file not found: {json_path}")

    params = params | defines  # -D beats the file, as in OpenSCAD
    if not params:
        return {}
    unknown = [key for key in params if key not in target]
    if unknown:
        source = f"parameter set {chosen!r}" if chosen is not None else "-D"
        warnings.warn(
            f"{source} sets {unknown} which do not exist in the target "
            f"namespace; ignoring them",
            stacklevel=2,
        )
    applied = {key: val for key, val in params.items() if key not in unknown}
    target.update(applied)
    return applied
