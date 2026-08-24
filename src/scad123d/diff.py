"""``scad123d-diff``: differential bisection against OpenSCAD.

    scad2step yourfile.scad          # converts, may be silently wrong?
    scad123d-diff yourfile.scad      # find out exactly where

Builds every subtree of the model's CSG both ways -- scad123d's BRep
walker and OpenSCAD's own mesh render -- and compares volumes, descending
into divergent subtrees until it reaches operations whose children all
agree individually but whose combined result does not. Those nodes are
the bugs (or the known approximations), printed with their emitted CSG
saved next to the input for replay.

This is the tool that found the empty-intersection-operand bug (a silent
63% volume error): unions of correct children were correct, differences
were correct, and the walk bottomed out on one ``intersection()`` whose
empty second operand scad123d had dropped. An OpenSCAD render of an empty
subtree exits with "Current top level object is empty"; that is treated
as volume 0, exactly the convention the walker itself uses.

Volumes are compared meshes-to-BRep, so small disagreement is expected
wherever geometry is curved (OpenSCAD inscribes facets); the default
tolerance of 2% is far above tessellation error and far below any real
semantic bug.
"""

import argparse
import sys
from pathlib import Path

from build123d import Shape

from .build import BuildOptions, build
from .emit import emit
from .errors import OpenSCADRunError
from .nodes import CsgNode
from .openscad import export_csg, export_mesh
from .parser import parse_csg


def _scad_volume(source: str, timeout: float) -> float | None:
    """OpenSCAD's own volume for a CSG subtree; 0 for legal-but-empty
    subtrees, None where OpenSCAD cannot mesh it (2D, or a real error)."""
    import shutil

    from build123d import Mesher

    try:
        path = export_mesh(source, suffix=".3mf", timeout=timeout)
    except OpenSCADRunError as exc:
        if "Current top level object is empty" in str(exc):
            return 0.0
        return None
    try:
        shapes = Mesher().read(str(path))
    except Exception:  # noqa: BLE001
        return None
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)
    return sum(s.volume for s in shapes)


def _our_volume(node: CsgNode, options: BuildOptions) -> float | None:
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            shape: Shape | None = build(node, options)
    except Exception:  # noqa: BLE001
        return None
    if shape is None or shape._wrapped is None:
        return 0.0
    if not shape.solids():
        return None  # 2D subtree: OpenSCAD can't mesh it either
    return shape.volume


class _Differ:
    def __init__(self, tolerance: float, timeout: float, out_dir: Path):
        self.tolerance = tolerance
        self.timeout = timeout
        self.out_dir = out_dir
        self.options = BuildOptions(timeout=timeout)
        self.culprits: list[tuple[str, CsgNode, float, float]] = []
        self.checked = 0

    def _diverges(self, node: CsgNode) -> tuple[float, float] | None:
        ours = _our_volume(node, self.options)
        ref = _scad_volume(emit(node), self.timeout)
        self.checked += 1
        if ours is None or ref is None:
            return None
        if abs(ours - ref) > self.tolerance * max(abs(ref), 1.0):
            return ours, ref
        return None

    def descend(self, node: CsgNode, path: str) -> None:
        top = self._diverges(node)
        if top is None:
            return
        divergent_children = []
        for i, child in enumerate(node.children):
            if self._diverges(child) is not None:
                divergent_children.append((i, child))
        if not divergent_children:
            self.culprits.append((path, node, *top))
            return
        for i, child in divergent_children:
            self.descend(child, f"{path}.{i}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scad123d-diff",
        description="Bisect a model against OpenSCAD to localize volume "
        "disagreements to the responsible CSG operations.",
    )
    parser.add_argument("input", type=Path, help="the .scad file to bisect")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.02,
        help="relative volume disagreement that counts as divergent "
        "(default: 0.02; tessellation error is well below this)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="seconds allowed per OpenSCAD invocation (default: 600)",
    )
    args = parser.parse_args(argv)

    tree = parse_csg(export_csg(args.input, None, args.timeout))
    differ = _Differ(args.tolerance, args.timeout, args.input.parent)
    print(f"scad123d-diff: bisecting {args.input} ...", file=sys.stderr)
    differ.descend(tree, "root")

    if not differ.culprits:
        print(
            f"agreement within {args.tolerance:.0%} everywhere "
            f"({differ.checked} subtrees checked)"
        )
        return 0

    for n, (path, node, ours, ref) in enumerate(differ.culprits):
        out = args.input.with_suffix(f".culprit{n}.csg")
        out.write_text(emit(node))
        print(
            f"DIVERGES at {path} [{node.name}]: scad123d={ours:.4f} "
            f"OpenSCAD={ref:.4f} -- children individually agree, so this "
            f"operation is at fault; subtree saved to {out}"
        )
    print(f"({differ.checked} subtrees checked)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
