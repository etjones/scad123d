"""``scad123d-review``: gather the worst conversions into one folder per
model -- source, CSG, OpenSCAD's STL, scad123d's STEP, a thumbnail of OpenSCAD's mesh, and a README with the ledger's
account of the failure -- and keep
that set fixed across runs so a bugfix can be measured against it.

    scad123d-review -o ~/models-step ~/review --top 100
    scad123d-review -o ~/models-step ~/review          # after a fix: same set, refreshed

The first run picks the N worst cases of a class (``mismatch`` by volume
disagreement, largest first) and records them in ``cases.json``. Every
later run keeps that selection, re-reads each case's current ledger row,
appends any change to the case's history, and rewrites ``INDEX.md`` with what has
been resolved since selection and since the previous run. ``--reselect``
starts a fresh set.

Files are symlinked, not copied, so the folder follows the output tree as
files are regenerated; open the STEP in your own viewer. The one thumbnail
is OpenSCAD's preview of its STL, the reference shape.
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifacts import ArtifactLedger, infer_source
from .batch import LEDGER_NAME
from .cli import CLASS_MISMATCH, CLASS_OK
from .openscad import require_openscad

CASES_FILE = "cases.json"
INDEX_FILE = "INDEX.md"
SUFFIXES = (".scad", ".csg", ".stl", ".step")

Thumbnailer = Callable[[Path, Path], bool]


# --- ledger rows ------------------------------------------------------------


@dataclass
class Row:
    path: str
    status: str
    message: str
    seconds: float | None
    attempts: int
    stage: str | None
    volume: float | None
    scad_volume: float | None
    meshed: list[str]
    warnings: list[str]
    traceback: str | None
    updated: float | None

    @property
    def pct(self) -> float | None:
        """Volume disagreement relative to OpenSCAD, signed."""
        if self.volume is None or not self.scad_volume:
            return None
        return (self.volume - self.scad_volume) / self.scad_volume * 100


COLUMNS = (
    "path, status, message, seconds, attempts, stage, volume, scad_volume,"
    " meshed, warnings, traceback, updated"
)


def _row(t: tuple[Any, ...]) -> Row:
    return Row(
        path=t[0],
        status=t[1],
        message=t[2] or "",
        seconds=t[3],
        attempts=t[4] or 0,
        stage=t[5],
        volume=t[6],
        scad_volume=t[7],
        meshed=json.loads(t[8]) if t[8] else [],
        warnings=json.loads(t[9]) if t[9] else [],
        traceback=t[10],
        updated=t[11],
    )


def worst(ledger: ArtifactLedger, status: str, top: int) -> list[Row]:
    """The *top* rows of a class, worst first: by volume disagreement for
    mismatches, by most recent failure otherwise."""
    rows = [
        _row(t)
        for t in ledger.query(
            f"SELECT {COLUMNS} FROM files WHERE status = ?", (status,)
        )
    ]
    if status == CLASS_MISMATCH:
        rows.sort(key=lambda r: -abs(r.pct or 0))
    else:
        rows.sort(key=lambda r: -(r.updated or 0))
    return rows[:top]


def current(ledger: ArtifactLedger, path: str) -> Row | None:
    found = ledger.query(f"SELECT {COLUMNS} FROM files WHERE path = ?", (path,))
    return _row(found[0]) if found else None


# --- thumbnails -------------------------------------------------------------


def _openscad_png(scad_text: str, png: Path) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        scad = Path(tmp) / "thumb.scad"
        scad.write_text(scad_text, encoding="utf-8")
        try:
            subprocess.run(
                [
                    str(require_openscad()),
                    "-o",
                    str(png),
                    "--imgsize=640,480",
                    "--autocenter",
                    "--viewall",
                    "--colorscheme=Tomorrow",
                    str(scad),
                ],
                capture_output=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False
    return png.exists()


def thumbnail_stl(stl: Path, png: Path) -> bool:
    return _openscad_png(f'import("{stl.resolve()}");\n', png)


def _stale(png: Path, source: Path) -> bool:
    return not png.exists() or png.stat().st_mtime < source.stat().st_mtime


# --- cases ------------------------------------------------------------------


@dataclass
class Case:
    rank: int
    path: str
    dir: str
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def first(self) -> dict[str, Any]:
        return self.history[0]

    @property
    def latest(self) -> dict[str, Any]:
        return self.history[-1]

    def to_json(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "path": self.path,
            "dir": self.dir,
            "history": self.history,
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> "Case":
        return Case(d["rank"], d["path"], d["dir"], d["history"])


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:60]


def _snapshot(row: Row | None, when: str) -> dict[str, Any]:
    if row is None:
        return {
            "date": when,
            "status": "missing",
            "message": "not in the ledger",
            "pct": None,
        }
    return {
        "date": when,
        "status": row.status,
        "message": row.message.splitlines()[0] if row.message else "",
        "pct": None if row.pct is None else round(row.pct, 2),
    }


def _changed(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (a["status"], a["message"]) != (b["status"], b["message"])


class Review:
    def __init__(
        self,
        out_dir: Path,
        review_dir: Path,
        *,
        source: Path | None = None,
        jobs: int = 4,
        thumbnails: bool = True,
        stl_thumbnailer: Thumbnailer | None = None,
    ) -> None:
        self.out_dir = out_dir.resolve()
        self.review_dir = review_dir.resolve()
        self.ledger = ArtifactLedger(self.out_dir / LEDGER_NAME)
        inferred = source or infer_source(self.ledger, self.out_dir)
        if inferred is None:
            raise ValueError(f"cannot infer the source root from {self.out_dir}")
        self.source = inferred.resolve()
        self.jobs = jobs
        self.thumbnails = thumbnails
        self.stl_thumbnailer = stl_thumbnailer or thumbnail_stl
        self.today = time.strftime("%Y-%m-%d")
        self.review_dir.mkdir(parents=True, exist_ok=True)

    # -- selection

    def load(self) -> list[Case]:
        f = self.review_dir / CASES_FILE
        if not f.exists():
            return []
        return [
            Case.from_json(d)
            for d in json.loads(f.read_text(encoding="utf-8"))["cases"]
        ]

    def save(self, cases: list[Case], status: str) -> None:
        payload = {
            "status": status,
            "out_dir": str(self.out_dir),
            "source": str(self.source),
            "cases": [c.to_json() for c in cases],
        }
        (self.review_dir / CASES_FILE).write_text(
            json.dumps(payload, indent=1), encoding="utf-8"
        )

    def select(self, status: str, top: int) -> list[Case]:
        cases = []
        for rank, row in enumerate(worst(self.ledger, status, top), start=1):
            rel = Path(row.path).relative_to(self.source)
            name = f"{rank:03d}-{_slug(rel.parent.name)}-{_slug(rel.stem)}"
            cases.append(Case(rank, row.path, name, [_snapshot(row, self.today)]))
        return cases

    # -- per case

    def outputs(self, path: str) -> dict[str, Path]:
        rel = Path(path).relative_to(self.source)
        found = {".scad": Path(path)}
        for suffix in SUFFIXES:
            candidate = self.out_dir / rel.with_suffix(suffix)
            if candidate.exists():
                found[suffix] = candidate
        return found

    def _link(self, target: Path, link: Path) -> None:
        if link.is_symlink() and link.readlink() == target:
            return
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)

    def refresh(self, case: Case) -> tuple[Row | None, dict[str, Path]]:
        """Bring one case up to date: history, links, thumbnails, README."""
        row = current(self.ledger, case.path)
        now = _snapshot(row, self.today)
        if _changed(case.latest, now):
            case.history.append(now)
        folder = self.review_dir / case.dir
        folder.mkdir(exist_ok=True)
        files = self.outputs(case.path)
        stem = Path(case.path).stem
        for suffix, target in files.items():
            self._link(target, folder / f"{stem}{suffix}")
        for stale in folder.glob("*"):
            if (
                stale.is_symlink()
                and stale.suffix in SUFFIXES
                and stale.suffix not in files
            ):
                stale.unlink()
        if self.thumbnails:
            self._thumbnails(folder, files)
        (folder / "README.md").write_text(
            self.readme(case, row, files, folder), encoding="utf-8"
        )
        return row, files

    def _thumbnails(self, folder: Path, files: dict[str, Path]) -> None:
        stl = files.get(".stl")
        if stl and _stale(folder / "stl.png", stl):
            self.stl_thumbnailer(stl, folder / "stl.png")

    # -- text

    def readme(
        self, case: Case, row: Row | None, files: dict[str, Path], folder: Path
    ) -> str:
        name = Path(case.path).name
        rel = Path(case.path).relative_to(self.source)
        lines = [
            f"# {name}",
            "",
            f"rank {case.rank} · `{rel.parent}` · [{case.path}]({case.path})",
            "",
        ]
        lines += ["## Status", ""]
        lines += [
            f"- **now** ({case.latest['date']}): `{case.latest['status']}` {case.latest['message']}"
        ]
        lines += [
            f"- **when selected** ({case.first['date']}): `{case.first['status']}` {case.first['message']}"
        ]
        if len(case.history) > 2:
            lines += ["", "| date | status | message |", "|---|---|---|"]
            lines += [
                f"| {h['date']} | {h['status']} | {h['message']} |"
                for h in case.history
            ]
        lines += ["", "## OpenSCAD's mesh", ""]
        if (folder / "stl.png").exists():
            lines += ["![OpenSCAD STL](stl.png)", ""]
        else:
            lines += ["no STL thumbnail (no STL, or the render failed)", ""]
        lines += self._details(row)
        lines += ["## Files", ""]
        stem = Path(case.path).stem
        for suffix in SUFFIXES:
            if suffix in files:
                lines.append(f"- [{stem}{suffix}]({stem}{suffix}) → `{files[suffix]}`")
            else:
                lines.append(f"- {stem}{suffix}: none")
        lines += ["", "## Source", ""]
        lines += ["```openscad", *self._source(case.path), "```", ""]
        return "\n".join(lines)

    def _details(self, row: Row | None) -> list[str]:
        if row is None:
            return ["## Ledger", "", "not in the ledger", ""]
        lines = ["## Ledger", ""]
        if row.pct is not None:
            lines.append(
                f"- volume: scad123d {row.volume:,.2f} vs OpenSCAD {row.scad_volume:,.2f} "
                f"({row.pct:+.1f}%)"
            )
        secs = f"{row.seconds:.2f} s" if row.seconds is not None else "?"
        lines.append(
            f"- build: {secs}, attempt {row.attempts}, stage {row.stage or '-'}"
        )
        if row.meshed:
            lines += ["- mesh fallbacks:"] + [f"  - {m}" for m in row.meshed[:10]]
        if row.warnings:
            lines += ["- OpenSCAD warnings:"] + [f"  - {w}" for w in row.warnings[:10]]
        if row.message and "\n" in row.message.strip():
            lines += ["", "```", row.message.strip(), "```"]
        if row.traceback:
            tail = "\n".join(row.traceback.rstrip().splitlines()[-16:])
            lines += [
                "",
                "<details><summary>traceback</summary>",
                "",
                "```",
                tail,
                "```",
                "",
                "</details>",
            ]
        return lines + [""]

    def _source(self, path: str, limit: int = 120) -> list[str]:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return [f"// unreadable: {exc}"]
        if len(text) > limit:
            return text[:limit] + [f"// ... {len(text) - limit} more lines"]
        return text

    def index(self, cases: list[Case], status: str) -> str:
        resolved = [c for c in cases if c.latest["status"] == CLASS_OK]
        changed_today = [
            c for c in cases if len(c.history) > 1 and c.latest["date"] == self.today
        ]
        lines = [
            f"# Review: {len(cases)} worst `{status}` conversions",
            "",
            (
                f"selected {cases[0].first['date'] if cases else '-'} from "
                f"`{self.out_dir}`; refreshed {self.today}"
            ),
            "",
            f"- **resolved since selection:** {len(resolved)} of {len(cases)}",
            f"- **changed in this refresh:** {len(changed_today)}"
            + (
                ": " + ", ".join(f"[{c.rank}](#{c.dir})" for c in changed_today[:30])
                if changed_today
                else ""
            ),
            "",
            "| rank | model | selected as | now | Δ vol | folder |",
            "|---:|---|---|---|---:|---|",
        ]
        for c in cases:
            first, last = c.first, c.latest
            now = (
                f"`{last['status']}`"
                if last["status"] == first["status"]
                else f"**`{last['status']}`**"
            )
            pct = "" if last["pct"] is None else f"{last['pct']:+.1f}%"
            lines.append(
                f"| {c.rank} | {Path(c.path).name} | `{first['status']}` {first['message'][:48]} "
                f"| {now} | {pct} | [{c.dir}]({c.dir}/README.md) |"
            )
        return "\n".join(lines) + "\n"

    # -- run

    def run(self, status: str, top: int, reselect: bool) -> list[Case]:
        cases = [] if reselect else self.load()
        if not cases:
            cases = self.select(status, top)
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            list(pool.map(self.refresh, cases))
        self.save(cases, status)
        (self.review_dir / INDEX_FILE).write_text(
            self.index(cases, status), encoding="utf-8"
        )
        return cases


# --- CLI --------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scad123d-review",
        description="Collect the worst conversions of a batch into one folder per "
        "model, with thumbnails and a README each, and track them across fixes.",
    )
    parser.add_argument("review_dir", type=Path, help="where the review folders go")
    parser.add_argument(
        "-o",
        "--out-dir",
        type=Path,
        required=True,
        help="the scad123d-batch output directory",
    )
    parser.add_argument(
        "--top", type=int, default=100, help="how many cases to select (default: 100)"
    )
    parser.add_argument(
        "--status",
        default=CLASS_MISMATCH,
        help="result class to review (default: mismatch)",
    )
    parser.add_argument(
        "--reselect", action="store_true", help="drop the recorded set and pick afresh"
    )
    parser.add_argument(
        "--source", type=Path, default=None, help="source root (default: inferred)"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=4, help="parallel thumbnail renders"
    )
    parser.add_argument("--no-thumbnails", action="store_true", help="skip rendering")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        review = Review(
            args.out_dir,
            args.review_dir,
            source=args.source,
            jobs=args.jobs,
            thumbnails=not args.no_thumbnails,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"scad123d-review: {exc}", file=sys.stderr)
        return 2
    try:
        cases = review.run(args.status, args.top, args.reselect)
    finally:
        review.ledger.close()
    resolved = sum(c.latest["status"] == CLASS_OK for c in cases)
    changed = sum(
        len(c.history) > 1 and c.latest["date"] == review.today for c in cases
    )
    print(
        f"scad123d-review: {len(cases)} cases in {review.review_dir}; "
        f"{resolved} resolved since selection, {changed} changed this run "
        f"({INDEX_FILE})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
