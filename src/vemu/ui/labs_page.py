"""lab.yaml workspace page."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..errors import VemuError
from .delegate import NoFocusDelegate
from .dialogs import ReportDialog, VemuDialog, dialog_layout
from .tokens import COMPONENT
from .widgets import EmptyStateTable
from .components import PageHeader


def _lab_known_names(result: dict) -> set[str]:
    """Return VM names used as ``name: message`` prefixes in a lab result."""
    known = {
        str(name) for name in result.get("vms", []) if str(name).strip()
    }
    for action in result.get("actions", []):
        prefix = str(action).partition(": ")[0]
        if prefix and " " not in prefix:
            known.add(prefix)
    return known


def _group_lab_entries(
    entries: list[str], known: set[str]
) -> list[str]:
    """Merge per-VM actions/warnings that share the same message.

    Example: four ``win11-a: shutdown`` style entries become one
    ``win11-a, win11-b, ...: shutdown`` line. Unknown prefixes and plain
    messages are kept as-is; exact duplicates are dropped.
    """
    order: list[tuple[str, str]] = []
    groups: dict[tuple[str, str], list[str]] = {}
    for raw in entries:
        entry = str(raw)
        prefix, separator, detail = entry.partition(": ")
        if separator and detail and prefix in known:
            key = ("group", detail)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(prefix)
            continue
        key = ("plain", entry)
        if key in groups:
            continue
        groups[key] = []
        order.append(key)

    lines: list[str] = []
    for key in order:
        if key[0] == "plain":
            lines.append(key[1])
            continue
        names = list(dict.fromkeys(groups[key]))
        lines.append(f"{', '.join(names)}: {key[1]}")
    return lines


class _LabTask(QThread):
    """Run a slow lab operation without blocking the GUI thread."""

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


class _DestroyLabDialog(VemuDialog):
    def __init__(self, parent, lab_name: str) -> None:
        super().__init__(parent, title="Destroy lab")
        self.setMinimumWidth(COMPONENT.confirm_width)
        layout = dialog_layout(self)
        heading = QLabel(f"Destroy {lab_name}?")
        heading.setObjectName("CardTitle")
        layout.addWidget(heading)
        note = QLabel(
            "This removes all lab VMs, their managed disk chains and lab "
            "networks. Running VMs are forced off. Templates and the "
            "lab.yaml file are kept."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.keep_networks = QCheckBox("Keep lab network profiles")
        layout.addWidget(self.keep_networks)
        self.clear_status = QCheckBox(
            "Clear lab status and remembered path"
        )
        self.clear_status.setChecked(True)
        self.clear_status.setToolTip(
            "Forget this lab.yaml path and hide missing lab rows."
        )
        layout.addWidget(self.clear_status)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        destroy = QPushButton("Destroy")
        destroy.setObjectName("DangerButton")
        destroy.clicked.connect(self.accept)
        buttons.addWidget(destroy)
        layout.addLayout(buttons)


class LabsPage(QWidget):
    status = Signal(str)
    lab_changed = Signal()

    def __init__(self, backend, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.payload: dict = {}
        self._settings = QSettings("v-EMU", "v-EMU")
        self._task: _LabTask | None = None
        self._progress: QProgressDialog | None = None
        self._on_success = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.addWidget(
            PageHeader("Labs", "Declarative lab.yaml workspaces")
        )
        header.addStretch(1)
        self.refresh_button = QPushButton("Load status")
        self.refresh_button.setObjectName("SecondaryButton")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("lab.yaml")
        self.path_edit.setText(self._default_path())
        path_row.addWidget(QLabel("lab.yaml"))
        path_row.addWidget(self.path_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.clicked.connect(self._browse_lab)
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        actions = QHBoxLayout()
        self.up_button = QPushButton("Bring up")
        self.up_button.setObjectName("PrimaryButton")
        self.up_button.clicked.connect(self._up)
        actions.addWidget(self.up_button)
        self.down_button = QPushButton("Shut down")
        self.down_button.setObjectName("SecondaryButton")
        self.down_button.clicked.connect(self._down)
        actions.addWidget(self.down_button)
        self.destroy_button = QPushButton("Destroy lab…")
        self.destroy_button.setObjectName("DangerButton")
        self.destroy_button.setToolTip(
            "Remove lab VMs, chains and lab networks; keep templates and lab.yaml."
        )
        self.destroy_button.setEnabled(False)
        self.destroy_button.clicked.connect(self._destroy)
        actions.addWidget(self.destroy_button)
        actions.addStretch(1)
        self.export_button = QPushButton("Export bundle…")
        self.export_button.setObjectName("SecondaryButton")
        self.export_button.clicked.connect(self._export)
        actions.addWidget(self.export_button)
        self.import_button = QPushButton("Import bundle…")
        self.import_button.setObjectName("SecondaryButton")
        self.import_button.clicked.connect(self._import)
        actions.addWidget(self.import_button)
        layout.addLayout(actions)

        self.summary = QLabel("Load a lab.yaml to see networks and VMs.")
        self.summary.setObjectName("Muted")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        networks_label = QLabel("NETWORKS")
        networks_label.setObjectName("SectionLabel")
        layout.addWidget(networks_label)
        self.networks = EmptyStateTable(0, 4)
        self.networks.setHorizontalHeaderLabels(
            ["NAME", "DEFINED", "ACTIVE", "ERROR"]
        )
        self._setup_table(self.networks, (200, 90, 90, 0))
        self.networks.setMaximumHeight(180)
        layout.addWidget(self.networks)

        vms_label = QLabel("VIRTUAL MACHINES")
        vms_label.setObjectName("SectionLabel")
        layout.addWidget(vms_label)
        self.vms = EmptyStateTable(0, 4)
        self.vms.setHorizontalHeaderLabels(
            ["NAME", "STATE", "NETWORKS", "TEMPLATE"]
        )
        self._setup_table(self.vms, (200, 100, 0, 260))
        self.vms.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        layout.addWidget(self.vms, 1)

        vm_actions = QHBoxLayout()
        self.reset_button = QPushButton("Reset selected…")
        self.reset_button.setObjectName("SecondaryButton")
        self.reset_button.setEnabled(False)
        self.reset_button.clicked.connect(self._reset_selected)
        vm_actions.addWidget(self.reset_button)
        note = QLabel(
            "Reset recreates selected VM disks from their templates. "
            "Networks are kept."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        vm_actions.addWidget(note, 1)
        layout.addLayout(vm_actions)

    def _default_path(self) -> str:
        saved = str(self._settings.value("lab/path", "") or "")
        if saved and Path(saved).is_file():
            return saved
        candidate = Path("lab.yaml")
        if candidate.is_file():
            return str(candidate.resolve())
        return ""

    def _setup_table(self, table: EmptyStateTable, widths) -> None:
        table.setItemDelegate(NoFocusDelegate(table))
        table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        table.horizontalHeader().setFixedHeight(30)
        for column, width in enumerate(widths):
            if width:
                table.horizontalHeader().setSectionResizeMode(
                    column, QHeaderView.ResizeMode.Fixed
                )
                table.horizontalHeader().resizeSection(column, width)
            else:
                table.horizontalHeader().setSectionResizeMode(
                    column, QHeaderView.ResizeMode.Stretch
                )
        table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        table.setShowGrid(False)

    def _browse_lab(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose lab.yaml",
            str(Path(self.path_edit.text()).parent),
            "Lab files (lab.yaml *.yaml *.yml);;All files (*)",
        )
        if path:
            self.path_edit.setText(path)
            self.refresh()

    def _path(self) -> str:
        return self.path_edit.text().strip()

    def _lab_call(self, name: str, *args, **kwargs):
        method = getattr(self.backend, name, None)
        if method is not None:
            return method(*args, **kwargs)
        from .. import lab as lab_module

        return getattr(lab_module, name)(*args, **kwargs)

    def refresh(self) -> None:
        path = self._path()
        if not path:
            self.summary.setText("Choose a lab.yaml first.")
            return
        try:
            self.payload = self._lab_call("lab_status", path)
        except (VemuError, AttributeError) as exc:
            self.payload = {}
            self.summary.setText(f"Cannot load lab: {exc}")
            self.networks.setRowCount(0)
            self.vms.setRowCount(0)
            self.networks.refresh_empty_state()
            self.vms.refresh_empty_state()
            self.reset_button.setEnabled(False)
            self.destroy_button.setEnabled(False)
            return
        self._settings.setValue("lab/path", path)
        networks = self.payload.get("networks", [])
        vms = self.payload.get("vms", [])
        self.summary.setText(
            f"{self.payload.get('lab', 'lab')} · "
            f"{len(networks)} network(s) · {len(vms)} VM(s)"
        )
        self._fill_rows(
            self.networks,
            [
                [
                    item.get("name", ""),
                    "yes" if item.get("defined") else "no",
                    "yes" if item.get("active") else "no",
                    item.get("error") or "",
                ]
                for item in networks
            ],
        )
        self._fill_rows(
            self.vms,
            [
                [
                    item.get("name", ""),
                    item.get("state", ""),
                    ", ".join(item.get("networks") or []) or "-",
                    item.get("template", ""),
                ]
                for item in vms
            ],
        )
        self.reset_button.setEnabled(bool(vms))
        self.destroy_button.setEnabled(bool(vms or networks))
        self.status.emit(f"Lab {self.payload.get('lab', '')} loaded")

    @staticmethod
    def _fill_rows(table: EmptyStateTable, rows: list[list[str]]) -> None:
        table.setRowCount(0)
        for values in rows:
            row = table.rowCount()
            table.insertRow(row)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                table.setItem(row, column, item)
        table.refresh_empty_state()

    def _start_operation(self, label: str, operation, on_success) -> None:
        if self._task is not None:
            return
        self._on_success = on_success
        self._set_busy(True)
        progress = QProgressDialog(label, None, 0, 0, self)
        progress.setWindowTitle("Lab operation")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setLabelText(label)
        progress.setMinimumWidth(COMPONENT.progress_width)
        progress.show()
        QApplication.processEvents()
        self._progress = progress
        task = _LabTask(operation)
        self._task = task
        task.done.connect(self._operation_done)
        task.finished.connect(task.deleteLater)
        task.start()

    def _operation_done(self, result, error) -> None:
        self._close_progress()
        self._task = None
        self._set_busy(False)
        if error is not None:
            QMessageBox.critical(
                self, "Lab operation failed", str(error)
            )
            self.refresh()
            return
        callback = self._on_success
        self._on_success = None
        try:
            if callback is not None:
                callback(result or {})
        finally:
            self.lab_changed.emit()

    def _close_progress(self) -> None:
        progress = self._progress
        self._progress = None
        if progress is not None:
            progress.close()
            progress.deleteLater()

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self.path_edit,
            self.refresh_button,
            self.up_button,
            self.down_button,
            self.destroy_button,
            self.reset_button,
            self.export_button,
            self.import_button,
        ):
            widget.setEnabled(not busy)
        if not busy:
            self.reset_button.setEnabled(bool(self.payload.get("vms")))
            self.destroy_button.setEnabled(
                bool(
                    self.payload.get("vms")
                    or self.payload.get("networks")
                )
            )

    def _clear_lab_view(self, lab_name: str) -> None:
        self.payload = {}
        self._settings.remove("lab/path")
        self._settings.sync()
        self.path_edit.clear()
        self.networks.setRowCount(0)
        self.vms.setRowCount(0)
        self.networks.refresh_empty_state()
        self.vms.refresh_empty_state()
        self.summary.setText(
            f"{lab_name} destroyed. Choose a lab.yaml to start again."
        )
        self.reset_button.setEnabled(False)
        self.destroy_button.setEnabled(False)

    def _up(self) -> None:
        path = self._path()
        if not path:
            return
        answer = QMessageBox.question(
            self,
            "Bring lab up",
            "Create/apply the lab networks, define VMs and start them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_operation(
            "Bringing lab up…",
            lambda: self._lab_call("lab_up", path),
            self._after_up,
        )

    def _after_up(self, result: dict) -> None:
        self._report("Lab up", result)
        self.refresh()

    def _down(self) -> None:
        path = self._path()
        if not path:
            return
        answer = QMessageBox.question(
            self,
            "Shut lab down",
            "Shut down the lab VMs and networks?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_operation(
            "Shutting lab down…",
            lambda: self._lab_call("lab_down", path),
            self._after_down,
        )

    def _after_down(self, result: dict) -> None:
        self._report("Lab down", result)
        self.refresh()

    def _reset_selected(self) -> None:
        path = self._path()
        if not path:
            return
        selection = self.vms.selectionModel()
        if selection is None:
            return
        names = []
        for index in selection.selectedRows():
            item = self.vms.item(index.row(), 0)
            if item is not None and item.text():
                names.append(item.text())
        if not names:
            QMessageBox.information(
                self, "Reset selected", "Select one or more VMs first."
            )
            return
        states = {
            str(item.get("name", "")): str(item.get("state", ""))
            for item in self.payload.get("vms", [])
        }
        running = [name for name in names if states.get(name) == "running"]
        text = (
            f"Reset {len(names)} VM(s)? Their current disks will be deleted "
            "and recreated from the lab templates."
        )
        if running:
            text += (
                "\n\nThese running VMs will be forced off: "
                + ", ".join(running)
            )
        answer = QMessageBox.question(
            self,
            "Reset selected VMs",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_operation(
            "Resetting selected VMs…",
            lambda: self._lab_call(
                "lab_reset",
                path,
                names,
                force=bool(running),
            ),
            self._after_reset,
        )

    def _after_reset(self, result: dict) -> None:
        self._report("Lab reset", result)
        self.refresh()

    def _destroy(self) -> None:
        path = self._path()
        if not path:
            return
        lab_name = str(self.payload.get("lab") or "this lab")
        dialog = _DestroyLabDialog(self, lab_name)
        if dialog.exec() != VemuDialog.DialogCode.Accepted:
            return
        keep_networks = dialog.keep_networks.isChecked()
        clear_status = dialog.clear_status.isChecked()
        self._start_operation(
            "Destroying lab…",
            lambda: self._lab_call(
                "lab_destroy",
                path,
                force=True,
                keep_networks=keep_networks,
            ),
            lambda result: self._after_destroy(
                result, lab_name=lab_name, clear_status=clear_status
            ),
        )

    def _after_destroy(
        self, result: dict, *, lab_name: str, clear_status: bool
    ) -> None:
        self._report("Lab destroyed", result)
        if clear_status:
            self._clear_lab_view(lab_name)
        else:
            self.refresh()

    def _export(self) -> None:
        path = self._path()
        if not path:
            return
        destination, _filter = QFileDialog.getSaveFileName(
            self,
            "Export lab bundle",
            str(Path(path).with_suffix(".tar.gz")),
            "Tar archives (*.tar.gz);;All files (*)",
        )
        if not destination:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Export lab bundle")
        box.setText("Include template images in the bundle?")
        include = QCheckBox("Include templates (can be large)", box)
        box.setCheckBox(include)
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if box.exec() != QMessageBox.StandardButton.Ok:
            return
        try:
            result = self._lab_call(
                "lab_export",
                path,
                destination,
                with_templates=include.isChecked(),
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot export lab", str(exc))
            return
        QMessageBox.information(
            self,
            "Lab exported",
            f"Bundle written to {result['destination']}.",
        )
        self.status.emit("Lab bundle exported")

    def _import(self) -> None:
        bundle, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose lab bundle",
            "",
            "Tar archives (*.tar.gz);;All files (*)",
        )
        if not bundle:
            return
        destination = QFileDialog.getExistingDirectory(
            self, "Choose import directory", str(Path(bundle).parent)
        )
        if not destination:
            return
        try:
            result = self._lab_call("lab_import", bundle, destination)
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot import lab", str(exc))
            return
        self.path_edit.setText(str(result["lab_file"]))
        self.refresh()
        self.lab_changed.emit()
        QMessageBox.information(
            self,
            "Lab imported",
            f"Imported {result['lab']} to {result['destination']}.",
        )

    def _report(self, title: str, result: dict) -> None:
        known = _lab_known_names(result)
        changes = _group_lab_entries(
            list(result.get("actions", [])), known
        )
        warnings = _group_lab_entries(
            list(result.get("warnings", [])), known
        )
        ReportDialog(
            self, title, changes=tuple(changes), warnings=tuple(warnings)
        ).exec()
