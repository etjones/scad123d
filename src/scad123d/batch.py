"""``scad123d-batch``: convert a whole tree of .scad files to STEP, in
parallel, with a ledger that makes the run resumable.

    scad123d-batch ~/models -o ~/models-step -j 12 --timeout 120
    scad123d-batch ~/models -o ~/models-step --retry timeout --timeout 900
    scad123d-batch --report ~/models-step

Why not ``find | xargs -P scad2step``: importing build123d costs ~2s per
process while a typical model converts in well under a second, so the
worker half of this tool (``scad2step --batch``) is a long-lived process
that converts file after file. The harness here owns everything a worker
cannot: a hard per-file timeout enforced by *killing* the worker (OCCT can
spin in C++ where no Python signal reaches), crash isolation and respawn,
memory-based recycling, discovery of a tree that may still be growing, a
SQLite ledger so Ctrl-C and re-run resume exactly where things stood,
content-hash deduplication, and a live per-worker dashboard.

Every input's STEP lands at the same relative path under the output
directory, with its OpenSCAD CSG export beside it (``--no-csg`` to skip):
that text is what scad123d actually built from, so a wrong result can be
replayed and bisected (``scad123d-diff``) without re-running OpenSCAD.

Needs the ``batch`` extra: ``pip install 'scad123d[batch]'``.
"""

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import includes
from .cli import (
    CLASS_ERROR,
    CLASS_OK,
    CLASS_TIMEOUT,
)
from .includes import overlay_dir

CLASS_CRASH = "crash"
CLASS_MEMORY = "memory"  # killed for exceeding a memory limit; retry with fewer workers
STATUS_PENDING = "pending"
STATUS_EXCLUDED = "excluded"  # deliberately out of the run (unresolved includes)

# Seconds past the worker's own OpenSCAD timeout before the harness kills
# it: covers a build that hangs inside OCCT, which no timeout inside the
# worker can interrupt.
KILL_GRACE = 15.0
LEDGER_NAME = "ledger.sqlite"


# --- ledger -----------------------------------------------------------------


class Ledger:
    """Per-file conversion state in SQLite, safe to share across threads.

    One row per discovered input. ``status`` is ``pending`` until a worker
    reports, then the result class (``ok``, ``timeout``, ...). A file whose
    size or mtime changes between runs goes back to ``pending``.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                size INTEGER,
                mtime REAL,
                sha256 TEXT,
                status TEXT NOT NULL,
                message TEXT,
                seconds REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                meshed TEXT,
                duplicate_of TEXT,
                updated REAL
            )"""
        )
        # Diagnostic columns, added after the first ledgers existed: bring an
        # older file up to date rather than failing on it.
        present = {row[1] for row in self._db.execute("PRAGMA table_info(files)")}
        for column, kind in (
            ("traceback", "TEXT"),
            ("warnings", "TEXT"),
            ("stage", "TEXT"),
            ("volume", "REAL"),
            ("scad_volume", "REAL"),
        ):
            if column not in present:
                self._db.execute(f"ALTER TABLE files ADD COLUMN {column} {kind}")
        self._db.execute("CREATE INDEX IF NOT EXISTS files_status ON files(status)")
        self._db.execute("CREATE INDEX IF NOT EXISTS files_sha ON files(sha256)")
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def discover(self, path: str, size: int, mtime: float, sha256: str) -> None:
        """Record an input; a changed file returns to pending."""
        with self._lock:
            row = self._db.execute(
                "SELECT size, mtime FROM files WHERE path = ?", (path,)
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO files (path, size, mtime, sha256, status, updated)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (path, size, mtime, sha256, STATUS_PENDING, time.time()),
                )
            elif (row[0], row[1]) != (size, mtime):
                self._db.execute(
                    "UPDATE files SET size=?, mtime=?, sha256=?, status=?,"
                    " message=NULL, duplicate_of=NULL, updated=? WHERE path=?",
                    (size, mtime, sha256, STATUS_PENDING, time.time(), path),
                )
            self._db.commit()

    def reset(self, statuses: set[str]) -> int:
        """Return files in the given statuses to pending (for --retry)."""
        if not statuses:
            return 0
        marks = ",".join("?" * len(statuses))
        with self._lock:
            cur = self._db.execute(
                f"UPDATE files SET status=?, message=NULL, traceback=NULL"
                f" WHERE status IN ({marks})",
                (STATUS_PENDING, *statuses),
            )
            self._db.commit()
            return cur.rowcount

    def exclude(self, reasons: dict[str, str]) -> int:
        """Take files out of the run: status ``excluded`` with the reason,
        whatever their status was (a hollow "ok" included). ``--retry
        excluded`` brings them back."""
        with self._lock:
            for path, reason in reasons.items():
                self._db.execute(
                    "UPDATE files SET status=?, message=?, updated=? WHERE path=?",
                    (STATUS_EXCLUDED, reason, time.time(), path),
                )
            self._db.commit()
        return len(reasons)

    def pending(self) -> list[tuple[str, str]]:
        """(path, sha256) of every file still to convert."""
        with self._lock:
            return self._db.execute(
                "SELECT path, sha256 FROM files WHERE status = ?", (STATUS_PENDING,)
            ).fetchall()

    def ok_by_sha(self) -> dict[str, str]:
        """sha256 -> path of one already-converted file per content hash."""
        with self._lock:
            rows = self._db.execute(
                "SELECT sha256, path FROM files WHERE status = ? AND duplicate_of IS NULL",
                (CLASS_OK,),
            ).fetchall()
        return {sha: path for sha, path in rows}

    def record(
        self,
        path: str,
        status: str,
        message: str | None = None,
        seconds: float | None = None,
        meshed: list[str] | None = None,
        duplicate_of: str | None = None,
        traceback: str | None = None,
        warnings: list[str] | None = None,
        stage: str | None = None,
        volume: float | None = None,
        scad_volume: float | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE files SET status=?, message=?, seconds=?, meshed=?,"
                " duplicate_of=?, traceback=?, warnings=?, stage=?, volume=?,"
                " scad_volume=?, attempts=attempts+1, updated=? WHERE path=?",
                (
                    status,
                    message,
                    seconds,
                    json.dumps(meshed) if meshed else None,
                    duplicate_of,
                    traceback,
                    json.dumps(warnings) if warnings else None,
                    stage,
                    volume,
                    scad_volume,
                    time.time(),
                    path,
                ),
            )
            self._db.commit()

    def failures(self) -> list[tuple[str, str, str | None, str | None]]:
        """(path, status, message, traceback) for every non-ok, non-pending file."""
        with self._lock:
            return self._db.execute(
                "SELECT path, status, message, traceback FROM files"
                " WHERE status NOT IN (?, ?)",
                (CLASS_OK, STATUS_PENDING),
            ).fetchall()

    def lookup(self, path: str) -> tuple[Any, ...] | None:
        """One file's full row, by exact path or unique path suffix (either
        separator: ledgers written on Windows hold backslashes)."""
        needle = path.lstrip("/\\")
        with self._lock:
            rows = self._db.execute(
                "SELECT path, status, stage, message, seconds, meshed, warnings,"
                " volume, scad_volume, traceback FROM files"
                " WHERE path = ? OR path LIKE ? OR path LIKE ?",
                (path, "%/" + needle, "%\\" + needle),
            ).fetchall()
        return rows[0] if len(rows) == 1 else None

    def counts(self) -> Counter[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) FROM files GROUP BY status"
            ).fetchall()
        return Counter(dict(rows))

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()


def exclude_unresolved(batch: "Batch", apply: bool = True) -> int:
    """Mark every model with an unresolvable include as excluded.

    Uses the static scanner (no OpenSCAD run), honoring the include overlay
    if one is configured. A model that converted earlier but has a missing
    include is excluded too: its result was a hollow render.
    """
    report = includes.scan(batch.source, overlay=batch.include_overlay)
    missing: dict[str, list[str]] = {}
    for m in report.missing:
        missing.setdefault(str(m.file), []).append(m.include)
    reasons = {
        path: "unresolved include: " + ", ".join(sorted(set(incs)))
        for path, incs in missing.items()
    }
    return batch.ledger.exclude(reasons) if apply else len(reasons)


def _total_memory_mb() -> float:
    try:
        import psutil

        return psutil.virtual_memory().total / (1 << 20)
    except ImportError:  # pragma: no cover
        return 16 * 1024.0


def _available_mb() -> float:
    try:
        import psutil

        return psutil.virtual_memory().available / (1 << 20)
    except ImportError:  # pragma: no cover
        return float("inf")


def _tree_rss_mb(pid: int) -> float:
    """Resident memory of a worker and everything it spawned (OpenSCAD)."""
    try:
        import psutil

        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
        total = 0
        for p in procs:
            try:
                total += p.memory_info().rss
            except psutil.Error:
                continue
        return total / (1 << 20)
    except (ImportError, Exception):  # noqa: BLE001 -- psutil.Error family
        return 0.0


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Kill a worker and everything it spawned (its OpenSCAD renders)."""
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    elif sys.platform == "win32":
        # No process groups to signal; taskkill /T walks the process tree.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    proc.kill()


# --- discovery --------------------------------------------------------------


def _is_dataless(st: os.stat_result) -> bool:
    """A cloud-sync placeholder: nonzero size but no blocks on disk yet."""
    return st.st_size > 0 and getattr(st, "st_blocks", 1) == 0


def discover(root: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Yield every readable .scad under root, skipping hidden dirs and
    files a cloud client hasn't actually downloaded."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or not name.lower().endswith(".scad"):
                continue
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_size == 0 or _is_dataless(st):
                continue
            yield path, st


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- workers ----------------------------------------------------------------


@dataclass
class Task:
    path: str
    output: Path
    csg: Path | None
    siblings: list[str] = field(default_factory=list)  # same-content inputs


@dataclass
class WorkerState:
    index: int
    proc: subprocess.Popen[str] | None = None
    current: Task | None = None
    started: float = 0.0
    done: int = 0
    rss_mb: float = 0.0
    kill_reason: str | None = None
    kill_detail: str = ""  # for the ledger message
    dump_at: float = 0.0  # when SIGUSR1 (stack dump request) was sent
    recycle_after: bool = False


class Stats:
    """Counts for the ledger as a whole (``counts``) and for this run alone
    (``run_counts``, ``run_done`` of ``run_total``). The two differ under
    --limit, or when re-running with most of the corpus already done, and
    the dashboard reports them separately."""

    def __init__(self, initial: Counter[str], run_total: int = 0) -> None:
        self.lock = threading.Lock()
        self.counts = Counter(initial)
        self.run_counts: Counter[str] = Counter()
        self.run_total = run_total
        self.run_done = 0
        self.run_started = time.time()
        self.recent: list[float] = []  # completion timestamps, for the rate

    def add(self, status: str) -> None:
        with self.lock:
            self.counts[STATUS_PENDING] -= 1
            self.counts[status] += 1
            self.run_counts[status] += 1
            self.run_done += 1
            now = time.time()
            self.recent.append(now)
            cutoff = now - 120
            while self.recent and self.recent[0] < cutoff:
                self.recent.pop(0)

    def rate(self) -> float:
        """Files per second over the last two minutes: what the ETA for the
        files still queued in this run should be based on."""
        with self.lock:
            if len(self.recent) < 2:
                return self.average_rate()
            span = self.recent[-1] - self.recent[0]
            return (len(self.recent) - 1) / span if span > 0 else 0.0

    def average_rate(self) -> float:
        """Files per second over the whole run: steadier than the trailing
        window, so the right basis for projecting the rest of the corpus."""
        elapsed = time.time() - self.run_started
        return self.run_done / elapsed if elapsed > 0 else 0.0


class Batch:
    def __init__(
        self,
        source: Path,
        out_dir: Path,
        *,
        jobs: int,
        timeout: float,
        keep_csg: bool = True,
        recycle: int = 200,
        max_rss_gb: float | None = None,
        memory_budget_gb: float | None = None,
        min_free_gb: float = 3.0,
        mesh_scope: str = "minimal",
        facet_threshold: int | None = None,
        verify: bool = False,
        include_overlay: Path | None = None,
        worker_command: list[str] | None = None,
    ) -> None:
        self.verify = verify
        self.include_overlay = include_overlay.resolve() if include_overlay else None
        self.source = source.resolve()
        self.out_dir = out_dir.resolve()
        self.jobs = jobs
        self.timeout = timeout
        self.keep_csg = keep_csg
        self.recycle = recycle
        # Memory limits, in MB. Per worker: a hard kill for the task that
        # crosses it (its process tree, OpenSCAD included). Aggregate: a
        # budget for all workers together, and a floor on what the rest of
        # the machine keeps -- either one breached kills the largest worker.
        # The defaults come from physical RAM and -j: twelve workers each
        # allowed 6 GB on a 48 GB machine (the old fixed default) pushed
        # macOS into swapping until the boot volume filled and it crashed.
        total = _total_memory_mb()
        self.max_rss = (
            (max_rss_gb * 1024) if max_rss_gb else max(2048.0, 0.6 * total / jobs)
        )
        self.memory_budget = (
            (memory_budget_gb * 1024) if memory_budget_gb else 0.6 * total
        )
        self.min_free = min_free_gb * 1024
        self.mem_total_mb = 0.0  # all worker trees, updated by _watch
        self.mem_available_mb = 0.0  # system-wide, updated by _watch
        self.mesh_scope = mesh_scope
        self.facet_threshold = facet_threshold
        self.worker_command = worker_command
        self.kill_grace = KILL_GRACE
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "logs").mkdir(exist_ok=True)
        self.ledger = Ledger(self.out_dir / LEDGER_NAME)
        self.queue: list[Task] = []
        self.queue_lock = threading.Lock()
        self.stop = threading.Event()
        self.abort = threading.Event()
        self.workers = [WorkerState(i + 1) for i in range(jobs)]
        self.stats = Stats(Counter())

    # -- planning

    def output_for(self, path: str) -> Path:
        rel = Path(path).relative_to(self.source)
        return self.out_dir / rel.with_suffix(".step")

    def scan(self) -> int:
        """Discover inputs into the ledger; returns how many were seen."""
        seen = 0
        for path, st in discover(self.source):
            self.ledger.discover(str(path), st.st_size, st.st_mtime, _sha256(path))
            seen += 1
        return seen

    def plan(self, limit: int | None = None, order: str = "shuffle") -> list[Task]:
        """Turn pending ledger rows into tasks, one per distinct content."""
        pending = self.ledger.pending()
        already = self.ledger.ok_by_sha()
        by_sha: dict[str, list[str]] = {}
        tasks: list[Task] = []
        for path, sha in pending:
            if sha in already:
                # Same bytes as a finished file: link its output, no work.
                self._finish_duplicate(path, already[sha])
                continue
            by_sha.setdefault(sha, []).append(path)
        for members in by_sha.values():
            first, *rest = members
            output = self.output_for(first)
            csg = output.with_suffix(".csg") if self.keep_csg else None
            tasks.append(Task(first, output, csg, rest))
        if order == "shuffle":
            random.Random(0).shuffle(tasks)
        elif order == "size":
            tasks.sort(key=lambda t: Path(t.path).stat().st_size, reverse=True)
        else:
            tasks.sort(key=lambda t: t.path)
        return tasks[:limit] if limit is not None else tasks

    def _finish_duplicate(self, path: str, canonical: str) -> None:
        src = self.output_for(canonical)
        dst = self.output_for(path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.exists() and not dst.exists():
            try:
                os.link(src, dst)
            except OSError:
                shutil.copyfile(src, dst)
        self.ledger.record(path, CLASS_OK, duplicate_of=canonical)

    # -- worker lifecycle

    def _spawn(self, state: WorkerState) -> None:
        command = self.worker_command or [
            sys.executable,
            "-m",
            "scad123d.cli",
            "--batch",
            "--timeout",
            str(self.timeout),
            "--mesh-scope",
            self.mesh_scope,
        ]
        if self.facet_threshold is not None and not self.worker_command:
            command += ["--facet-threshold", str(self.facet_threshold)]
        # The child inherits the log descriptor; the parent's copy can close
        # right away, or a long run with recycling leaks one per spawn.
        # Each worker leads its own process group (start_new_session), so
        # killing it kills the OpenSCAD it may be running: a worker killed
        # on timeout mid-render otherwise leaves that render orphaned,
        # burning three cores until it finishes for nobody.
        with open(self.out_dir / "logs" / f"worker-{state.index}.log", "a") as log:
            state.proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        state.done = 0
        state.rss_mb = 0.0
        state.recycle_after = False
        state.kill_reason = None

    def _retire(self, state: WorkerState) -> None:
        proc = state.proc
        state.proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            _kill_group(proc)
            proc.wait()

    def _next_task(self) -> Task | None:
        with self.queue_lock:
            return self.queue.pop() if self.queue else None

    def _worker_loop(self, state: WorkerState) -> None:
        while not self.stop.is_set():
            task = self._next_task()
            if task is None:
                break
            if (
                state.proc is None
                or state.proc.poll() is not None
                or state.recycle_after
            ):
                self._retire(state)
                self._spawn(state)
            proc = state.proc
            assert (
                proc is not None and proc.stdin is not None and proc.stdout is not None
            )
            request = {
                "input": task.path,
                "output": str(task.output),
                "csg": str(task.csg) if task.csg else None,
                "verify": self.verify,
                "openscadpath": (
                    str(overlay_dir(self.include_overlay, self.source, Path(task.path)))
                    if self.include_overlay
                    else None
                ),
            }
            state.current = task
            state.started = time.time()
            try:
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            except (OSError, ValueError):
                line = ""
            state.current = None
            result = self._interpret(state, task, line)
            self._record(task, result)
            state.done += 1
            if self.recycle and state.done >= self.recycle:
                state.recycle_after = True
        self._retire(state)

    def _interpret(self, state: WorkerState, task: Task, line: str) -> dict[str, Any]:
        if line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return {
                    "status": CLASS_ERROR,
                    "message": f"unparseable result: {line[:200]}",
                }
        # EOF: the worker is gone. Either we killed it or it crashed.
        proc = state.proc
        code = proc.wait() if proc is not None else None
        state.proc = None
        if state.kill_reason == "timeout":
            return {
                "status": CLASS_TIMEOUT,
                "message": (
                    f"killed after {time.time() - state.started:.0f}s; Python stack "
                    f"at the hang is in logs/worker-{state.index}.log"
                ),
            }
        if state.kill_reason == "abort":
            return {"status": STATUS_PENDING}
        if state.kill_reason == "memory":
            return {
                "status": CLASS_MEMORY,
                "message": (
                    f"killed for memory: {state.kill_detail}. Retry with fewer "
                    "workers (-j) or a higher --max-rss-gb"
                ),
            }
        detail = f"signal {-code}" if code is not None and code < 0 else f"exit {code}"
        return {
            "status": CLASS_CRASH,
            "message": (
                f"worker died ({detail}); faulthandler stack, if any, is in "
                f"logs/worker-{state.index}.log"
            ),
        }

    def _record(self, task: Task, result: dict[str, Any]) -> None:
        status = result.get("status", CLASS_ERROR)
        if status == STATUS_PENDING:
            return  # aborted mid-file: leave it for the next run
        fields = {
            "message": result.get("message"),
            "seconds": result.get("seconds"),
            "meshed": result.get("meshed") or None,
            "traceback": result.get("traceback"),
            "warnings": result.get("openscad_warnings") or None,
            "stage": result.get("stage"),
            "volume": result.get("volume"),
            "scad_volume": result.get("scad_volume"),
        }
        self.ledger.record(task.path, status, **fields)
        self.stats.add(status)
        for sibling in task.siblings:
            if status == CLASS_OK:
                self._finish_duplicate(sibling, task.path)
            else:
                self.ledger.record(sibling, status, **fields)
            self.stats.add(status)

    def _watch(self) -> None:
        """Enforce timeouts and memory limits on running workers."""
        deadline = self.timeout + self.kill_grace
        for state in self.workers:
            proc = state.proc
            if proc is None or proc.poll() is not None:
                continue
            if state.current is not None and time.time() - state.started > deadline:
                # Ask faulthandler for the Python stack first (SIGUSR1 --
                # see cli._run_batch_mode), then kill once it has had a
                # moment to write it. Where the platform has no SIGUSR1, or
                # the worker never registered a handler, the signal or the
                # kill ends it either way.
                if state.kill_reason is None:
                    state.kill_reason = "timeout"
                    state.dump_at = time.time()
                    if hasattr(signal, "SIGUSR1"):
                        try:
                            proc.send_signal(signal.SIGUSR1)
                        except OSError:
                            pass
                        continue
                if time.time() - state.dump_at >= 2:
                    _kill_group(proc)
                continue
            state.rss_mb = _tree_rss_mb(proc.pid)
            if state.current is not None and state.rss_mb > self.max_rss:
                self._kill_for_memory(
                    state,
                    f"{state.rss_mb / 1024:.1f} GB, over the per-worker limit of "
                    f"{self.max_rss / 1024:.1f} GB",
                )
            elif state.rss_mb > self.max_rss / 2:
                # Leaked or retained memory: hand the next file to a fresh
                # process rather than carry it along.
                state.recycle_after = True
        self.mem_total_mb = sum(w.rss_mb for w in self.workers if w.proc is not None)
        self.mem_available_mb = _available_mb()
        busy = [
            w
            for w in self.workers
            if w.proc is not None and w.current is not None and w.kill_reason is None
        ]
        if not busy:
            return
        if self.mem_total_mb > self.memory_budget:
            why = (
                f"workers together at {self.mem_total_mb / 1024:.1f} GB, over the "
                f"budget of {self.memory_budget / 1024:.1f} GB"
            )
        elif self.mem_available_mb < self.min_free:
            why = (
                f"only {self.mem_available_mb / 1024:.1f} GB left for the machine "
                f"(floor {self.min_free / 1024:.1f} GB)"
            )
        else:
            return
        largest = max(busy, key=lambda w: w.rss_mb)
        self._kill_for_memory(largest, f"{largest.rss_mb / 1024:.1f} GB; {why}")

    def _kill_for_memory(self, state: WorkerState, detail: str) -> None:
        state.kill_reason = "memory"
        state.kill_detail = detail
        if state.proc is not None:
            _kill_group(state.proc)

    def _kill_all(self, reason: str) -> None:
        for state in self.workers:
            if state.proc is not None and state.proc.poll() is None:
                state.kill_reason = reason
                _kill_group(state.proc)

    # -- run

    def run(self, tasks: list[Task], dashboard: "Dashboard | None") -> Counter[str]:
        self.queue = list(reversed(tasks))  # pop() from the end
        # In files, not tasks: a task's duplicate siblings are recorded too.
        run_total = sum(1 + len(t.siblings) for t in tasks)
        self.stats = Stats(self.ledger.counts(), run_total)
        threads = [
            threading.Thread(target=self._worker_loop, args=(w,), daemon=True)
            for w in self.workers
        ]
        for t in threads:
            t.start()
        interrupts = 0

        def on_sigint(_sig: int, _frame: Any) -> None:
            nonlocal interrupts
            interrupts += 1
            self.stop.set()
            if interrupts == 1:
                with self.queue_lock:
                    self.queue.clear()
                if dashboard:
                    dashboard.note(
                        "stopping: finishing files in progress (Ctrl-C again to kill)"
                    )
            else:
                self.abort.set()
                self._kill_all("abort")

        previous = signal.signal(signal.SIGINT, on_sigint)
        try:
            while any(t.is_alive() for t in threads):
                self._watch()
                if dashboard:
                    dashboard.refresh(self)
                time.sleep(0.5)
        finally:
            signal.signal(signal.SIGINT, previous)
            for state in self.workers:
                self._retire(state)
            if dashboard:
                dashboard.refresh(self)
                dashboard.close()
        return self.ledger.counts()


# --- dashboard ---------------------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class Dashboard:
    """Live per-worker view with rich; falls back to periodic log lines."""

    def __init__(self, total: int, live: bool, console: Any = None) -> None:
        self.total = total
        self.notes: list[str] = []
        self.live = None
        self._last_log = 0.0
        if live:
            from rich.console import Console
            from rich.live import Live

            # stderr, like every other status line: stdout stays clean for
            # anything a caller pipes.
            console = console or Console(stderr=True)
            self.live = Live(console=console, refresh_per_second=4, transient=False)
            self.live.start()

    def note(self, text: str) -> None:
        self.notes.append(text)

    def _summary(self, batch: Batch) -> str:
        """Progress and ETA for *this run*, then -- only when the run does
        not cover the whole ledger (--limit, or a partial re-run) -- how
        long the rest of the corpus would take at this run's average rate.
        An ETA answers "when is my terminal free"; the projection answers
        "is a full run feasible", and the two are labeled apart."""
        stats = batch.stats
        rate = stats.rate()
        left = max(stats.run_total - stats.run_done, 0)
        eta = _fmt_duration(left / rate) if rate > 0 else "--:--:--"
        elapsed = _fmt_duration(time.time() - stats.run_started)
        failed = sum(v for k, v in stats.run_counts.items() if k != CLASS_OK)
        text = (
            f"this run {stats.run_done}/{stats.run_total}  "
            f"ok {stats.run_counts.get(CLASS_OK, 0)}  failed {failed}  |  "
            f"{rate * 60:.1f}/min  ETA {eta}  elapsed {elapsed}"
        )
        beyond = stats.counts.get(STATUS_PENDING, 0) - left
        if beyond > 0:
            average = stats.average_rate()
            projection = _fmt_duration(beyond / average) if average > 0 else "?"
            text += f"  |  corpus: {beyond} more pending, ~{projection} at this rate"
        if batch.mem_total_mb or batch.mem_available_mb:
            text += (
                f"  |  mem {batch.mem_total_mb / 1024:.1f}/{batch.memory_budget / 1024:.0f} GB, "
                f"free {batch.mem_available_mb / 1024:.1f}"
            )
        return text

    def refresh(self, batch: Batch) -> None:
        if self.live is None:
            if time.time() - self._last_log >= 10:
                self._last_log = time.time()
                print(
                    f"scad123d-batch: {self._summary(batch)}",
                    file=sys.stderr,
                    flush=True,
                )
            return
        from rich.table import Table

        table = Table(title=self._summary(batch), expand=True)
        table.add_column("#", width=3)
        table.add_column("file", ratio=1, no_wrap=True)
        table.add_column("elapsed", width=8, justify="right")
        table.add_column("done", width=6, justify="right")
        table.add_column("rss", width=8, justify="right")
        for w in batch.workers:
            current = w.current
            name = ""
            elapsed = ""
            if current is not None:
                try:
                    name = str(Path(current.path).relative_to(batch.source))
                except ValueError:
                    name = current.path
                elapsed = f"{time.time() - w.started:.1f}s"
            table.add_row(
                str(w.index),
                name if current else "[dim]idle[/]",
                elapsed,
                str(w.done),
                f"{w.rss_mb:.0f} MB" if w.rss_mb else "",
            )
        counts = batch.stats.run_counts
        breakdown = "this run:  " + "  ".join(
            f"{k} {v}" for k, v in sorted(counts.items()) if v
        )
        table.caption = breakdown + (
            "\n" + "\n".join(self.notes[-3:]) if self.notes else ""
        )
        self.live.update(table)

    def close(self) -> None:
        if self.live is not None:
            self.live.stop()


# --- report ------------------------------------------------------------------


def report(out_dir: Path) -> int:
    ledger_path = out_dir / LEDGER_NAME
    if not ledger_path.exists():
        print(f"scad123d-batch: no ledger at {ledger_path}", file=sys.stderr)
        return 1
    ledger = Ledger(ledger_path)
    counts = ledger.counts()
    total = sum(counts.values())
    print(f"{total} files in {ledger_path}")
    for status, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {status:14s} {n:7d}  {100 * n / total:5.1f}%")
    secs = ledger.query(
        "SELECT SUM(seconds), MAX(seconds) FROM files WHERE seconds IS NOT NULL"
    )[0]
    if secs[0]:
        print(f"conversion time {_fmt_duration(secs[0])} total, slowest {secs[1]:.1f}s")
    slow = ledger.query(
        "SELECT path, seconds FROM files WHERE status=? ORDER BY seconds DESC LIMIT 10",
        (CLASS_OK,),
    )
    if slow:
        print("slowest successes:")
        for path, seconds in slow:
            print(f"  {seconds:7.1f}s  {path}")
    failures = ledger.failures()
    if failures:
        print("most common failures:")
        keyed = Counter(
            (status, (message or "").splitlines()[0][:100])
            for _path, status, message, _trace in failures
        )
        for (status, head), n in keyed.most_common(15):
            print(f"  {n:6d}  {status:14s} {head}")
        sites = Counter(
            (status, failure_site(trace))
            for _path, status, _message, trace in failures
            if trace
        )
        if sites:
            print("where they fail (innermost scad123d/solid123d frame):")
            for (status, site), n in sites.most_common(15):
                print(f"  {n:6d}  {status:14s} {site}")
    ledger.close()
    return 0


_FRAME = re.compile(r'^\s*File "(.*?)", line (\d+), in (\w+)')


def failure_site(trace: str) -> str:
    """The innermost scad123d/solid123d frame of a traceback, as file:line fn.

    Grouping failures by this is how a corpus run turns into a bug list:
    one site with 400 files behind it is one bug, not 400.
    """
    site = "(no scad123d frame)"
    for line in trace.splitlines():
        match = _FRAME.match(line)
        if not match:
            continue
        file, lineno, function = match.groups()
        if {"scad123d", "solid123d"} & set(Path(file).parts):
            site = f"{Path(file).name}:{lineno} {function}"
    return site


def list_class(out_dir: Path, status: str) -> int:
    """Print every input in a result class, with its message, tab-separated."""
    ledger = Ledger(out_dir / LEDGER_NAME)
    rows = ledger.query(
        "SELECT path, message FROM files WHERE status = ? ORDER BY path", (status,)
    )
    for path, message in rows:
        head = (message or "").splitlines()[0] if message else ""
        print(f"{path}\t{head}")
    ledger.close()
    return 0 if rows else 1


def show_file(out_dir: Path, path: str) -> int:
    """Print everything the ledger holds on one input."""
    ledger = Ledger(out_dir / LEDGER_NAME)
    row = ledger.lookup(path)
    ledger.close()
    if row is None:
        print(
            f"scad123d-batch: no unique ledger entry matches {path!r}", file=sys.stderr
        )
        return 1
    (
        full,
        status,
        stage,
        message,
        seconds,
        meshed,
        warnings,
        volume,
        scad_volume,
        trace,
    ) = row
    print(f"{full}\nstatus: {status}" + (f" (in {stage})" if stage else ""))
    if seconds is not None:
        print(f"seconds: {seconds}")
    if volume is not None:
        print(f"volume: {volume}  openscad: {scad_volume}")
    if message:
        print(f"message: {message}")
    for label, blob in (("meshed", meshed), ("openscad warnings", warnings)):
        if blob:
            print(f"{label}:")
            for item in json.loads(blob):
                print(f"  {item}")
    if trace:
        print("traceback:")
        print(trace.rstrip())
    return 0


# --- CLI ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scad123d-batch",
        description="Convert every .scad file under a directory to STEP, in parallel.",
    )
    parser.add_argument("source", type=Path, nargs="?", help="directory of .scad files")
    parser.add_argument("-o", "--out-dir", type=Path, help="where STEP files go")
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=os.cpu_count() or 4,
        help="parallel workers (default: CPU count)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120,
        help="seconds allowed per file before the worker is killed (default: 120)",
    )
    parser.add_argument(
        "--retry",
        default="",
        help="comma-separated result classes to re-queue, e.g. timeout,crash",
    )
    parser.add_argument("--force", action="store_true", help="redo every file")
    parser.add_argument(
        "--limit", type=int, default=None, help="convert at most N files"
    )
    parser.add_argument(
        "--order",
        choices=["shuffle", "size", "name"],
        default="shuffle",
        help="queue order (default: shuffle, for a steady ETA)",
    )
    parser.add_argument(
        "--no-csg", action="store_true", help="don't keep .csg beside each STEP"
    )
    parser.add_argument(
        "--recycle",
        type=int,
        default=200,
        help="restart a worker after this many files (default: 200; 0 never)",
    )
    parser.add_argument(
        "--max-rss-gb",
        type=float,
        default=None,
        help="kill a file whose worker (with its OpenSCAD) exceeds this; it is "
        "recorded as 'memory' (default: 60%% of RAM divided by -j, at least 2)",
    )
    parser.add_argument(
        "--memory-budget-gb",
        type=float,
        default=None,
        help="all workers together; the largest is killed when exceeded "
        "(default: 60%% of RAM)",
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=3.0,
        help="kill the largest worker when the machine has less than this free "
        "(default: 3)",
    )
    parser.add_argument("--mesh-scope", choices=["minimal", "hoist"], default="minimal")
    parser.add_argument("--facet-threshold", type=int, default=None)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="also render each model with OpenSCAD and flag a >2%% volume "
        "disagreement as 'mismatch' (catches silently wrong output)",
    )
    parser.add_argument(
        "--include-overlay",
        type=Path,
        metavar="DIR",
        help="overlay tree of supplied includes (from scad123d-includes resolve/fetch); "
        "each model's mirror folder goes on OPENSCADPATH for that model",
    )
    parser.add_argument(
        "--skip-unresolved-includes",
        action="store_true",
        help="exclude models with an include/use that does not resolve (OpenSCAD "
        "would render them hollow with only a warning); they get status 'excluded'",
    )
    parser.add_argument("--dry-run", action="store_true", help="scan and plan only")
    parser.add_argument("--no-dashboard", action="store_true", help="plain log lines")
    parser.add_argument(
        "--report", type=Path, metavar="OUT_DIR", help="summarize a ledger"
    )
    parser.add_argument(
        "--list",
        nargs=2,
        metavar=("OUT_DIR", "CLASS"),
        help="print every input in a result class (e.g. occt-error)",
    )
    parser.add_argument(
        "--show",
        nargs=2,
        metavar=("OUT_DIR", "PATH"),
        help="print one input's result, warnings, and traceback",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.report:
        return report(args.report)
    if args.list:
        return list_class(Path(args.list[0]), args.list[1])
    if args.show:
        return show_file(Path(args.show[0]), args.show[1])
    if args.source is None or args.out_dir is None:
        parser.error("source directory and -o OUT_DIR are required")
    if not args.source.is_dir():
        parser.error(f"not a directory: {args.source}")

    batch = Batch(
        args.source,
        args.out_dir,
        jobs=args.jobs,
        timeout=args.timeout,
        keep_csg=not args.no_csg,
        recycle=args.recycle,
        max_rss_gb=args.max_rss_gb,
        memory_budget_gb=args.memory_budget_gb,
        min_free_gb=args.min_free_gb,
        mesh_scope=args.mesh_scope,
        facet_threshold=args.facet_threshold,
        verify=args.verify,
        include_overlay=args.include_overlay,
    )
    print(
        f"scad123d-batch: memory limits: {batch.max_rss / 1024:.1f} GB per worker, "
        f"{batch.memory_budget / 1024:.0f} GB for all {args.jobs}, "
        f"{batch.min_free / 1024:.0f} GB kept free",
        file=sys.stderr,
    )
    print(f"scad123d-batch: scanning {batch.source} ...", file=sys.stderr)
    seen = batch.scan()
    if args.force:
        batch.ledger.reset(set(batch.ledger.counts()) - {STATUS_PENDING})
    elif args.retry:
        n = batch.ledger.reset({c.strip() for c in args.retry.split(",") if c.strip()})
        print(f"scad123d-batch: re-queued {n} files", file=sys.stderr)
    if args.skip_unresolved_includes:
        n = exclude_unresolved(batch, apply=not args.dry_run)
        print(
            f"scad123d-batch: {'would exclude' if args.dry_run else 'excluded'} {n} models whose includes do not resolve "
            "(scad123d-includes scan shows why; --retry excluded to reconsider)",
            file=sys.stderr,
        )
    tasks = batch.plan(limit=args.limit, order=args.order)
    counts = batch.ledger.counts()
    print(
        f"scad123d-batch: {seen} inputs, {counts.get(STATUS_PENDING, 0)} pending, "
        f"{len(tasks)} to convert now with {args.jobs} workers",
        file=sys.stderr,
    )
    if args.dry_run or not tasks:
        batch.ledger.close()
        return 0

    live = not args.no_dashboard and sys.stderr.isatty()
    dashboard = Dashboard(len(tasks), live)
    try:
        final = batch.run(tasks, dashboard)
    finally:
        batch.ledger.close()
    run = batch.stats.run_counts
    failed = sum(v for k, v in run.items() if k != CLASS_OK)
    print(
        f"scad123d-batch: this run ok {run.get(CLASS_OK, 0)}  failed {failed}  "
        f"(of {batch.stats.run_total}); ledger: {final.get(CLASS_OK, 0)} ok, "
        f"{final.get(STATUS_PENDING, 0)} pending  ({batch.ledger.path})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
