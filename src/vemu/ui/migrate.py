"""Full-migration preflight and execution dialog."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .dialogs import VemuDialog, dialog_layout


class _Task(QThread):
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


def _human_bytes(value: int | float) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _error_text(error: Exception) -> str:
    message = str(error)
    if "Unknown method" in message:
        message += (
            "\n\nvemud is outdated. Run `./setup.sh --standard` and restart "
            "v-EMU."
        )
    return message


class MigrateDialog(VemuDialog):
    """Run the Full-migration estimator, then migrate the VM."""

    def __init__(
        self,
        backend,
        domain_name: str,
        managed_by: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domain_name = domain_name
        self.managed_by = managed_by
        self.plan: dict[str, Any] | None = None
        self.migrated = False
        self.result: dict[str, Any] | None = None
        self._estimate_task: _Task | None = None
        self._migrate_task: _Task | None = None

        self.setWindowTitle(f"Migrate to v-EMU · {domain_name}")
        self.resize(820, 660)
        self.setMinimumSize(760, 600)

        layout = dialog_layout(self)

        title = QLabel(f"Migrate {domain_name} to v-EMU")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        intro = QLabel(
            "Full migration builds a v-EMU v2 chain; the VM must be shut down. "
            "Existing libvirt/Cockpit snapshots can be imported, otherwise the "
            "current disk state is flattened and the old files are kept."
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.warning_label = QLabel()
        self.warning_label.setObjectName("Warning")
        self.warning_label.setWordWrap(True)
        self.warning_label.hide()
        layout.addWidget(self.warning_label)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["DISK", "SOURCE", "ESTIMATE"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(0, 80)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(2, 110)
        self.table.setMaximumHeight(170)
        layout.addWidget(self.table)

        summary = QFormLayout()
        summary.setSpacing(6)
        self.estimate_label = QLabel("-")
        self.headroom_label = QLabel("-")
        self.required_label = QLabel("-")
        self.available_label = QLabel("-")
        self.destination_label = QLabel("-")
        self.destination_label.setWordWrap(True)
        summary.addRow("Estimated", self.estimate_label)
        summary.addRow("Safety margin (15%)", self.headroom_label)
        summary.addRow("Required", self.required_label)
        summary.addRow("Available", self.available_label)
        summary.addRow("Destination", self.destination_label)
        layout.addLayout(summary)

        self.new_firmware_check = QCheckBox(
            "Use new v-EMU firmware and a fresh NVRAM "
            "(guest without BitLocker/FDE)"
        )
        self.new_firmware_check.toggled.connect(self._estimate)
        layout.addWidget(self.new_firmware_check)
        self.firmware_label = QLabel()
        self.firmware_label.setObjectName("Muted")
        self.firmware_label.setWordWrap(True)
        self.firmware_label.hide()
        layout.addWidget(self.firmware_label)

        self.import_check = QCheckBox(
            "Import existing libvirt/Cockpit snapshots into the chain"
        )
        self.import_check.toggled.connect(self._estimate)
        self.import_check.hide()
        layout.addWidget(self.import_check)

        self.snapshot_check = QCheckBox(
            "I understand existing external snapshots are not imported"
        )
        self.snapshot_check.toggled.connect(self._update_gate)
        self.snapshot_check.hide()
        layout.addWidget(self.snapshot_check)

        self.start_check = QCheckBox("Start the VM after migration")
        layout.addWidget(self.start_check)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)

        self.status_label = QLabel("Running preflight…")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("SecondaryButton")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.migrate_button = QPushButton("Migrate")
        self.migrate_button.setObjectName("PrimaryButton")
        self.migrate_button.setEnabled(False)
        self.migrate_button.clicked.connect(self._migrate)
        buttons.addWidget(self.migrate_button)
        layout.addLayout(buttons)

        QTimer.singleShot(0, self._estimate)

    # ------------------------------------------------------------------ estimate

    def _busy(self) -> bool:
        return self._estimate_task is not None or self._migrate_task is not None

    def _estimate(self) -> None:
        if self._busy():
            return
        self.plan = None
        self.migrate_button.setEnabled(False)
        self.warning_label.hide()
        self.firmware_label.hide()
        self.snapshot_check.hide()
        self.import_check.setEnabled(False)
        self.new_firmware_check.setEnabled(False)
        self.status_label.setText("Running preflight and size estimate…")
        self.progress.show()
        task = _Task(
            lambda: self.backend.vm_migration_plan(
                self.domain_name,
                new_firmware=self.new_firmware_check.isChecked(),
                import_snapshots=self.import_check.isChecked(),
            )
        )
        self._estimate_task = task
        task.done.connect(self._estimate_done)
        task.start()

    def _estimate_done(self, result, error) -> None:
        self._estimate_task = None
        self.progress.hide()
        self.new_firmware_check.setEnabled(True)
        self.import_check.setEnabled(True)
        if error is not None:
            self.status_label.setText("Estimate failed.")
            self.warning_label.setText(_error_text(error))
            self.warning_label.show()
            return
        self.plan = result or {}
        self._populate()
        self._update_gate()

    def _populate(self) -> None:
        plan = self.plan or {}
        disks = plan.get("disks") or []
        self.table.setRowCount(len(disks))
        for row, disk in enumerate(disks):
            values = (
                str(disk.get("target", "")),
                str(disk.get("source", "")),
                str(disk.get("required_human", "")),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        self.estimate_label.setText(str(plan.get("estimated_human", "-")))
        self.headroom_label.setText(
            _human_bytes(int(plan.get("headroom_bytes") or 0))
        )
        self.required_label.setText(str(plan.get("required_human", "-")))
        self.available_label.setText(str(plan.get("available_human", "-")))
        self.destination_label.setText(str(plan.get("destination") or "-"))

        firmware = plan.get("firmware") or {}
        if plan.get("new_firmware") and firmware:
            self.firmware_label.setText(
                f"Loader: {firmware.get('loader', '-')}\n"
                f"NVRAM: {firmware.get('nvram_path', '-')}"
            )
            self.firmware_label.show()
        else:
            self.firmware_label.hide()

        snapshot_import = plan.get("snapshot_import") or {}
        has_snapshots = bool(plan.get("snapshots"))
        can_import = bool(has_snapshots and snapshot_import.get("supported"))
        self.import_check.setVisible(has_snapshots)
        self.import_check.setEnabled(can_import)
        if not can_import and self.import_check.isChecked():
            blocked = QSignalBlocker(self.import_check)
            self.import_check.setChecked(False)
            del blocked
        if has_snapshots:
            if can_import:
                count = len(plan.get("snapshots") or [])
                layers = int(snapshot_import.get("layer_count") or 0)
                self.import_check.setText(
                    f"Import {count} existing snapshot(s) "
                    f"into {layers} chain layer(s)"
                )
                self.import_check.setToolTip(
                    "Snapshot overlays are copied into the v-EMU chain; the "
                    "old files stay in place."
                )
            else:
                self.import_check.setText(
                    "Existing snapshots cannot be imported"
                )
                self.import_check.setToolTip(
                    str(snapshot_import.get("reason", "unsupported layout"))
                )
        else:
            self.import_check.setToolTip("")

        if plan.get("snapshot_loss_required"):
            self.snapshot_check.show()
        else:
            self.snapshot_check.setChecked(False)
            self.snapshot_check.hide()

        reasons = list(plan.get("reasons") or [])
        if reasons:
            self.warning_label.setText("\n".join(f"• {item}" for item in reasons))
            self.warning_label.show()
            self.status_label.setText("Migration is blocked.")
        else:
            self.warning_label.hide()
            if self.import_check.isChecked():
                self.status_label.setText(
                    "Ready to migrate; snapshots will be imported."
                )
            else:
                self.status_label.setText("Ready to migrate.")

    def _update_gate(self) -> None:
        plan = self.plan or {}
        ready = bool(plan.get("supported")) and bool(plan.get("sufficient"))
        if plan.get("snapshot_loss_required") and not self.snapshot_check.isChecked():
            ready = False
        if self._busy():
            ready = False
        self.migrate_button.setEnabled(ready)
        if not ready and plan.get("snapshot_loss_required"):
            self.migrate_button.setToolTip(
                "Confirm that external snapshots are not imported."
            )
        else:
            self.migrate_button.setToolTip("")

    # ------------------------------------------------------------------ migrate

    def _migrate(self) -> None:
        plan = self.plan or {}
        if not self.migrate_button.isEnabled():
            return
        lines = [f"Migrate {self.domain_name} to v-EMU Full?", ""]
        lines.append(f"Required: {plan.get('required_human', '-')}")
        lines.append(f"Available: {plan.get('available_human', '-')}")
        if self.import_check.isChecked():
            lines.append(
                "Existing libvirt/Cockpit snapshots will be imported."
            )
        elif plan.get("snapshot_loss_required"):
            lines.append("External snapshots will not be imported.")
        if self.new_firmware_check.isChecked():
            lines.append("A new v-EMU firmware and NVRAM will be used.")
        lines.append("")
        answer = QMessageBox.question(
            self,
            "Migrate to v-EMU",
            "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_controls(False)
        self.progress.show()
        self.status_label.setText(
            "Migrating… this can take several minutes. Do not close this window."
        )
        task = _Task(
            lambda: self.backend.vm_migrate_full(
                self.domain_name,
                accept_snapshot_loss=bool(
                    plan.get("snapshot_loss_required")
                ),
                import_snapshots=self.import_check.isChecked(),
                start=self.start_check.isChecked(),
                new_firmware=self.new_firmware_check.isChecked(),
            )
        )
        self._migrate_task = task
        task.done.connect(self._migrate_done)
        task.start()

    def _migrate_done(self, result, error) -> None:
        self._migrate_task = None
        self.progress.hide()
        if error is not None:
            self._set_controls(True)
            self.status_label.setText("Migration failed.")
            self.warning_label.setText(_error_text(error))
            self.warning_label.show()
            self._update_gate()
            return
        self.migrated = True
        self.result = result or {}
        backup = self.result.get("backup", "")
        chain = self.result.get("chain", "")
        firmware = self.result.get("firmware_applied")
        imported = self.result.get("imported_snapshots") or []
        lines = [f"{self.domain_name} migrated to v-EMU Full."]
        if imported:
            lines.append(
                "Imported snapshots: " + ", ".join(str(item) for item in imported)
            )
        if chain:
            lines.append(f"Chain: {chain}")
        if backup:
            lines.append(f"Backup XML: {backup}")
        if firmware:
            lines.append("New v-EMU firmware and NVRAM applied.")
        lines.append("")
        lines.append("Old disks were kept. Verify the VM before deleting them.")
        QMessageBox.information(self, "Migration complete", "\n".join(lines))
        self.accept()

    def _set_controls(self, enabled: bool) -> None:
        self.table.setEnabled(enabled)
        self.new_firmware_check.setEnabled(enabled)
        self.import_check.setEnabled(enabled)
        self.snapshot_check.setEnabled(enabled)
        self.start_check.setEnabled(enabled)
        self.cancel_button.setEnabled(enabled)
        self.migrate_button.setEnabled(enabled and self.migrate_button.isEnabled())

    def reject(self) -> None:
        if self._migrate_task is not None:
            return
        if self._estimate_task is not None:
            return
        super().reject()
