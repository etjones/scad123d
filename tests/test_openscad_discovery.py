"""Tier 1: the binary-discovery logic in scad123d.openscad. No real OpenSCAD
binary needed -- platform.system(), shutil.which() and Path.exists() are all
mocked, so this exercises the override/PATH/platform-candidate branching (and
the Windows/Linux candidate paths this machine can never actually reach)
without needing to run on those OSes.
"""

from pathlib import Path

import pytest

from scad123d import openscad as osc


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """find_openscad() caches a successful result at module scope; start each
    test from a clean slate rather than leaking state between cases.
    """
    monkeypatch.setattr(osc, "_cached_binary", None)


def test_env_override_wins_when_it_exists(monkeypatch, tmp_path):
    binary = tmp_path / "openscad"
    binary.touch()
    monkeypatch.setenv(osc._ENV_VAR, str(binary))
    monkeypatch.setattr(osc.shutil, "which", lambda name: pytest.fail("should not search PATH"))

    assert osc.find_openscad() == binary


def test_env_override_ignored_when_path_missing(monkeypatch, tmp_path):
    monkeypatch.setenv(osc._ENV_VAR, str(tmp_path / "nope"))
    monkeypatch.setattr(osc.shutil, "which", lambda name: None)
    monkeypatch.setattr(osc.platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "exists", lambda self: False)

    assert osc.find_openscad() is None


def test_require_openscad_reports_the_bad_override_path(monkeypatch, tmp_path):
    bad_path = str(tmp_path / "nope")
    monkeypatch.setenv(osc._ENV_VAR, bad_path)
    monkeypatch.setattr(osc.shutil, "which", lambda name: None)
    monkeypatch.setattr(osc.platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "exists", lambda self: False)

    with pytest.raises(osc.OpenSCADNotFoundError, match=bad_path):
        osc.require_openscad()


def test_require_openscad_generic_message_with_no_override(monkeypatch):
    monkeypatch.delenv(osc._ENV_VAR, raising=False)
    monkeypatch.setattr(osc.shutil, "which", lambda name: None)
    monkeypatch.setattr(osc.platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "exists", lambda self: False)

    with pytest.raises(osc.OpenSCADNotFoundError, match=osc._ENV_VAR):
        osc.require_openscad()


def test_path_is_searched_before_platform_candidates(monkeypatch, tmp_path):
    monkeypatch.delenv(osc._ENV_VAR, raising=False)
    on_path = tmp_path / "openscad"
    monkeypatch.setattr(osc.shutil, "which", lambda name: str(on_path) if name == "openscad" else None)
    monkeypatch.setattr(osc.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(Path, "exists", lambda self: pytest.fail("should not check candidate paths"))

    assert osc.find_openscad() == on_path


@pytest.mark.parametrize(
    ("system", "candidates"),
    [
        ("Darwin", osc._MAC_CANDIDATES),
        ("Windows", osc._WINDOWS_CANDIDATES),
        ("Linux", osc._LINUX_CANDIDATES),
    ],
)
def test_platform_candidates_are_tried_in_order(monkeypatch, system, candidates):
    """Only the last candidate 'exists'; confirms the earlier ones were
    actually checked and rejected, not skipped.
    """
    monkeypatch.delenv(osc._ENV_VAR, raising=False)
    monkeypatch.setattr(osc.shutil, "which", lambda name: None)
    monkeypatch.setattr(osc.platform, "system", lambda: system)
    winner = candidates[-1]
    monkeypatch.setattr(Path, "exists", lambda self: str(self) == winner)

    assert str(osc.find_openscad()) == winner


def test_unrecognised_platform_falls_back_to_linux_candidates(monkeypatch):
    """platform.system() can report other values (e.g. 'FreeBSD'); anything
    that isn't Darwin/Windows gets the Linux candidate list rather than an
    empty one.
    """
    monkeypatch.delenv(osc._ENV_VAR, raising=False)
    monkeypatch.setattr(osc.shutil, "which", lambda name: None)
    monkeypatch.setattr(osc.platform, "system", lambda: "FreeBSD")
    winner = osc._LINUX_CANDIDATES[-1]
    monkeypatch.setattr(Path, "exists", lambda self: str(self) == winner)

    assert str(osc.find_openscad()) == winner


def test_nothing_found_returns_none(monkeypatch):
    monkeypatch.delenv(osc._ENV_VAR, raising=False)
    monkeypatch.setattr(osc.shutil, "which", lambda name: None)
    monkeypatch.setattr(osc.platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "exists", lambda self: False)

    assert osc.find_openscad() is None


def test_a_successful_search_is_not_repeated(monkeypatch, tmp_path):
    binary = tmp_path / "openscad"
    calls = []

    def which(name):
        calls.append(name)
        return str(binary) if name == "openscad" else None

    monkeypatch.delenv(osc._ENV_VAR, raising=False)
    monkeypatch.setattr(osc.shutil, "which", which)
    monkeypatch.setattr(osc.platform, "system", lambda: "Darwin")

    first = osc.find_openscad()
    second = osc.find_openscad()

    assert first == second == binary
    assert calls == ["openscad"]  # only searched once


def test_a_failed_search_is_retried_next_call(monkeypatch):
    """The bug this guards against: find_openscad() used to cache a failed
    search (None) for the rest of the process via lru_cache, so installing
    OpenSCAD mid-session (a notebook, a long batch job) had no effect until
    the interpreter restarted.
    """
    calls = []

    def which(name):
        calls.append(name)
        return None

    monkeypatch.delenv(osc._ENV_VAR, raising=False)
    monkeypatch.setattr(osc.shutil, "which", which)
    monkeypatch.setattr(osc.platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "exists", lambda self: False)

    assert osc.find_openscad() is None
    assert osc.find_openscad() is None
    # 3 PATH names tried per call ("openscad", "openscad-nightly", "OpenSCAD");
    # 6 total confirms the second call searched again rather than reading a
    # stale cached None.
    assert len(calls) == 6


def test_matching_version_does_not_skip(monkeypatch):
    from . import conftest

    # conftest imported openscad_version by name ("from ... import
    # openscad_version"), so it must be patched on conftest itself -- patching
    # scad123d.openscad.openscad_version would not affect that already-bound
    # copy.
    monkeypatch.setattr(conftest, "openscad_version", lambda: "OpenSCAD version 2025.07.18")
    conftest.require_fixture_openscad_version({"_openscad_version": "OpenSCAD version 2025.07.18"})


def test_mismatched_version_skips(monkeypatch):
    from . import conftest

    monkeypatch.setattr(conftest, "openscad_version", lambda: "OpenSCAD version 2021.01")
    with pytest.raises(pytest.skip.Exception, match="2021.01"):
        conftest.require_fixture_openscad_version({"_openscad_version": "OpenSCAD version 2025.07.18"})


def test_no_recorded_version_does_not_skip(monkeypatch):
    """Older metrics.json without _openscad_version (or a hand-authored one)
    should not spuriously skip -- only an actual, known mismatch does.
    """
    from . import conftest

    monkeypatch.setattr(conftest, "openscad_version", lambda: "OpenSCAD version 2025.07.18")
    conftest.require_fixture_openscad_version({})
