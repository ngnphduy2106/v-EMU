"""Shared dialog base for consistent v-EMU styling."""

from __future__ import annotations

import html as html_module

from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtGui import QGuiApplication, QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QTextBrowser,
    QVBoxLayout,
)

from .tokens import COMPONENT, SPACE, TYPE


def dialog_layout(widget) -> QVBoxLayout:
    """Standard body layout for dialogs and modal panels."""
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(
        COMPONENT.dialog_padding,
        COMPONENT.dialog_padding,
        COMPONENT.dialog_padding,
        COMPONENT.dialog_padding,
    )
    layout.setSpacing(SPACE.md)
    return layout


class VemuDialog(QDialog):
    """Independent, non-modal dialog that the window manager can move.

    Qt makes a dialog with a parent a transient child of its owner window.
    Some window managers, GNOME/Mutter in particular, then attach the dialog
    to the main window, so dragging the dialog also drags the whole
    application. Creating the dialog as an independent top-level window
    avoids that. Dialogs are non-modal so the main window, console and other
    dialogs can be used together; ``exec()`` keeps the synchronous return
    value without application modality.
    """

    def __init__(self, parent=None, *, title: str = "") -> None:
        super().__init__(None, Qt.WindowType.Dialog)
        self._owner = parent
        self._centred = False
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        if title:
            self.setWindowTitle(title)

    def exec(self) -> int:
        """Run a synchronous local event loop without blocking other windows."""
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        loop = QEventLoop()
        self.finished.connect(loop.quit)
        try:
            self.show()
            self.raise_()
            self.activateWindow()
            loop.exec()
        finally:
            self.finished.disconnect(loop.quit)
        return int(self.result())

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._centred:
            self._centred = True
            self._centre_on_owner()

    def _centre_on_owner(self) -> None:
        owner = self._owner
        screen = owner.screen() if owner is not None else None
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        reference = (
            owner.frameGeometry() if owner is not None else available
        )
        geometry = self.frameGeometry()
        geometry.moveCenter(reference.center())
        left = max(
            available.left(),
            min(geometry.left(), available.right() - geometry.width() + 1),
        )
        top = max(
            available.top(),
            min(geometry.top(), available.bottom() - geometry.height() + 1),
        )
        self.move(left, top)
        self.raise_()


class ConfirmDialog(QDialog):
    """Modal confirmation with full-size action buttons.

    ``QMessageBox`` sizes itself from its message text and then squeezes
    custom buttons, which clips their labels. Destructive confirmations stay
    modal, so this dialog keeps the standard design layout and lets every
    button keep the width it needs for its label.
    """

    def __init__(
        self,
        parent,
        *,
        title: str,
        heading: str,
        body: str = "",
        actions: tuple[tuple[str, int, str], ...],
        default_index: int = 0,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedWidth(COMPONENT.confirm_width)
        layout = dialog_layout(self)

        header = QHBoxLayout()
        header.setSpacing(SPACE.md)
        icon = QLabel()
        icon.setFixedSize(28, 28)
        icon.setPixmap(
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
            .pixmap(28, 28)
        )
        header.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        title_label = QLabel(heading)
        title_label.setObjectName("DialogTitle")
        title_label.setWordWrap(True)
        title_label.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(title_label, 1)
        layout.addLayout(header)

        if body:
            body_label = QLabel(body)
            body_label.setWordWrap(True)
            body_label.setTextFormat(Qt.TextFormat.PlainText)
            body_label.setStyleSheet(f"font-size: {TYPE.body}px;")
            body_label.setFixedWidth(
                COMPONENT.confirm_width
                - 2 * COMPONENT.dialog_padding
                - 28
                - SPACE.md
            )
            body_row = QHBoxLayout()
            body_row.setContentsMargins(28 + SPACE.md, 0, 0, 0)
            body_row.addWidget(body_label)
            layout.addLayout(body_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(SPACE.sm)
        buttons.addStretch(1)
        for index, (label, code, object_name) in enumerate(actions):
            button = QPushButton(label)
            if object_name:
                button.setObjectName(object_name)
            button.setAutoDefault(False)
            button.setMinimumWidth(max(96, button.sizeHint().width()))
            button.clicked.connect(
                lambda _checked=False, value=code: self.done(value)
            )
            if index == default_index:
                button.setDefault(True)
                button.setFocus()
            buttons.addWidget(button)
        layout.addLayout(buttons)


def _report_html(changes: tuple[str, ...], warnings: tuple[str, ...]) -> str:
    sections: list[str] = []
    for heading, lines in (("Changes", changes), ("Warnings", warnings)):
        if not lines:
            continue
        body = "<br>".join(
            f"• {html_module.escape(str(line))}" for line in lines
        )
        sections.append(
            f"<p style='margin:0 0 4px 0;'><b>{heading}</b></p>"
            f"<p style='margin:0 0 10px 0;'>{body}</p>"
        )
    return "".join(sections) or "<p style='margin:0;'>No changes needed.</p>"


class ReportDialog(VemuDialog):
    """Read-only report with consistent margins and scrollable body.

    QMessageBox reflows its label differently from the rest of the dialogs
    and gets very tall for lab reports. This dialog keeps the standard v-EMU
    body margins, caps the height and scrolls long reports.
    """

    def __init__(
        self,
        parent,
        title: str,
        *,
        changes: tuple[str, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> None:
        super().__init__(parent, title=title)
        layout = dialog_layout(self)

        header = QHBoxLayout()
        header.setSpacing(SPACE.sm)
        icon = QLabel()
        icon.setFixedSize(24, 24)
        icon.setPixmap(
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
            .pixmap(24, 24)
        )
        header.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        heading = QLabel(title)
        heading.setObjectName("DialogTitle")
        header.addWidget(heading, 1)
        layout.addLayout(header)

        self.browser = QTextBrowser()
        self.browser.setObjectName("ReportBrowser")
        self.browser.setFrameShape(QFrame.Shape.NoFrame)
        self.browser.setOpenExternalLinks(False)
        self.browser.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.browser.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.browser.document().setDocumentMargin(0)
        self.browser.setHtml(_report_html(changes, warnings))

        longest = max(
            (len(str(line)) for line in (*changes, *warnings)),
            default=40,
        )
        width = max(360, min(640, longest * 7 + 80))
        self.browser.setFixedWidth(width)
        self.browser.document().setTextWidth(width - 8)
        height = int(self.browser.document().size().height()) + 8
        self.browser.setFixedHeight(max(48, min(480, height)))
        layout.addWidget(self.browser)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
