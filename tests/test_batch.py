"""scad123d-batch: the ledger, discovery, dedup, and worker supervision.

Worker supervision (crash, timeout, recycling) is tested with a fake worker
script so it runs in milliseconds and without OpenSCAD; one needs_openscad
test runs the real thing over the fixture directory and resumes it.
"""

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from scad123d.batch import (
    CLASS_CRASH,
    CLASS_OK,
    CLASS_TIMEOUT,
    STATUS_PENDING,
    Batch,
    Ledger,
    discover,
    main,
)

FAKE_WORKER = textwrap.dedent(
    """
    import faulthandler, json, os, signal, sys, time
    if hasattr(signal, "SIGUSR1"):  # not on Windows
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
    for line in sys.stdin:
        task = json.loads(line)
        name = os.path.basename(task["input"])
        if name.startswith("crash"):
            os._exit(3)
        if name.startswith("slow"):
            # like a worker mid-render: a child that would outlive us
            import subprocess
            # stdio detached, as a real OpenSCAD child's is (subprocess.run
            # gives it its own pipes): otherwise the child would keep the
            # worker's stdout pipe open after the worker is killed, and the
            # harness's readline would wait on it.
            child = subprocess.Popen(
                ["sleep", "300"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            print(f"child pid {child.pid}", file=sys.stderr, flush=True)
            time.sleep(30)
        if name.startswith("hog"):
            big = bytearray(int(name.split("hog")[1].split(".")[0] or 300) << 20)
            for i in range(0, len(big), 4096):
                big[i] = 1  # touch every page so it is resident
            time.sleep(30)
        if name.startswith("bad"):
            print(json.dumps({
                "status": "openscad-error", "message": "nope", "seconds": 0.01,
                "stage": "export",
                "openscad_warnings": ["WARNING: Ignoring unknown module 'foo'"],
                "traceback": (
                    'Traceback (most recent call last):\\n'
                    '  File "/x/scad123d/cli.py", line 10, in export\\n'
                    '  File "/x/scad123d/openscad.py", line 99, in _run\\n'
                    "OpenSCADRunError: nope\\n"
                ),
            }))
        else:
            open(task["output"], "w").write("step")
            print(json.dumps({"status": "ok", "seconds": 0.01, "meshed": []}))
        sys.stdout.flush()
    """
)


def _tree(root: Path, names: list[str], content: str = "cube(1);") -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


@pytest.fixture
def fake_batch(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text(FAKE_WORKER)

    def make(names: list[str], **kwargs) -> Batch:  # type: ignore[no-untyped-def]
        src = tmp_path / "src"
        _tree(src, names)
        batch = Batch(
            src,
            tmp_path / "out",
            jobs=kwargs.pop("jobs", 2),
            timeout=kwargs.pop("timeout", 0.5),
            worker_command=[sys.executable, str(script)],
            **kwargs,
        )
        batch.kill_grace = 0.2
        batch.scan()
        return batch

    return make


# --- ledger and discovery ----------------------------------------------------


def test_discover_skips_hidden_dirs_empty_files_and_other_suffixes(tmp_path):
    _tree(tmp_path, ["a.scad", "sub/b.SCAD", ".git/c.scad", "d.txt"])
    (tmp_path / "empty.scad").write_text("")
    found = sorted(p.name for p, _ in discover(tmp_path))
    assert found == ["a.scad", "b.SCAD"]


def test_ledger_tracks_status_and_reverts_changed_files(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    ledger.discover("/x/a.scad", 10, 1.0, "sha-a")
    assert ledger.pending() == [("/x/a.scad", "sha-a", False)]
    ledger.record("/x/a.scad", CLASS_OK, seconds=0.5)
    assert ledger.pending() == []
    assert ledger.counts() == {CLASS_OK: 1}
    # unchanged: stays done
    ledger.discover("/x/a.scad", 10, 1.0, "sha-a")
    assert ledger.pending() == []
    # edited: back to pending
    ledger.discover("/x/a.scad", 12, 2.0, "sha-a2")
    assert ledger.pending() == [("/x/a.scad", "sha-a2", True)]  # tried before


def test_ledger_reset_requeues_only_the_named_classes(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    for i, status in enumerate([CLASS_OK, CLASS_TIMEOUT, CLASS_CRASH]):
        ledger.discover(f"/x/{i}.scad", 1, 1.0, f"sha{i}")
        ledger.record(f"/x/{i}.scad", status)
    assert ledger.reset({CLASS_TIMEOUT}) == 1
    assert ledger.counts() == {CLASS_OK: 1, STATUS_PENDING: 1, CLASS_CRASH: 1}


# --- planning ----------------------------------------------------------------


def test_plan_queues_one_task_per_distinct_content(fake_batch):
    batch = fake_batch(["a.scad", "b.scad", "sub/c.scad"])
    tasks = batch.plan(order="name")
    assert len(tasks) == 1  # all three files are byte-identical
    assert sorted(tasks[0].siblings) == sorted(
        str(batch.source / n) for n in ("b.scad", "sub/c.scad")
    )
    assert tasks[0].output == batch.out_dir / "a.step"
    assert tasks[0].csg == batch.out_dir / "a.csg"


def test_plan_links_duplicates_of_already_converted_files(fake_batch):
    batch = fake_batch(["a.scad"])
    batch.run(batch.plan(), dashboard=None)
    assert (batch.out_dir / "a.step").exists()
    _tree(batch.source, ["later.scad"])
    batch.scan()
    assert batch.plan() == []  # nothing to convert: same bytes as a.scad
    assert (batch.out_dir / "later.step").read_text() == "step"
    assert batch.ledger.query(
        "SELECT status, duplicate_of FROM files WHERE path LIKE '%later.scad'"
    ) == [(CLASS_OK, str(batch.source / "a.scad"))]


# --- supervision -------------------------------------------------------------


def test_results_land_in_the_ledger_and_siblings_share_them(fake_batch):
    batch = fake_batch(["a.scad", "b.scad"])
    _tree(batch.source, ["bad.scad"], content="nonsense")
    batch.scan()
    counts = batch.run(batch.plan(), dashboard=None)
    assert counts == {CLASS_OK: 2, "openscad-error": 1}
    assert (batch.out_dir / "b.step").exists()  # linked from a's output


def test_crashed_worker_is_recorded_and_replaced(fake_batch):
    batch = fake_batch(["crash.scad"])
    _tree(batch.source, ["fine.scad"], content="sphere(2);")
    batch.scan()
    counts = batch.run(batch.plan(order="name"), dashboard=None)
    assert counts == {CLASS_CRASH: 1, CLASS_OK: 1}
    (message,) = batch.ledger.query(
        "SELECT message FROM files WHERE status=?", (CLASS_CRASH,)
    )[0]
    assert "exit 3" in message


def test_hung_worker_is_killed_on_timeout(fake_batch):
    batch = fake_batch(["slow.scad"])
    start = time.time()
    counts = batch.run(batch.plan(), dashboard=None)
    assert counts == {CLASS_TIMEOUT: 1}
    assert time.time() - start < 10  # not the fake worker's 30s sleep


def test_workers_are_recycled_after_n_files(fake_batch):
    batch = fake_batch([f"f{i}.scad" for i in range(6)], jobs=1, recycle=2)
    for i in range(6):
        (batch.source / f"f{i}.scad").write_text(f"cube({i});")
    batch.scan()
    counts = batch.run(batch.plan(), dashboard=None)
    assert counts == {CLASS_OK: 6}
    # a recycled worker's done-count restarts, so it never exceeds `recycle`
    assert batch.workers[0].done <= 2


# --- CLI ---------------------------------------------------------------------


def test_report_summarizes_a_ledger(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    ledger = Ledger(out / "ledger.sqlite")
    ledger.discover("/x/a.scad", 1, 1.0, "a")
    ledger.record("/x/a.scad", CLASS_OK, seconds=1.5)
    ledger.discover("/x/b.scad", 1, 1.0, "b")
    ledger.record("/x/b.scad", CLASS_TIMEOUT, message="killed after 120s")
    ledger.close()
    assert main(["--report", str(out)]) == 0
    text = capsys.readouterr().out
    assert "2 files" in text
    assert "timeout" in text and "killed after 120s" in text


@pytest.mark.needs_openscad
def test_real_run_over_fixtures_then_resume(tmp_path):
    fixtures = Path(__file__).parent / "fixtures" / "scad"
    out = tmp_path / "out"
    assert (
        main(
            [str(fixtures), "-o", str(out), "-j", "2", "--no-dashboard", "--limit", "3"]
        )
        == 0
    )
    ledger = Ledger(out / "ledger.sqlite")
    counts = ledger.counts()
    assert counts[CLASS_OK] == 3
    remaining = counts[STATUS_PENDING]
    ledger.close()
    assert remaining == len(list(fixtures.glob("*.scad"))) - 3
    steps = list(out.glob("*.step"))
    assert len(steps) == 3 and all(s.stat().st_size > 0 for s in steps)
    assert len(list(out.glob("*.csg"))) == 3
    # Resume: only the remainder is converted.
    assert main([str(fixtures), "-o", str(out), "-j", "2", "--no-dashboard"]) == 0
    ledger = Ledger(out / "ledger.sqlite")
    assert ledger.counts() == {CLASS_OK: remaining + 3}
    ledger.close()


def test_dry_run_plans_without_converting(tmp_path, capsys):
    _tree(tmp_path / "src", ["a.scad"])
    assert main([str(tmp_path / "src"), "-o", str(tmp_path / "out"), "--dry-run"]) == 0
    assert "1 to convert" in capsys.readouterr().err
    assert not list((tmp_path / "out").glob("*.step"))
    assert json.loads(json.dumps(os.listdir(tmp_path / "out")))  # ledger + logs exist


def test_dashboard_renders_worker_rows_and_totals(fake_batch):
    import io

    from rich.console import Console

    from scad123d.batch import Dashboard

    batch = fake_batch(["a.scad"])
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=120)
    dashboard = Dashboard(1, live=True, console=console)
    dashboard.note("hello from the test")
    counts = batch.run(batch.plan(), dashboard)
    assert counts == {CLASS_OK: 1}
    text = buffer.getvalue()
    assert "this run 1/1" in text and "ok 1" in text
    assert "corpus:" not in text  # the run covers the whole ledger
    assert "idle" in text  # workers shown even when between files
    assert "hello from the test" in text


# --- diagnostics -------------------------------------------------------------


def test_failure_details_are_kept_and_shown(fake_batch, capsys):
    batch = fake_batch(["bad.scad"])
    batch.run(batch.plan(), dashboard=None)
    row = batch.ledger.lookup("bad.scad")
    assert row is not None
    _path, status, stage, message, *_rest, warnings, _v, _sv, trace = row
    assert (status, stage, message) == ("openscad-error", "export", "nope")
    assert json.loads(warnings) == ["WARNING: Ignoring unknown module 'foo'"]
    assert "openscad.py" in trace

    assert main(["--show", str(batch.out_dir), "bad.scad"]) == 0
    text = capsys.readouterr().out
    assert "status: openscad-error (in export)" in text
    assert "unknown module 'foo'" in text and 'File "/x/scad123d/openscad.py"' in text

    assert main(["--list", str(batch.out_dir), "openscad-error"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.endswith("bad.scad\tnope")
    assert main(["--list", str(batch.out_dir), "timeout"]) == 1  # nothing in it


def test_report_groups_failures_by_innermost_frame(fake_batch, capsys):
    batch = fake_batch(["bad.scad"])
    _tree(batch.source, ["bad2.scad"], content="also bad")
    batch.scan()
    batch.run(batch.plan(), dashboard=None)
    assert main(["--report", str(batch.out_dir)]) == 0
    text = capsys.readouterr().out
    assert "where they fail" in text
    assert "2  openscad-error openscad.py:99 _run" in text


@pytest.mark.skipif(
    not hasattr(__import__("signal"), "SIGUSR1"), reason="no SIGUSR1 on Windows"
)
def test_timeout_asks_the_worker_for_a_stack_dump_before_killing(fake_batch):
    batch = fake_batch(["slow.scad"])
    counts = batch.run(batch.plan(), dashboard=None)
    assert counts == {CLASS_TIMEOUT: 1}
    (message,) = batch.ledger.query("SELECT message FROM files")[0]
    assert "worker-1.log" in message
    log = (batch.out_dir / "logs" / "worker-1.log").read_text()
    # faulthandler's dump names the sleeping frame
    assert "Current thread" in log and "worker.py" in log


def test_verify_flag_reaches_the_worker(fake_batch, tmp_path):
    echo = tmp_path / "echo.py"
    echo.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    t = json.loads(line); open(t['output'], 'w').write('x')\n"
        "    print(json.dumps({'status': 'ok', 'message': json.dumps(t)})); sys.stdout.flush()\n"
    )
    batch = fake_batch(["a.scad"], verify=True)
    batch.worker_command = [sys.executable, str(echo)]
    batch.run(batch.plan(), dashboard=None)
    (message,) = batch.ledger.query("SELECT message FROM files")[0]
    assert json.loads(message)["verify"] is True


def test_summary_is_scoped_to_the_run_with_a_corpus_projection(fake_batch):
    from scad123d.batch import Dashboard

    batch = fake_batch([f"f{i}.scad" for i in range(4)])
    for i in range(4):
        (batch.source / f"f{i}.scad").write_text(f"cube({i});")  # distinct
    batch.scan()
    tasks = batch.plan(order="name", limit=1)
    batch.run(tasks, dashboard=None)
    text = Dashboard(1, live=False)._summary(batch)
    assert text.startswith("this run 1/1  ok 1  failed 0")
    assert "corpus: 3 more pending" in text and "at this rate" in text
    assert "1/4" not in text  # the ledger total is never presented as the run


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_killing_a_hung_worker_also_kills_its_child_process(fake_batch):
    import re
    import signal

    batch = fake_batch(["slow.scad"])
    counts = batch.run(batch.plan(), dashboard=None)
    assert counts == {CLASS_TIMEOUT: 1}
    log = (batch.out_dir / "logs" / "worker-1.log").read_text()
    child = int(re.search(r"child pid (\d+)", log).group(1))
    time.sleep(0.5)
    alive = True
    try:
        os.kill(child, 0)
    except ProcessLookupError:
        alive = False
    if alive:  # zombie or genuinely running? a running sleep answers signal 0
        os.kill(child, signal.SIGKILL)
    assert not alive, "the worker's child survived the worker's kill"


def test_include_overlay_reaches_the_worker_as_openscadpath(fake_batch, tmp_path):
    echo = tmp_path / "echo.py"
    echo.write_text(
        "import json, os, sys\n"
        "for line in sys.stdin:\n"
        "    t = json.loads(line); os.makedirs(os.path.dirname(t['output']), exist_ok=True)\n"
        "    open(t['output'], 'w').write('x')\n"
        "    print(json.dumps({'status': 'ok', 'message': json.dumps(t)})); sys.stdout.flush()\n"
    )
    batch = fake_batch(["0100_0/body.scad"], include_overlay=tmp_path / "overlay")
    batch.worker_command = [sys.executable, str(echo)]
    batch.run(batch.plan(), dashboard=None)
    (message,) = batch.ledger.query("SELECT message FROM files")[0]
    assert json.loads(message)["openscadpath"] == str(
        (tmp_path / "overlay" / "0100_0").resolve()
    )


def test_skip_unresolved_includes_excludes_and_retry_restores(tmp_path, capsys):
    from scad123d.batch import STATUS_EXCLUDED

    src = tmp_path / "src"
    _tree(src, ["0100_0/whole.scad"], content="cube(1);")
    _tree(src, ["0200_0/hollow.scad"], content="use <gone.scad>\ncube(1);")
    out = tmp_path / "out"
    assert (
        main([str(src), "-o", str(out), "--skip-unresolved-includes", "--dry-run"]) == 0
    )
    err = capsys.readouterr().err
    assert (
        "would exclude 1 models" in err and "2 to convert" in err
    )  # dry run: nothing written
    assert (
        main([str(src), "-o", str(out), "--skip-unresolved-includes", "--limit", "0"])
        == 0
    )
    capsys.readouterr()
    ledger = Ledger(out / "ledger.sqlite")
    rows = dict(ledger.query("SELECT path, status FROM files"))
    assert rows[str(src / "0200_0" / "hollow.scad")] == STATUS_EXCLUDED
    assert rows[str(src / "0100_0" / "whole.scad")] == STATUS_PENDING
    (message,) = ledger.query(
        "SELECT message FROM files WHERE status=?", (STATUS_EXCLUDED,)
    )[0]
    assert message == "unresolved include: gone.scad"
    # an overlay that supplies the file un-excludes it on the next pass
    ledger.close()
    overlay = tmp_path / "overlay" / "0200_0"
    overlay.mkdir(parents=True)
    (overlay / "gone.scad").write_text("")
    assert (
        main(
            [
                str(src),
                "-o",
                str(out),
                "--skip-unresolved-includes",
                "--retry",
                "excluded",
                "--include-overlay",
                str(tmp_path / "overlay"),
                "--dry-run",
            ]
        )
        == 0
    )
    assert "would exclude 0 models" in capsys.readouterr().err


def test_task_exceeding_the_per_worker_limit_is_killed_as_memory(fake_batch):
    batch = fake_batch(["hog300.scad"], timeout=20, max_rss_gb=0.15)
    start = time.time()
    counts = batch.run(batch.plan(), dashboard=None)
    assert counts == {"memory": 1}
    assert time.time() - start < 15  # not the fake worker's 30 s sleep
    (message,) = batch.ledger.query("SELECT message FROM files")[0]
    assert "over the per-worker limit" in message and "GB" in message


def test_aggregate_budget_kills_the_largest_worker(fake_batch):
    batch = fake_batch(
        ["hog250.scad", "hog120.scad"], timeout=20, max_rss_gb=2, memory_budget_gb=0.3
    )
    # distinct contents, or the sha dedup makes them one task with a sibling
    (batch.source / "hog250.scad").write_text("cube(250);")
    (batch.source / "hog120.scad").write_text("cube(120);")
    batch.scan()
    counts = batch.run(batch.plan(order="name"), dashboard=None)
    assert counts["memory"] >= 1
    rows = dict(batch.ledger.query("SELECT path, status FROM files"))
    assert rows[str(batch.source / "hog250.scad")] == "memory"  # the largest went first
    (message,) = batch.ledger.query(
        "SELECT message FROM files WHERE path LIKE '%hog250%'"
    )[0]
    assert "over the budget" in message


def test_memory_defaults_derive_from_ram_and_jobs(tmp_path):
    from scad123d.batch import _total_memory_mb

    b = Batch(tmp_path, tmp_path / "out", jobs=4, timeout=1)
    total = _total_memory_mb()
    assert b.max_rss == pytest.approx(max(2048, 0.6 * total / 4))
    assert b.memory_budget == pytest.approx(0.6 * total)
    assert b.min_free == 3 * 1024


def test_requeued_files_come_before_never_seen_ones(fake_batch):
    # `--retry X --limit N` must redo the X files, not N random pending ones.
    batch = fake_batch(["bad.scad"])
    _tree(batch.source, [f"fresh{i}.scad" for i in range(5)], content="sphere(1);")
    for i in range(5):
        (batch.source / f"fresh{i}.scad").write_text(f"sphere({i + 1});")
    batch.scan()
    batch.run(
        batch.plan(order="name", limit=1), dashboard=None
    )  # converts bad.scad -> fails
    assert batch.ledger.counts()["openscad-error"] == 1
    assert batch.ledger.reset({"openscad-error"}) == 1
    tasks = batch.plan(order="shuffle", limit=2)
    assert tasks[0].path.endswith("bad.scad") and tasks[0].retry
    assert not tasks[1].retry
