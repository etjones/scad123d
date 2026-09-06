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
    classify_stl,
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
    out.with_suffix(".path").write_text(openscadpath or "")


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
        (source / "thing" / f"{name}.scad").write_text(text)
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
        assert (thing / f"{name}.scad").read_text() == (
            source / "thing" / f"{name}.scad"
        ).read_text()
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
    assert (
        link.is_symlink() and link.readlink() == (source / "thing" / "a.scad").resolve()
    )
    assert not (out / "thing" / "a.stl").exists()
    assert p.ledger.counts() == {}
    p.ledger.close()
    # a later copy run replaces the link with a real file, and vice versa
    place_source(source / "thing" / "a.scad", link, symlink=False)
    assert not link.is_symlink() and link.read_text() == "cube(1);"
    place_source(source / "thing" / "a.scad", link, symlink=True)
    assert link.is_symlink()


def test_include_overlay_reaches_the_renderer_as_openscadpath(corpus, tmp_path):
    source, out = corpus
    overlay = tmp_path / "overlay"
    p = run_pass(source, out, statuses={CLASS_OK}, include_overlay=overlay)
    assert (out / "thing" / "a.path").read_text() == str(overlay / "thing")
    p.ledger.close()


def test_classify_stl_sees_empty_binary_and_ascii_meshes(tmp_path):
    f = tmp_path / "m.stl"
    f.write_bytes(binary_stl(0))
    assert classify_stl(f) == CLASS_EMPTY
    f.write_bytes(binary_stl(2))
    assert classify_stl(f) == CLASS_OK
    f.write_text("solid OpenSCAD_Model\nendsolid OpenSCAD_Model\n")
    assert classify_stl(f) == CLASS_EMPTY
    f.write_text("solid x\n facet normal 0 0 1\n endfacet\nendsolid x\n")
    assert classify_stl(f) == CLASS_OK


def test_cli_defaults_to_every_class_but_excluded_and_reports(
    corpus, capsys, monkeypatch
):
    source, out = corpus
    monkeypatch.setattr("scad123d.artifacts.render_stl", fake_renderer)
    monkeypatch.setattr("scad123d.artifacts.require_openscad", lambda: Path("openscad"))
    assert main([str(source), "-o", str(out), "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "6 inputs in ['error', 'ok'], 1 duplicates, 6 STLs to render" in err

    assert main([str(source), "-o", str(out), "-j", "2"]) == 0
    assert main(["--report", str(out)]) == 0
    text = capsys.readouterr().out
    assert "6 STL renders recorded" in text
    assert "ERROR: Parser error" in text
    assert "timeout" in text


def test_missing_ledger_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        ArtifactLedger(tmp_path / LEDGER_NAME)


@pytest.mark.needs_openscad
def test_real_openscad_renders_a_binary_stl(tmp_path):
    scad = tmp_path / "cube.scad"
    scad.write_text("cube(10);")
    out = tmp_path / "cube.stl"
    render_stl(scad, out, timeout=120, openscadpath=None)
    assert classify_stl(out) == CLASS_OK
    assert not out.with_name("cube.stl.part").exists()
    assert out.stat().st_size == 84 + 50 * 12  # a cube is 12 triangles

    scad.write_text("module m() {}")
    with pytest.raises(EmptyModel):
        render_stl(scad, out, timeout=120, openscadpath=None)
