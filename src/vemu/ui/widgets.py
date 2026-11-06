"""Small reusable widgets for the v-EMU desktop shell."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)


# Keep combo boxes the same height as line edits and spin boxes.
FIELD_HEIGHT = 32


class ComboBox(QComboBox):
    """QComboBox with a stable height and without content-driven shrinking.

    Qt recomputes a combo's size hint whenever its items change. Inside dense
    form layouts that can squeeze the widget until the text and arrow are
    clipped, especially after rebuilding the item list from capabilities.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(FIELD_HEIGHT)
        self.setMinimumContentsLength(8)
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )


class EmptyStateTable(QTableWidget):
    """QTableWidget with a centered placeholder when no rows are visible."""

    def __init__(self, rows: int, columns: int, parent=None) -> None:
        super().__init__(rows, columns, parent)
        self._empty = QWidget(self.viewport())
        self._empty.setObjectName("EmptyState")
        self._empty.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        layout = QVBoxLayout(self._empty)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_title = QLabel()
        self._empty_title.setObjectName("EmptyStateTitle")
        self._empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_title.setWordWrap(True)
        self._empty_title.setMinimumWidth(240)
        self._empty_title.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._empty_body = QLabel()
        self._empty_body.setObjectName("EmptyStateBody")
        self._empty_body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_body.setWordWrap(True)
        self._empty_body.setMinimumWidth(240)
        self._empty_body.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        layout.addWidget(self._empty_title)
        layout.addWidget(self._empty_body)
        self._empty.hide()
        self.viewport().installEventFilter(self)

    def set_empty_state(self, title: str, body: str = "") -> None:
        self._empty_title.setText(title)
        self._empty_body.setText(body)
        self._empty_body.setVisible(bool(body))
        self._position_empty()

    def refresh_empty_state(self, visible_rows: int | None = None) -> None:
        if visible_rows is None:
            visible_rows = sum(
                0 if self.isRowHidden(row) else 1
                for row in range(self.rowCount())
            )
        self._empty.setVisible(visible_rows == 0)
        if visible_rows == 0:
            self._position_empty()

    def _position_empty(self) -> None:
        self._empty.setGeometry(self.viewport().rect())

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if watched is self.viewport() and event.type() == QEvent.Type.Resize:
            self._position_empty()
        return super().eventFilter(watched, event)
