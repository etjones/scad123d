"""scad123d-artifacts: source placement, STL rendering bookkeeping, and
duplicate linking, driven by a fake renderer so no OpenSCAD is needed; one
needs_openscad test renders a real cube."""

import struct
import subprocess
from pathlib import Path

import pytest

from scad123d.artifacts import (
    ArtifactLedger,
    ArtifactPass,
    EmptyModel,
    check_source,
    classify_stl,
    infer_source,
    main,
    place_source,
    render_stl,
)
from scad123d.batch import LEDGER_NAME, STATUS_EXCLUDED, Batch
from scad123d.cli import (
    CLASS_EMPTY,
    CLASS_ERROR,
    CLASS_OK,
    CLASS_OPENSCAD,
    CLASS_TIMEOUT,
)


def binary_stl(triangles: int) -> bytes:
    return b"\0" * 80 + struct.pack("<I", triangles) + b"\0" * 50 * triangles


def fake_renderer(
    scad: Path, out: Path, timeout: float, openscadpath: str | None
) -> None:
    """Behaves by file name: ``bad*`` fails, ``slow*`` times out, ``hollow*``
    has nothing to export, anything else renders one triangle. Records the
    OPENSCADPATH it was given beside the output for the overlay test."""
    name = scad.name
    if name.startswith("bad"):
        raise RuntimeError("OpenSCAD exited 1: ERROR: Parser error")
    if name.startswith("slow"):
        raise subprocess.TimeoutExpired(["openscad"], timeout)
    if name.startswith("hollow"):
        raise EmptyModel("Current top level object is empty.")
    out.write_bytes(binary_stl(1))
    out.with_suffix(".path").write_text(openscadpath or "", encoding="utf-8")


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, Path]:
    """A source tree and a batch ledger over it, as scad123d-batch leaves it:
    ``a`` ok, ``dup`` a byte-identical duplicate of ``a``, ``bad`` and
    ``slow`` and ``hollow`` ok as far as STEP goes, ``b`` errored, ``lib``
    excluded."""
    source = tmp_path / "src"
    (source / "thing").mkdir(parents=True)
    for name, text in {
        "a": "cube(1);",
        "dup": "cube(1);",
        "b": "sphere(1);",
        "bad": "cube(2);",
        "slow": "cube(3);",
        "hollow": "cube(4);",
        "lib": "module m() {}",
    }.items():
        (source / "thing" / f"{name}.scad").write_text(text, encoding="utf-8")
    out = tmp_path / "out"
    batch = Batch(source, out, jobs=1, timeout=1)
    batch.scan()
    for name in ("a", "bad", "slow", "hollow"):
        batch.ledger.record(str(source / "thing" / f"{name}.scad"), CLASS_OK)
    batch.ledger.record(
        str(source / "thing" / "dup.scad"),
        CLASS_OK,
        duplicate_of=str(source / "thing" / "a.scad"),
    )
    batch.ledger.record(str(source / "thing" / "b.scad"), CLASS_ERROR, "boom")
    batch.ledger.record(str(source / "thing" / "lib.scad"), STATUS_EXCLUDED)
    batch.ledger.close()
    (out / "thing").mkdir()
    (out / "thing" / "a.step").write_text("ISO-10303-21;", encoding="utf-8")
    return source, out


def run_pass(source: Path, out: Path, **kwargs) -> ArtifactPass:
    p = ArtifactPass(source, out, jobs=2, timeout=5, renderer=fake_renderer, **kwargs)
    p.run(*p.plan())
    return p


def test_places_source_and_stl_beside_step_for_every_non_excluded_input(corpus):
    source, out = corpus
    p = run_pass(source, out, statuses={CLASS_OK, CLASS_ERROR})
    thing = out / "thing"
    for name in ("a", "dup", "b", "bad", "slow", "hollow"):
        assert (thing / f"{name}.scad").read_text(encoding="utf-8") == (
            source / "thing" / f"{name}.scad"
        ).read_text(encoding="utf-8")
        assert not (thing / f"{name}.scad").is_symlink()
    assert not (thing / "lib.scad").exists()
    assert (thing / "a.stl").exists() and (thing / "b.stl").exists()
    assert not (thing / "bad.stl").exists()
    assert not (thing / "hollow.stl").exists()
    counts = p.ledger.counts()
    assert counts == {
        CLASS_OK: 3,  # a, dup, b
        CLASS_EMPTY: 1,  # hollow
        CLASS_OPENSCAD: 1,
        CLASS_TIMEOUT: 1,
    }
    failures = {
        Path(path).name: (status, message)
        for path, status, message in p.ledger.failures()
    }
    assert failures["bad.scad"][1].startswith("OpenSCAD exited 1")
    assert failures["slow.scad"] == (CLASS_TIMEOUT, "no STL after 5s")
    p.ledger.close()


def test_duplicates_share_the_original_stl_instead_of_rendering(corpus):
    source, out = corpus
    p = run_pass(source, out, statuses={CLASS_OK})
    a, dup = out / "thing" / "a.stl", out / "thing" / "dup.stl"
    assert dup.stat().st_ino == a.stat().st_ino  # hard link
    assert not (out / "thing" / "dup.path").exists()  # the renderer never saw it
    assert p.ledger.stl_status(str(source / "thing" / "dup.scad")) == CLASS_OK
    p.ledger.close()


def test_rerun_skips_finished_stls_and_retries_failures(corpus):
    source, out = corpus
    p = run_pass(source, out, statuses={CLASS_OK})
    stamp = (out / "thing" / "a.stl").stat().st_mtime_ns
    (out / "thing" / "a.path").unlink()
    p.ledger.close()

    p = run_pass(source, out, statuses={CLASS_OK})
    assert (out / "thing" / "a.stl").stat().st_mtime_ns == stamp
    assert not (out / "thing" / "a.path").exists()  # not re-rendered
    # retried: the two failures, and hollow (no file, so nothing to skip)
    assert p.progress.counts == {CLASS_OPENSCAD: 1, CLASS_TIMEOUT: 1, CLASS_EMPTY: 1}
    assert p.progress.skipped == 2  # a, dup
    p.ledger.close()

    p = run_pass(source, out, statuses={CLASS_OK}, force=True)
    assert (out / "thing" / "a.path").exists()
    assert p.progress.skipped == 0
    p.ledger.close()


def test_symlink_option_links_the_source(corpus):
    source, out = corpus
    p = run_pass(source, out, statuses={CLASS_OK}, symlink=True, no_stl=True)
    link = out / "thing" / "a.scad"
    # Compare resolved targets, not the raw link text: on Windows
    # readlink() returns the extended-length form (//?/C:/...) where
    # resolve() does not, though both name the same file.
    assert link.is_symlink()
    assert link.resolve() == (source / "thing" / "a.scad").resolve()
    assert not (out / "thing" / "a.stl").exists()
    assert p.ledger.counts() == {}
    p.ledger.close()
    # a later copy run replaces the link with a real file, and vice versa
    place_source(source / "thing" / "a.scad", link, symlink=False)
    assert not link.is_symlink() and link.read_text(encoding="utf-8") == "cube(1);"
    place_source(source / "thing" / "a.scad", link, symlink=True)
    assert link.is_symlink()


def test_include_overlay_reaches_the_renderer_as_openscadpath(corpus, tmp_path):
    source, out = corpus
    overlay = tmp_path / "overlay"
    p = run_pass(source, out, statuses={CLASS_OK}, include_overlay=overlay)
    assert (out / "thing" / "a.path").read_text(encoding="utf-8") == str(
        overlay / "thing"
    )
    p.ledger.close()


def test_classify_stl_sees_empty_binary_and_ascii_meshes(tmp_path):
    f = tmp_path / "m.stl"
    f.write_bytes(binary_stl(0))
    assert classify_stl(f) == CLASS_EMPTY
    f.write_bytes(binary_stl(2))
    assert classify_stl(f) == CLASS_OK
    f.write_text("solid OpenSCAD_Model\nendsolid OpenSCAD_Model\n", encoding="utf-8")
    assert classify_stl(f) == CLASS_EMPTY
    f.write_text(
        "solid x\n facet normal 0 0 1\n endfacet\nendsolid x\n", encoding="utf-8"
    )
    assert classify_stl(f) == CLASS_OK


def test_cli_defaults_to_every_class_but_excluded_and_reports(
    corpus, capsys, monkeypatch
):
    source, out = corpus
    monkeypatch.setattr("scad123d.artifacts.render_stl", fake_renderer)
    monkeypatch.setattr("scad123d.artifacts.require_openscad", lambda: Path("openscad"))
    assert main([str(source), "-o", str(out), "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "6 inputs of ['error', 'ok'] classes, 1 duplicates, 6 STLs to render" in err

    assert main([str(source), "-o", str(out), "-j", "2"]) == 0
    assert main(["--report", str(out)]) == 0
    text = capsys.readouterr().out
    assert "6 STL renders recorded" in text
    assert "ERROR: Parser error" in text
    assert "timeout" in text


def test_limit_plans_only_that_many_unfinished_inputs(corpus, capsys):
    source, out = corpus
    p = ArtifactPass(source, out, jobs=2, timeout=5, renderer=fake_renderer)
    p.statuses = {CLASS_OK}
    originals, duplicates = p.plan(limit=2)
    assert [Path(j.path).name for j in originals + duplicates] == ["a.scad", "bad.scad"]
    p.run(originals, duplicates)
    assert p.progress.total == 2 and p.progress.done == 2
    assert "2/2" in capsys.readouterr().err
    # the next slice skips what is finished and reaches the duplicate wave
    originals, duplicates = p.plan(limit=4)
    names = [Path(j.path).name for j in originals + duplicates]
    assert names == ["bad.scad", "hollow.scad", "slow.scad", "dup.scad"]
    p.ledger.close()


def test_wrong_source_root_is_refused_and_the_right_one_inferred(corpus, capsys):
    source, out = corpus
    ledger = ArtifactLedger(out / LEDGER_NAME)
    assert infer_source(ledger, out) == source.resolve()
    check_source(ledger, source.resolve(), out)  # the root the batch used
    with pytest.raises(ValueError, match="sit under source root"):
        check_source(ledger, (source / "thing").resolve(), out)
    ledger.close()
    with pytest.raises(SystemExit):
        main([str(source / "thing"), "-o", str(out), "--no-stl", "--dry-run"])
    assert "sit under source root" in capsys.readouterr().err
    assert main(["-o", str(out), "--no-stl", "--dry-run"]) == 0
    assert f"source root {source.resolve()}" in capsys.readouterr().err


def test_missing_ledger_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        ArtifactLedger(tmp_path / LEDGER_NAME)


@pytest.mark.needs_openscad
def test_real_openscad_renders_a_binary_stl(tmp_path):
    scad = tmp_path / "cube.scad"
    scad.write_text("cube(10);", encoding="utf-8")
    out = tmp_path / "cube.stl"
    render_stl(scad, out, timeout=120, openscadpath=None)
    assert classify_stl(out) == CLASS_OK
    assert not out.with_name("cube.stl.part").exists()
    assert out.stat().st_size == 84 + 50 * 12  # a cube is 12 triangles

    scad.write_text("module m() {}", encoding="utf-8")
    with pytest.raises(EmptyModel):
        render_stl(scad, out, timeout=120, openscadpath=None)


class TestWorthLookingAt:
    """The STL beside a STEP is there to be compared by eye, so what
    matters is whether it represents the model, not whether it is
    flawless."""

    @staticmethod
    def write_stl(path, triangles):
        import struct

        with open(path, "wb") as fh:
            fh.write(b"\0" * 80 + struct.pack("<I", len(triangles)))
            for tri in triangles:
                flat = [c for corner in tri for c in corner]
                fh.write(struct.pack("<12fH", 0, 0, 0, *flat, 0))

    @staticmethod
    def tetrahedron(scale=1.0):
        a, b, c, d = (0, 0, 0), (scale, 0, 0), (0, scale, 0), (0, 0, scale)
        return [(a, c, b), (a, b, d), (a, d, c), (b, c, d)]

    def test_a_closed_positive_mesh_is_worth_looking_at(self, tmp_path):
        from scad123d.artifacts import worth_looking_at

        path = tmp_path / "t.stl"
        self.write_stl(path, self.tetrahedron())
        assert worth_looking_at(path) == (True, "")

    def test_an_open_surface_is_not(self, tmp_path):
        from scad123d.artifacts import worth_looking_at

        path = tmp_path / "open.stl"
        self.write_stl(path, self.tetrahedron()[:3])  # a face missing
        ok, why = worth_looking_at(path)
        assert not ok and "open surface" in why

    def test_an_inside_out_mesh_is_not(self, tmp_path):
        from scad123d.artifacts import worth_looking_at

        path = tmp_path / "flipped.stl"
        self.write_stl(path, [tuple(reversed(t)) for t in self.tetrahedron()])
        ok, why = worth_looking_at(path)
        assert not ok and "negative volume" in why

    def test_a_few_touching_edges_are_fine(self, tmp_path):
        """Two solids meeting along an edge is ordinary geometry, and it
        exports as edges shared by four triangles. That must not condemn
        an otherwise good render."""
        from scad123d.artifacts import worth_looking_at

        big = self.tetrahedron(scale=40.0)
        touching = [
            tuple((x, y, -z) for x, y, z in reversed(tri)) for tri in self.tetrahedron()
        ]
        path = tmp_path / "touch.stl"
        self.write_stl(path, big + touching)
        assert worth_looking_at(path)[0], worth_looking_at(path)[1]

    def test_a_shredded_mesh_is_not(self, tmp_path):
        from scad123d.artifacts import worth_looking_at

        # every triangle laid on the same three corners: all edges shared
        path = tmp_path / "shredded.stl"
        self.write_stl(path, self.tetrahedron() * 4)  # every edge, four times over
        ok, why = worth_looking_at(path)
        assert not ok
        assert "self-intersecting" in why or "which way is out" in why

    def test_an_unreadable_file_is_not(self, tmp_path):
        from scad123d.artifacts import worth_looking_at

        path = tmp_path / "junk.stl"
        path.write_bytes(b"not an stl")
        ok, why = worth_looking_at(path)
        assert not ok and "could not be read" in why


class TestExactFallback:
    """When the fast kernel shreds a model, re-render with the exact one.

    Manifold is the default because it is two orders of magnitude faster,
    but on self-intersecting input the two kernels disagree: a corpus
    bevel gear came back as 165,211 from Manifold where CGAL's exact
    arithmetic says 703,784.
    """

    @staticmethod
    def fake_pass(tmp_path, renders):
        """A pass whose renderer writes whatever *renders* says for the
        backend it is given."""
        from scad123d.artifacts import EXACT_BACKEND

        calls = []

        def renderer(scad, out, timeout, openscadpath, backend=None):
            calls.append(backend)
            body = renders[backend]
            if body is None:
                raise RuntimeError("render failed")
            TestWorthLookingAt.write_stl(out, body)

        return renderer, calls, EXACT_BACKEND

    def run(self, tmp_path, renders):
        from scad123d.artifacts import ArtifactPass, Job

        renderer, calls, _exact = self.fake_pass(tmp_path, renders)
        job = Job(str(tmp_path / "m.scad"), tmp_path / "m.stl", None)
        (tmp_path / "m.scad").write_text("cube(1);", encoding="utf-8")
        run = ArtifactPass.__new__(ArtifactPass)
        run.renderer = renderer
        run.timeout = 30
        run.exact_fallback = True
        run.include_overlay = None
        run.source = tmp_path
        status, message = run._render(job)
        return status, message, calls, job.stl

    def test_a_good_fast_render_is_kept_and_the_exact_one_never_runs(self, tmp_path):
        good = TestWorthLookingAt.tetrahedron()
        _status, message, calls, _ = self.run(tmp_path, {None: good})
        assert calls == [None] and message is None

    def test_a_shredded_render_is_replaced_by_the_exact_one(self, tmp_path):
        shredded = TestWorthLookingAt.tetrahedron() * 4
        good = TestWorthLookingAt.tetrahedron(scale=2.0)
        _status, message, calls, stl = self.run(
            tmp_path, {None: shredded, "CGAL": good}
        )
        assert calls == [None, "CGAL"]
        assert "rendered with CGAL" in message
        from scad123d.artifacts import worth_looking_at

        assert worth_looking_at(stl)[0]

    def test_a_failed_exact_render_leaves_the_fast_one(self, tmp_path):
        shredded = TestWorthLookingAt.tetrahedron() * 4
        _status, message, calls, stl = self.run(
            tmp_path, {None: shredded, "CGAL": None}
        )
        assert calls == [None, "CGAL"]
        assert "render failed" in message and stl.exists()

    def test_two_bad_renders_say_so(self, tmp_path):
        shredded = TestWorthLookingAt.tetrahedron() * 4
        _status, message, _calls, _ = self.run(
            tmp_path, {None: shredded, "CGAL": shredded}
        )
        assert "both renders unusable" in message
