"""Bundled UI fonts and font-family resolution.

The Qt UI ships Inter Variable (UI) and JetBrains Mono (technical text) under
the SIL Open Font License. Fonts are loaded from the application bundle so
rendering does not depend on fontconfig or on fonts installed by the desktop
environment.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase, QFontInfo

FONT_DIR = Path(__file__).with_name("assets") / "fonts"

UI_FONT_STACK = (
    "Inter Variable",
    "Inter",
    "Noto Sans",
    "Adwaita Sans",
    "Source Sans 3",
    "DejaVu Sans",
    "Liberation Sans",
    "Roboto",
    "Cantarell",
    "Ubuntu",
    "Segoe UI",
    "Helvetica Neue",
    "Arial",
)

MONO_FONT_STACK = (
    "JetBrains Mono",
    "Source Code Pro",
    "DejaVu Sans Mono",
    "Liberation Mono",
    "Noto Sans Mono",
    "Courier New",
)


def load_bundled_fonts() -> list[str]:
    """Register the bundled fonts and return the family names Qt exposed."""
    loaded: list[str] = []
    for name in ("InterVariable.ttf", "JetBrainsMono-Regular.ttf"):
        path = FONT_DIR / name
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            continue
        loaded.extend(QFontDatabase.applicationFontFamilies(font_id))
    return loaded


def ui_font(point_size: int = 10) -> QFont:
    """Return the first available UI family, then any proportional face.

    Inter is bundled, so the first entry normally wins. The rest of the stack
    keeps the UI usable when an asset is missing or broken. Resolve explicitly:
    a minimal font set (SLES 16 ships Adwaita and Noto CJK only) makes
    fontconfig resolve a missing "Inter" request to Adwaita Mono, which renders
    the whole UI monospaced.
    """
    families = set(QFontDatabase.families())
    for family in UI_FONT_STACK:
        if family in families:
            return QFont(family, point_size)
    for family in QFontDatabase.families():
        if not QFontInfo(QFont(family)).fixedPitch():
            return QFont(family, point_size)
    return QFont("", point_size)


def mono_font(point_size: int = 10) -> QFont:
    """Return the bundled technical font, then a common monospace face."""
    families = set(QFontDatabase.families())
    for family in MONO_FONT_STACK:
        if family in families:
            return QFont(family, point_size)
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
