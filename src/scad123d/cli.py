"""``scad2step``: convert an OpenSCAD file to a STEP file from a shell,
no Python or build123d knowledge required.

    scad2step yourfile.scad -o out.step

Exposed as a ``[project.scripts]`` entry point, so ``uvx --from scad123d
scad2step ...`` works without installing anything permanently. The standalone
``scad2step`` PyPI package is a thin wrapper around this module, existing
only so the command also works as plain ``uvx scad2step ...``.
"""

import argparse
import sys
import time
import warnings
from pathlib import Path
from typing import Any

from build123d import export_step
from solid123d.customizer import resolve_param_set

from . import import_scad
from .errors import MeshFallbackWarning, OpenSCADNotFoundError, Scad123dError
from .facets import DEFAULT_FACET_THRESHOLD


def _parse_value(raw: str) -> Any:
    """Interpret a ``-D`` value the way a shell user would expect, without
    requiring OpenSCAD literal syntax or extra quoting for a plain string.
    """
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _override(text: str) -> tuple[str, Any]:
    name, sep, raw = text.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"-D expects name=value, got {text!r}")
    return name, _parse_value(raw)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scad2step",
        description="Convert an OpenSCAD file to a STEP file.",
    )
    parser.add_argument("input", type=Path, help="the .scad file to convert")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output .step path (default: input file with a .step extension)",
    )
    parser.add_argument(
        "-D",
        dest="overrides",
        metavar="name=value",
        action="append",
        default=[],
        help="override a top-level variable, same as OpenSCAD's -D (repeatable)",
    )
    parser.add_argument(
        "-P",
        "--parameter-set",
        default=None,
        help="customizer parameter set to apply, same as OpenSCAD's -P",
    )
    parser.add_argument(
        "-p",
        "--parameter-file",
        type=Path,
        default=None,
        help="customizer parameter file (default: input file with a .json extension)",
    )
    parser.add_argument(
        "--no-customizer",
        action="store_true",
        help="ignore any customizer parameter file next to the input",
    )
    parser.add_argument(
        "--facet-threshold",
        type=int,
        default=DEFAULT_FACET_THRESHOLD,
        help=f"honor $fn below this as real geometry, not a facet count (default: {DEFAULT_FACET_THRESHOLD})",
    )
    parser.add_argument(
        "--mesh-scope",
        choices=["minimal", "hoist"],
        default="minimal",
        help="how much to mesh when part of the model has no exact equivalent (default: minimal)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="seconds allowed for OpenSCAD to run (default: 600)",
    )
    return parser


def _customizer_params(args: argparse.Namespace) -> dict[str, Any] | None:
    """Load the customizer parameter set to apply, honoring the CLI flags.

    Returns {} when there is nothing to apply, or None for a reportable
    error (already printed). A parameter file the user named must exist;
    the default sibling .json is optional.
    """
    if args.no_customizer:
        return {}
    param_file = args.parameter_file or args.input.with_suffix(".json")
    if not param_file.is_file():
        if args.parameter_file is not None:
            print(f"scad2step: no such parameter file: {param_file}", file=sys.stderr)
            return None
        if args.parameter_set is not None:
            print(
                f"scad2step: -P {args.parameter_set} given but no parameter "
                f"file found at {param_file}",
                file=sys.stderr,
            )
            return None
        return {}
    try:
        chosen, params = resolve_param_set(param_file, args.parameter_set)
    except (KeyError, ValueError) as exc:
        print(f"scad2step: {exc.args[0]}", file=sys.stderr)
        return None
    print(
        f"scad2step: applying customizer parameter set {chosen!r} from "
        f"{param_file} (use --no-customizer to ignore it)",
        file=sys.stderr,
    )
    return params


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    output = args.output or args.input.with_suffix(".step")
    try:
        overrides = dict(_override(text) for text in args.overrides)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover -- parser.error() itself exits

    params = _customizer_params(args)
    if params is None:
        return 1
    # -D beats the parameter file, matching OpenSCAD's own precedence
    overrides = params | overrides

    print(f"scad2step: converting {args.input} -> {output}", file=sys.stderr)
    start = time.perf_counter()
    try:
        # Mesh-fallback notes are routine, so print them as plain one-liners
        # instead of Python's warning format (whose file:line and source-line
        # echo reads like a stack trace). catch_warnings() restores the
        # default presentation on exit; other warning categories keep it.
        with warnings.catch_warnings():
            default_show = warnings.showwarning

            def show(message, category, filename, lineno, file=None, line=None):  # type: ignore[no-untyped-def]
                if issubclass(category, MeshFallbackWarning):
                    text = str(message).removeprefix("scad123d: ")
                    print(f"scad2step: note: {text}", file=sys.stderr)
                else:
                    default_show(message, category, filename, lineno, file, line)

            warnings.showwarning = show
            part = import_scad(
                args.input,
                facet_threshold=args.facet_threshold,
                mesh_scope=args.mesh_scope,
                timeout=args.timeout,
                **overrides,
            )
            export_step(part, str(output))
    except OpenSCADNotFoundError as exc:
        print(
            f"scad2step: {exc}\nscad2step needs the OpenSCAD program installed "
            "-- see https://openscad.org/downloads.html",
            file=sys.stderr,
        )
        return 1
    except FileNotFoundError:
        # Whatever raised this only got the bare Path as its args, so str()
        # on it alone has no "No such file" text -- not useful to a user
        # who isn't reading a Python traceback.
        print(f"scad2step: no such file: {args.input}", file=sys.stderr)
        return 1
    except Scad123dError as exc:
        print(f"scad2step: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {output} ({time.perf_counter() - start:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
