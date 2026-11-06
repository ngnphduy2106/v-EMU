"""VirtioFS host/guest share editor."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
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
)

from ..errors import VemuError
from .delegate import NoFocusDelegate
from .dialogs import VemuDialog, dialog_layout
from .components import form_layout
from .widgets import EmptyStateTable


class ShareAddDialog(VemuDialog):
    def __init__(self, domain: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Add VirtioFS share · {domain}")
        self.resize(560, 280)
        layout = dialog_layout(self)
        title = QLabel(f"Add VirtioFS share · {domain}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        form = form_layout()
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("/home/user/share")
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.clicked.connect(self._browse)
        source_row.addWidget(browse)
        form.addRow("Host directory", source_row)
        self.tag_edit = QLineEdit("share")
        form.addRow("Guest tag", self.tag_edit)
        self.read_only_check = QCheckBox("Mount read-only in the guest")
        form.addRow("", self.read_only_check)
        layout.addLayout(form)
        note = QLabel(
            "Linux guests mount the tag with `mount -t virtiofs <tag> /mnt`. "
            "Windows guests need virtiofs/WinFsp drivers. Restart the VM after "
            "changing shares. Templates and host files are never modified."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        add = QPushButton("Add share")
        add.setObjectName("PrimaryButton")
        add.clicked.connect(self._accept)
        buttons.addWidget(add)
        layout.addLayout(buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose host directory", self.source_edit.text()
        )
        if path:
            self.source_edit.setText(path)
            if self.tag_edit.text() in {"", "share"}:
                self.tag_edit.setText(Path(path).name or "share")

    def _accept(self) -> None:
        source = self.source_edit.text().strip()
        tag = self.tag_edit.text().strip()
        if not source or not tag:
            QMessageBox.warning(
                self, "VirtioFS", "Host directory and guest tag are required."
            )
            return
        if not Path(source).expanduser().is_dir():
            QMessageBox.warning(
                self, "VirtioFS", "The host directory does not exist."
            )
            return
        self.accept()


class AttachCatalogDialog(VemuDialog):
    def __init__(self, backend, domain: str, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domain = domain
        self.setWindowTitle(f"Attach shared folder · {domain}")
        self.resize(560, 380)
        layout = dialog_layout(self)
        title = QLabel(f"Attach shared folder · {domain}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        form = form_layout()
        self.share_combo = QComboBox()
        self.tag_edit = QLineEdit()
        self.read_only_check = QCheckBox("Read-only in the guest")
        self.auto_mount_check = QCheckBox(
            "Auto-mount after Windows guest agent is ready"
        )
        self.drive_combo = QComboBox()
        self.drive_combo.addItem("Auto (next free)", None)
        for letter in "ZYXWVUTSRQPONMLKJIHGFED":
            self.drive_combo.addItem(f"{letter}:", letter)
        self.mountpoint_edit = QLineEdit()
        self.mountpoint_edit.setPlaceholderText(
            "Linux only; blank uses /mnt/vemu/<tag>"
        )
        self.description = QLabel()
        self.description.setObjectName("Muted")
        self.description.setWordWrap(True)
        try:
            self.shares = self.backend.shares_list()
        except (VemuError, AttributeError):
            self.shares = []
        for item in self.shares:
            self.share_combo.addItem(
                f"{item['name']} · {item['path']}", item["name"]
            )
        self.share_combo.currentIndexChanged.connect(self._share_changed)
        form.addRow("Catalog share", self.share_combo)
        form.addRow("Guest tag", self.tag_edit)
        form.addRow("", self.read_only_check)
        form.addRow("", self.auto_mount_check)
        form.addRow("Windows drive", self.drive_combo)
        form.addRow("Linux mount point", self.mountpoint_edit)
        layout.addLayout(form)
        layout.addWidget(self.description)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        attach = QPushButton("Attach")
        attach.setObjectName("PrimaryButton")
        attach.clicked.connect(self._attach)
        buttons.addWidget(attach)
        layout.addLayout(buttons)
        self._share_changed()

    def _selected(self) -> dict | None:
        name = self.share_combo.currentData()
        return next((item for item in self.shares if item["name"] == name), None)

    def _share_changed(self) -> None:
        item = self._selected()
        if item is None:
            self.description.setText(
                "No catalog shares. Add one under Shared folders first."
            )
            return
        self.tag_edit.setText(str(item.get("tag", "")))
        self.read_only_check.setChecked(bool(item.get("read_only")))
        self.mountpoint_edit.setText(f"/mnt/vemu/{item.get('tag', '')}")
        self.description.setText(
            f"{item.get('description') or 'Host directory'} · "
            f"{item.get('path', '')}"
        )

    def _attach(self) -> None:
        item = self._selected()
        tag = self.tag_edit.text().strip()
        if item is None or not tag:
            QMessageBox.warning(
                self, "Attach shared folder", "Choose a catalog share and tag."
            )
            return
        try:
            self.backend.vm_share_attach(
                domain=self.domain,
                share=str(item["name"]),
                tag=tag,
                read_only=self.read_only_check.isChecked(),
                auto_mount=self.auto_mount_check.isChecked(),
                drive=self.drive_combo.currentData(),
                mountpoint=self.mountpoint_edit.text().strip() or None,
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot attach share", str(exc))
            return
        self.accept()


class ShareOptionsDialog(VemuDialog):
    def __init__(self, backend, domain: str, share: dict, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domain = domain
        self.share = share
        self.setWindowTitle(f"Share options · {share.get('tag', '')}")
        self.resize(520, 280)
        layout = dialog_layout(self)
        title = QLabel(f"Share options · {share.get('tag', '')}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        form = form_layout()
        self.auto_mount_check = QCheckBox(
            "Auto-mount after Windows guest agent is ready"
        )
        self.auto_mount_check.setChecked(bool(share.get("auto_mount")))
        form.addRow("", self.auto_mount_check)
        self.drive_combo = QComboBox()
        self.drive_combo.addItem("Auto (next free)", None)
        for letter in "ZYXWVUTSRQPONMLKJIHGFED":
            self.drive_combo.addItem(f"{letter}:", letter)
        current = str(share.get("drive") or "").upper()[:1]
        index = self.drive_combo.findData(current or None)
        if index >= 0:
            self.drive_combo.setCurrentIndex(index)
        form.addRow("Preferred Windows drive", self.drive_combo)
        self.mountpoint_edit = QLineEdit(str(share.get("mountpoint") or ""))
        self.mountpoint_edit.setPlaceholderText(
            "Linux only; blank uses /mnt/vemu/<tag>"
        )
        form.addRow("Linux mount point", self.mountpoint_edit)
        layout.addLayout(form)
        note = QLabel(
            "Auto-mount is evaluated only for this VM, and only after the "
            "guest agent responds."
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

    def _save(self) -> None:
        try:
            self.backend.vm_share_options(
                domain=self.domain,
                share=str(self.share["share"]),
                auto_mount=self.auto_mount_check.isChecked(),
                drive=self.drive_combo.currentData(),
                mountpoint=self.mountpoint_edit.text().strip() or None,
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot save share options", str(exc))
            return
        self.accept()


class _ShareTask(QThread):
    done = Signal(object, object)

    def __init__(self, operation) -> None:
        super().__init__()
        self._operation = operation

    def run(self) -> None:
        try:
            result = self._operation()
        except Exception as exc:  # noqa: BLE001 - forwarded to the dialog
            self.done.emit(None, exc)
        else:
            self.done.emit(result, None)


class VirtioFsDialog(VemuDialog):
    def __init__(self, backend, domain: str, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domain = domain
        self.payload: dict = {}
        self._task: _ShareTask | None = None
        self._task_done_callback = None
        self.setWindowTitle(f"VirtioFS shares · {domain}")
        self.resize(720, 460)
        layout = dialog_layout(self)
        title = QLabel(f"VirtioFS shares · {domain}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        note = QLabel(
            "Attach reusable catalog shares to this VM. Nothing is mounted "
            "globally. The VM needs shared memory backing and a guest VirtioFS "
            "driver; after adding a share, fully stop/start the VM."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.banner = QLabel()
        self.banner.setObjectName("Warning")
        self.banner.setWordWrap(True)
        self.banner.hide()
        layout.addWidget(self.banner)
        self.busy_label = QLabel()
        self.busy_label.setObjectName("Muted")
        self.busy_label.hide()
        layout.addWidget(self.busy_label)

        self.table = EmptyStateTable(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["TAG", "HOST PATH", "MODE", "GUEST", "STATUS"]
        )
        self.table.setItemDelegate(NoFocusDelegate(self.table))
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setFixedHeight(30)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(0, 120)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        for column, width in ((2, 90), (3, 160), (4, 120)):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Fixed
            )
            self.table.horizontalHeader().resizeSection(column, width)
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
        self.table.itemSelectionChanged.connect(self._selection)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.attach_button = QPushButton("Attach catalog share…")
        self.attach_button.setObjectName("PrimaryButton")
        self.attach_button.setEnabled(hasattr(self.backend, "shares_list"))
        self.attach_button.clicked.connect(self._attach_catalog)
        actions.addWidget(self.attach_button)
        self.add_button = QPushButton("Add ad-hoc share…")
        self.add_button.setObjectName("SecondaryButton")
        self.add_button.clicked.connect(self._add)
        actions.addWidget(self.add_button)
        self.options_button = QPushButton("Options…")
        self.options_button.setObjectName("SecondaryButton")
        self.options_button.setEnabled(False)
        self.options_button.clicked.connect(self._options)
        actions.addWidget(self.options_button)
        self.remove_button = QPushButton("Detach")
        self.remove_button.setObjectName("SecondaryButton")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove)
        actions.addWidget(self.remove_button)
        actions.addStretch(1)
        self.unmount_button = QPushButton("Unmount in guest")
        self.unmount_button.setObjectName("SecondaryButton")
        self.unmount_button.setEnabled(False)
        self.unmount_button.clicked.connect(self._unmount)
        actions.addWidget(self.unmount_button)
        self.mount_button = QPushButton("Mount in guest")
        self.mount_button.setObjectName("SecondaryButton")
        self.mount_button.setEnabled(hasattr(self.backend, "vm_share_mount"))
        self.mount_button.clicked.connect(self._mount)
        actions.addWidget(self.mount_button)
        self.restart_button = QPushButton("Restart VM now")
        self.restart_button.setObjectName("SecondaryButton")
        self.restart_button.clicked.connect(self._restart)
        self.restart_button.hide()
        actions.addWidget(self.restart_button)
        close = QPushButton("Close")
        close.setObjectName("SecondaryButton")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.refresh()

    def refresh(self) -> None:
        try:
            if hasattr(self.backend, "vm_share_status"):
                self.payload = self.backend.vm_share_status(self.domain)
            else:
                self.payload = self.backend.vm_shares(self.domain)
        except VemuError as exc:
            QMessageBox.critical(self, "VirtioFS", str(exc))
            self.payload = {"shares": [], "state": "unknown"}
        self.table.setRowCount(0)
        running = self.payload.get("state") == "running"
        pending = bool(self.payload.get("pending_restart"))
        if pending:
            self.banner.setText(
                "Some shares were added after the VM started. Shut down and "
                "start the VM again (not a Windows reboot) before mounting."
            )
            self.banner.show()
            self.restart_button.show()
        else:
            self.banner.hide()
            self.restart_button.hide()
        for share in self.payload.get("shares", []):
            row = self.table.rowCount()
            self.table.insertRow(row)
            guest = (
                share.get("guest_mount")
                or share.get("drive")
                or share.get("mountpoint")
                or ("auto" if share.get("auto_mount") else "-")
            )
            if share.get("pending_restart"):
                status = "Restart required"
            elif not running:
                status = "Saved"
            elif share.get("mounted"):
                status = "Mounted"
            else:
                status = "Not mounted"
            values = (
                share.get("tag", ""),
                share.get("source", ""),
                "read-only" if share.get("read_only") else "rw",
                guest,
                status,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, column, item)
        self.table.refresh_empty_state()
        self.remove_button.setEnabled(False)
        self.options_button.setEnabled(False)
        self.unmount_button.setEnabled(False)
        self.mount_button.setEnabled(False)

    def _selection(self) -> None:
        row = self.table.currentRow()
        selected = row >= 0
        share = self._selected_share()
        running = self.payload.get("state") == "running"
        pending = bool(self.payload.get("pending_restart"))
        mounted = bool(share and share.get("mounted"))
        self.remove_button.setEnabled(selected and self._task is None)
        self.options_button.setEnabled(
            selected
            and self._task is None
            and bool(share and share.get("share"))
            and hasattr(self.backend, "vm_share_options")
        )
        self.unmount_button.setEnabled(
            selected and running and mounted and self._task is None
        )
        self.mount_button.setEnabled(
            selected
            and running
            and not pending
            and not mounted
            and self._task is None
            and hasattr(self.backend, "vm_share_mount")
        )

    def _selected_share(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        return next(
            (
                entry
                for entry in self.payload.get("shares", [])
                if str(entry.get("tag", "")) == item.text()
            ),
            None,
        )

    def _options(self) -> None:
        share = self._selected_share()
        if share is None or not share.get("share"):
            return
        dialog = ShareOptionsDialog(self.backend, self.domain, share, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def _attach_catalog(self) -> None:
        dialog = AttachCatalogDialog(self.backend, self.domain, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def _run_share_task(self, label: str, operation, callback) -> None:
        if self._task is not None:
            return
        self.busy_label.setText(label)
        self.busy_label.show()
        self._task_done_callback = callback
        self.mount_button.setEnabled(False)
        self.unmount_button.setEnabled(False)
        self.options_button.setEnabled(False)
        self.remove_button.setEnabled(False)
        self._task = _ShareTask(operation)
        self._task.done.connect(self._share_task_done)
        self._task.finished.connect(self._share_task_finished)
        self._task.start()

    def _share_task_done(self, payload, error) -> None:
        if error is not None:
            QMessageBox.critical(self, "VirtioFS", str(error))
        elif self._task_done_callback is not None:
            self._task_done_callback(payload)

    def _share_task_finished(self) -> None:
        self.busy_label.hide()
        if self._task is not None:
            self._task.deleteLater()
        self._task = None
        self._task_done_callback = None
        self.refresh()
        self._selection()

    def _mount(self) -> None:
        share = self._selected_share()
        if share is None:
            return
        tag = str(share.get("tag", ""))

        def mounted(payload) -> None:
            item = next(
                (
                    entry
                    for entry in payload.get("mounts", [])
                    if entry.get("tag") == tag
                ),
                {},
            )
            if item.get("mounted"):
                QMessageBox.information(
                    self,
                    "VirtioFS mount",
                    f"{tag} → {item.get('drive') or item.get('path') or 'mounted'}",
                )
            else:
                QMessageBox.warning(
                    self,
                    "VirtioFS mount",
                    f"{tag}: {item.get('error') or item.get('output') or 'failed'}",
                )

        self._run_share_task(
            f"Mounting {tag} in the guest…",
            lambda: self.backend.vm_share_mount(self.domain, tag),
            mounted,
        )

    def _unmount(self) -> None:
        share = self._selected_share()
        if share is None:
            return
        tag = str(share.get("tag", ""))

        def unmounted(payload) -> None:
            QMessageBox.information(
                self,
                "VirtioFS unmount",
                f"{tag}: "
                + (
                    "unmounted"
                    if payload.get("unmounted")
                    else str(payload.get("output") or "not mounted")
                ),
            )

        self._run_share_task(
            f"Unmounting {tag} in the guest…",
            lambda: self.backend.vm_share_unmount(self.domain, tag),
            unmounted,
        )

    def _restart(self) -> None:
        answer = QMessageBox.question(
            self,
            "Restart VM",
            "Restart the VM now to attach the new VirtioFS device? "
            "Unsaved guest data may be lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def restarted(_payload) -> None:
            QMessageBox.information(
                self,
                "VM restarted",
                "The VM was restarted. Use Mount in guest when the guest "
                "agent is ready if auto-mount is disabled.",
            )

        self._run_share_task(
            "Restarting the VM…",
            lambda: self.backend.restart(self.domain),
            restarted,
        )

    def _add(self) -> None:
        dialog = ShareAddDialog(self.domain, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.backend.vm_share_add(
                self.domain,
                dialog.tag_edit.text().strip(),
                dialog.source_edit.text().strip(),
                dialog.read_only_check.isChecked(),
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot add share", str(exc))
            return
        self.refresh()

    def _remove(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        tag = item.text()
        share = next(
            (
                entry
                for entry in self.payload.get("shares", [])
                if str(entry.get("tag", "")) == tag
            ),
            None,
        )
        answer = QMessageBox.question(
            self,
            "Detach share",
            f"Detach VirtioFS share '{tag}'? Host files are not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            if share is not None and share.get("share"):
                self.backend.vm_share_detach(
                    self.domain, str(share["share"])
                )
            else:
                self.backend.vm_share_remove(self.domain, tag)
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot detach share", str(exc))
            return
        self.refresh()
