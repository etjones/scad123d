"""``scad2step``: convert an OpenSCAD file to a STEP file from a shell,
no Python or build123d knowledge required.

    scad2step yourfile.scad -o out.step

Exposed as a ``[project.scripts]`` entry point, so ``uvx --from scad123d
scad2step ...`` works without installing anything permanently. The standalone
``scad2step`` PyPI package is a thin wrapper around this module, existing
only so the command also works as plain ``uvx scad2step ...``.

``scad2step --batch`` is the worker half of ``scad123d-batch`` (see batch.py):
one long-lived process that converts file after file, paying the ~2s
build123d import once instead of once per file. It reads JSON task lines
on stdin and writes one JSON result line per task on stdout; everything
else (OpenSCAD notes, warnings) goes to stderr. Usable on its own from a
shell too::

    printf '{"input":"a.scad","output":"a.step"}\\n' | scad2step --batch
"""

import argparse
import gc
import json
import platform
import resource
import subprocess
import sys
import time
import traceback
import warnings
from pathlib import Path
from typing import Any, TextIO

from build123d import export_step
from solid123d.customizer import resolve_param_set

from . import import_csg
from .errors import (
    MeshFallbackWarning,
    MeshImportError,
    OpenSCADNotFoundError,
    OpenSCADRunError,
    Scad123dError,
    UnsupportedNodeError,
)
from .facets import DEFAULT_FACET_THRESHOLD
from .mesh import clear_cache
from .openscad import export_csg

# Result classes a worker can report. The parent (batch.py) adds "timeout"
# and "crash", which by their nature the worker itself cannot report.
CLASS_OK = "ok"
CLASS_EMPTY = "empty"
CLASS_OPENSCAD = "openscad-error"
CLASS_UNSUPPORTED = "unsupported"
CLASS_OCCT = "occt-error"
CLASS_MESH = "mesh-error"
CLASS_EXPORT = "export-error"
CLASS_TIMEOUT = "timeout"
CLASS_MISSING = "missing"
CLASS_ERROR = "error"


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
    parser.add_argument("input", type=Path, nargs="?", help="the .scad file to convert")
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
    parser.add_argument(
        "--batch",
        action="store_true",
        help="worker mode: read JSON tasks from stdin, one per line, and write a "
        "JSON result per line to stdout (see scad123d-batch)",
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


class _Conversion:
    """One .scad -> .step conversion, staged so failures can be classified.

    ``export`` (OpenSCAD's CSG export) and ``build`` (the BRep walk plus STEP
    write) are separate so a batch caller can tell an OpenSCAD failure from
    a geometry one, and can keep the CSG text for replay.
    """

    def __init__(
        self,
        input_path: Path,
        output_path: Path,
        *,
        overrides: dict[str, Any],
        facet_threshold: int,
        mesh_scope: str,
        timeout: float,
    ) -> None:
        self.input = input_path
        self.output = output_path
        self.overrides = overrides
        self.facet_threshold = facet_threshold
        self.mesh_scope = mesh_scope
        self.timeout = timeout
        self.meshed: list[str] = []

    def export(self) -> str:
        return export_csg(self.input, self.overrides or None, self.timeout)

    def build(self, csg_text: str) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            part = import_csg(
                csg_text,
                facet_threshold=self.facet_threshold,
                mesh_scope=self.mesh_scope,
                timeout=self.timeout,
            )
        for w in caught:
            if issubclass(w.category, MeshFallbackWarning):
                self.meshed.append(str(w.message).removeprefix("scad123d: "))
            else:
                warnings.showwarning(w.message, w.category, w.filename, w.lineno)
        if not part.label:
            part.label = self.input.stem
        self.output.parent.mkdir(parents=True, exist_ok=True)
        export_step(part, str(self.output))

    def run(self) -> None:
        self.build(self.export())


def _run_single(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.input is None:
        parser.error("an input .scad file is required (or use --batch)")
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
    conversion = _Conversion(
        args.input,
        output,
        overrides=overrides,
        facet_threshold=args.facet_threshold,
        mesh_scope=args.mesh_scope,
        timeout=args.timeout,
    )
    try:
        conversion.run()
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

    # Mesh-fallback notes are routine, so print them as plain one-liners
    # instead of Python's warning format (whose file:line and source-line
    # echo reads like a stack trace).
    for note in conversion.meshed:
        print(f"scad2step: note: {note}", file=sys.stderr)
    print(f"wrote {output} ({time.perf_counter() - start:.1f}s)")
    return 0


# --- batch (worker) mode ------------------------------------------------


def classify(exc: BaseException) -> tuple[str, str]:
    """Map an exception to a (class, message) pair for the batch ledger."""
    message = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return CLASS_TIMEOUT, "OpenSCAD exceeded the per-file timeout"
    if isinstance(exc, FileNotFoundError):
        return CLASS_MISSING, message
    if isinstance(exc, OpenSCADRunError):
        return CLASS_OPENSCAD, message
    if isinstance(exc, MeshImportError):
        return CLASS_MESH, message
    if isinstance(exc, UnsupportedNodeError):
        if "no geometry" in str(exc):
            return CLASS_EMPTY, message
        return CLASS_UNSUPPORTED, message
    # OCP surfaces OCCT's Standard_Failure family as plain exceptions whose
    # type name carries the OCCT class; catch them by name rather than
    # importing OCP here.
    name = type(exc).__name__
    if name.startswith("Standard_") or "BRep" in name or "Geom" in name:
        return CLASS_OCCT, message
    return CLASS_ERROR, message


def _peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kilobytes.
    return rss / (1024 * 1024 if platform.system() == "Darwin" else 1024)


def _batch_task(task: dict[str, Any], defaults: argparse.Namespace) -> dict[str, Any]:
    """Convert one task and return its result record (never raises)."""
    input_path = Path(task["input"])
    output_path = Path(task.get("output") or input_path.with_suffix(".step"))
    result: dict[str, Any] = {"input": str(input_path), "output": str(output_path)}
    start = time.perf_counter()
    conversion = _Conversion(
        input_path,
        output_path,
        overrides=dict(task.get("overrides") or {}),
        facet_threshold=int(task.get("facet_threshold", defaults.facet_threshold)),
        mesh_scope=str(task.get("mesh_scope", defaults.mesh_scope)),
        timeout=float(task.get("timeout", defaults.timeout)),
    )
    stage = "export"
    try:
        csg_text = conversion.export()
        csg_path = task.get("csg")
        if csg_path:
            Path(csg_path).parent.mkdir(parents=True, exist_ok=True)
            Path(csg_path).write_text(csg_text)
        stage = "build"
        conversion.build(csg_text)
    except Exception as exc:  # noqa: BLE001 -- a worker must survive anything
        cls, message = classify(exc)
        if cls == CLASS_ERROR and stage == "build":
            # A STEP writer failure is worth its own class; everything else
            # unexpected stays generic but keeps a traceback for diagnosis.
            cls = CLASS_EXPORT if "export_step" in traceback.format_exc() else cls
        result.update(status=cls, message=message[:2000])
        if cls == CLASS_ERROR:
            result["traceback"] = traceback.format_exc()[-4000:]
    else:
        result["status"] = CLASS_OK
    finally:
        # The mesh memo is keyed on subtree text and would otherwise grow
        # for the life of the worker; nothing from one model helps the next.
        clear_cache()
        gc.collect()
    result["seconds"] = round(time.perf_counter() - start, 3)
    result["meshed"] = conversion.meshed
    result["peak_rss_mb"] = round(_peak_rss_mb(), 1)
    return result


def run_batch(tasks: TextIO, results: TextIO, defaults: argparse.Namespace) -> int:
    """Convert every JSON task line from ``tasks``, one result line each."""
    for line in tasks:
        line = line.strip()
        if not line:
            continue
        try:
            task = json.loads(line)
        except json.JSONDecodeError as exc:
            results.write(
                json.dumps({"status": CLASS_ERROR, "message": f"bad task line: {exc}"})
                + "\n"
            )
            results.flush()
            continue
        results.write(json.dumps(_batch_task(task, defaults)) + "\n")
        results.flush()
    return 0


def _run_batch_mode(args: argparse.Namespace) -> int:
    # Results are the only thing that may appear on stdout, and OCCT/OpenSCAD
    # occasionally print there. Keep a private handle on the real stdout for
    # results and point fd 1 at stderr for everyone else.
    import os

    results_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    results = os.fdopen(results_fd, "w", buffering=1)
    try:
        return run_batch(sys.stdin, results, args)
    finally:
        results.close()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.batch:
        if args.input is not None:
            parser.error("--batch reads tasks from stdin; no input argument")
        return _run_batch_mode(args)
    return _run_single(args, parser)


if __name__ == "__main__":
    raise SystemExit(main())
