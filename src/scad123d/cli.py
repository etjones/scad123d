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
import faulthandler
import gc
import json
import platform
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
import traceback
import warnings
from pathlib import Path
from typing import Any, TextIO

from build123d import Shape, export_step
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
from .mesh_import import mesh_volume, unit_extrusion
from .openscad import export_csg_with_warnings, export_mesh
from .parser import parse_csg

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
CLASS_MISMATCH = "mismatch"  # built, but disagrees with OpenSCAD's own render
CLASS_ERROR = "error"

# --verify: relative volume disagreement with OpenSCAD's own render that
# counts as a wrong result. The render is re-tessellated finely ($fa=1,
# $fs=0.2: a circle of radius 3 gets 94 segments instead of 9), so facet
# error is ~0.1% and 1% is far above it while far below any semantic bug.
# Where part of *our* model is a mesh fallback it carries the original
# coarse tessellation, so a second comparison against the coarse render
# is allowed a looser bar.
VERIFY_TOLERANCE = 0.01
VERIFY_TOLERANCE_COARSE = 0.05
_TESSELLATION = re.compile(r"\$fa = [0-9.eE+-]+, \$fs = [0-9.eE+-]+")


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
        self.part: Shape | None = None
        self.openscad_warnings: list[str] = []

    def export(self) -> str:
        text, stderr = export_csg_with_warnings(
            self.input, self.overrides or None, self.timeout
        )
        # OpenSCAD reports an unknown module or variable as a WARNING and
        # exits 0 with less geometry than the author meant; for an "empty"
        # or wrong result these lines are usually the whole explanation.
        self.openscad_warnings = [
            line.strip()
            for line in stderr.splitlines()
            if line.startswith(("WARNING", "ERROR", "DEPRECATED"))
        ]
        return text

    def build(self, csg_text: str) -> None:
        # Parse here rather than handing import_csg() the text: its
        # str-is-source-or-path heuristic keys on CSG punctuation, and the
        # export of a model with no top-level geometry (every library file)
        # is a bare newline -- which it would try to open as a path.
        tree = parse_csg(csg_text)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            part = import_csg(
                tree,
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
        self.part = part
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


def refine_tessellation(csg_text: str) -> str:
    """The same CSG with every ``$fa``/``$fs`` pair set fine, so OpenSCAD's
    render converges on the exact volume. An explicit ``$fn`` wins over
    both in OpenSCAD, so deliberately low-poly geometry is untouched."""
    return _TESSELLATION.sub("$fa = 1, $fs = 0.2", csg_text)


def _openscad_volume(csg_text: str, timeout: float, two_d: bool = False) -> float:
    """Volume of OpenSCAD's own full render of the model (0 if empty); for
    a 2D model, its area, via a 1 mm extrusion."""
    if two_d:
        csg_text = unit_extrusion(csg_text)
    try:
        path = export_mesh(csg_text, suffix=".3mf", timeout=timeout)
    except OpenSCADRunError as exc:
        if "Current top level object is empty" in str(exc):
            return 0.0
        raise
    try:
        return mesh_volume(path)
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def _relative_error(ours: float, theirs: float) -> float:
    scale = max(ours, theirs)
    return abs(ours - theirs) / scale if scale > 1e-9 else 0.0


def _verify(conversion: _Conversion, csg_text: str, result: dict[str, Any]) -> None:
    """Cross-check the built part against OpenSCAD's render; sets status."""
    assert conversion.part is not None
    # A purely 2D model has no volume to compare; its area is the same
    # check, and OpenSCAD's render of a 1 mm extrusion measures it.
    two_d = not conversion.part.solids()
    ours = abs(conversion.part.area if two_d else conversion.part.volume)
    fine = _openscad_volume(refine_tessellation(csg_text), conversion.timeout, two_d)
    result["volume"] = round(ours, 6)
    result["scad_volume"] = round(fine, 6)
    if two_d:
        result["measure"] = "area"
    error = _relative_error(ours, fine)
    if error <= VERIFY_TOLERANCE:
        return
    if conversion.meshed:
        # Our meshed regions were rendered at the model's own coarse
        # tessellation, so compare against that render too; a real bug
        # disagrees with both.
        coarse = _openscad_volume(csg_text, conversion.timeout, two_d)
        coarse_error = _relative_error(ours, coarse)
        if coarse_error <= VERIFY_TOLERANCE_COARSE:
            result["message"] = (
                f"volume {ours:.6g} matches OpenSCAD's coarse render "
                f"{coarse:.6g} ({100 * coarse_error:.1f}%), not its fine one "
                f"{fine:.6g}: mesh-fallback tessellation, not a bug"
            )
            return
    # A magnitude bucket leads the message so --report groups mismatches by
    # severity rather than by their (unique) volumes. The 1-2% bucket is
    # where a known, deliberate divergence lands: a minkowski() whose ball
    # is a faceted polyhedron (BOSL2's cuboid(rounding=)) is built as an
    # exact sphere, slightly larger than OpenSCAD's inscribed facets.
    bucket = next(
        label
        for limit, label in (
            (0.02, "1-2%"),
            (0.05, "2-5%"),
            (0.2, "5-20%"),
            (1e9, ">20%"),
        )
        if error <= limit
    )
    result["status"] = CLASS_MISMATCH
    result["message"] = (
        f"volume off by {bucket}: {ours:.6g} vs OpenSCAD {fine:.6g} "
        f"({100 * error:.1f}%)"
    )


def _batch_task(task: dict[str, Any], defaults: argparse.Namespace) -> dict[str, Any]:
    """Convert one task and return its result record (never raises)."""
    input_path = Path(task["input"])
    output_path = Path(task.get("output") or input_path.with_suffix(".step"))
    result: dict[str, Any] = {"input": str(input_path), "output": str(output_path)}
    # A marker per file in the worker's stderr log, so a faulthandler dump
    # (segfault, or SIGUSR1 from the harness on a timeout) can be tied to
    # the input that caused it.
    print(f"--- {time.strftime('%H:%M:%S')} converting {input_path}", file=sys.stderr)
    sys.stderr.flush()
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
        result["status"] = CLASS_OK
        if task.get("verify"):
            stage = "verify"
            _verify(conversion, csg_text, result)
    except Exception as exc:  # noqa: BLE001 -- a worker must survive anything
        cls, message = classify(exc)
        trace = traceback.format_exc()
        if cls == CLASS_ERROR and stage == "build" and "export_step" in trace:
            cls = CLASS_EXPORT
        result.update(status=cls, message=message[:2000], stage=stage)
        # Every failure keeps its traceback: the ones that are scad123d
        # bugs (occt-error, unsupported, mesh-error) need it most.
        if cls not in (CLASS_MISSING, CLASS_TIMEOUT):
            result["traceback"] = trace[-6000:]
    finally:
        # The mesh memo is keyed on subtree text and would otherwise grow
        # for the life of the worker; nothing from one model helps the next.
        clear_cache()
        gc.collect()
    result["seconds"] = round(time.perf_counter() - start, 3)
    result["meshed"] = conversion.meshed
    result["openscad_warnings"] = conversion.openscad_warnings[:50]
    result["peak_rss_mb"] = round(_peak_rss_mb(), 1)
    print(
        f"--- {result['status']} in {result['seconds']}s: {input_path}", file=sys.stderr
    )
    sys.stderr.flush()
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
    # A segfault inside OCCT would otherwise leave nothing behind; with
    # faulthandler the Python stack (which build.py branch, which solid123d
    # call) lands in the worker log, after this file's "converting" marker.
    # SIGUSR1 is the harness asking for the same dump before it kills a
    # worker that has run past its timeout -- where OCCT was spinning.
    faulthandler.enable(file=sys.stderr, all_threads=True)
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
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
