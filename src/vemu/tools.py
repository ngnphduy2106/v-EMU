"""Locate host tools consistently across distributions.

Some distributions (notably openSUSE) do not put ``/usr/sbin`` or ``/sbin``
in a regular user's PATH. The daemon runs as root and finds those tools, but
the direct CLI and the test suite must also locate ``sfdisk``, ``mkfs.vfat``,
``restorecon`` and friends.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_EXTRA_DIRS = ("/usr/sbin", "/sbin", "/usr/libexec")


def which(tool: str) -> str | None:
    """Return an executable path, falling back to the system sbin dirs."""
    path = shutil.which(tool)
    if path:
        return path
    for base in _EXTRA_DIRS:
        candidate = Path(base) / tool
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def find(tools: tuple[str, ...]) -> str | None:
    """Return the first available tool from *tools*."""
    for tool in tools:
        path = which(tool)
        if path:
            return path
    return None
