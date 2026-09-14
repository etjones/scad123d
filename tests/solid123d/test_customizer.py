import json
import runpy
from pathlib import Path

import pytest

from solid123d.customizer import (
    customize,
    load_params,
    parameter_sets,
    resolve_param_set,
)


@pytest.fixture
def param_file(tmp_path: Path) -> Path:
    path = tmp_path / "model.json"
    path.write_text(
        json.dumps(
            {
                "fileFormatVersion": "1",
                "parameterSets": {
                    "only": {
                        "size": "12.5",
                        "count": "3",
                        "label": "S",
                        "enabled": "true",
                        "offsets": "[1, 2.5, 3]",
                        "font": "Academy Engraved LET",
                    }
                },
            }
        )
    )
    return path


@pytest.fixture
def coin_json(tmp_path: Path) -> Path:
    """A multi-set file in OpenSCAD's customizer format, incl. a set
    named "default"."""
    path = tmp_path / "coin.json"
    path.write_text(
        json.dumps(
            {
                "fileFormatVersion": "1",
                "parameterSets": {
                    "default": {"coin_diam": "35", "engrave": "true"},
                    "big_thin": {"coin_diam": "55", "engrave": "true"},
                    "blank": {"coin_diam": "35", "engrave": "false"},
                },
            }
        )
    )
    return path


class TestCoercion:
    def test_types(self, param_file: Path) -> None:
        params = load_params(param_file)
        assert params == {
            "size": 12.5,
            "count": 3,
            "label": "S",
            "enabled": True,
            "offsets": [1, 2.5, 3],
            "font": "Academy Engraved LET",
        }
        assert isinstance(params["count"], int)
        assert isinstance(params["enabled"], bool)


class TestSetSelection:
    def test_single_set_needs_no_name(self, param_file: Path) -> None:
        assert load_params(param_file)["count"] == 3

    def test_missing_set_lists_available(self, param_file: Path) -> None:
        with pytest.raises(KeyError, match="only"):
            load_params(param_file, "nope")

    def test_multiple_sets_require_name(self, coin_json: Path) -> None:
        with pytest.raises(KeyError, match="big_thin"):
            load_params(coin_json)

    def test_parameter_sets_listing(self, coin_json: Path) -> None:
        assert parameter_sets(coin_json) == ["default", "big_thin", "blank"]

    def test_resolve_prefers_default_then_only_set(
        self, coin_json: Path, param_file: Path
    ) -> None:
        assert resolve_param_set(coin_json)[0] == "default"
        assert resolve_param_set(coin_json, "blank")[0] == "blank"
        assert resolve_param_set(param_file)[0] == "only"


class TestScadPathResolution:
    def test_scad_path_finds_sibling_json(self, coin_json: Path) -> None:
        scad = coin_json.with_suffix(".scad")
        scad.write_text("coin_diam = 1;")
        params = load_params(scad, "big_thin")
        assert params["coin_diam"] == 55

    def test_missing_sibling_raises(self, tmp_path: Path) -> None:
        scad = tmp_path / "lonely.scad"
        scad.write_text("cube(1);")
        with pytest.raises(FileNotFoundError, match="lonely.json"):
            load_params(scad)


class TestCustomize:
    def test_applies_to_target(self, coin_json: Path) -> None:
        target = {"coin_diam": 35.0, "engrave": False}
        applied = customize(target, path=coin_json, set_name="big_thin", argv=[])
        assert target == {"coin_diam": 55, "engrave": True}
        assert applied == {"coin_diam": 55, "engrave": True}

    def test_unknown_keys_warn_and_skip(self, coin_json: Path) -> None:
        target = {"coin_diam": 35.0}
        with pytest.warns(UserWarning, match="engrave"):
            applied = customize(target, path=coin_json, set_name="big_thin", argv=[])
        assert applied == {"coin_diam": 55}

    def test_default_set_precedence(self, coin_json: Path) -> None:
        target = {"coin_diam": 0.0, "engrave": False}
        customize(target, path=coin_json, argv=[])
        assert target["coin_diam"] == 35

    def test_multiple_sets_without_default_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text(
            json.dumps(
                {
                    "fileFormatVersion": "1",
                    "parameterSets": {"a": {"x": "1"}, "b": {"x": "2"}},
                }
            )
        )
        with pytest.raises(KeyError, match="-P NAME"):
            customize({"x": 0}, path=path, argv=[])

    def test_cli_set_selection_consumes_flags(self, coin_json: Path) -> None:
        target = {"coin_diam": 0.0, "engrave": False}
        argv = ["coin.py", "-P", "big_thin", "--out", "f.step"]
        customize(target, path=coin_json, argv=argv)
        assert target["coin_diam"] == 55
        assert argv == ["coin.py", "--out", "f.step"]

    def test_no_customizer_switch(self, coin_json: Path) -> None:
        target = {"coin_diam": 0.0}
        argv = ["coin.py", "--no-customizer"]
        applied = customize(target, path=coin_json, argv=argv)
        assert applied == {}
        assert target == {"coin_diam": 0.0}
        assert argv == ["coin.py"]

    def test_applying_announces_on_stderr(
        self, coin_json: Path, capsys: pytest.CaptureFixture
    ) -> None:
        customize({"coin_diam": 0.0, "engrave": False}, path=coin_json, argv=[])
        err = capsys.readouterr().err
        assert "Applying customizer parameter set 'default'" in err
        assert "coin.json" in err
        assert "--no-customizer" in err

    def test_no_customizer_is_quiet(
        self, coin_json: Path, capsys: pytest.CaptureFixture
    ) -> None:
        customize({"coin_diam": 0.0}, path=coin_json, argv=["x", "--no-customizer"])
        assert capsys.readouterr().err == ""

    def test_missing_sibling_json_is_silent(self, tmp_path: Path) -> None:
        target = {"x": 1, "__file__": str(tmp_path / "lonely.py")}
        assert customize(target, argv=[]) == {}
        assert target["x"] == 1

    def test_explicit_missing_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            customize({"x": 1}, path=tmp_path / "absent.json", argv=[])

    def test_caller_globals_default(self, tmp_path: Path) -> None:
        (tmp_path / "part.json").write_text(
            json.dumps(
                {
                    "fileFormatVersion": "1",
                    "parameterSets": {"only": {"size": "42"}},
                }
            )
        )
        script = tmp_path / "part.py"
        script.write_text(
            "from solid123d.customizer import customize\nsize = 1\ncustomize(argv=[])\n"
        )
        result = runpy.run_path(str(script))
        assert result["size"] == 42


class TestDashD:
    def test_define_overrides_default(self, tmp_path: Path) -> None:
        target = {"width": 10, "__file__": str(tmp_path / "s.py")}
        argv = ["s.py", "-D", "width=40"]
        applied = customize(target, argv=argv)
        assert target["width"] == 40
        assert applied == {"width": 40}
        assert argv == ["s.py"]

    def test_define_beats_parameter_file(self, coin_json: Path) -> None:
        target = {"coin_diam": 0.0, "engrave": False}
        customize(target, path=coin_json, argv=["x", "-D", "coin_diam=99"])
        assert target["coin_diam"] == 99
        assert target["engrave"] is True  # rest of the set still applies

    def test_define_survives_no_customizer(self, coin_json: Path) -> None:
        target = {"coin_diam": 0.0}
        customize(
            target,
            path=coin_json,
            argv=["x", "--no-customizer", "-D", "coin_diam=7"],
        )
        assert target["coin_diam"] == 7

    def test_define_is_repeatable_and_coerced(self, tmp_path: Path) -> None:
        target = {
            "width": 0,
            "label": "",
            "on": False,
            "__file__": str(tmp_path / "s.py"),
        }
        customize(
            target,
            argv=["s.py", "-D", "width=1.5", "-D", "label=hi", "-D", "on=true"],
        )
        assert target["width"] == 1.5
        assert target["label"] == "hi"
        assert target["on"] is True

    def test_define_unknown_name_warns(self, tmp_path: Path) -> None:
        target = {"width": 0, "__file__": str(tmp_path / "s.py")}
        with pytest.warns(UserWarning, match="nope"):
            customize(target, argv=["s.py", "-D", "nope=1"])
        assert "nope" not in target

    def test_define_without_equals_exits(self, tmp_path: Path) -> None:
        target = {"width": 0, "__file__": str(tmp_path / "s.py")}
        with pytest.raises(SystemExit, match="name=value"):
            customize(target, argv=["s.py", "-D", "width"])
