"""``scad123d-includes``: find, classify, and supply the files a corpus of
.scad models ``include``/``use`` but does not ship with.

    scad123d-includes scan ~/models              # what's missing, and why
    scad123d-includes resolve ~/models -o ~/overlay        # siblings -> overlay
    scad123d-includes fetch ~/models -o ~/overlay          # Thingiverse -> overlay
    scad123d-batch ~/models -o ~/out --include-overlay ~/overlay

A scraped corpus (CodeCAD: one .scad per folder, the folder named
``<thing id>_<file index>``) loses the files a model includes -- its
``configuration.scad``, its ``helpers.scad``, the library it was written
against. OpenSCAD treats an unresolvable include as a *warning* and renders
whatever survives, so those models come out silently wrong rather than
failing. Nothing here needs a language model: includes are read straight
from the source text, resolution follows OpenSCAD's own search order, and
the missing files come from three deterministic places:

* **known libraries** -- a curated table of names people ``use`` (BOSL2,
  Write.scad, threads.scad ...) with where to get them; install these into
  OpenSCAD's library directory once;
* **siblings** -- the same thing's other files, already in the corpus under
  a neighbouring folder (26% of the missing local files in CodeCAD);
* **Thingiverse** -- the thing's file list via its API (token required),
  for the rest.

Supplied files go into an *overlay* tree mirroring the corpus layout,
never into the corpus itself; ``scad123d-batch --include-overlay`` puts
the right overlay folder on ``OPENSCADPATH`` for each model.
"""

import argparse
import json
import os
import platform
import re
import shutil
import sys
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .openscad import find_openscad

# --- known libraries -----------------------------------------------------------

# (pattern on the include path, library name, where to get it). Order
# matters: first match wins. Paths are as written in the .scad file.
KNOWN_LIBRARIES: list[tuple[str, str, str]] = [
    (r"^BOSL2/", "BOSL2", "https://github.com/BelfrySCAD/BOSL2"),
    (r"^BOSL/", "BOSL (v1, not BOSL2)", "https://github.com/revarbat/BOSL"),
    (r"^MCAD/", "MCAD", "https://github.com/openscad/MCAD (bundled with OpenSCAD)"),
    (r"^NopSCADlib/", "NopSCADlib", "https://github.com/nophead/NopSCADlib"),
    (r"^dotSCAD/", "dotSCAD", "https://github.com/JustinSDK/dotSCAD"),
    (r"^nutsnbolts/", "nutsnbolts", "https://github.com/JohK/nutsnbolts"),
    (r"^scad-utils/", "scad-utils", "https://github.com/openscad/scad-utils"),
    (r"^threadlib/", "threadlib", "https://github.com/adrianschlatter/threadlib"),
    (
        r"^gridfinity-rebuilt-openscad/",
        "gridfinity-rebuilt-openscad",
        "https://github.com/kennetek/gridfinity-rebuilt-openscad",
    ),
    (
        r"^Round-Anything/",
        "Round-Anything",
        "https://github.com/Irev-Dev/Round-Anything",
    ),
    (
        r"^Chamfers-for-OpenSCAD/",
        "Chamfers-for-OpenSCAD",
        "https://github.com/SebiTimeWaster/Chamfers-for-OpenSCAD",
    ),
    (r"^obiscad/", "obiscad", "https://github.com/Obijuan/obiscad"),
    (
        r"(^|/)[Ww]rite\.scad$",
        "Write.scad",
        "https://github.com/rohieb/Write.scad (mirror; install as write/Write.scad with its .dxf fonts)",
    ),
    (
        r"(^|/)threads\.scad$",
        "threads.scad",
        (
            "two libraries share this name with different APIs: Dan Kirshner's "
            "(metric_thread) at dkprojects.net/openscad-threads, and "
            "https://github.com/rcolyer/threads-scad (ScrewThread); check the call"
        ),
    ),
    (
        r"(^|/)text_on\.scad$",
        "text_on",
        "https://github.com/brodykenrick/text_on_OpenSCAD",
    ),
    (
        r"^utils/",
        "Thingiverse Customizer shared libraries (utils/build_plate.scad ...)",
        "the MakerBot Customizer library set; search 'customizer utils build_plate.scad'",
    ),
    (
        r"involute_gear",
        "parametric_involute_gear (Greg Frost)",
        "Thingiverse thing:3575 and mirrors",
    ),
    (r"^bitbeam-lib/", "bitbeam-lib", "search 'bitbeam-lib openscad'"),
]

_INCLUDE = re.compile(r"^\s*(include|use)\s*<([^>]+)>", re.MULTILINE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")


def includes_of(source: str) -> list[str]:
    """The paths a .scad file includes or uses, in order, comments stripped."""
    text = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))
    return [path.strip() for _, path in _INCLUDE.findall(text)]


# --- OpenSCAD's search path -----------------------------------------------------


def user_library_dir() -> Path:
    system = platform.system()
    if system == "Linux":
        return Path.home() / ".local/share/OpenSCAD/libraries"
    return Path.home() / "Documents/OpenSCAD/libraries"  # macOS and Windows


def bundled_library_dir() -> Path | None:
    binary = find_openscad()
    if binary is None:
        return None
    candidates = [
        binary.parent.parent / "Resources/libraries",  # macOS .app
        binary.parent.parent / "share/openscad/libraries",  # Linux prefix
        binary.parent / "libraries",  # Windows
    ]
    return next((c for c in candidates if c.is_dir()), None)


def library_dirs(extra: list[Path] | None = None) -> list[Path]:
    """Where OpenSCAD looks after the including file's own directory."""
    dirs = list(extra or [])
    dirs += [Path(p) for p in os.environ.get("OPENSCADPATH", "").split(os.pathsep) if p]
    dirs.append(user_library_dir())
    bundled = bundled_library_dir()
    if bundled:
        dirs.append(bundled)
    return dirs


def resolves(include: str, from_dir: Path, dirs: list[Path]) -> bool:
    if (from_dir / include).is_file():
        return True
    return any((d / include).is_file() for d in dirs)


# --- scanning ------------------------------------------------------------------


@dataclass
class Missing:
    file: Path  # the .scad that includes it
    include: str  # as written
    kind: str = ""  # library | sibling | absent
    library: str = ""  # for kind == library
    source: str = ""  # for kind == library: where to get it
    sibling: Path | None = None  # for kind == sibling: the file to supply


@dataclass
class Report:
    files: int = 0
    with_missing: int = 0
    missing: list[Missing] = field(default_factory=list)

    def by_kind(self) -> Counter[str]:
        return Counter(m.kind for m in self.missing)


def thing_of(path: Path) -> str | None:
    """The scraped-corpus thing id of a model: its folder name up to the last
    ``_<index>``; None for folders not named that way."""
    name = path.parent.name
    head, sep, tail = name.rpartition("_")
    return head if sep and tail.isdigit() and head else None


def scan(
    root: Path, dirs: list[Path] | None = None, overlay: Path | None = None
) -> Report:
    """Find every include that would not resolve, and classify it.

    ``overlay`` is a tree written by ``resolve``/``fetch``: each model's
    mirror folder in it counts as resolvable for that model."""
    dirs = library_dirs() if dirs is None else dirs
    report = Report()
    by_thing: dict[str, dict[str, Path]] = defaultdict(
        dict
    )  # thing -> basename -> path
    scad_files: list[Path] = []
    for path in _walk(root):
        scad_files.append(path)
        thing = thing_of(path)
        if thing:
            by_thing[thing].setdefault(path.name, path)
    report.files = len(scad_files)
    for path in scad_files:
        try:
            wanted = includes_of(path.read_text(errors="replace"))
        except OSError:
            continue
        search = ([overlay_dir(overlay, root, path)] if overlay else []) + dirs
        unresolved = [inc for inc in wanted if not resolves(inc, path.parent, search)]
        if not unresolved:
            continue
        report.with_missing += 1
        for inc in unresolved:
            report.missing.append(_classify(Missing(path, inc), by_thing))
    return report


def _walk(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.lower().endswith(".scad") and not name.startswith("."):
                yield Path(dirpath) / name


def _classify(m: Missing, by_thing: dict[str, dict[str, Path]]) -> Missing:
    for pattern, name, source in KNOWN_LIBRARIES:
        if re.search(pattern, m.include):
            m.kind, m.library, m.source = "library", name, source
            return m
    thing = thing_of(m.file)
    base = Path(m.include).name
    if thing and base in by_thing.get(thing, {}) and by_thing[thing][base] != m.file:
        m.kind, m.sibling = "sibling", by_thing[thing][base]
        return m
    m.kind = "absent"
    return m


# --- supplying files -----------------------------------------------------------


def overlay_dir(overlay: Path, root: Path, model: Path) -> Path:
    """The overlay folder for one model: mirrors its folder under the root."""
    return overlay / model.parent.relative_to(root)


def resolve_siblings(report: Report, root: Path, overlay: Path, apply: bool) -> int:
    """Copy each sibling-resolvable include into the model's overlay folder."""
    done = 0
    for m in report.missing:
        if m.kind != "sibling" or m.sibling is None:
            continue
        target = overlay_dir(overlay, root, m.file) / m.include
        if target.exists():
            continue
        if apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(m.sibling, target)
        done += 1
    return done


Fetcher = Callable[[str, str], bytes]  # (url, token) -> body


def _http_get(url: str, token: str) -> bytes:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def fetch_from_thingiverse(
    report: Report,
    root: Path,
    overlay: Path,
    token: str,
    apply: bool,
    http_get: Fetcher = _http_get,
    log: Callable[[str], None] = lambda s: None,
) -> int:
    """For each absent include whose model came from a Thingiverse thing,
    look the thing's files up and download a name match into the overlay.

    A fetched file may include further files; run ``scan`` again after.
    """
    wanted: dict[str, list[Missing]] = defaultdict(list)
    for m in report.missing:
        if m.kind == "absent" and (thing := thing_of(m.file)):
            wanted[str(int(thing))].append(m)  # folder ids are zero-padded
    done = 0
    for thing_id, items in sorted(wanted.items()):
        try:
            listing = json.loads(
                http_get(f"https://api.thingiverse.com/things/{thing_id}/files", token)
            )
        except Exception as exc:  # noqa: BLE001 -- network: report and go on
            log(f"thing {thing_id}: {type(exc).__name__}: {exc}")
            continue
        by_name = {
            entry.get("name", ""): entry for entry in listing if isinstance(entry, dict)
        }
        for m in items:
            entry = by_name.get(Path(m.include).name)
            if entry is None:
                log(
                    f"thing {thing_id}: no file named {Path(m.include).name!r} for {m.file.name}"
                )
                continue
            target = overlay_dir(overlay, root, m.file) / m.include
            if target.exists():
                continue
            if apply:
                url = entry.get("download_url") or entry.get("public_url")
                if not url:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(http_get(url, token))
            done += 1
            log(
                f"thing {thing_id}: {'fetched' if apply else 'would fetch'} {m.include} for {m.file.name}"
            )
    return done


# --- CLI -----------------------------------------------------------------------


def _print_summary(report: Report) -> None:
    kinds = report.by_kind()
    print(
        f"{report.files} .scad files; {report.with_missing} with an include that "
        f"does not resolve ({len(report.missing)} references): "
        + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
    )
    libraries = Counter(
        (m.library, m.source) for m in report.missing if m.kind == "library"
    )
    if libraries:
        print("\nknown libraries to install (files affected):")
        for (name, source), n in libraries.most_common():
            print(f"  {n:5d}  {name}\n         {source}")
    absent = Counter(m.include for m in report.missing if m.kind == "absent")
    if absent:
        print("\nmost common absent files (project-local; not in the corpus):")
        for inc, n in absent.most_common(15):
            print(f"  {n:5d}  {inc}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scad123d-includes",
        description="Find, classify, and supply the files a .scad corpus includes but lacks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("scan", "report unresolved includes"),
        ("resolve", "copy sibling files into an overlay"),
        ("fetch", "download absent files from Thingiverse into an overlay"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("root", type=Path, help="directory of .scad files")
        p.add_argument(
            "--library-dir",
            type=Path,
            action="append",
            default=[],
            help="extra library directory to count as resolvable (repeatable)",
        )
        if name != "scan":
            p.add_argument(
                "-o",
                "--overlay",
                type=Path,
                required=True,
                help="overlay tree to write",
            )
            p.add_argument(
                "--apply", action="store_true", help="write files (default: dry run)"
            )
        else:
            p.add_argument("--json", type=Path, help="write the full report as JSON")
    sub.choices["fetch"].add_argument(
        "--token",
        default=os.environ.get("THINGIVERSE_TOKEN"),
        help="Thingiverse app token (default: $THINGIVERSE_TOKEN)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dirs = library_dirs(args.library_dir)
    overlay = args.overlay if args.command in ("resolve", "fetch") else None
    report = scan(args.root, dirs, overlay=overlay)
    if args.command == "scan":
        _print_summary(report)
        if args.json:
            payload = [
                {
                    "file": str(m.file),
                    "include": m.include,
                    "kind": m.kind,
                    "library": m.library,
                    "source": m.source,
                    "sibling": str(m.sibling) if m.sibling else None,
                }
                for m in report.missing
            ]
            args.json.write_text(json.dumps(payload, indent=1))
            print(f"\nreport written to {args.json}")
        return 0
    if args.command == "resolve":
        n = resolve_siblings(report, args.root, args.overlay, args.apply)
        print(
            f"{'copied' if args.apply else 'would copy'} {n} sibling files into {args.overlay}"
        )
        return 0
    if not args.token:
        print(
            "scad123d-includes: fetch needs a Thingiverse app token: register an app "
            "at https://www.thingiverse.com/developers and pass --token or set "
            "$THINGIVERSE_TOKEN",
            file=sys.stderr,
        )
        return 1
    n = fetch_from_thingiverse(
        report,
        args.root,
        args.overlay,
        args.token,
        args.apply,
        log=lambda s: print(s, file=sys.stderr),
    )
    print(f"{'fetched' if args.apply else 'would fetch'} {n} files into {args.overlay}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
