"""Qt desktop shell for v-EMU."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def configure_webengine_environment() -> None:
    """Avoid Chromium GPU command-buffer flashes on virtio-gpu/Wayland.

    Users with a working WebEngine GPU path can set VEMU_WEBENGINE_GPU=1.
    """
    if os.environ.get("VEMU_WEBENGINE_GPU") == "1":
        return
    current = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    additions = [
        flag
        for flag in (
            "--disable-gpu",
            "--disable-gpu-compositing",
            "--disable-accelerated-video-decode",
        )
        if flag not in current
    ]
    if additions:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(
            [current, *additions]
        ).strip()


configure_webengine_environment()


def configure_display_environment() -> None:
    """Pin UI scaling and font rendering across desktop environments.

    The layout targets a 1280x720 minimum and assumes 1:1 device pixels.
    Letting each desktop apply its own scale factor makes the same window
    look different under GNOME, KDE and COSMIC, so scaling is pinned to 1.
    ``VEMU_UI_SCALE`` can still override it for unusually dense displays.

    Full hinting with grayscale antialiasing is forced for the same reason:
    otherwise the host desktop's font configuration leaks into the
    application and text metrics change between sessions.
    """
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "0")
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
    os.environ.setdefault(
        "QT_SCALE_FACTOR", os.environ.get("VEMU_UI_SCALE", "1")
    )
    os.environ.setdefault("QT_XFT_HINTSTYLE", "hintfull")
    os.environ.setdefault("QT_XFT_ANTIALIAS", "1")
    os.environ.setdefault("QT_XFT_RGBA", "none")


configure_display_environment()


def configure_dialog_environment() -> None:
    """Prefer the desktop's native file chooser over the Fusion-styled one.

    Qt only uses the portal/native file dialog when a platform theme plugin is
    active. The app forces Fusion for consistent widgets, so select a platform
    theme explicitly before QApplication is created. Set
    VEMU_NATIVE_DIALOGS=0 to keep the fully styled Qt dialog.
    """
    if os.environ.get("VEMU_NATIVE_DIALOGS") == "0":
        return
    if os.environ.get("QT_QPA_PLATFORMTHEME"):
        return
    if not sys.platform.startswith("linux"):
        return
    platform = os.environ.get("QT_QPA_PLATFORM", "")
    if platform.startswith(("offscreen", "minimal")):
        return
    desktop = (
        os.environ.get("XDG_CURRENT_DESKTOP", "")
        or os.environ.get("XDG_SESSION_DESKTOP", "")
    ).lower()
    # The portal dialog runs out of process, so the application stylesheet
    # (dark theme) cannot leak into it. The in-process KDE helper inherits
    # the QSS and makes alternating rows unreadable on a light desktop theme.
    candidates: list[str] = ["xdgdesktopportal"]
    if "kde" in desktop or "plasma" in desktop:
        candidates.append("kde")
    candidates.append("gtk3")
    patterns = {
        "kde": "*KDEPlasmaPlatformTheme*",
        "xdgdesktopportal": "*xdgdesktopportal*",
        "gtk3": "*gtk3*",
    }
    plugin_dirs = [
        Path("/usr/lib64/qt6/plugins/platformthemes"),
        Path("/usr/lib/qt6/plugins/platformthemes"),
    ]
    try:
        from PySide6.QtCore import QLibraryInfo

        bundled = Path(
            QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)
        ) / "platformthemes"
        if bundled.is_dir():
            plugin_dirs.append(bundled)
    except Exception:  # PySide6 missing or plugin path unavailable
        pass
    for name in dict.fromkeys(candidates):
        pattern = patterns[name]
        if any(base.is_dir() and any(base.glob(pattern)) for base in plugin_dirs):
            os.environ["QT_QPA_PLATFORMTHEME"] = name
            return


configure_dialog_environment()
