"""Regenerate committed .csg fixtures and reference metrics.

Run via `just fixtures`. Needs the OpenSCAD binary. CSG export is
deterministic and .csg -> .csg is idempotent, which is what makes committing
these stable and lets tier-1 tests run without the binary.
"""

import json
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
SCAD_DIR = FIXTURES / "scad"


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import scad123d
    from scad123d.openscad import export_csg, find_openscad, openscad_version

    if find_openscad() is None:
        print("OpenSCAD binary not found; cannot regenerate fixtures", file=sys.stderr)
        return 1

    metrics: dict[str, dict] = {"_openscad_version": openscad_version()}
    for scad in sorted(SCAD_DIR.glob("*.scad")):
        csg_path = FIXTURES / f"{scad.stem}.csg"
        csg_path.write_text(export_csg(scad))
        try:
            shape = scad123d.import_csg(csg_path)
        except Exception as exc:  # noqa: BLE001
            print(f"  {scad.stem}: BUILD FAILED {type(exc).__name__}: {exc}")
            continue
        bbox, centre = shape.bounding_box(), shape.center()
        metrics[scad.stem] = {
            "volume": shape.volume,
            "bbox": [bbox.size.X, bbox.size.Y, bbox.size.Z],
            "centroid": [centre.X, centre.Y, centre.Z],
            "faces": len(shape.faces()),
        }
        print(f"  {scad.stem}: vol={shape.volume:.4f} faces={len(shape.faces())}")

    (FIXTURES / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(metrics) - 1} fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
