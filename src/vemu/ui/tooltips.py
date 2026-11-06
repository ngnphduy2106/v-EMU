"""Consistent, compact tooltips for the desktop UI.

Design: one short title line, then at most a single-sentence detail. Long
technical notes belong in Help/Manual. Text is wrapped to a fixed width and
capped at four lines so a tooltip never spans the whole window.
"""

from __future__ import annotations

import textwrap

from PySide6.QtCore import QEvent, QObject, QPoint
from PySide6.QtWidgets import QToolTip, QWidget

TOOLTIP_WRAP = 56
TOOLTIP_MAX_LINES = 4
TOOLTIP_MAX_CHARS = 240


def compact_tooltip(text: str) -> str:
    """Normalize and wrap tooltip text for the shared tooltip style."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    if "\n" in raw:
        title, detail = raw.split("\n", 1)
    else:
        title, detail = "", raw
    title = " ".join(title.split())
    detail = " ".join(detail.split())
    if len(title) + len(detail) > TOOLTIP_MAX_CHARS:
        room = max(24, TOOLTIP_MAX_CHARS - len(title) - 1)
        detail = detail[:room].rsplit(" ", 1)[0].rstrip(" .,;:") + "…"
    lines: list[str] = []
    if title:
        lines.extend(
            textwrap.wrap(
                title,
                width=TOOLTIP_WRAP,
                break_long_words=True,
                break_on_hyphens=False,
            )
        )
    if detail:
        lines.extend(
            textwrap.wrap(
                detail,
                width=TOOLTIP_WRAP,
                break_long_words=True,
                break_on_hyphens=False,
            )
        )
    if not lines:
        return ""
    if len(lines) > TOOLTIP_MAX_LINES:
        lines = lines[:TOOLTIP_MAX_LINES]
        lines[-1] = lines[-1].rstrip(" .,;:") + "…"
    return "\n".join(lines)


def tip(title: str, detail: str | None = None) -> str:
    title = " ".join(str(title or "").split())
    if not detail:
        return compact_tooltip(title)
    return compact_tooltip(f"{title}\n{detail}")


class _AboveTooltipFilter(QObject):
    """Show icon-button tooltips above the button, not over the button row."""

    def __init__(self, widget: QWidget, text: str) -> None:
        super().__init__(widget)
        self.widget = widget
        self.text = text

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt API
        if event.type() == QEvent.Type.ToolTip:
            pos = self.widget.mapToGlobal(
                QPoint(self.widget.width() // 2, -8)
            )
            QToolTip.showText(pos, self.text)
            return True
        if event.type() in (
            QEvent.Type.Leave,
            QEvent.Type.MouseButtonPress,
        ):
            QToolTip.hideText()
        return False


def attach_tooltip(widget: QWidget, text: str) -> None:
    text = compact_tooltip(text)
    widget.setToolTip(text)
    filt = _AboveTooltipFilter(widget, text)
    widget.installEventFilter(filt)
    widget._vemu_tooltip_filter = filt  # type: ignore[attr-defined]


def set_tooltip(widget: QWidget, text: str) -> None:
    text = compact_tooltip(text)
    widget.setToolTip(text)
    filt = getattr(widget, "_vemu_tooltip_filter", None)
    if filt is None:
        attach_tooltip(widget, text)
    else:
        filt.text = text
