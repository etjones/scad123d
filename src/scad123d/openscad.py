"""Locating and invoking the OpenSCAD binary.

scad123d delegates the entire OpenSCAD language to OpenSCAD itself, so the
binary is a hard requirement. CSG export needs no OpenGL, so headless Linux
does not need an xvfb wrapper.
"""

import os
import platform
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from .errors import OpenSCADNotFoundError, OpenSCADRunError

_ENV_VAR = "SCAD123D_OPENSCAD"

_MAC_CANDIDATES = (
    "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
    "/Applications/OpenSCAD-nightly.app/Contents/MacOS/OpenSCAD",
)
_WINDOWS_CANDIDATES = (
    r"C:\Program Files\OpenSCAD\openscad.exe",
    r"C:\Program Files\OpenSCAD (Nightly)\openscad.exe",
    r"C:\Program Files (x86)\OpenSCAD\openscad.exe",
)
_LINUX_CANDIDATES = (
    "/var/lib/flatpak/exports/bin/org.openscad.OpenSCAD",
    "/snap/bin/openscad",
    "/usr/local/bin/openscad",
    "/usr/bin/openscad",
)

# Deliberately not functools.lru_cache: that would cache a failed search
# (None) for the rest of the process. In a long-lived session (a notebook, a
# batch job) installing OpenSCAD after the first failed call should not
# require restarting the interpreter -- only a *successful* lookup is worth
# avoiding repeated filesystem/PATH probing for, so only that gets cached.
_cached_binary: Path | None = None


def _search() -> Path | None:
    override = os.environ.get(_ENV_VAR)
    if override:
        path = Path(override).expanduser()
        return path if path.exists() else None

    for name in ("openscad", "openscad-nightly", "OpenSCAD"):
        found = shutil.which(name)
        if found:
            return Path(found)

    system = platform.system()
    candidates = {
        "Darwin": _MAC_CANDIDATES,
        "Windows": _WINDOWS_CANDIDATES,
    }.get(system, _LINUX_CANDIDATES)
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def find_openscad() -> Path | None:
    """Locate the OpenSCAD binary, or return None.

    Order: ``$SCAD123D_OPENSCAD``, then ``$PATH``, then platform-specific
    install locations. A successful result is cached; a failed search is not,
    so installing OpenSCAD after an earlier failed call is picked up on the
    next call rather than requiring a fresh process.
    """
    global _cached_binary
    if _cached_binary is not None:
        return _cached_binary
    _cached_binary = _search()
    return _cached_binary


def require_openscad() -> Path:
    binary = find_openscad()
    if binary is not None:
        return binary

    override = os.environ.get(_ENV_VAR)
    if override:
        raise OpenSCADNotFoundError(
            f"${_ENV_VAR} is set to {override!r}, but no OpenSCAD binary "
            "exists at that path."
        )
    raise OpenSCADNotFoundError(
        "The OpenSCAD binary is required but was not found. Install "
        "OpenSCAD (https://openscad.org/downloads.html) or set "
        f"${_ENV_VAR} to its full path."
    )


@lru_cache(maxsize=1)
def openscad_version() -> str:
    result = subprocess.run(
        [str(require_openscad()), "--version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return (result.stdout + result.stderr).strip().splitlines()[0] if (
        result.stdout or result.stderr
    ) else "unknown"


def _run(args: list[str], timeout: float) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise OpenSCADRunError(
            f"OpenSCAD exited {result.returncode}\n"
            f"  command: {' '.join(args)}\n"
            f"  stderr: {result.stderr.strip()[:2000]}"
        )
    return result.stderr


def export_csg(
    scad_path: str | Path,
    overrides: dict[str, object] | None = None,
    timeout: float = 600,
) -> str:
    """Run OpenSCAD's CSG export and return the flattened tree as text.

    ``overrides`` sets top-level variables, the same as OpenSCAD's ``-D``.
    """
    binary = require_openscad()
    scad_path = Path(scad_path).resolve()
    if not scad_path.exists():
        raise FileNotFoundError(scad_path)

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.csg"
        args = [str(binary), "-o", str(out)]
        for key, value in (overrides or {}).items():
            args += ["-D", f"{key}={_scad_literal(value)}"]
        args.append(str(scad_path))
        _run(args, timeout)
        if not out.exists():
            raise OpenSCADRunError(f"OpenSCAD produced no CSG output for {scad_path}")
        return out.read_text()


def export_mesh(
    source: str,
    suffix: str = ".stl",
    timeout: float = 600,
) -> Path:
    """Render CSG or OpenSCAD source text to a mesh file, returning its path.

    The caller owns the returned file and should unlink it. ``.csg`` is itself
    valid OpenSCAD input, which is what makes the subtree fallback possible.
    """
    binary = require_openscad()
    tmpdir = Path(tempfile.mkdtemp(prefix="scad123d-"))
    src = tmpdir / "subtree.csg"
    src.write_text(source)
    out = tmpdir / f"subtree{suffix}"
    _run([str(binary), "-o", str(out), str(src)], timeout)
    if not out.exists():
        raise OpenSCADRunError("OpenSCAD produced no mesh output")
    return out


def _scad_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_scad_literal(v) for v in value) + "]"
    return repr(value)
