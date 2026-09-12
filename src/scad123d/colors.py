"""Cross-checking per-color volumes against OpenSCAD's own render.

``--verify`` compares total volume; this compares the volume *per color*,
which catches a class of bug total volume cannot see: material assigned to
the wrong color. Getting the total right while painting half of it the
wrong color is a silently wrong multi-material print.

The comparison is only defined where OpenSCAD defines it. OpenSCAD's
``color()`` is a display attribute on surfaces, not a material assignment
to volume: in a union of two overlapping colored cubes, the interface
between them carries no triangles at all -- the union removed them -- so
each per-color triangle group is an *open* surface with no volume. Where
the colored regions are disjoint (the ordinary multi-material case) every
group is closed and its volume is exact. So this module measures what it
can and says plainly when it cannot, rather than comparing nonsense:
``openscad_color_volumes`` returns None for a model whose color groups do
not close.
"""

import io
import itertools
import shutil
import struct
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from collections.abc import Sequence

from solid123d import color_label

from .openscad import export_mesh

# OpenSCAD's viewport default color, which uncolored geometry is exported
# with. A model that deliberately uses this exact yellow is indistinguishable
# from uncolored geometry here; nothing downstream depends on telling them
# apart.
OPENSCAD_DEFAULT = "#f9d72c"
UNCOLORED = "uncolored"
MODEL_PATH = "3D/3dmodel.model"
NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}


def openscad_channel(value: float) -> int:
    """The 8-bit channel OpenSCAD writes for a float color component.

    OpenSCAD keeps a color as 32-bit floats and its 3MF exporter truncates
    ``c * 255`` rather than rounding, so the byte it writes is usually one
    *below* the authored one: the float came from dividing a byte by 255
    and lands a hair under the integer. lightgreen's #90ee90 is written
    #90ed90, chocolate's #d2691e is written #d1691d.

    Reproducing that exactly, rather than comparing our rounded byte
    against theirs with a tolerance, is the difference between knowing the
    two sides agree and hoping they are close enough. Verified against
    OpenSCAD for all 256 channel values; the 32-bit narrowing is load
    bearing, since the double the CSG text parses to truncates differently
    at 248.
    """
    narrowed = struct.unpack("f", struct.pack("f", value))[0]
    return int(narrowed * 255)


def openscad_key(rgba: Sequence[float]) -> str:
    """The palette entry OpenSCAD would write for *rgba* -- the key both
    sides of the comparison are stated in."""
    return "#" + "".join(f"{openscad_channel(c):02x}" for c in list(rgba)[:3])


def label_for(hex_color: str) -> str:
    """A human-readable name for an export key, for the message only --
    never for matching. The key is OpenSCAD's truncated byte, so it usually
    names no CSS color even when the authored one did."""
    if not hex_color.startswith("#"):
        return hex_color  # already UNCOLORED
    text = hex_color.lstrip("#")[:6].lower()
    if f"#{text}" == OPENSCAD_DEFAULT:
        return UNCOLORED
    # The exporter truncated an unknown subset of the channels by one, so
    # try putting each back: whichever combination names a CSS color is
    # almost certainly what the author wrote, and the message can say
    # "lightgreen" instead of "#90ed90". Cosmetic only -- matching is done
    # on the key itself, and an unrecognized key just stays hex.
    channels = [int(text[i : i + 2], 16) for i in (0, 2, 4)]
    for bump in itertools.product((0, 1), repeat=3):
        candidate = [min(c + b, 255) for c, b in zip(channels, bump)]
        named = color_label([c / 255 for c in candidate])
        if not named.startswith("#"):
            return named
    return f"#{text}"


def _signed_volume(vertices: list[tuple[float, float, float]], triangles) -> float:
    """Volume enclosed by a closed triangle mesh (divergence theorem)."""
    total = 0.0
    for i, j, k in triangles:
        ax, ay, az = vertices[i]
        bx, by, bz = vertices[j]
        cx, cy, cz = vertices[k]
        total += (
            ax * (by * cz - bz * cy)
            - ay * (bx * cz - bz * cx)
            + az * (bx * cy - by * cx)
        )
    return abs(total) / 6.0


def _is_closed(triangles) -> bool:
    """Every edge shared by exactly two triangles: a closed surface, and
    the only case where the enclosed volume means anything."""
    edges: Counter = Counter()
    for tri in triangles:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            edges[(a, b) if a < b else (b, a)] += 1
    return bool(edges) and all(count == 2 for count in edges.values())


def _normalized(displaycolor: str) -> str:
    """A 3MF ``displaycolor`` as an export key: six lowercase hex digits,
    alpha dropped, OpenSCAD's viewport default folded into ``uncolored``."""
    text = displaycolor.lstrip("#")[:6].lower()
    return UNCOLORED if f"#{text}" == OPENSCAD_DEFAULT else f"#{text}"


def parse_3mf_color_volumes(data: bytes) -> dict[str, float] | None:
    """Volume per color in a 3MF, keyed by ``openscad_key``, or None when a
    color group is not closed.

    An open group means the model's colors overlap, where OpenSCAD assigns
    no volume to a color at all (see the module docstring).
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        xml = archive.read(MODEL_PATH)
    root = ET.fromstring(xml)

    palette: dict[str, str] = {}
    for materials in root.iter(f"{{{NS['m']}}}basematerials"):
        group = materials.get("id", "")
        for index, base in enumerate(materials):
            palette[f"{group}:{index}"] = base.get("displaycolor", "")

    volumes: dict[str, float] = {}
    for obj in root.iter(f"{{{NS['m']}}}object"):
        mesh = obj.find("m:mesh", NS)
        if mesh is None:
            continue
        vertices = [
            (float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0)))
            for v in mesh.iterfind("m:vertices/m:vertex", NS)
        ]
        default_group = obj.get("pid", "")
        default_index = obj.get("pindex", "0")
        grouped: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
        for tri in mesh.iterfind("m:triangles/m:triangle", NS):
            group = tri.get("pid", default_group)
            index = tri.get("p1", default_index)
            grouped[f"{group}:{index}"].append(
                (int(tri.get("v1")), int(tri.get("v2")), int(tri.get("v3")))
            )
        for key, triangles in grouped.items():
            if not _is_closed(triangles):
                return None
            entry = _normalized(palette.get(key, OPENSCAD_DEFAULT))
            volumes[entry] = volumes.get(entry, 0.0) + _signed_volume(
                vertices, triangles
            )
    return {k: round(v, 6) for k, v in volumes.items()} or None


def openscad_color_volumes(csg_text: str, timeout: float) -> dict[str, float] | None:
    """OpenSCAD's own volume per color for a CSG tree, or None where it
    defines none. Rendering the CSG (not the .scad) is what keeps this
    comparable to the total-volume check: same tree, same overrides."""
    path = export_mesh(csg_text, suffix=".3mf", timeout=timeout)
    try:
        return parse_3mf_color_volumes(path.read_bytes())
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def compare(
    ours: dict[str, float], theirs: dict[str, float], tolerance: float
) -> str | None:
    """A message naming the worst per-color disagreement, or None if every
    color agrees within *tolerance*.

    Both sides are keyed by ``openscad_key``, so the keys are OpenSCAD's own
    8-bit channels and match exactly or not at all.
    """
    worst: tuple[float, str] | None = None
    for key in sorted(set(ours) | set(theirs)):
        mine = ours.get(key, 0.0)
        yours = theirs.get(key, 0.0)
        scale = max(abs(mine), abs(yours))
        error = abs(mine - yours) / scale if scale > 1e-9 else 0.0
        if error > tolerance and (worst is None or error > worst[0]):
            worst = (
                error,
                (
                    f"{label_for(key)} {mine:.6g} vs OpenSCAD "
                    f"{yours:.6g} ({100 * error:.1f}%)"
                ),
            )
    if worst is None:
        return None
    return f"color volume off by {worst[1]}"
