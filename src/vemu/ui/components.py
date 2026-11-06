"""Small reusable UI components built on the design tokens."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .icons import make_icon
from .tokens import SPACE


def form_layout(parent: QWidget | None = None) -> QFormLayout:
    """Consistent two-column form grid for dialogs and pages."""
    form = QFormLayout(parent)
    form.setHorizontalSpacing(SPACE.md)
    form.setVerticalSpacing(SPACE.md)
    form.setLabelAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    form.setFormAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
    )
    form.setFieldGrowthPolicy(
        QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
    )
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    return form



class InfoBadge(QLabel):
    """Small Clarity-style info/alert badge with a hover tooltip."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "info",
        color: str = "#5f6b6b",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("InfoBadge")
        self.setFixedSize(16, 16)
        self.setScaledContents(True)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.set_message(message, kind=kind, color=color)

    def set_message(
        self,
        message: str,
        *,
        kind: str | None = None,
        color: str | None = None,
    ) -> None:
        if kind is not None:
            self._kind = kind
        if color is not None:
            self._color = color
        self.setToolTip(message)
        icon = make_icon(
            getattr(self, "_kind", "info"),
            getattr(self, "_color", "#5f6b6b"),
        )
        self.setPixmap(icon.pixmap(16, 16))


class FieldLabel(QWidget):
    """Form label with an optional badge whose details live in a tooltip."""

    def __init__(
        self,
        text: str,
        message: str = "",
        *,
        kind: str = "info",
        color: str = "#5f6b6b",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE.xs)
        self.label = QLabel(text)
        layout.addWidget(self.label)
        self.badge = InfoBadge(
            message, kind=kind, color=color, parent=self
        )
        self.badge.setVisible(bool(message))
        layout.addWidget(self.badge)
        layout.addStretch(1)



class PageHeader(QWidget):
    """Consistent page title + subtitle block for the content header row."""

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE.xxs)
        self.title = QLabel(title)
        self.title.setObjectName("PageTitle")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("PageSubtitle")
        # Page subtitles are one-line descriptive copy. Word wrap made them
        # collapse to two lines as soon as action buttons shared the header
        # row, even at the 1280 px minimum window width.
        self.subtitle.setWordWrap(False)
        self.subtitle.setVisible(bool(subtitle))
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
