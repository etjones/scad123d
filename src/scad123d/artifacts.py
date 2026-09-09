"""``scad123d-artifacts``: after a ``scad123d-batch`` run, put everything a
comparison needs beside each model's STEP: the source ``.scad`` (copied, or
``--symlink``ed) and OpenSCAD's own STL render of it.

    scad123d-artifacts ~/models -o ~/models-step -j 8 --timeout 300
    scad123d-artifacts ~/models -o ~/models-step --include-overlay ~/overlay
    scad123d-artifacts --report ~/models-step

A separate pass, on purpose: the STEP run is the expensive, fragile one and
should not share its workers with OpenSCAD renders. This tool reads the
batch ledger for the list of inputs and their result classes (``--status``
picks which classes get an STL; every class but ``excluded`` by default,
since a failed STEP still wants a reference mesh for the repair pass), and
records each render in an ``stl`` table of the same ledger so it, too, is
resumable: re-run the same command and only what is missing or failed is
done again. Byte-identical inputs are rendered once and hard-linked, the
same as the batch does for STEP.

Renders go through OpenSCAD directly (Manifold backend when the binary has
it, binary STL), with the same ``--include-overlay`` mechanism as the
batch, so the STL sees exactly the includes the STEP saw.
"""

import argparse
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .batch import LEDGER_NAME, STATUS_EXCLUDED
from .cli import CLASS_EMPTY, CLASS_OK, CLASS_OPENSCAD, CLASS_TIMEOUT
from .includes import overlay_dir
from .openscad import mesh_backend, require_openscad

STL_TABLE = "stl"
EMPTY_MARKER = "top level object is empty"


class EmptyModel(RuntimeError):
    """OpenSCAD refused to export: the model has no top-level geometry."""


BINARY_STL_HEADER = 84  # 80-byte header + uint32 triangle count

Renderer = Callable[[Path, Path, float, str | None], None]


# --- ledger -----------------------------------------------------------------


class ArtifactLedger:
    """The batch ledger, plus an ``stl`` table this pass owns.

    Read-only on the batch's ``files`` table; the ``stl`` table has one row
    per rendered input with the render's result class.
    """

    def __init__(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(
                f"no batch ledger at {path}; run scad123d-batch first"
            )
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            f"""CREATE TABLE IF NOT EXISTS {STL_TABLE} (
                path TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                message TEXT,
                seconds REAL,
                updated REAL
            )"""
        )
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def inputs(self, statuses: set[str] | None) -> list[tuple[str, str, str | None]]:
        """``(path, step status, duplicate_of)`` for every batch row, or
        only those in *statuses*."""
        with self._lock:
            rows = self._db.execute(
                "SELECT path, status, duplicate_of FROM files ORDER BY path"
            ).fetchall()
        if statuses is None:
            return rows
        return [row for row in rows if row[1] in statuses]

    def stl_status(self, path: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT status FROM {STL_TABLE} WHERE path = ?", (path,)
            ).fetchone()
        return row[0] if row else None

    def record(
        self, path: str, status: str, message: str | None, seconds: float | None
    ) -> None:
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO {STL_TABLE}"
                " (path, status, message, seconds, updated) VALUES (?, ?, ?, ?, ?)",
                (path, status, message, seconds, time.time()),
            )
            self._db.commit()

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def counts(self) -> Counter[str]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT status, COUNT(*) FROM {STL_TABLE} GROUP BY status"
            ).fetchall()
        return Counter(dict(rows))

    def failures(self) -> list[tuple[str, str, str | None]]:
        with self._lock:
            return self._db.execute(
                f"SELECT path, status, message FROM {STL_TABLE}"
                " WHERE status NOT IN (?, ?) ORDER BY path",
                (CLASS_OK, CLASS_EMPTY),
            ).fetchall()


def infer_source(ledger: ArtifactLedger, out_dir: Path) -> Path | None:
    """The source root the batch was run with: the ancestor of a finished
    input under which its STEP sits at the mirrored path in *out_dir*."""
    for path, status, duplicate_of in ledger.inputs({CLASS_OK}):
        if duplicate_of:
            continue
        candidate = Path(path)
        for root in candidate.parents:
            rel = candidate.relative_to(root).with_suffix(".step")
            if (out_dir / rel).exists():
                return root
        return None
    return None


def check_source(ledger: ArtifactLedger, source: Path, out_dir: Path) -> None:
    """Fail fast when *source* is not the root the batch used: outputs would
    land in a parallel tree beside the STEPs instead of next to them."""
    ok = [(path, dup) for path, _s, dup in ledger.inputs({CLASS_OK}) if not dup][:50]
    if not ok:
        return
    for path, _dup in ok:
        try:
            rel = Path(path).relative_to(source).with_suffix(".step")
        except ValueError:
            break
        if (out_dir / rel).exists():
            return
    inferred = infer_source(ledger, out_dir)
    hint = f"; the ledger's STEPs sit under source root {inferred}" if inferred else ""
    raise ValueError(
        f"no STEP from the ledger is found under {out_dir} for source root "
        f"{source}{hint}. Pass the same source directory scad123d-batch was run "
        "with, or omit it to use the inferred one."
    )


# --- the work ---------------------------------------------------------------


def render_stl(scad: Path, out: Path, timeout: float, openscadpath: str | None) -> None:
    """OpenSCAD's own STL export of *scad* to *out*.

    Renders to a temporary name beside the target and moves it into place,
    so a render killed mid-write never leaves a truncated ``.stl`` that a
    later run would mistake for a finished one.
    """
    binary = require_openscad()
    tmp = out.with_name(out.name + ".part")
    args = [str(binary), "-o", str(tmp), "--export-format", "binstl"]
    backend = mesh_backend()
    if backend:
        args.append(f"--backend={backend}")
    args.append(str(scad))
    env = dict(os.environ)
    if openscadpath:
        env["OPENSCADPATH"] = os.pathsep.join(
            p for p in (openscadpath, os.environ.get("OPENSCADPATH")) if p
        )
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, env=env, check=False
    )
    if result.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        stderr = result.stderr.strip()
        if EMPTY_MARKER in stderr:
            raise EmptyModel(stderr)
        raise RuntimeError(
            f"OpenSCAD exited {result.returncode}: {stderr[-2000:]}"
            if stderr
            else f"OpenSCAD exited {result.returncode} without output"
        )
    tmp.replace(out)


def classify_stl(path: Path) -> str:
    """``empty`` for a mesh with no triangles, else ``ok``. OpenSCAD normally
    refuses to export an empty model at all (see ``EmptyModel``); this
    catches a mesh that exported but holds nothing."""
    with open(path, "rb") as fh:
        head = fh.read(BINARY_STL_HEADER)
        if head.startswith(b"solid") and not _looks_binary(head, path):
            return CLASS_EMPTY if b"facet" not in head + fh.read() else CLASS_OK
    if len(head) < BINARY_STL_HEADER:
        return CLASS_EMPTY
    (count,) = struct.unpack("<I", head[80:84])
    return CLASS_EMPTY if count == 0 else CLASS_OK


def _looks_binary(head: bytes, path: Path) -> bool:
    """A binary STL may also start with ``solid``; its size is exact."""
    if len(head) < BINARY_STL_HEADER:
        return False
    (count,) = struct.unpack("<I", head[80:84])
    return path.stat().st_size == BINARY_STL_HEADER + 50 * count


def place_source(scad: Path, dest: Path, symlink: bool) -> None:
    """Copy (or symlink) the model's source beside its outputs; idempotent."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if symlink:
        if dest.is_symlink() and dest.readlink() == scad:
            return
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        dest.symlink_to(scad)
        return
    if dest.is_symlink():
        dest.unlink()
    elif dest.exists():
        src_st, dst_st = scad.stat(), dest.stat()
        if (src_st.st_size, int(src_st.st_mtime)) == (
            dst_st.st_size,
            int(dst_st.st_mtime),
        ):
            return
    shutil.copy2(scad, dest)


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


@dataclass
class Job:
    path: str
    stl: Path
    canonical: str | None  # a byte-identical input whose STL this can share


@dataclass
class Progress:
    started: float
    total: int
    done: int = 0
    skipped: int = 0
    counts: Counter[str] = field(default_factory=Counter)


class ArtifactPass:
    def __init__(
        self,
        source: Path,
        out_dir: Path,
        *,
        jobs: int = 4,
        timeout: float = 300,
        statuses: set[str] | None = None,
        symlink: bool = False,
        force: bool = False,
        include_overlay: Path | None = None,
        no_stl: bool = False,
        no_source: bool = False,
        renderer: Renderer | None = None,
    ) -> None:
        self.source = source.resolve()
        self.out_dir = out_dir.resolve()
        self.jobs = jobs
        self.timeout = timeout
        self.statuses = statuses
        self.symlink = symlink
        self.force = force
        self.include_overlay = include_overlay.resolve() if include_overlay else None
        self.no_stl = no_stl
        self.no_source = no_source
        self.renderer = renderer or render_stl
        self.ledger = ArtifactLedger(self.out_dir / LEDGER_NAME)
        self.stop = threading.Event()
        self.progress = Progress(time.time(), 0)
        self._lock = threading.Lock()

    def output_for(self, path: str, suffix: str) -> Path:
        rel = Path(path).relative_to(self.source)
        return self.out_dir / rel.with_suffix(suffix)

    def plan(self, limit: int | None = None) -> tuple[list[Job], list[Job]]:
        """Jobs in two waves: originals first, then duplicates, which can
        link the original's STL once it exists.

        With *limit*, only inputs that still need work are planned, at most
        that many, so the run's total is the number asked for."""
        originals: list[Job] = []
        duplicates: list[Job] = []
        for path, _status, duplicate_of in self.ledger.inputs(self.statuses):
            job = Job(path, self.output_for(path, ".stl"), duplicate_of)
            (duplicates if duplicate_of else originals).append(job)
        if limit is not None:
            originals = [j for j in originals if self._needs_work(j)][:limit]
            duplicates = [j for j in duplicates if self._needs_work(j)]
            duplicates = duplicates[: max(limit - len(originals), 0)]
        return originals, duplicates

    def _needs_work(self, job: Job) -> bool:
        return self.no_stl or self._needs_stl(job)

    def _needs_stl(self, job: Job) -> bool:
        if self.force or not job.stl.exists():
            return True
        return self.ledger.stl_status(job.path) not in (CLASS_OK, CLASS_EMPTY)

    def _render(self, job: Job) -> tuple[str, str | None]:
        job.stl.parent.mkdir(parents=True, exist_ok=True)
        openscadpath = (
            str(overlay_dir(self.include_overlay, self.source, Path(job.path)))
            if self.include_overlay
            else None
        )
        try:
            self.renderer(Path(job.path), job.stl, self.timeout, openscadpath)
        except subprocess.TimeoutExpired:
            job.stl.with_name(job.stl.name + ".part").unlink(missing_ok=True)
            return CLASS_TIMEOUT, f"no STL after {self.timeout:g}s"
        except EmptyModel:
            job.stl.unlink(missing_ok=True)
            return CLASS_EMPTY, "no top-level geometry"
        except Exception as exc:  # noqa: BLE001 - any failure is a row, not a crash
            return CLASS_OPENSCAD, str(exc)[:2000]
        return classify_stl(job.stl), None

    def _do(self, job: Job) -> None:
        if self.stop.is_set():
            return
        if not self.no_source:
            place_source(
                Path(job.path), self.output_for(job.path, ".scad"), self.symlink
            )
        if self.no_stl or not self._needs_stl(job):
            self._advance(None)
            return
        started = time.perf_counter()
        if job.canonical:
            shared = self.output_for(job.canonical, ".stl")
            status = self.ledger.stl_status(job.canonical)
            if shared.exists() and status in (CLASS_OK, CLASS_EMPTY):
                _link_or_copy(shared, job.stl)
                self.ledger.record(job.path, status, None, 0.0)
                self._advance(status)
                return
        status, message = self._render(job)
        self.ledger.record(job.path, status, message, time.perf_counter() - started)
        self._advance(status)

    def _advance(self, status: str | None) -> None:
        with self._lock:
            self.progress.done += 1
            if status is None:
                self.progress.skipped += 1
            else:
                self.progress.counts[status] += 1

    def _summary(self) -> str:
        p = self.progress
        elapsed = time.time() - p.started
        worked = p.done - p.skipped
        rate = worked / elapsed if elapsed > 0 else 0.0
        left = max(p.total - p.done, 0)
        eta = f"{left / rate / 60:.0f} min" if rate > 0 else "?"
        failed = sum(v for k, v in p.counts.items() if k not in (CLASS_OK, CLASS_EMPTY))
        return (
            f"{p.done}/{p.total}  rendered {worked} (ok {p.counts.get(CLASS_OK, 0)}, "
            f"empty {p.counts.get(CLASS_EMPTY, 0)}, failed {failed})  "
            f"skipped {p.skipped}  {rate * 60:.1f}/min  ETA {eta}"
        )

    def run(self, originals: list[Job], duplicates: list[Job]) -> Counter[str]:
        self.progress = Progress(time.time(), len(originals) + len(duplicates))
        for wave in (originals, duplicates):
            if wave and not self.stop.is_set():
                self._run_wave(wave)
        print(f"scad123d-artifacts: {self._summary()}", file=sys.stderr, flush=True)
        return self.ledger.counts()

    def _run_wave(self, jobs: list[Job]) -> None:
        last_log = time.time()
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            futures = [pool.submit(self._do, job) for job in jobs]
            try:
                for future in futures:
                    future.result()
                    if time.time() - last_log >= 10:
                        last_log = time.time()
                        print(
                            f"scad123d-artifacts: {self._summary()}",
                            file=sys.stderr,
                            flush=True,
                        )
            except KeyboardInterrupt:
                self.stop.set()
                print(
                    "scad123d-artifacts: stopping after renders in progress",
                    file=sys.stderr,
                    flush=True,
                )
                for future in futures:
                    future.cancel()
                raise


# --- reporting --------------------------------------------------------------


def report(out_dir: Path) -> int:
    ledger = ArtifactLedger(out_dir / LEDGER_NAME)
    try:
        counts = ledger.counts()
        total = sum(counts.values())
        print(f"{total} STL renders recorded in {ledger.path}")
        for status, n in counts.most_common():
            print(f"  {status:16} {n}")
        failures = ledger.failures()
        if failures:
            print("\nmost common failure messages:")
            common = Counter(_first_line(m) for _, _, m in failures if m)
            for message, n in common.most_common(10):
                print(f"  {n:6}  {message[:100]}")
    finally:
        ledger.close()
    return 0


def _first_line(message: str) -> str:
    lines = [ln.strip() for ln in message.splitlines() if ln.strip()]
    for line in lines:
        if line.startswith("ERROR"):
            return line
    return lines[-1] if lines else message


# --- CLI --------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scad123d-artifacts",
        description="After scad123d-batch: put each model's .scad and OpenSCAD's "
        "STL render beside its STEP and CSG.",
    )
    parser.add_argument(
        "source",
        type=Path,
        nargs="?",
        help="the directory scad123d-batch was run on (default: inferred from "
        "the ledger and the STEPs in OUT_DIR)",
    )
    parser.add_argument(
        "-o", "--out-dir", type=Path, help="the scad123d-batch output directory"
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=os.cpu_count() or 4,
        help="parallel OpenSCAD renders (default: CPU count)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="seconds allowed per render (default: 300)",
    )
    parser.add_argument(
        "--status",
        default=None,
        help="comma-separated STEP result classes to render STLs for "
        "(default: every class except excluded)",
    )
    parser.add_argument(
        "--symlink",
        action="store_true",
        help="symlink the .scad instead of copying it",
    )
    parser.add_argument(
        "--no-source", action="store_true", help="don't place the .scad"
    )
    parser.add_argument("--no-stl", action="store_true", help="don't render STLs")
    parser.add_argument(
        "--force", action="store_true", help="re-render STLs that already exist"
    )
    parser.add_argument(
        "--include-overlay",
        type=Path,
        metavar="DIR",
        help="the overlay tree given to scad123d-batch, so renders see the "
        "same includes",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="process at most N files"
    )
    parser.add_argument("--dry-run", action="store_true", help="plan only")
    parser.add_argument(
        "--report", type=Path, metavar="OUT_DIR", help="summarize STL renders"
    )
    return parser


def _statuses(arg: str | None, ledger: ArtifactLedger) -> set[str]:
    if arg:
        return {s.strip() for s in arg.split(",") if s.strip()}
    return {status for _, status, _ in ledger.inputs(None)} - {STATUS_EXCLUDED}


def _resolve_source(source: Path | None, out_dir: Path) -> Path:
    ledger = ArtifactLedger(out_dir.resolve() / LEDGER_NAME)
    try:
        if source is None:
            inferred = infer_source(ledger, out_dir.resolve())
            if inferred is None:
                raise ValueError(
                    "cannot infer the source root: no finished STEP found under "
                    f"{out_dir}; pass the source directory explicitly"
                )
            print(f"scad123d-artifacts: source root {inferred}", file=sys.stderr)
            return inferred
        check_source(ledger, source.resolve(), out_dir.resolve())
        return source
    finally:
        ledger.close()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.report:
        return report(args.report)
    if args.out_dir is None:
        parser.error("-o OUT_DIR is required")
    if args.source is not None and not args.source.is_dir():
        parser.error(f"not a directory: {args.source}")
    if not args.no_stl:
        require_openscad()
    try:
        source = _resolve_source(args.source, args.out_dir)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    try:
        artifact_pass = ArtifactPass(
            source,
            args.out_dir,
            jobs=args.jobs,
            timeout=args.timeout,
            symlink=args.symlink,
            force=args.force,
            include_overlay=args.include_overlay,
            no_stl=args.no_stl,
            no_source=args.no_source,
        )
    except FileNotFoundError as exc:
        parser.error(str(exc))
    artifact_pass.statuses = _statuses(args.status, artifact_pass.ledger)
    originals, duplicates = artifact_pass.plan(limit=args.limit)
    pending = sum(1 for j in originals + duplicates if artifact_pass._needs_stl(j))
    scope = f"limited to {args.limit} of the" if args.limit is not None else "of"
    print(
        f"scad123d-artifacts: {len(originals) + len(duplicates)} inputs {scope} "
        f"{sorted(artifact_pass.statuses)} classes, {len(duplicates)} duplicates, "
        f"{pending} STLs to render with {args.jobs} workers",
        file=sys.stderr,
    )
    if args.dry_run:
        artifact_pass.ledger.close()
        return 0
    try:
        final = artifact_pass.run(originals, duplicates)
    except KeyboardInterrupt:
        return 130
    finally:
        artifact_pass.ledger.close()
    print(
        f"scad123d-artifacts: ledger now {dict(final)}  ({artifact_pass.ledger.path})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
