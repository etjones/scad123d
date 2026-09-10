"""Tests for scad123d.cli (the scad2step command).

Tier 1 (no binary): argument/value parsing, which never touches OpenSCAD.
Tier 2 (needs_openscad): actually converting a file, via main() end to end.
"""

import argparse
import io
import json
import subprocess
import types

import pytest

from scad123d.cli import (
    _build_parser,
    _override,
    _parse_value,
    classify,
    main,
    run_batch,
)
from scad123d.errors import MeshImportError, OpenSCADRunError, UnsupportedNodeError


def test_parse_value_recognizes_booleans():
    assert _parse_value("true") is True
    assert _parse_value("TRUE") is True
    assert _parse_value("false") is False


def test_parse_value_recognizes_numbers():
    assert _parse_value("42") == 42
    assert isinstance(_parse_value("42"), int)
    assert _parse_value("3.5") == 3.5
    assert isinstance(_parse_value("3.5"), float)


def test_parse_value_falls_back_to_a_plain_string():
    # No quoting needed for the common case -- a shell user typing
    # -D label=hello shouldn't need to know OpenSCAD string-literal syntax.
    assert _parse_value("hello") == "hello"


def test_override_splits_name_and_value():
    assert _override("width=40") == ("width", 40)


def test_override_rejects_missing_equals():
    with pytest.raises(argparse.ArgumentTypeError):
        _override("width")


def test_default_output_is_input_with_step_extension():
    parser = _build_parser()
    args = parser.parse_args(["design.scad"])
    assert args.output is None  # main() fills this in from args.input
    assert args.input.with_suffix(".step").name == "design.step"


def test_default_facet_threshold_and_mesh_scope():
    parser = _build_parser()
    args = parser.parse_args(["design.scad"])
    assert args.mesh_scope == "minimal"
    assert args.facet_threshold > 0


def test_repeated_d_flags_accumulate():
    parser = _build_parser()
    args = parser.parse_args(["design.scad", "-D", "width=40", "-D", "holes=6"])
    assert args.overrides == ["width=40", "holes=6"]


@pytest.mark.needs_openscad
def test_missing_input_file_is_a_clean_error(tmp_path, capsys):
    # needs_openscad because import_scad() checks for the OpenSCAD binary
    # before it ever looks at the input path -- without the binary this
    # would hit OpenSCADNotFoundError first, not FileNotFoundError.
    exit_code = main([str(tmp_path / "does_not_exist.scad")])
    assert exit_code == 1
    assert "no such file" in capsys.readouterr().err


@pytest.mark.needs_openscad
def test_converts_a_file_to_step(tmp_path, capsys):
    scad = tmp_path / "box.scad"
    scad.write_text("cube([10, 5, 5]);")
    output = tmp_path / "box.step"

    exit_code = main([str(scad), "-o", str(output)])

    assert exit_code == 0
    assert output.exists()
    assert "wrote" in capsys.readouterr().out


@pytest.mark.needs_openscad
def test_default_output_path_is_used_when_not_specified(tmp_path):
    scad = tmp_path / "box.scad"
    scad.write_text("cube([10, 5, 5]);")

    exit_code = main([str(scad)])

    assert exit_code == 0
    assert (tmp_path / "box.step").exists()


@pytest.mark.needs_openscad
def test_d_override_actually_changes_the_geometry(tmp_path):
    from build123d import import_step

    scad = tmp_path / "box.scad"
    scad.write_text("width = 10;\ncube([width, 5, 5]);")
    default_out = tmp_path / "default.step"
    override_out = tmp_path / "override.step"

    assert main([str(scad), "-o", str(default_out)]) == 0
    assert main([str(scad), "-o", str(override_out), "-D", "width=50"]) == 0

    default_volume = import_step(str(default_out)).volume
    override_volume = import_step(str(override_out)).volume
    assert override_volume == pytest.approx(default_volume * 5, rel=1e-9)


@pytest.mark.needs_openscad
def test_progress_and_timing_lines(tmp_path, capsys):
    scad = tmp_path / "box.scad"
    scad.write_text("cube([10, 5, 5]);")
    output = tmp_path / "box.step"

    exit_code = main([str(scad), "-o", str(output)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert f"converting {scad} -> {output}" in captured.err
    assert "wrote" in captured.out and "s)" in captured.out


@pytest.mark.needs_openscad
def test_mesh_fallback_warning_prints_as_one_clean_line(tmp_path, capsys):
    scad = tmp_path / "blob.scad"
    # three unequal spheres: no analytic hull rung -> mesh fallback
    scad.write_text(
        "hull() { sphere(r=2); translate([9,0,0]) sphere(r=3);"
        " translate([0,9,0]) sphere(r=4); }"
    )
    exit_code = main([str(scad), "-o", str(tmp_path / "blob.step")])

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "scad2step: note: hull() has no BRep equivalent" in err
    # no Python-warning furniture: no file:line echo, no source-line echo
    assert "UserWarning" not in err
    assert "return _fallback" not in err


def _param_json(tmp_path, sets):
    import json

    path = tmp_path / "box.json"
    path.write_text(json.dumps({"fileFormatVersion": "1", "parameterSets": sets}))
    return path


def test_customizer_flags_parse():
    parser = _build_parser()
    args = parser.parse_args(["design.scad", "-P", "big", "--no-customizer"])
    assert args.parameter_set == "big"
    assert args.no_customizer is True
    assert args.parameter_file is None


def test_named_parameter_file_must_exist(tmp_path, capsys):
    scad = tmp_path / "box.scad"
    scad.write_text("cube(1);")
    exit_code = main([str(scad), "-p", str(tmp_path / "absent.json")])
    assert exit_code == 1
    assert "no such parameter file" in capsys.readouterr().err


def test_parameter_set_without_file_is_an_error(tmp_path, capsys):
    scad = tmp_path / "box.scad"
    scad.write_text("cube(1);")
    exit_code = main([str(scad), "-P", "big"])
    assert exit_code == 1
    assert "no parameter file found" in capsys.readouterr().err


def test_two_sets_without_default_need_a_choice(tmp_path, capsys):
    scad = tmp_path / "box.scad"
    scad.write_text("width = 10; cube(width);")
    _param_json(tmp_path, {"a": {"width": "20"}, "b": {"width": "30"}})
    exit_code = main([str(scad)])
    assert exit_code == 1
    assert "-P NAME" in capsys.readouterr().err


@pytest.mark.needs_openscad
def test_sibling_json_applies_automatically(tmp_path, capsys):
    from build123d import import_step

    scad = tmp_path / "box.scad"
    scad.write_text("width = 10;\ncube([width, 5, 5]);")
    _param_json(tmp_path, {"wide": {"width": "40"}})  # only set -> chosen

    exit_code = main([str(scad), "-o", str(tmp_path / "box.step")])

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "applying customizer parameter set 'wide'" in err
    assert "--no-customizer" in err
    part = import_step(str(tmp_path / "box.step"))
    assert part.bounding_box().size.X == pytest.approx(40)


@pytest.mark.needs_openscad
def test_no_customizer_ignores_sibling_json(tmp_path, capsys):
    from build123d import import_step

    scad = tmp_path / "box.scad"
    scad.write_text("width = 10;\ncube([width, 5, 5]);")
    _param_json(tmp_path, {"wide": {"width": "40"}})

    exit_code = main([str(scad), "--no-customizer", "-o", str(tmp_path / "box.step")])

    assert exit_code == 0
    assert "applying customizer" not in capsys.readouterr().err
    part = import_step(str(tmp_path / "box.step"))
    assert part.bounding_box().size.X == pytest.approx(10)


@pytest.mark.needs_openscad
def test_dash_d_beats_the_parameter_file(tmp_path):
    from build123d import import_step

    scad = tmp_path / "box.scad"
    scad.write_text("width = 10;\ncube([width, 5, 5]);")
    _param_json(tmp_path, {"wide": {"width": "40"}})

    exit_code = main([str(scad), "-D", "width=25", "-o", str(tmp_path / "box.step")])

    assert exit_code == 0
    part = import_step(str(tmp_path / "box.step"))
    assert part.bounding_box().size.X == pytest.approx(25)


@pytest.mark.needs_openscad
def test_dash_p_selects_a_set(tmp_path, capsys):
    from build123d import import_step

    scad = tmp_path / "box.scad"
    scad.write_text("width = 10;\ncube([width, 5, 5]);")
    _param_json(tmp_path, {"a": {"width": "20"}, "default": {"width": "30"}})

    exit_code = main([str(scad), "-P", "a", "-o", str(tmp_path / "box.step")])

    assert exit_code == 0
    assert "parameter set 'a'" in capsys.readouterr().err
    part = import_step(str(tmp_path / "box.step"))
    assert part.bounding_box().size.X == pytest.approx(20)


# --- batch (worker) mode -------------------------------------------------------


def test_classify_maps_exceptions_to_ledger_classes():
    assert classify(OpenSCADRunError("x"))[0] == "openscad-error"
    assert (
        classify(UnsupportedNodeError("the CSG tree produced no geometry"))[0]
        == "empty"
    )
    assert classify(UnsupportedNodeError("weird node"))[0] == "unsupported"
    assert classify(MeshImportError("x"))[0] == "mesh-error"
    assert classify(FileNotFoundError("x"))[0] == "missing"
    assert classify(subprocess.TimeoutExpired("openscad", 5))[0] == "timeout"
    assert classify(ValueError("x"))[0] == "error"

    class Standard_Failure(Exception):
        pass

    assert classify(Standard_Failure("boom"))[0] == "occt-error"


def test_batch_rejects_a_positional_input():
    with pytest.raises(SystemExit):
        main(["--batch", "x.scad"])


@pytest.mark.needs_openscad
def test_run_batch_converts_and_classifies_per_line(tmp_path):
    scad = tmp_path / "box.scad"
    scad.write_text("cube([10, 10, 10]);")
    tasks = io.StringIO(
        json.dumps(
            {
                "input": str(scad),
                "output": str(tmp_path / "box.step"),
                "csg": str(tmp_path / "box.csg"),
            }
        )
        + "\n"
        + json.dumps({"input": str(tmp_path / "missing.scad")})
        + "\n"
        + "not json\n"
    )
    results = io.StringIO()
    defaults = _build_parser().parse_args(["--batch"])
    assert run_batch(tasks, results, defaults) == 0
    lines = [json.loads(line) for line in results.getvalue().splitlines()]
    assert [r["status"] for r in lines] == ["ok", "missing", "error"]
    assert (tmp_path / "box.step").stat().st_size > 0
    assert "cube(size = [10, 10, 10]" in (tmp_path / "box.csg").read_text()
    assert lines[0]["seconds"] > 0 and lines[0]["peak_rss_mb"] > 0


@pytest.mark.needs_openscad
def test_run_batch_verify_cross_checks_volume_against_openscad(tmp_path):
    scad = tmp_path / "box.scad"
    scad.write_text("cube([10, 10, 10]);")
    tasks = io.StringIO(
        json.dumps(
            {"input": str(scad), "output": str(tmp_path / "box.step"), "verify": True}
        )
        + "\n"
    )
    results = io.StringIO()
    assert run_batch(tasks, results, _build_parser().parse_args(["--batch"])) == 0
    (result,) = [json.loads(line) for line in results.getvalue().splitlines()]
    assert result["status"] == "ok"
    assert result["volume"] == pytest.approx(1000)
    assert result["scad_volume"] == pytest.approx(1000)
    assert result["openscad_warnings"] == []


@pytest.mark.needs_openscad
def test_run_batch_keeps_openscad_warnings_and_tracebacks(tmp_path):
    scad = tmp_path / "lib.scad"
    scad.write_text("module unused() { cube(1); }\nnot_a_module();\n")
    tasks = io.StringIO(
        json.dumps({"input": str(scad), "output": str(tmp_path / "o.step")}) + "\n"
    )
    results = io.StringIO()
    run_batch(tasks, results, _build_parser().parse_args(["--batch"]))
    (result,) = [json.loads(line) for line in results.getvalue().splitlines()]
    assert result["status"] == "empty"
    assert any("not_a_module" in w for w in result["openscad_warnings"])
    assert "UnsupportedNodeError" in result["traceback"]
    assert result["stage"] == "build"


@pytest.mark.needs_openscad
def test_run_batch_verify_compares_area_for_a_2d_model(tmp_path):
    scad = tmp_path / "flat.scad"
    scad.write_text(
        "difference() { square(10); translate([5, 5]) circle(2, $fn = 16); }"
    )
    tasks = io.StringIO(
        json.dumps(
            {"input": str(scad), "output": str(tmp_path / "flat.step"), "verify": True}
        )
        + "\n"
    )
    results = io.StringIO()
    assert run_batch(tasks, results, _build_parser().parse_args(["--batch"])) == 0
    (result,) = [json.loads(line) for line in results.getvalue().splitlines()]
    assert result["status"] == "ok", result.get("message")
    assert result["measure"] == "area"
    assert result["volume"] == pytest.approx(result["scad_volume"], rel=1e-6)
    assert 87 < result["volume"] < 88  # 100 - 16-gon of r=2 (~12.2)


def test_measure_counts_leaves_of_a_nested_compound():
    # build123d's Compound.volume only sees direct Solid children; a
    # color-partitioned union nests Compounds, so its .volume reads 0.
    from build123d import Box, Compound, Pos, Rectangle

    from scad123d.cli import measure

    nested = Compound(
        [Compound([Box(2, 2, 2)]), Compound([Pos(10, 0, 0) * Box(1, 1, 1)])]
    )
    assert nested.volume == 0  # the blind spot this guards against
    assert measure(nested) == pytest.approx(9)
    flat = Compound(
        [Compound([Rectangle(2, 3)]), Compound([Pos(10, 0, 0) * Rectangle(1, 1)])]
    )
    assert measure(flat, two_d=True) == pytest.approx(7)


@pytest.mark.needs_openscad
def test_group_by_color_and_per_color_volumes(tmp_path):
    scad = tmp_path / "two.scad"
    scad.write_text(
        'color("red") cube(10);\n'
        'color("blue") translate([20, 0, 0]) cube(5);\n'
        "translate([0, 20, 0]) cube(2);\n"
    )
    # single-file path: the author's grouping by default, per-color groups on request
    assert main([str(scad), "-o", str(tmp_path / "tree.step")]) == 0
    assert (
        main([str(scad), "-o", str(tmp_path / "grouped.step"), "--group-by-color"]) == 0
    )
    tree = (tmp_path / "tree.step").read_text()
    grouped = (tmp_path / "grouped.step").read_text()
    for text in (tree, grouped):
        assert "PRODUCT('red'" in text and "PRODUCT('blue'" in text
    assert "PRODUCT('uncolored'" not in tree
    assert "PRODUCT('uncolored'" in grouped

    # batch path: per-color volumes travel in the result record
    tasks = io.StringIO(
        json.dumps({"input": str(scad), "output": str(tmp_path / "b.step")}) + "\n"
    )
    results = io.StringIO()
    defaults = _build_parser().parse_args(["--batch"])
    assert run_batch(tasks, results, defaults) == 0
    record = json.loads(results.getvalue().splitlines()[0])
    assert record["status"] == "ok"
    assert record["colors"] == {
        "red": pytest.approx(1000),
        "blue": pytest.approx(125),
        "uncolored": pytest.approx(8),
    }


@pytest.mark.needs_openscad
def test_verify_checks_volume_per_color_and_says_when_it_cannot(tmp_path):
    """--verify compares each color's volume once the total agrees, and
    reports the comparison undefined where OpenSCAD defines no per-color
    volume (overlapping colors)."""
    disjoint = tmp_path / "disjoint.scad"
    disjoint.write_text(
        'color("red") cube(10);\ncolor("blue") translate([20, 0, 0]) cube(5);\n'
    )
    overlapping = tmp_path / "overlapping.scad"
    overlapping.write_text(
        'color("red") cube(10);\ncolor("blue") translate([5, 0, 0]) cube(10);\n'
    )
    tasks = io.StringIO(
        "\n".join(
            json.dumps(
                {
                    "input": str(path),
                    "output": str(path.with_suffix(".step")),
                    "verify": True,
                }
            )
            for path in (disjoint, overlapping)
        )
        + "\n"
    )
    results = io.StringIO()
    defaults = _build_parser().parse_args(["--batch"])
    assert run_batch(tasks, results, defaults) == 0
    first, second = (json.loads(line) for line in results.getvalue().splitlines())

    assert first["status"] == "ok"
    assert first["colors"] == {"red": pytest.approx(1000), "blue": pytest.approx(125)}
    assert first["scad_colors"] == {
        "red": pytest.approx(1000, rel=1e-6),
        "blue": pytest.approx(125, rel=1e-6),
    }
    assert "colors_unchecked" not in first

    # Overlap: our partition gives red 500 / blue 1000, OpenSCAD gives no
    # per-color volume at all, so the check is skipped with a reason.
    assert second["status"] == "ok"
    assert second["colors"] == {"red": pytest.approx(500), "blue": pytest.approx(1000)}
    assert "colors overlap" in second["colors_unchecked"]
    assert "scad_colors" not in second


class TestReferenceUsability:
    """A volume comparison is only as good as the mesh it compares against,
    but "flawless" is the wrong bar.

    An open surface has no inside and a closed one enclosing negative
    volume is inside out; neither can be measured. A handful of
    non-manifold edges can: that is what two solids touching along an edge
    exports as. Measured over the corpus, 303 of the 512 references a
    stricter rule rejected had under 1% non-manifold edges -- real
    disagreements, wrongly set aside -- while visibly shredded renders run
    from 4% to 69%.
    """

    @staticmethod
    def report(**kwargs):
        from scad123d.mesh_import import MeshReport

        base = {
            "volume": 100.0,
            "triangles": 1000,
            "boundary_edges": 0,
            "nonmanifold_edges": 0,
            "flipped_edges": 0,
        }
        return MeshReport(**{**base, **kwargs})

    def test_a_closed_positive_mesh_is_usable(self):
        assert self.report().usable

    def test_an_open_surface_is_not(self):
        assert not self.report(boundary_edges=4).usable

    def test_an_inside_out_mesh_is_not(self):
        assert not self.report(volume=-1568.3).usable

    def test_a_shredded_mesh_is_not(self):
        """1,000 triangles is about 1,500 edges; 21% of them was the corpus
        bevel gear, whose reference volume was wrong by a factor of four."""
        assert not self.report(nonmanifold_edges=315).usable

    def test_a_few_touching_edges_are_still_usable(self):
        assert self.report(nonmanifold_edges=9).usable

    def test_zero_volume_is_not_a_defect(self):
        """An empty render is a legitimate measurement of nothing."""
        assert self.report(volume=0.0).usable

    def test_triangles_that_disagree_about_which_way_is_out(self):
        """A regular tetrahedron whose four faces were wound
        inconsistently: OpenSCAD renders it as 0.000035 where its exact
        volume is 41,666.67. Only orientation gives that away -- no edge
        has three triangles on it, and the surface is closed."""
        report = self.report(volume=3.5e-5, triangles=4, flipped_edges=4)
        assert not report.usable
        assert "which way is out" in report.fault()

    def test_a_small_mesh_may_touch_itself(self):
        """Two tetrahedra meeting along an edge. Six edges make the
        percentage allowance zero, so without a floor one honest touching
        edge would condemn them."""
        assert self.report(
            triangles=8, nonmanifold_edges=1, flipped_edges=1
        ).usable

    def test_the_two_counts_are_not_added_together(self):
        """A touching edge appears in both counts, being shared by four
        triangles and walked twice each way. Summing them would double
        every legitimate touch."""
        edges = self.report(triangles=1000).edges
        near = int(edges * 0.02)
        assert self.report(
            triangles=1000, nonmanifold_edges=near, flipped_edges=near
        ).usable

    def test_the_fault_names_what_is_wrong(self):
        assert "open surface" in self.report(boundary_edges=4).fault()
        assert "negative volume" in self.report(volume=-5.0).fault()
        assert "self-intersecting" in self.report(nonmanifold_edges=900).fault()
        assert "which way is out" in self.report(flipped_edges=900).fault()


class TestSecondOpinion:
    """When the fast renderer cannot be measured, ask the exact one.

    OpenSCAD's default here is Manifold, two orders of magnitude faster
    than CGAL and normally identical. On self-intersecting input it is
    not: a corpus bevel gear came back from Manifold as 165,211, from a
    mesh 21% self-intersecting, where CGAL says 703,784 and this
    converter says 702,711.
    """

    @staticmethod
    def run(monkeypatch, ours, fast, exact):
        """_verify with both renderers stubbed; returns the result dict."""
        from scad123d import cli

        def render(csg_text, timeout, two_d=False, backend=None):
            report = exact if backend == cli.EXACT_BACKEND else fast
            if isinstance(report, Exception):
                raise report
            return report

        monkeypatch.setattr(cli, "_openscad_render", render)
        monkeypatch.setattr(cli, "measure", lambda part, two_d=False: ours)
        monkeypatch.setattr(cli, "refine_tessellation", lambda text: text)

        class Part:
            def solids(self):
                return [object()]

        conversion = types.SimpleNamespace(part=Part(), timeout=30, meshed=None)
        result: dict = {}
        cli._verify(conversion, "cube(1);", result)
        return result

    @staticmethod
    def report(volume, nonmanifold=0, boundary=0):
        from scad123d.mesh_import import MeshReport

        return MeshReport(volume, 1000, boundary, nonmanifold, 0)

    def test_the_exact_render_clears_us_when_it_agrees(self, monkeypatch):
        result = self.run(
            monkeypatch,
            ours=702711.0,
            fast=self.report(165211.0, nonmanifold=315),
            exact=self.report(703784.0),
        )
        assert result.get("status") is None  # not a mismatch
        assert "matches CGAL's exact render" in result["message"]
        assert result["exact_volume"] == 703784.0

    def test_the_exact_render_convicts_us_when_it_agrees_with_the_fast_one(
        self, monkeypatch
    ):
        result = self.run(
            monkeypatch,
            ours=10734.0,
            fast=self.report(20148.0, nonmanifold=315),
            exact=self.report(20147.8),
        )
        assert result["status"] == "mismatch"
        assert "off by >20%" in result["message"]

    def test_neither_measurable_is_unchecked(self, monkeypatch):
        result = self.run(
            monkeypatch,
            ours=100.0,
            fast=self.report(1.0, nonmanifold=315),
            exact=self.report(2.0, boundary=40),
        )
        assert result["status"] == "unchecked"
        assert "neither of OpenSCAD's renderers" in result["message"]

    def test_an_exact_render_that_fails_is_unchecked(self, monkeypatch):
        from scad123d.errors import OpenSCADRunError

        result = self.run(
            monkeypatch,
            ours=100.0,
            fast=self.report(1.0, nonmanifold=315),
            exact=OpenSCADRunError("CGAL fell over"),
        )
        assert result["status"] == "unchecked"
        assert "CGAL fell over" in result["message"]

    def test_a_usable_fast_render_never_asks_twice(self, monkeypatch):
        """The exact renderer is 30x slower; it must not run on the path
        every conversion takes."""
        from scad123d import cli

        asked: list = []

        def render(csg_text, timeout, two_d=False, backend=None):
            asked.append(backend)
            return self.report(100.0, nonmanifold=9)

        monkeypatch.setattr(cli, "_openscad_render", render)
        monkeypatch.setattr(cli, "measure", lambda part, two_d=False: 50.0)
        monkeypatch.setattr(cli, "refine_tessellation", lambda text: text)

        class Part:
            def solids(self):
                return [object()]

        result: dict = {}
        cli._verify(
            types.SimpleNamespace(part=Part(), timeout=30, meshed=None),
            "cube(1);",
            result,
        )
        assert result["status"] == "mismatch"
        assert cli.EXACT_BACKEND not in asked
