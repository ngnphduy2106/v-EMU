"""Global VirtioFS share catalog."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..errors import VemuError
from .delegate import NoFocusDelegate
from .dialogs import VemuDialog, dialog_layout
from .widgets import EmptyStateTable
from .components import form_layout, PageHeader


class ShareCatalogDialog(VemuDialog):
    def __init__(
        self,
        backend,
        item: dict[str, Any] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.item = item
        self.saved: dict[str, Any] | None = None
        self.setWindowTitle("Edit shared folder" if item else "Add shared folder")
        self.resize(580, 360)
        layout = dialog_layout(self)
        title = QLabel(
            "Edit shared folder" if item else "Add shared folder"
        )
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        form = form_layout()
        self.name_edit = QLineEdit(str((item or {}).get("name", "")))
        self.name_edit.setPlaceholderText("Pictures")
        self.name_edit.setEnabled(item is None)
        form.addRow("Name", self.name_edit)
        self.path_edit = QLineEdit(str((item or {}).get("path", "")))
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        form.addRow("Host directory", path_row)
        self.tag_edit = QLineEdit(str((item or {}).get("tag", "")))
        self.tag_edit.setPlaceholderText("Pictures")
        form.addRow("Guest tag", self.tag_edit)
        self.read_only_check = QCheckBox("Read-only by default")
        self.read_only_check.setChecked(bool((item or {}).get("read_only", False)))
        form.addRow("", self.read_only_check)
        self.description_edit = QLineEdit(
            str((item or {}).get("description", ""))
        )
        form.addRow("Description", self.description_edit)
        layout.addLayout(form)
        note = QLabel(
            "This catalog only defines shared folders. Attach a catalog entry "
            "to a VM from Configure Share (VirtioFS). Nothing is attached or "
            "mounted globally."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("Save")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._save)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose host directory", self.path_edit.text()
        )
        if path:
            self.path_edit.setText(path)
            if not self.name_edit.text().strip():
                self.name_edit.setText(Path(path).name)

    def _save(self) -> None:
        name = self.name_edit.text().strip()
        path = self.path_edit.text().strip()
        tag = self.tag_edit.text().strip()
        if not name or not path or not tag:
            QMessageBox.warning(
                self,
                "Shared folder",
                "Name, host directory and guest tag are required.",
            )
            return
        params = {
            "name": name,
            "path": path,
            "tag": tag,
            "read_only": self.read_only_check.isChecked(),
            "description": self.description_edit.text().strip(),
        }
        try:
            if self.item:
                self.saved = self.backend.share_catalog_update(**params)
            else:
                self.saved = self.backend.share_catalog_add(**params)
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot save shared folder", str(exc))
            return
        self.accept()


class SharesPage(QWidget):
    status = Signal(str)

    def __init__(self, backend, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.shares: list[dict[str, Any]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.addWidget(
            PageHeader(
                "Shared folders",
                "Reusable host directories. Attach them per VM; nothing "
                "mounts globally.",
            )
        )
        header.addStretch(1)
        add = QPushButton("Add folder")
        add.setToolTip("Add folder (Ctrl+N)")
        add.setObjectName("PrimaryButton")
        add.clicked.connect(self._add)
        header.addWidget(add)
        refresh = QPushButton("Refresh")
        refresh.setObjectName("SecondaryButton")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        layout.addLayout(header)

        self.table = EmptyStateTable(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["NAME", "HOST PATH", "GUEST TAG", "MODE"]
        )
        self.table.setItemDelegate(NoFocusDelegate(self.table))
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.horizontalHeader().setFixedHeight(32)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(0, 180)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(2, 160)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(3, 100)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.itemSelectionChanged.connect(self._selection)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.edit_button = QPushButton("Edit")
        self.edit_button.setObjectName("SecondaryButton")
        self.edit_button.setEnabled(False)
        self.edit_button.clicked.connect(self._edit)
        actions.addWidget(self.edit_button)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("SecondaryButton")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove)
        actions.addWidget(self.remove_button)
        actions.addStretch(1)
        note = QLabel(
            "To attach: select a VM → right-click → Configure Share "
            "(VirtioFS)…"
        )
        note.setObjectName("Muted")
        actions.addWidget(note)
        layout.addLayout(actions)

    def refresh(self) -> None:
        try:
            self.shares = self.backend.shares_list()
        except (VemuError, AttributeError) as exc:
            self.shares = []
            self.table.setRowCount(0)
            self.table.set_empty_state("Cannot load shared folders", str(exc))
            self.table.refresh_empty_state()
            self.status.emit(str(exc))
            return
        self.table.setRowCount(0)
        for item in self.shares:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                item.get("name", ""),
                item.get("path", ""),
                item.get("tag", ""),
                "read-only" if item.get("read_only") else "read-write",
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, column, cell)
        if not self.shares:
            self.table.set_empty_state(
                "No shared folders",
                "Add a host directory once, then attach it to a VM.",
            )
        self.table.refresh_empty_state()
        self._selection()
        self.status.emit(f"{len(self.shares)} shared folder(s)")

    def _selected(self) -> dict[str, Any] | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        return next(
            (share for share in self.shares if share.get("name") == item.text()),
            None,
        )

    def _selection(self) -> None:
        selected = self._selected() is not None
        self.edit_button.setEnabled(selected)
        self.remove_button.setEnabled(selected)

    def _add(self) -> None:
        dialog = ShareCatalogDialog(self.backend, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def _edit(self) -> None:
        item = self._selected()
        if item is None:
            return
        dialog = ShareCatalogDialog(self.backend, item, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def _remove(self) -> None:
        item = self._selected()
        if item is None:
            return
        answer = QMessageBox.question(
            self,
            "Remove shared folder",
            f"Remove '{item['name']}' from the catalog? "
            "VM attachments are removed from the catalog as well; existing "
            "VM devices must be detached separately.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.share_catalog_remove(str(item["name"]))
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot remove shared folder", str(exc))
            return
        self.refresh()
