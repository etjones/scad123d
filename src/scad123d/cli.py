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
from pathlib import Path
from typing import Any

from build123d import export_step

from . import import_scad
from .errors import OpenSCADNotFoundError, Scad123dError
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    output = args.output or args.input.with_suffix(".step")
    try:
        overrides = dict(_override(text) for text in args.overrides)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover -- parser.error() itself exits

    try:
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

    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
