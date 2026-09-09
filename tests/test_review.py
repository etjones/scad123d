"""scad123d-review: a fixed set of worst cases, one folder each with links,
thumbnails, and a README; the set survives re-runs and records what a fix
resolved. Thumbnails are faked; one needs_openscad test renders for real."""

import json
from pathlib import Path

import pytest

from scad123d.batch import Batch
from scad123d.cli import CLASS_MISMATCH, CLASS_OK
from scad123d.review import (
    CASES_FILE,
    INDEX_FILE,
    Review,
    main,
    thumbnail_stl,
)


def fake_thumb(src: Path, png: Path) -> bool:
    png.write_bytes(b"PNG" + src.name.encode())
    return True


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "src"
    out = tmp_path / "out"
    names = {"big": 60.0, "mid": 12.0, "small": 1.5, "fine": None}
    for name in names:
        (source / "thing").mkdir(parents=True, exist_ok=True)
        (source / "thing" / f"{name}.scad").write_text(
            f"cube({name!r});", encoding="utf-8"
        )
    batch = Batch(source, out, jobs=1, timeout=1)
    batch.scan()
    for name, off in names.items():
        path = str(source / "thing" / f"{name}.scad")
        (out / "thing").mkdir(exist_ok=True)
        (out / "thing" / f"{name}.step").write_text("ISO-10303-21;", encoding="utf-8")
        (out / "thing" / f"{name}.csg").write_text("cube();", encoding="utf-8")
        if name != "small":
            (out / "thing" / f"{name}.stl").write_bytes(b"\0" * 84)
        if off is None:
            batch.ledger.record(path, CLASS_OK, volume=100, scad_volume=100)
        else:
            batch.ledger.record(
                path,
                CLASS_MISMATCH,
                f"volume off by >20%: {100 + off} vs OpenSCAD 100 ({off}%)",
                volume=100 + off,
                scad_volume=100,
                meshed=["hull() has no BRep equivalent"] if name == "big" else None,
            )
    batch.ledger.close()
    return source, out


def make(out: Path, review_dir: Path) -> Review:
    return Review(out, review_dir, jobs=2, stl_thumbnailer=fake_thumb)


def test_selects_worst_first_and_builds_a_folder_per_case(corpus, tmp_path):
    source, out = corpus
    r = make(out, tmp_path / "review")
    cases = r.run(CLASS_MISMATCH, top=2, reselect=False)
    r.ledger.close()
    assert [Path(c.path).name for c in cases] == ["big.scad", "mid.scad"]
    folder = tmp_path / "review" / cases[0].dir
    assert folder.name == "001-thing-big"
    for suffix in (".scad", ".csg", ".stl", ".step"):
        link = folder / f"big{suffix}"
        assert link.is_symlink(), suffix
    assert (folder / "big.scad").resolve() == (source / "thing" / "big.scad").resolve()
    assert (folder / "stl.png").exists()
    assert not (folder / "step.png").exists()
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert "scad123d 160.00 vs OpenSCAD 100.00 (+60.0%)" in readme
    assert "hull() has no BRep equivalent" in readme
    assert "cube('big');" in readme
    index = (tmp_path / "review" / INDEX_FILE).read_text(encoding="utf-8")
    assert "resolved since selection:** 0 of 2" in index
    assert "| 1 | big.scad |" in index


def test_missing_outputs_are_reported_not_linked(corpus, tmp_path):
    _source, out = corpus
    r = make(out, tmp_path / "review")
    cases = r.run(CLASS_MISMATCH, top=3, reselect=False)
    r.ledger.close()
    folder = tmp_path / "review" / cases[2].dir  # small: no STL
    assert not (folder / "small.stl").exists()
    assert "small.stl: none" in (folder / "README.md").read_text(encoding="utf-8")
    assert not (folder / "stl.png").exists()


def test_rerun_keeps_the_set_and_records_what_a_fix_resolved(corpus, tmp_path):
    source, out = corpus
    r = make(out, tmp_path / "review")
    r.run(CLASS_MISMATCH, top=2, reselect=False)
    r.ledger.close()
    # a "fix" lands: big converts fine now, and its STEP is rewritten
    batch = Batch(source, out, jobs=1, timeout=1)
    batch.ledger.record(
        str(source / "thing" / "big.scad"), CLASS_OK, volume=100, scad_volume=100
    )
    batch.ledger.close()

    r = make(out, tmp_path / "review")
    r.today = "2099-01-01"
    cases = r.run(CLASS_MISMATCH, top=2, reselect=False)
    r.ledger.close()
    assert [c.rank for c in cases] == [1, 2]  # same set, big is still rank 1
    big = cases[0]
    assert big.first["status"] == CLASS_MISMATCH and big.latest["status"] == CLASS_OK
    assert big.latest["date"] == "2099-01-01"
    index = (tmp_path / "review" / INDEX_FILE).read_text(encoding="utf-8")
    assert "resolved since selection:** 1 of 2" in index
    assert "changed in this refresh:** 1" in index
    recorded = json.loads(
        (tmp_path / "review" / CASES_FILE).read_text(encoding="utf-8")
    )
    assert len(recorded["cases"][0]["history"]) == 2
    assert "**when selected**" in (
        tmp_path / "review" / "001-thing-big" / "README.md"
    ).read_text(encoding="utf-8")


def test_reselect_starts_over(corpus, tmp_path):
    _source, out = corpus
    r = make(out, tmp_path / "review")
    r.run(CLASS_MISMATCH, top=1, reselect=False)
    r.ledger.close()
    r = make(out, tmp_path / "review")
    cases = r.run(CLASS_MISMATCH, top=3, reselect=True)
    r.ledger.close()
    assert len(cases) == 3 and len(cases[0].history) == 1


def test_cli(corpus, tmp_path, capsys, monkeypatch):
    _source, out = corpus
    monkeypatch.setattr("scad123d.review.thumbnail_stl", fake_thumb)
    assert (
        main([str(tmp_path / "rv"), "-o", str(out), "--top", "2", "--no-thumbnails"])
        == 0
    )
    assert "2 cases" in capsys.readouterr().err
    assert (tmp_path / "rv" / INDEX_FILE).exists()
    assert main([str(tmp_path / "rv"), "-o", str(tmp_path / "nowhere")]) == 2


@pytest.mark.needs_openscad
def test_real_stl_thumbnail(tmp_path):
    stl = tmp_path / "cube.stl"
    stl.write_text(
        "solid c\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n"
        "vertex 0 1 0\nendloop\nendfacet\nendsolid c\n",
        encoding="utf-8",
    )
    assert thumbnail_stl(stl, tmp_path / "cube.png")
    assert (tmp_path / "cube.png").stat().st_size > 100
