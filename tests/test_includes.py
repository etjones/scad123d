"""scad123d-includes: static include scanning, classification, and supply.

Tier 1 throughout (a temp corpus, a fake Thingiverse), except the one test
that converts a model whose include lives only in an overlay.
"""

import io
import json
from pathlib import Path

import pytest

from scad123d.includes import (
    Missing,
    Report,
    fetch_from_thingiverse,
    includes_of,
    main,
    overlay_dir,
    resolve_siblings,
    scan,
    thing_of,
)


def test_includes_of_reads_include_and_use_and_ignores_comments():
    source = (
        "// use <commented_out.scad>\n"
        "include <BOSL2/std.scad>\n"
        "  use <helpers.scad>\n"
        "/* include <also_commented.scad> */\n"
        "include<../config.scad>\n"
    )
    assert includes_of(source) == ["BOSL2/std.scad", "helpers.scad", "../config.scad"]


def test_thing_of_parses_scraped_folder_names(tmp_path):
    assert thing_of(tmp_path / "0125994_4" / "body.scad") == "0125994"
    assert thing_of(tmp_path / "2536326618_0" / "x.scad") == "2536326618"
    assert thing_of(tmp_path / "plain_folder" / "x.scad") is None
    assert thing_of(tmp_path / "x.scad") is None


def _corpus(root: Path) -> None:
    (root / "0100_0").mkdir(parents=True)
    (root / "0100_0" / "body.scad").write_text(
        "use <helpers.scad>\ninclude <config.scad>\nuse <write/Write.scad>\ncube(1);\n"
    )
    (root / "0100_1").mkdir()
    (root / "0100_1" / "helpers.scad").write_text("module h() cube(2);\n")
    (root / "0200_0").mkdir()
    (root / "0200_0" / "config.scad").write_text(
        "w = 3;\n"
    )  # a different thing's config
    (root / "0200_0" / "part.scad").write_text("include <config.scad>\ncube(w);\n")


def test_scan_classifies_library_sibling_and_absent(tmp_path):
    _corpus(tmp_path)
    report = scan(tmp_path, dirs=[])  # no library dirs: nothing resolves but siblings
    assert report.files == 4
    assert report.with_missing == 1  # part.scad's config.scad sits beside it
    kinds = {(m.include, m.kind) for m in report.missing}
    assert kinds == {
        ("helpers.scad", "sibling"),
        ("config.scad", "absent"),  # another thing's config.scad must not be used
        ("write/Write.scad", "library"),
    }
    lib = next(m for m in report.missing if m.kind == "library")
    assert lib.library == "Write.scad" and "HarlanDMii" in lib.source
    sib = next(m for m in report.missing if m.kind == "sibling")
    assert sib.sibling == tmp_path / "0100_1" / "helpers.scad"


def test_library_dir_makes_an_include_resolvable(tmp_path):
    _corpus(tmp_path)
    libs = tmp_path / "libs"
    (libs / "write").mkdir(parents=True)
    (libs / "write" / "Write.scad").write_text("")
    report = scan(tmp_path, dirs=[libs])
    assert "write/Write.scad" not in {m.include for m in report.missing}


def test_resolve_siblings_writes_the_overlay_only_when_applied(tmp_path):
    _corpus(tmp_path)
    overlay = tmp_path / "overlay"
    report = scan(tmp_path, dirs=[])
    assert resolve_siblings(report, tmp_path, overlay, apply=False) == 1
    assert not overlay.exists()
    assert resolve_siblings(report, tmp_path, overlay, apply=True) == 1
    target = overlay / "0100_0" / "helpers.scad"
    assert target.read_text() == "module h() cube(2);\n"
    assert (
        overlay_dir(overlay, tmp_path, tmp_path / "0100_0" / "body.scad")
        == overlay / "0100_0"
    )
    # once supplied, a scan that knows the overlay no longer reports it
    again = scan(tmp_path, dirs=[overlay / "0100_0"])
    assert "helpers.scad" not in {m.include for m in again.missing}
    # and the corpus itself was not touched
    assert not (tmp_path / "0100_0" / "helpers.scad").exists()


def test_fetch_uses_the_thing_file_list_and_writes_the_overlay(tmp_path):
    _corpus(tmp_path)
    overlay = tmp_path / "overlay"
    report = scan(tmp_path, dirs=[])
    calls: list[str] = []

    def fake_http(url: str, token: str) -> bytes:
        calls.append(url)
        assert token == "tok"
        if url.endswith("/things/100/files"):  # zero-padded folder id -> real id
            return json.dumps(
                [
                    {"name": "config.scad", "download_url": "https://x/config"},
                    {"name": "other.stl"},
                ]
            ).encode()
        if url == "https://x/config":
            return b"w = 7;\n"
        raise AssertionError(url)

    log: list[str] = []
    n = fetch_from_thingiverse(
        report,
        tmp_path,
        overlay,
        "tok",
        apply=False,
        http_get=fake_http,
        log=log.append,
    )
    assert n == 1 and not overlay.exists()
    n = fetch_from_thingiverse(
        report, tmp_path, overlay, "tok", apply=True, http_get=fake_http, log=log.append
    )
    assert n == 1
    assert (overlay / "0100_0" / "config.scad").read_text() == "w = 7;\n"
    assert calls.count("https://x/config") == 1


def test_fetch_reports_a_thing_without_the_file(tmp_path):
    report = Report(
        missing=[Missing(tmp_path / "0300_0" / "m.scad", "gone.scad", kind="absent")]
    )
    log: list[str] = []
    n = fetch_from_thingiverse(
        report,
        tmp_path,
        tmp_path / "ov",
        "tok",
        apply=True,
        http_get=lambda url, token: b"[]",
        log=log.append,
    )
    assert n == 0 and any("no file named 'gone.scad'" in line for line in log)


def test_cli_scan_prints_a_summary_and_json(tmp_path, capsys):
    _corpus(tmp_path)
    out = tmp_path / "report.json"
    assert (
        main(
            [
                "scan",
                str(tmp_path),
                "--json",
                str(out),
                "--library-dir",
                str(tmp_path / "nolibs"),
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "known libraries to install" in text and "Write.scad" in text
    assert "absent files" in text and "config.scad" in text
    kinds = {(e["include"], e["kind"]) for e in json.loads(out.read_text())}
    assert ("helpers.scad", "sibling") in kinds


def test_cli_fetch_without_a_token_explains(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("THINGIVERSE_TOKEN", raising=False)
    _corpus(tmp_path)
    assert main(["fetch", str(tmp_path), "-o", str(tmp_path / "ov")]) == 1
    assert "thingiverse.com/developers" in capsys.readouterr().err


@pytest.mark.needs_openscad
def test_worker_puts_the_overlay_on_openscadpath(tmp_path):
    from scad123d.cli import _build_parser, run_batch

    model = tmp_path / "0100_0" / "body.scad"
    model.parent.mkdir(parents=True)
    model.write_text("use <helpers.scad>\nh();\n")
    overlay = tmp_path / "overlay" / "0100_0"
    overlay.mkdir(parents=True)
    (overlay / "helpers.scad").write_text("module h() cube([2, 3, 4]);\n")
    tasks = io.StringIO(
        json.dumps(
            {
                "input": str(model),
                "output": str(tmp_path / "o.step"),
                "openscadpath": str(overlay),
            }
        )
        + "\n"
        + json.dumps({"input": str(model), "output": str(tmp_path / "o2.step")})
        + "\n"
    )
    results = io.StringIO()
    run_batch(tasks, results, _build_parser().parse_args(["--batch"]))
    with_overlay, without = [
        json.loads(line) for line in results.getvalue().splitlines()
    ]
    assert with_overlay["status"] == "ok"
    assert without["status"] == "empty"  # the same model, include unresolved
    assert any("helpers.scad" in w for w in without["openscad_warnings"])
