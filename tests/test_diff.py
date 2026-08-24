"""scad123d-diff: differential bisection localizes the faulty operation."""

import pytest

from scad123d import diff as D
from scad123d.parser import parse_csg

_SOURCE = """\
union() {
    cube(size = [10, 10, 10], center = false);
    intersection() {
        cube(size = [4, 4, 4], center = false);
        cube(size = [6, 6, 6], center = false);
    }
}"""


def test_bisection_localizes_the_faulty_operation(monkeypatch, tmp_path):
    """Mock both volume oracles; make intersections come out 50% too big
    on 'our' side. The walk must bottom out exactly on the intersection
    node, not its (agreeing) children and not the whole union."""
    tree = parse_csg(_SOURCE)

    truth = {"cube": 1000.0, "intersection": 64.0, "union": 1064.0}

    def fake_ours(node, options):
        base = truth.get(node.name, 1000.0)
        if node.name == "intersection":
            return base * 1.5
        if node.name == "union":
            return truth["union"] + 32.0  # carries the child's error
        return base

    def fake_ref(source, timeout):
        for name in ("intersection", "union"):
            if source.lstrip().startswith(name):
                return truth[name]
        return truth["cube"]

    monkeypatch.setattr(D, "_our_volume", fake_ours)
    monkeypatch.setattr(D, "_scad_volume", fake_ref)

    differ = D._Differ(tolerance=0.02, timeout=1, out_dir=tmp_path)
    differ.descend(tree, "root")
    assert len(differ.culprits) == 1
    _path, node, ours, ref = differ.culprits[0]
    assert node.name == "intersection"
    assert ours == pytest.approx(96.0)
    assert ref == pytest.approx(64.0)


@pytest.mark.needs_openscad
def test_healthy_model_reports_agreement(tmp_path, capsys):
    scad = tmp_path / "box.scad"
    scad.write_text("cube([10, 5, 5]);")
    exit_code = D.main([str(scad)])
    assert exit_code == 0
    assert "agreement" in capsys.readouterr().out
