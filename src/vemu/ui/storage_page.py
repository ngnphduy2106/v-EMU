"""Storage locations page."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..errors import VemuError
from .delegate import NoFocusDelegate
from .dialogs import VemuDialog, dialog_layout
from .tooltips import set_tooltip
from .widgets import ComboBox, EmptyStateTable
from .components import PageHeader
from .tokens import TYPE


def _format_location(location: dict[str, Any]) -> str:
    selinux = location.get("selinux", {})
    access = location.get("qemu_access", {})
    lines = [
        f"<b style='font-size:{TYPE.body_lg}px'>{location.get('path', '')}</b><br>",
        f"Role: {location.get('role')} · FS: {location.get('filesystem')}<br>",
        f"Free: {location.get('free_bytes', 0) / (1024 ** 3):.1f} GB<br>",
        f"Owner: {location.get('owner')} · Mode: {location.get('mode')}<br>",
        f"SELinux: {selinux.get('mode')} · {selinux.get('label') or 'unknown'} "
        f"(expected {selinux.get('expected')})<br>",
        f"qemu access: traverse={access.get('traverse')} "
        f"read={access.get('read')} write={access.get('write')}<br>",
        f"nodatacow: {location.get('nodatacow')}",
    ]
    issues = location.get("issues") or []
    if issues:
        lines.append("<br><br><b>Issues</b>")
        lines.extend(f"<br>· {issue}" for issue in issues)
    warnings = location.get("warnings") or []
    if warnings:
        lines.append("<br><br><b>Warnings</b>")
        lines.extend(f"<br>· {warning}" for warning in warnings)
    return "".join(lines)


class _StorageAddTask(QThread):
    done = Signal(object, object)

    def __init__(
        self, backend, path: str, role: str, *, dry_run: bool
    ) -> None:
        super().__init__()
        self._backend = backend
        self._path = path
        self._role = role
        self._dry_run = dry_run

    def run(self) -> None:
        try:
            payload = self._backend.storage_add(
                self._path, self._role, dry_run=self._dry_run
            )
        except Exception as exc:  # noqa: BLE001 - forwarded to the dialog
            self.done.emit(None, exc)
        else:
            self.done.emit(payload, None)


class StorageAddDialog(VemuDialog):
    def __init__(self, backend, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self._task: _StorageAddTask | None = None
        self._task_mode = "preview"
        self.setWindowTitle("Add storage location")
        self.resize(680, 520)

        layout = dialog_layout(self)
        title = QLabel("Add storage location")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        helper = QLabel(
            "Preview checks permissions, SELinux labels and free space; "
            "Apply registers the location."
        )
        helper.setObjectName("Muted")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("/home/VM/vemu")
        path_row.addWidget(self.path_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        role_row = QHBoxLayout()
        role_row.addWidget(QLabel("Role"))
        self.role_combo = ComboBox()
        self.role_combo.addItems(["labs", "templates", "isos"])
        role_row.addWidget(self.role_combo)
        role_row.addStretch(1)
        layout.addLayout(role_row)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText(
            "Preview shows the actions that will be applied."
        )
        layout.addWidget(self.output, 1)

        buttons = QHBoxLayout()
        self.preview_button = QPushButton("Preview")
        self.preview_button.setObjectName("SecondaryButton")
        self.preview_button.clicked.connect(self._preview)
        buttons.addWidget(self.preview_button)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.apply_button = QPushButton("Apply")
        self.apply_button.setObjectName("PrimaryButton")
        self.apply_button.clicked.connect(self._apply)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)

    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose directory")
        if directory:
            self.path_edit.setText(directory)

    def _payload_text(self, payload: dict[str, Any]) -> str:
        location = payload["location"]
        actions = payload.get("actions", [])
        lines = [f"Path: {location['path']}", f"Role: {location['role']}"]
        if actions:
            lines.append("")
            lines.append("Actions:")
            for action in actions:
                state = "applied" if action.get("applied") else "planned"
                lines.append(
                    f"  [{state}] {action['description']}: "
                    f"{' '.join(action['command'])}"
                )
        issues = location.get("issues") or []
        if issues:
            lines.append("")
            lines.append("Issues:")
            lines.extend(f"  - {issue}" for issue in issues)
        warnings = location.get("warnings") or []
        if warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend(f"  - {warning}" for warning in warnings)
        return "\n".join(lines)

    def _start_task(self, *, dry_run: bool) -> None:
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "Storage", "Enter a directory path.")
            return
        if self._task is not None:
            return
        self._task_mode = "preview" if dry_run else "apply"
        self.output.setPlainText(
            "Inspecting storage permissions, SELinux labels and free space…\n"
            "Large directory trees can take a moment."
            if dry_run
            else "Applying storage permissions and SELinux labels…\n"
            "This can take a moment on large directory trees."
        )
        self.preview_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._task = _StorageAddTask(
            self.backend,
            path,
            self.role_combo.currentText(),
            dry_run=dry_run,
        )
        self._task.done.connect(self._task_done)
        self._task.finished.connect(self._task_finished)
        self._task.start()

    def _preview(self) -> None:
        self._start_task(dry_run=True)

    def _apply(self) -> None:
        self._start_task(dry_run=False)

    def _task_done(self, payload, error) -> None:
        if error is not None:
            title = (
                "Preview failed"
                if self._task_mode == "preview"
                else "Add failed"
            )
            QMessageBox.critical(self, title, str(error))
            return
        self.output.setPlainText(self._payload_text(payload))
        if self._task_mode == "apply":
            self.accept()

    def _task_finished(self) -> None:
        QApplication.restoreOverrideCursor()
        self.preview_button.setEnabled(True)
        self.apply_button.setEnabled(True)
        if self._task is not None:
            self._task.deleteLater()
        self._task = None


class StoragePage(QWidget):
    status = Signal(str)

    def __init__(self, backend, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.locations: list[dict[str, Any]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.addWidget(
            PageHeader(
                "Storage",
                "Locations for templates, lab disks and ISOs.",
            )
        )
        header.addStretch(1)

        self.add_button = QPushButton("Add location")
        self.add_button.setToolTip("Add location (Ctrl+N)")
        self.add_button.setObjectName("PrimaryButton")
        self.add_button.clicked.connect(self._add)
        header.addWidget(self.add_button)

        refresh = QPushButton("Refresh")
        refresh.setObjectName("SecondaryButton")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        layout.addLayout(header)

        self.table = EmptyStateTable(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["PATH", "ROLE", "FS", "FREE", "SELINUX", "HEALTH"]
        )
        for column, alignment in enumerate(
            (
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
            )
        ):
            header_item = self.table.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setTextAlignment(
                    alignment | Qt.AlignmentFlag.AlignVCenter
                )
        self.table.setItemDelegate(NoFocusDelegate(self.table))
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.horizontalHeader().setFixedHeight(32)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(1, 90)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(2, 80)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(3, 100)
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(5, 90)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.table, 1)

        self.details = QLabel("Select a storage location to inspect it.")
        self.details.setObjectName("Muted")
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        layout.addWidget(self.details)

        actions = QHBoxLayout()
        self.files_button = QPushButton("Manage files…")
        self.files_button.setObjectName("SecondaryButton")
        self.files_button.setEnabled(False)
        set_tooltip(
            self.files_button,
            "Add or delete ISOs/images; referenced images are protected.",
        )
        self.files_button.clicked.connect(self._manage_files)
        actions.addWidget(self.files_button)
        self.clean_button = QPushButton("Clean garbage…")
        self.clean_button.setObjectName("SecondaryButton")
        self.clean_button.setEnabled(False)
        self.clean_button.setToolTip(
            "Review unreferenced images and orphan chains, then delete them."
        )
        self.clean_button.clicked.connect(self._clean_garbage)
        actions.addWidget(self.clean_button)
        self.fix_button = QPushButton("Fix labels and access")
        self.fix_button.setObjectName("SecondaryButton")
        self.fix_button.setEnabled(False)
        self.fix_button.clicked.connect(self._fix)
        actions.addWidget(self.fix_button)
        self.remove_button = QPushButton("Remove from registry")
        self.remove_button.setObjectName("SecondaryButton")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove)
        actions.addWidget(self.remove_button)
        actions.addStretch(1)
        layout.addLayout(actions)

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        if not hasattr(self.backend, "storage_list"):
            self.locations = []
            self.details.setText(
                "Storage management requires the v-EMU daemon."
            )
            self.add_button.setEnabled(False)
            self.fix_button.setEnabled(False)
            self.files_button.setEnabled(False)
            self.clean_button.setEnabled(False)
            self.remove_button.setEnabled(False)
            self.table.setRowCount(0)
            self.table.set_empty_state(
                "Storage needs vemud",
                "Start the v-EMU daemon to add and repair storage locations.",
            )
            self.table.refresh_empty_state(0)
            return
        selected = self._selected_path()
        try:
            self.locations = self.backend.storage_list()
        except VemuError as exc:
            self.locations = []
            self.details.setText(str(exc))
            self.fix_button.setEnabled(False)
            self.clean_button.setEnabled(False)
            self.remove_button.setEnabled(False)
            self.table.setRowCount(0)
            self.table.set_empty_state(
                "Cannot load storage locations", str(exc)
            )
            self.table.refresh_empty_state(0)
            return

        self.table.setRowCount(0)
        for location in self.locations:
            row = self.table.rowCount()
            self.table.insertRow(row)
            selinux = location.get("selinux", {})
            values = [
                location.get("path", ""),
                location.get("role", ""),
                location.get("filesystem", ""),
                f"{location.get('free_bytes', 0) / (1024 ** 3):.1f} GB",
                f"{selinux.get('mode', '')}:{selinux.get('label') or '-'}",
                "yes" if location.get("healthy") else "no",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                alignment = Qt.AlignmentFlag.AlignLeft
                item.setTextAlignment(
                    alignment | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, column, item)
        if not self.locations:
            self.fix_button.setEnabled(False)
            self.files_button.setEnabled(False)
            self.clean_button.setEnabled(False)
            self.remove_button.setEnabled(False)
            self.table.set_empty_state(
                "No storage locations yet",
                "Add a directory for lab disks, templates or ISOs.",
            )
        self.table.refresh_empty_state()
        if selected:
            self._select_path(selected)

    def _selected_path(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.text() if item is not None else None

    def _select_path(self, path: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == path:
                self.table.selectRow(row)
                return

    def _selection_changed(self) -> None:
        path = self._selected_path()
        location = next(
            (item for item in self.locations if item.get("path") == path), None
        )
        if location is None:
            self.details.setText("Select a storage location to inspect it.")
            self.fix_button.setEnabled(False)
            self.files_button.setEnabled(False)
            self.clean_button.setEnabled(False)
            self.remove_button.setEnabled(False)
            return
        self.details.setText(_format_location(location))
        self.fix_button.setEnabled(True)
        self.files_button.setEnabled(hasattr(self.backend, "storage_files"))
        self.clean_button.setEnabled(hasattr(self.backend, "storage_files"))
        self.remove_button.setEnabled(True)

    # ------------------------------------------------------------------ actions

    def _build_context_menu(self, location: dict[str, Any] | None) -> QMenu:
        menu = QMenu(self)
        menu.addAction("Add location…", self._add)
        menu.addSeparator()
        files = menu.addAction("Manage files…", self._manage_files)
        clean = menu.addAction("Clean garbage…", self._clean_garbage)
        fix = menu.addAction("Fix labels and access", self._fix)
        remove = menu.addAction("Remove from registry", self._remove)
        has_location = location is not None
        files.setEnabled(
            has_location and hasattr(self.backend, "storage_files")
        )
        clean.setEnabled(
            has_location and hasattr(self.backend, "storage_files")
        )
        fix.setEnabled(has_location)
        remove.setEnabled(has_location)
        menu.addSeparator()
        copy = menu.addAction(
            "Copy path",
            lambda: QGuiApplication.clipboard().setText(
                location.get("path", "") if location else ""
            ),
        )
        copy.setEnabled(has_location)
        return menu

    def _show_context_menu(self, position) -> None:
        row = self.table.rowAt(position.y())
        if row < 0:
            return
        self.table.selectRow(row)
        item = self.table.item(row, 0)
        path = item.text() if item is not None else None
        location = next(
            (entry for entry in self.locations if entry.get("path") == path),
            None,
        )
        self._build_context_menu(location).exec(
            self.table.viewport().mapToGlobal(position)
        )

    def _add(self) -> None:
        dialog = StorageAddDialog(self.backend, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            path = dialog.path_edit.text().strip()
            self.status.emit(
                f"Storage location added: {path}" if path else "Storage location added."
            )

    def _manage_files(self) -> None:
        path = self._selected_path()
        if not path:
            return
        location = next(
            (item for item in self.locations if item.get("path") == path), None
        )
        if location is None:
            return
        dialog = StorageFilesDialog(self.backend, location, self)
        dialog.exec()
        self.refresh()
        self._select_path(path)

    def _clean_garbage(self) -> None:
        path = self._selected_path()
        if not path:
            return
        location = next(
            (item for item in self.locations if item.get("path") == path), None
        )
        if location is None:
            return
        if location.get("role") == "templates":
            QMessageBox.information(
                self,
                "Clean garbage",
                "Template locations are cleaned from the Templates page.",
            )
            return
        try:
            payload = self.backend.storage_files(path)
        except VemuError as exc:
            QMessageBox.critical(self, "Clean garbage", str(exc))
            return
        entries = list(payload.get("files") or []) + list(
            payload.get("chains") or []
        )
        garbage = [item for item in entries if item.get("deletable")]
        if not garbage:
            QMessageBox.information(
                self,
                "Clean garbage",
                "No unreferenced images or orphan chains were found.",
            )
            return
        dialog = StorageGarbageDialog(
            self.backend, location, garbage, self
        )
        dialog.exec()
        self.refresh()
        self._select_path(path)
        if dialog.deleted:
            self.status.emit(
                f"Deleted {dialog.deleted} unreferenced item(s) from {path}"
            )

    def _fix(self) -> None:
        path = self._selected_path()
        if not path:
            return
        location = next(
            (item for item in self.locations if item.get("path") == path), None
        )
        if location is None:
            return
        answer = QMessageBox.question(
            self,
            "Fix storage",
            f"Apply SELinux/permission fixes to {path}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            payload = self.backend.storage_repair(path, location["role"])
        except VemuError as exc:
            QMessageBox.critical(self, "Repair failed", str(exc))
            return
        self.details.setText(_format_location(payload["location"]))
        self.refresh()
        self.status.emit(f"Fixed labels and access: {path}")

    def _remove(self) -> None:
        path = self._selected_path()
        if not path:
            return
        answer = QMessageBox.question(
            self,
            "Remove storage location",
            f"Remove {path} from the v-EMU registry? Files are not deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.storage_remove(path)
        except VemuError as exc:
            QMessageBox.critical(self, "Remove failed", str(exc))
            return
        self.refresh()
        self.status.emit(f"Removed from registry: {path} (files kept)")


def _format_size(size: int | float) -> str:
    value = float(size or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


class StorageGarbageDialog(VemuDialog):
    """Select and delete unreferenced images and orphan chain directories."""

    def __init__(
        self, backend, location: dict[str, Any], entries, parent=None
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.location = location
        self.entries = [
            dict(item) for item in entries if item.get("deletable")
        ]
        self.deleted = 0
        path = str(location.get("path", ""))
        self.setWindowTitle(f"Clean garbage · {path}")
        self.resize(820, 520)

        layout = dialog_layout(self)
        title = QLabel("Clean garbage")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        note = QLabel(
            f"{path} · only images and chains with no VM, backing-file or "
            "template reference are listed."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["", "NAME", "TYPE", "SIZE", "STATUS"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(0, 34)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        for column, width in ((2, 90), (3, 110)):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Fixed
            )
            self.table.horizontalHeader().resizeSection(column, width)
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table, 1)

        for entry in self.entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QTableWidgetItem("")
            check.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
            )
            check.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check)
            for column, value in enumerate(
                [
                    entry.get("name", ""),
                    entry.get("kind", ""),
                    _format_size(entry.get("size", 0)),
                    entry.get("status", ""),
                ],
                start=1,
            ):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, column, item)
        self.table.itemChanged.connect(lambda _item: self._sync())

        buttons = QHBoxLayout()
        select_all = QPushButton("Select all")
        select_all.setObjectName("SecondaryButton")
        select_all.clicked.connect(self._select_all)
        buttons.addWidget(select_all)
        clear = QPushButton("Clear")
        clear.setObjectName("SecondaryButton")
        clear.clicked.connect(self._clear)
        buttons.addWidget(clear)
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        buttons.addWidget(self.status)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.delete_button = QPushButton("Delete selected")
        self.delete_button.setObjectName("PrimaryButton")
        self.delete_button.clicked.connect(self._delete)
        buttons.addWidget(self.delete_button)
        layout.addLayout(buttons)
        self._sync()

    def _checked(self) -> list[dict[str, Any]]:
        selected = []
        for row, entry in enumerate(self.entries):
            item = self.table.item(row, 0)
            if (
                item is not None
                and item.checkState() == Qt.CheckState.Checked
            ):
                selected.append(entry)
        return selected

    def _sync(self) -> None:
        count = len(self._checked())
        self.delete_button.setEnabled(bool(count))
        self.status.setText(
            f"{count} of {len(self.entries)} selected"
            if count
            else "Select items to delete"
        )

    def _select_all(self) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked)

    def _clear(self) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Unchecked)

    def _delete(self) -> None:
        selected = self._checked()
        if not selected:
            return
        size = sum(int(item.get("size") or 0) for item in selected)
        answer = QMessageBox.question(
            self,
            "Delete garbage",
            f"Delete {len(selected)} unreferenced item(s) "
            f"({_format_size(size)})?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        errors: list[str] = []
        for entry in selected:
            try:
                self.backend.storage_delete_file(entry["path"])
            except Exception as exc:  # noqa: BLE001 - show per-item failures
                errors.append(f"{entry.get('name', '')}: {exc}")
                continue
            self.deleted += 1
        if errors:
            QMessageBox.warning(
                self,
                "Garbage cleanup",
                f"Deleted {self.deleted} item(s).\n\n"
                + "\n".join(errors[:8]),
            )
        self.accept()


class StorageFilesDialog(VemuDialog):
    """Manage ISOs/images in a storage location, deleting only orphans."""

    def __init__(self, backend, location: dict[str, Any], parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.location = location
        self.entries: list[dict[str, Any]] = []
        path = str(location.get("path", ""))
        role = str(location.get("role", ""))
        self.setWindowTitle(f"Files · {path}")
        self.resize(820, 520)

        layout = dialog_layout(self)

        title = QLabel("Storage files")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            f"{path} · role {role}. Images are only deletable when no VM, "
            "chain or backing file references them."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.table = EmptyStateTable(0, 4)
        self.table.setHorizontalHeaderLabels(["NAME", "TYPE", "SIZE", "STATUS"])
        self.table.setItemDelegate(NoFocusDelegate(self.table))
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.horizontalHeader().setFixedHeight(32)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for column, width in ((1, 90), (2, 110)):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Fixed
            )
            self.table.horizontalHeader().resizeSection(column, width)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setShowGrid(False)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        self.hint = QLabel("")
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add file…")
        self.add_button.setObjectName("SecondaryButton")
        self.add_button.setEnabled(
            role != "templates" and hasattr(self.backend, "storage_import_file")
        )
        self.add_button.clicked.connect(self._add)
        buttons.addWidget(self.add_button)
        self.delete_button = QPushButton("Delete selected")
        self.delete_button.setObjectName("SecondaryButton")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._delete)
        buttons.addWidget(self.delete_button)
        self.clean_button = QPushButton("Clean garbage…")
        self.clean_button.setObjectName("SecondaryButton")
        self.clean_button.setEnabled(False)
        self.clean_button.clicked.connect(self._clean_garbage)
        buttons.addWidget(self.clean_button)
        buttons.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.setObjectName("SecondaryButton")
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(refresh)
        close = QPushButton("Close")
        close.setObjectName("SecondaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.refresh()

    def refresh(self) -> None:
        try:
            payload = self.backend.storage_files(self.location["path"])
        except VemuError as exc:
            self.entries = []
            self.table.setRowCount(0)
            self.table.set_empty_state("Cannot list files", str(exc))
            self.table.refresh_empty_state(0)
            self.hint.setText(str(exc))
            self.clean_button.setEnabled(False)
            self._selection_changed()
            return
        entries = list(payload.get("files") or []) + list(
            payload.get("chains") or []
        )
        entries.sort(key=lambda item: (item.get("kind") != "chain", item["name"]))
        self.entries = entries
        self.table.setRowCount(0)
        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                entry.get("name", ""),
                entry.get("kind", ""),
                _format_size(entry.get("size", 0)),
                entry.get("status", ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, column, item)
        if not entries:
            self.table.set_empty_state(
                "No ISOs or images",
                "Add an ISO/image with Add file, or create a VM in this "
                "location.",
            )
        self.table.refresh_empty_state()
        files = len(payload.get("files") or [])
        chains = len(payload.get("chains") or [])
        self.hint.setText(
            f"{files} file(s), {chains} chain dir(s). Deleting is only "
            "enabled for unreferenced items."
        )
        self.clean_button.setEnabled(
            any(item.get("deletable") for item in entries)
        )
        self._selection_changed()

    def _selected_entry(self) -> dict[str, Any] | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        if 0 <= row < len(self.entries):
            return self.entries[row]
        return None

    def _selection_changed(self) -> None:
        entry = self._selected_entry()
        self.delete_button.setEnabled(bool(entry and entry.get("deletable")))

    def _clean_garbage(self) -> None:
        garbage = [item for item in self.entries if item.get("deletable")]
        if not garbage:
            QMessageBox.information(
                self,
                "Clean garbage",
                "No unreferenced images or orphan chains were found.",
            )
            return
        dialog = StorageGarbageDialog(
            self.backend, self.location, garbage, self
        )
        dialog.exec()
        self.refresh()

    def _add(self) -> None:
        source, _filter = QFileDialog.getOpenFileName(
            self,
            "Add ISO or image",
            "",
            "Disk images (*.iso *.qcow2 *.raw *.img);;All files (*)",
        )
        if not source:
            return
        try:
            self.backend.storage_import_file(
                source, self.location["path"]
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot import file", str(exc))
            return
        self.hint.setText(f"Imported {source}")
        self.refresh()

    def _delete(self) -> None:
        entry = self._selected_entry()
        if entry is None or not entry.get("deletable"):
            return
        name = entry.get("name", "")
        if entry.get("kind") == "chain":
            body = (
                f"Delete the orphan chain {name}?\n\n"
                "This removes every layer and chain.json in that directory."
            )
        else:
            body = f"Delete {name}?\n\nThis cannot be undone."
        answer = QMessageBox.question(
            self,
            "Delete storage file",
            body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.storage_delete_file(entry["path"])
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot delete file", str(exc))
            return
        self.hint.setText(f"Deleted {name}")
        self.refresh()
