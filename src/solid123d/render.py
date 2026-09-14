"""Drop-in replacements for SolidPython's render functions.

There is no OpenSCAD source to render, so ``scad_render_to_file`` exports
real geometry instead: a ``.scad`` filename is transparently rewritten to
``.step``. ``.step``/``.stp``/``.stl`` filenames are exported as-is.
"""

import warnings
from pathlib import Path

from build123d import Shape
from build123d import export_stl as _export_stl

from .export import export_step as _export_step


def scad_render_to_file(
    scad_object: Shape,
    filepath: str | Path | None = None,
    out_dir: str | Path = "",
    file_header: str = "",
    include_orig_code: bool = True,
    filename: str | Path | None = None,
    **kwargs: object,
) -> str:
    path = Path(filepath if filepath is not None else (filename or "output.step"))
    if out_dir:
        path = Path(out_dir) / path
    suffix = path.suffix.lower()
    if suffix == ".scad":
        path = path.with_suffix(".step")
        warnings.warn(
            f"solid123d cannot write OpenSCAD source; exporting STEP to {path} instead",
            stacklevel=2,
        )
        suffix = ".step"
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".stl":
        _export_stl(scad_object, str(path))
    elif suffix in (".step", ".stp"):
        _export_step(scad_object, str(path))
    else:
        raise ValueError(f"unsupported export format: {path.suffix}")
    return str(path)


def scad_render(scad_object: Shape, file_header: str = "") -> str:
    raise NotImplementedError(
        "solid123d builds real geometry, not OpenSCAD source; use "
        "scad_render_to_file() to export STEP/STL, or pass the object to "
        "build123d viewers/exporters directly"
    )
