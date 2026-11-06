"""Small desktop-notification helper for console events."""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger(__name__)


def notify(title: str, message: str) -> None:
    """Send a notification through the desktop's notification service."""
    command: list[str] | None = None
    notify_send = shutil.which("notify-send")
    kdialog = shutil.which("kdialog")
    zenity = shutil.which("zenity")
    if notify_send:
        command = [
            notify_send,
            "--app-name=v-EMU",
            "--icon=org.vemu.VEMU",
            "--expire-time=5000",
            title,
            message,
        ]
    elif kdialog:
        command = [
            kdialog,
            "--title",
            title,
            "--passivepopup",
            message,
            "5",
        ]
    elif zenity:
        command = [zenity, "--notification", f"--text={title}: {message}"]
    if command is None:
        log.info("%s: %s", title, message)
        return
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        log.warning("cannot send notification: %s", exc)
