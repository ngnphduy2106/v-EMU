"""Snapshot manager for a v-EMU managed VM."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..errors import VemuError
from .delegate import NoFocusDelegate
from .dialogs import VemuDialog, dialog_layout
from .components import form_layout


_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _created_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:10]
    return f"{parsed.day:02d}-{_MONTHS[parsed.month - 1]}-{parsed.year:04d}"


class SnapshotEditDialog(VemuDialog):
    """Create or edit the user-facing name/description of a snapshot."""

    def __init__(
        self,
        parent=None,
        *,
        title: str = "New snapshot",
        name: str = "",
        description: str = "",
        editing: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 380)
        layout = dialog_layout(self)

        heading = QLabel(title)
        heading.setObjectName("DialogTitle")
        layout.addWidget(heading)

        form = form_layout()
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("e.g. before-upgrade")
        form.addRow("Name", self.name_edit)
        layout.addLayout(form)

        description_label = QLabel("Description")
        description_label.setObjectName("Muted")
        layout.addWidget(description_label)
        self.description_edit = QPlainTextEdit(description)
        self.description_edit.setPlaceholderText(
            "Optional: what changed, why this snapshot exists, rollback notes…"
        )
        layout.addWidget(self.description_edit, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = QPushButton("Save" if editing else "Create")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.clicked.connect(self.accept)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    def values(self) -> tuple[str, str]:
        return (
            self.name_edit.text().strip(),
            self.description_edit.toPlainText().strip(),
        )


class SnapshotsDialog(VemuDialog):
    def __init__(self, backend, domain_name: str, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domain_name = domain_name
        self.info: dict[str, Any] = {}
        self.layers: dict[str, dict[str, Any]] = {}
        self.changed = False

        self.setWindowTitle(f"Snapshots · {domain_name}")
        self.resize(680, 570)

        layout = dialog_layout(self)
        title = QLabel(f"Snapshots · {domain_name}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        self.state_label = QLabel()
        self.state_label.setObjectName("Muted")
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        self.table = QTreeWidget()
        self.table.setColumnCount(5)
        self.table.setHeaderLabels(
            ["ID", "NAME", "PARENT", "SIZE", "CREATED AT"]
        )
        self.table.setRootIsDecorated(True)
        self.table.setIndentation(20)
        self.table.setUniformRowHeights(True)
        for column, alignment in enumerate(
            (
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
            )
        ):
            header_item = self.table.headerItem()
            if header_item is not None:
                header_item.setTextAlignment(
                    column, alignment | Qt.AlignmentFlag.AlignVCenter
                )
        self.table.setItemDelegate(NoFocusDelegate(self.table))
        self.table.header().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.header().setFixedHeight(30)
        self.table.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.table.header().resizeSection(0, 120)
        self.table.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Fixed
        )
        self.table.header().resizeSection(2, 90)
        self.table.header().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Fixed
        )
        self.table.header().resizeSection(3, 100)
        self.table.header().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Fixed
        )
        self.table.header().resizeSection(4, 130)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.table, 1)

        details = QGroupBox("Description")
        details.setMinimumHeight(190)
        details_layout = QVBoxLayout(details)
        details_layout.setSpacing(6)
        self.details_description = QPlainTextEdit()
        self.details_description.setReadOnly(True)
        self.details_description.setPlaceholderText("No description.")
        self.details_description.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        details_layout.addWidget(self.details_description)
        layout.addWidget(details)

        buttons = QHBoxLayout()
        self.create_button = QPushButton("New snapshot")
        self.create_button.setObjectName("PrimaryButton")
        self.create_button.clicked.connect(self._create)
        buttons.addWidget(self.create_button)

        self.edit_button = QPushButton("Edit")
        self.edit_button.setObjectName("SecondaryButton")
        self.edit_button.clicked.connect(self._edit)
        buttons.addWidget(self.edit_button)

        self.revert_button = QPushButton("Revert")
        self.revert_button.setObjectName("SecondaryButton")
        self.revert_button.clicked.connect(self._revert)
        buttons.addWidget(self.revert_button)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("SecondaryButton")
        self.delete_button.clicked.connect(self._delete)
        buttons.addWidget(self.delete_button)

        buttons.addStretch(1)
        close_button = QPushButton("Close")
        close_button.setObjectName("SecondaryButton")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self.refresh()

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        try:
            self.info = self.backend.snapshots_info(self.domain_name)
        except VemuError as exc:
            self.state_label.setText(str(exc))
            self.table.clear()
            self._set_actions(False)
            return

        if not self.info.get("managed"):
            self._show_external()
            return

        self._set_headers(
            "ID", "NAME", "PARENT", "SIZE", "CREATED AT"
        )
        running = bool(self.info.get("running"))
        if running:
            text = (
                "The VM is running. Live snapshots are disabled; shut it down "
                "to create an offline snapshot."
            )
        else:
            text = f"Chain: {self.info.get('chain', '')} · VM is stopped."
        if self.info.get("multi_disk"):
            count = int(self.info.get("disk_count", 0))
            if count == 1:
                text += " Single-disk v2 chain."
            else:
                text += (
                    f" Multi-disk chain ({count} disks); "
                    "snapshots apply to every disk."
                )
        self.state_label.setText(text)

        self.table.clear()
        layers = list(self.info.get("layers", []))
        self.layers = {
            str(layer.get("id", "")): layer for layer in layers
        }
        by_parent: dict[str, list[dict[str, Any]]] = {}
        for layer in layers:
            by_parent.setdefault(str(layer.get("parent", "")), []).append(layer)
        ids = {str(layer.get("id", "")) for layer in layers}

        def add_layer(
            layer: dict[str, Any], parent_item: QTreeWidgetItem | None = None
        ) -> None:
            layer_id = str(layer.get("id", ""))
            active = bool(layer.get("active"))
            size = layer.get("actual_size", 0) / (1024 * 1024)
            item = QTreeWidgetItem(
                [
                    ("* " if active else "  ") + layer_id,
                    str(layer.get("label", "") or "-"),
                    str(layer.get("parent", "")),
                    f"{size:.1f} MB",
                    _created_text(layer.get("created_at")),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, layer_id)
            if active:
                font = item.font(0)
                font.setBold(True)
                for column in range(5):
                    item.setFont(column, font)
            if parent_item is None:
                self.table.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            for child in by_parent.get(layer_id, []):
                add_layer(child, item)

        roots = [layer for layer in layers if str(layer.get("parent")) == "base"]
        if not roots:
            roots = [
                layer
                for layer in layers
                if str(layer.get("parent")) not in ids
            ]
        for layer in roots:
            add_layer(layer)
        self.table.expandAll()

        self.create_button.setText("New snapshot")
        self.create_button.setEnabled(not running)
        self.edit_button.setEnabled(False)
        self.revert_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self._show_details()

    def _set_headers(self, *labels: str) -> None:
        self.table.setColumnCount(len(labels))
        self.table.setHeaderLabels(list(labels))
        header = self.table.header()
        for column in range(len(labels)):
            item = self.table.headerItem()
            if item is not None:
                item.setTextAlignment(
                    column,
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter,
                )
        if labels and labels[0] == "NAME":
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(1, 90)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(2, 90)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            return
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(0, 120)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(2, 90)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(3, 100)
        if len(labels) > 4:
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(4, 130)

    def _show_external(self) -> None:
        snapshots = list(self.info.get("snapshots", []))
        self._set_headers("NAME", "TYPE", "DISKS", "SOURCE")
        reason = self.info.get("reason", "This VM is not managed by v-EMU.")
        if snapshots:
            self.state_label.setText(
                f"{reason}\nLibvirt/Cockpit snapshots are shown read-only; "
                "Full migration does not import them."
            )
        else:
            self.state_label.setText(
                f"{reason}\nNo libvirt/Cockpit snapshots found."
            )
        self.table.clear()
        self.layers = {}
        for snapshot in snapshots:
            name = str(snapshot.get("name", ""))
            disks = list(snapshot.get("disks", []))
            item = QTreeWidgetItem(
                [
                    name,
                    str(snapshot.get("kind", "")),
                    str(len(disks)),
                    "",
                ]
            )
            for disk in disks:
                item.addChild(
                    QTreeWidgetItem(
                        [
                            str(disk.get("target", "")),
                            str(disk.get("snapshot", "")),
                            "",
                            str(disk.get("source", "")),
                        ]
                    )
                )
            self.table.addTopLevelItem(item)
        self.table.expandAll()
        self._set_actions(False)
        self._show_details()

    def _selection_changed(self) -> None:
        running = bool(self.info.get("running"))
        selected = self._selected_layer() is not None
        managed = bool(self.info.get("managed"))
        self.edit_button.setEnabled(selected and managed)
        self.revert_button.setEnabled(selected and not running and managed)
        self.delete_button.setEnabled(selected and not running and managed)
        self._show_details()

    def _set_actions(self, enabled: bool) -> None:
        self.create_button.setEnabled(enabled)
        self.edit_button.setEnabled(False)
        self.revert_button.setEnabled(False)
        self.delete_button.setEnabled(False)

    def _show_details(self) -> None:
        managed = bool(self.info.get("managed"))
        if not managed:
            item = self.table.currentItem()
            while item is not None and item.parent() is not None:
                item = item.parent()
            if item is None:
                self.details_description.setPlainText("")
                return
            name = item.text(0)
            snapshot = next(
                (
                    entry
                    for entry in self.info.get("snapshots", [])
                    if str(entry.get("name", "")) == name
                ),
                None,
            )
            description = (
                str(snapshot.get("description", ""))
                if snapshot is not None
                else ""
            )
            self.details_description.setPlainText(
                description or "No description."
            )
            return

        layer_id = self._selected_layer()
        layer = self.layers.get(layer_id or "")
        if layer is None:
            self.details_description.setPlainText("")
            return
        self.details_description.setPlainText(
            str(layer.get("description", "")) or "No description."
        )

    def _selected_layer(self) -> str | None:
        selected = self.table.selectedItems()
        if not selected:
            return None
        value = selected[0].data(0, Qt.ItemDataRole.UserRole)
        return str(value) if value is not None else None

    # ------------------------------------------------------------------ actions

    def _build_context_menu(self) -> QMenu:
        menu = QMenu(self)
        create = menu.addAction(self.create_button.text(), self._create)
        create.setEnabled(self.create_button.isEnabled())
        menu.addSeparator()
        layer = self._selected_layer()
        edit = menu.addAction("Edit snapshot…", self._edit)
        edit.setEnabled(layer is not None and bool(self.info.get("managed")))
        can_modify = layer is not None and not bool(self.info.get("running"))
        revert = menu.addAction("Revert", self._revert)
        revert.setEnabled(can_modify)
        delete = menu.addAction("Delete", self._delete)
        delete.setEnabled(can_modify)
        menu.addSeparator()
        copy = menu.addAction(
            "Copy ID",
            lambda: QGuiApplication.clipboard().setText(layer or ""),
        )
        copy.setEnabled(layer is not None)
        return menu

    def _show_context_menu(self, position) -> None:
        item = self.table.itemAt(position)
        if item is None:
            return
        self.table.setCurrentItem(item)
        self._build_context_menu().exec(
            self.table.viewport().mapToGlobal(position)
        )

    def _create(self) -> None:
        dialog = SnapshotEditDialog(
            self, title="New snapshot", editing=False
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        label, description = dialog.values()
        try:
            payload = self.backend.snapshots_create(
                self.domain_name, label, description
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Snapshot failed", str(exc))
            return
        warnings = (
            payload.get("warnings", []) if isinstance(payload, dict) else []
        )
        if warnings:
            QMessageBox.warning(
                self, "Snapshot warning", "\n".join(str(item) for item in warnings)
            )
        self._show_ejected(payload)
        self.changed = True
        self.refresh()

    def _edit(self) -> None:
        layer = self._selected_layer()
        if layer is None or not bool(self.info.get("managed")):
            return
        data = self.layers.get(layer, {})
        dialog = SnapshotEditDialog(
            self,
            title="Edit snapshot",
            name=str(data.get("label", "")),
            description=str(data.get("description", "")),
            editing=True,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        label, description = dialog.values()
        try:
            self.backend.snapshots_update(
                self.domain_name,
                layer,
                label=label,
                description=description,
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Snapshot update failed", str(exc))
            return
        self.changed = True
        self.refresh()

    def _show_ejected(self, payload: object) -> None:
        ejected = (
            payload.get("ejected", []) if isinstance(payload, dict) else []
        )
        if not ejected:
            return
        QMessageBox.information(
            self,
            "CD/DVD ejected",
            "Ejected media: "
            + ", ".join(str(item) for item in ejected)
            + "\n\nRe-insert it from the Hardware editor if needed.",
        )

    def _revert(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            return
        answer = QMessageBox.question(
            self,
            "Revert snapshot",
            f"Revert to layer {layer}? Changes after this snapshot will be lost.\n\n"
            "CD/DVD media will be ejected first.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            payload = self.backend.snapshots_revert(self.domain_name, layer)
        except VemuError as exc:
            QMessageBox.critical(self, "Revert failed", str(exc))
            return
        self._show_ejected(payload)
        self.changed = True
        self.refresh()

    def _delete(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            return
        is_active = layer == self.info.get("active")
        extra = " This is the active layer; current state will be discarded." if is_active else ""
        answer = QMessageBox.question(
            self,
            "Delete snapshot",
            f"Delete layer {layer}?{extra}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.snapshots_delete(
                self.domain_name, layer, force=is_active
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Delete failed", str(exc))
            return
        self.changed = True
        self.refresh()
