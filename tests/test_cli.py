"""Tests for scad123d.cli (the scad2step command).

Tier 1 (no binary): argument/value parsing, which never touches OpenSCAD.
Tier 2 (needs_openscad): actually converting a file, via main() end to end.
"""

import argparse

import pytest

from scad123d.cli import _build_parser, _override, _parse_value, main


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
