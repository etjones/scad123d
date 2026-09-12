"""Resolve font family names to font files, fontconfig-style.

OpenSCAD finds fonts via fontconfig, which matches on the family name in
the font's name table. build123d delegates to OCCT's Font_FontMgr, which
registers fonts with nonstandard subfamilies (e.g. "Plain") under a
combined name, so lookups by plain family name silently fall back to
Arial. This module scans the system font directories with fontTools and
resolves an OpenSCAD-style ``"Family"`` or ``"Family:style=Style"`` spec
to a concrete font file path.
"""

import sys
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont
from OCP.Font import Font_FontMgr

_FONT_SUFFIXES = (".ttf", ".otf", ".ttc", ".otc")
_DEFAULT_STYLES = ("regular", "plain", "normal", "book", "roman", "medium")

# What OpenSCAD draws with when it cannot resolve a family. It ships the
# Liberation family and falls back to Liberation Sans silently, with no
# warning at all -- so a model naming a font nobody has still renders,
# just not in the font its author meant. OCCT falls back to Arial
# instead, a different design: the same glyph came out 5% larger in area
# and visibly different in shape. Matching OpenSCAD's choice is the only
# way those models agree.
FALLBACK_FAMILY = "Liberation Sans"

# Our own copy, from the same Liberation 2.00.1 OpenSCAD ships. Vendored
# because the face cannot be assumed present: a stock macOS does not have
# it, nor do this project's CI runners, and without it the fallback would
# be whatever the host happens to substitute.
_VENDORED = Path(__file__).parent / "_fonts" / "LiberationSans-Regular.ttf"

# Where OpenSCAD keeps the family, preferred over our copy when present:
# the installed OpenSCAD is the one a model will be compared against, and
# releases differ enough to matter (two Liberation Sans versions on this
# machine disagreed by 3% on the area of a five-letter word).
_BUNDLED_FALLBACKS = (
    Path("/Applications/OpenSCAD.app/Contents/Resources/fonts"),
    Path("/usr/share/openscad/fonts"),
    Path(r"C:\Program Files\OpenSCAD\fonts"),
)


def _font_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        return [
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            home / "Library" / "Fonts",
        ]
    if sys.platform.startswith("win"):
        dirs = [Path(r"C:\Windows\Fonts")]
        local = home / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts"
        return dirs + [local]
    return [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        home / ".local" / "share" / "fonts",
        home / ".fonts",
    ]


def _faces_in_file(path: Path) -> list[tuple[str, str]]:
    """Return (family, subfamily) for each face in a font file."""
    faces: list[tuple[str, str]] = []
    try:
        if path.suffix.lower() in (".ttc", ".otc"):
            collection = TTCollection(path, lazy=True)
            fonts, shared = collection.fonts, True
        else:
            fonts, shared = [TTFont(path, lazy=True)], False
        for font in fonts:
            family = font["name"].getDebugName(1)
            subfamily = font["name"].getDebugName(2) or ""
            if family:
                faces.append((family, subfamily))
            # Not font.close() inside the loop: the faces of a collection
            # share one file handle, so closing the first one made every
            # later read fail silently. Helvetica.ttc holds six weights
            # and indexed as Regular alone, which is why asking for
            # "Helvetica:style=Bold" found no file.
            if not shared:
                font.close()
        if shared:
            collection.close()
    except Exception:  # noqa: BLE001, S110 -- probing arbitrary font files; unreadable ones are simply skipped
        pass
    return faces


@lru_cache(maxsize=1)
def _font_index() -> dict[str, dict[str, Path]]:
    """Map lowercase family name -> {lowercase style: file path}."""
    index: dict[str, dict[str, Path]] = {}
    for directory in _font_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.suffix.lower() not in _FONT_SUFFIXES:
                continue
            for family, subfamily in _faces_in_file(path):
                styles = index.setdefault(family.lower(), {})
                styles.setdefault(subfamily.lower(), path)
    return index


def parse_font_spec(spec: str) -> tuple[str, str | None]:
    """Split OpenSCAD's ``"Family:style=Style"`` syntax."""
    family, _, rest = spec.partition(":")
    style: str | None = None
    for part in rest.split(":"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "style" and value.strip():
            style = value.strip()
    return family.strip(), style


def find_font_path(spec: str) -> str | None:
    """Resolve a font spec to a file path, or None if no family matches."""
    family, style = parse_font_spec(spec)
    styles = _font_index().get(family.lower())
    if not styles:
        return None
    if style is not None:
        path = styles.get(style.lower())
        return str(path) if path is not None else None
    for preferred in _DEFAULT_STYLES:
        if preferred in styles:
            return str(styles[preferred])
    return str(next(iter(styles.values())))


@lru_cache(maxsize=1)
def fallback_font_path() -> Path | None:
    """The font to draw with when the requested family cannot be found.

    OpenSCAD's, so that a model naming an absent font still agrees with
    it: its own bundled copy first, then ours, then an installed one.
    """
    for root in _BUNDLED_FALLBACKS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("LiberationSans-Regular.ttf")):
            return path
    if _VENDORED.is_file():
        return _VENDORED
    installed = find_font_path(FALLBACK_FAMILY)
    return Path(installed) if installed else None


def known_family(family: str) -> bool:
    """Is this family installed, whatever styles it happens to expose?

    A collection file (.ttc) holding several weights indexes as a single
    face, so ``find_font_path("Helvetica:style=Bold")`` finds nothing even
    though Helvetica is right there. Asking about the family alone is what
    separates "this font is missing" from "this style has no file".
    """
    return family.strip().lower() in _font_index()


# OCCT's font database is process-global and caches a face under its family
# name: once "Arial:style=Bold" has been drawn, a later request for plain
# Arial comes back bold. Nothing in the request says so -- the volume is
# simply wrong, by 45% in the case that found this -- and the poisoning
# outlives the model, so in a batch worker the *next* model's text is drawn
# in the previous model's font. Rebuilding the database is the only reset
# OCCT offers; at 118 ms it is far too costly to run per model, so it runs
# only when a lookup is about to contradict an earlier one.
_ASKED: dict[str, str] = {}


def reset_font_database() -> None:
    """Rebuild OCCT's font database, dropping every cached family->face."""
    manager = Font_FontMgr.GetInstance_s()
    manager.ClearFontDataBase()
    manager.InitFontDataBase()
    _ASKED.clear()


def isolate_family(family: str, style: str | None) -> None:
    """Keep a family lookup from inheriting an earlier lookup's style.

    Call before asking OCCT for a font *by name*. Asking by path is not
    affected and needs no reset.
    """
    key = family.strip().lower()
    wanted = (style or "regular").strip().lower()
    if _ASKED.get(key, wanted) != wanted:
        reset_font_database()
    _ASKED[key] = wanted
