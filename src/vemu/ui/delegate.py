"""Shared table delegates."""

from __future__ import annotations

from PySide6.QtWidgets import QStyleOptionViewItem, QStyledItemDelegate, QStyle


class NoFocusDelegate(QStyledItemDelegate):
    """Draw rows without the dotted focus rectangle around the current cell."""

    def paint(self, painter, option, index) -> None:  # type: ignore[override]
        clean = QStyleOptionViewItem(option)
        clean.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter, clean, index)
