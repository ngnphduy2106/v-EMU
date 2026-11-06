"""Import an OVA appliance or an existing disk image as a VM."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QFrame,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .dialogs import VemuDialog
from .components import InfoBadge, form_layout


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


def _error_text(error: Exception) -> str:
    message = str(error)
    if "Unknown method" in message:
        message += (
            "\n\nvemud is outdated. Run `./setup.sh --standard` and restart "
            "v-EMU."
        )
    return message


class ImportVmDialog(VemuDialog):
    def __init__(self, backend, kind: str, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.kind = kind
        self.plan: dict[str, Any] | None = None
        self.created: dict[str, Any] | None = None
        self._plan_task: _Task | None = None
        self._import_task: _Task | None = None
        self._networks: list[dict[str, Any]] = []

        title_text = (
            "Import OVA appliance" if kind == "ova" else "Import disk image"
        )
        source_hint = (
            "Choose an OVA or ZIP archive…"
            if kind == "ova"
            else "Choose a QCOW2/raw/VMDK image or ZIP…"
        )
        disclaimer_text = (
            "OVA/ZIP only. Follow the vendor's deployment and configuration "
            "guide, and verify the disk list before starting. Arbitrary OVAs "
            "may not boot."
            if kind == "ova"
            else "QCOW2/raw/VMDK or ZIP only. Follow the vendor's KVM image "
            "guide for hardware and drivers; the disk is converted into a "
            "managed chain."
        )
        self.setWindowTitle(title_text)
        self.resize(760, 700)
        self.setMinimumSize(680, 600)
        self.setSizeGripEnabled(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 10)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel(title_text)
        title.setObjectName("DialogTitle")
        title_row.addWidget(title)
        disclaimer = InfoBadge(disclaimer_text, kind="info")
        title_row.addWidget(disclaimer)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        source_group = QGroupBox("Source")
        source_form = form_layout(source_group)
        source_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )

        self.source_edit = QLineEdit()
        self.source_edit.setMinimumWidth(360)
        self.source_edit.setPlaceholderText(source_hint)
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.setMinimumWidth(96)
        browse.clicked.connect(self._browse)
        source_row.addWidget(browse)
        source_form.addRow("Source", source_row)

        self.name_edit = QLineEdit()
        self.name_edit.editingFinished.connect(self._plan)
        source_form.addRow("VM name", self.name_edit)

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Generic VM", "generic")
        self.preset_combo.addItem("Network appliance", "appliance")
        if kind == "ova":
            self.preset_combo.addItem("VMware VCSA (Tiny)", "vcsa")
        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        source_form.addRow("Preset", self.preset_combo)

        self.network_combo = QComboBox()
        source_form.addRow("Default network", self.network_combo)
        layout.addWidget(source_group)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)

        resources_group = QGroupBox("Resources")
        resources_form = form_layout(resources_group)
        self.vcpu_spin = QSpinBox()
        self.vcpu_spin.setRange(1, 64)
        resources_form.addRow("vCPU", self.vcpu_spin)
        self.memory_spin = QSpinBox()
        self.memory_spin.setRange(256, 262144)
        self.memory_spin.setSuffix(" MiB")
        resources_form.addRow("Memory", self.memory_spin)
        self.data_disk_spin = QSpinBox()
        self.data_disk_spin.setRange(0, 4096)
        self.data_disk_spin.setSuffix(" GiB")
        resources_form.addRow("Extra data disk", self.data_disk_spin)
        grid.addWidget(resources_group, 0, 0)

        machine_group = QGroupBox("Machine")
        machine_form = form_layout(machine_group)
        self.firmware_combo = QComboBox()
        self.firmware_combo.addItems(["bios", "efi"])
        machine_form.addRow("Firmware", self.firmware_combo)
        self.machine_combo = QComboBox()
        self.machine_combo.addItems(["q35", "pc"])
        machine_form.addRow("Machine", self.machine_combo)
        self.disk_bus_combo = QComboBox()
        self.disk_bus_combo.addItems(
            ["virtio", "sata", "scsi", "ide", "nvme"]
        )
        machine_form.addRow("Disk bus", self.disk_bus_combo)
        self.nic_model_combo = QComboBox()
        self.nic_model_combo.addItems(
            ["virtio", "e1000e", "e1000", "rtl8139", "pcnet"]
        )
        machine_form.addRow("NIC model", self.nic_model_combo)
        self.video_combo = QComboBox()
        self.video_combo.addItems(
            ["vga", "virtio", "qxl", "cirrus", "vmvga", "bochs"]
        )
        machine_form.addRow("Video", self.video_combo)
        self.serial_check = QCheckBox("Add serial console")
        machine_form.addRow("", self.serial_check)
        self.start_check = QCheckBox("Start after import")
        machine_form.addRow("", self.start_check)
        grid.addWidget(machine_group, 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self.summary_label = QLabel("Run the estimator to inspect the source.")
        self.summary_label.setObjectName("Muted")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.disk_label = QLabel()
        self.disk_label.setObjectName("Muted")
        self.disk_label.setWordWrap(True)
        layout.addWidget(self.disk_label)
        self.warning_label = QLabel()
        self.warning_label.setObjectName("Warning")
        self.warning_label.setWordWrap(True)
        self.warning_label.hide()
        layout.addWidget(self.warning_label)

        nic_header = QHBoxLayout()
        nic_label = QLabel("Network interfaces")
        nic_label.setObjectName("SectionTitle")
        nic_header.addWidget(nic_label)
        nic_header.addStretch(1)
        add_nic = QPushButton("Add NIC")
        add_nic.setObjectName("SecondaryButton")
        add_nic.clicked.connect(lambda: self._add_nic_row())
        nic_header.addWidget(add_nic)
        remove_nic = QPushButton("Remove NIC")
        remove_nic.setObjectName("SecondaryButton")
        remove_nic.clicked.connect(self._remove_nic_row)
        nic_header.addWidget(remove_nic)
        layout.addLayout(nic_header)

        self.nic_table = QTableWidget(0, 4)
        self.nic_table.setHorizontalHeaderLabels(
            ["NIC", "NETWORK", "MODEL", "MAC"]
        )
        self.nic_table.verticalHeader().setVisible(False)
        self.nic_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.nic_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.nic_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.nic_table.horizontalHeader().resizeSection(0, 70)
        self.nic_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.nic_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Fixed
        )
        self.nic_table.horizontalHeader().resizeSection(2, 110)
        self.nic_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Fixed
        )
        self.nic_table.horizontalHeader().resizeSection(3, 170)
        self.nic_table.setMinimumHeight(90)
        self.nic_table.setMaximumHeight(180)
        layout.addWidget(self.nic_table)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.estimate_button = QPushButton("Run estimator")
        self.estimate_button.setObjectName("SecondaryButton")
        self.estimate_button.setMinimumWidth(140)
        self.estimate_button.clicked.connect(self._plan)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("SecondaryButton")
        self.cancel_button.clicked.connect(self.reject)
        self.import_button = QPushButton("Import")
        self.import_button.setObjectName("PrimaryButton")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self._import)

        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 8, 18, 18)
        footer_layout.addWidget(self.estimate_button)
        footer_layout.addWidget(self.progress, 1)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.cancel_button)
        footer_layout.addWidget(self.import_button)
        outer.addWidget(footer)

        self._load_networks()
        self._apply_preset()
        self.name_edit.setText(
            Path(self.source_edit.text()).stem if self.source_edit.text() else ""
        )
        QTimer.singleShot(0, self._apply_preset)

    # ------------------------------------------------------------------ helpers

    def _load_networks(self) -> None:
        try:
            self._networks = list(self.backend.networks_list())
        except Exception:  # noqa: BLE001 - keep the dialog usable
            self._networks = []
        names = [str(item.get("name")) for item in self._networks]
        if not names:
            names = ["NAT", "Isolated-Net", "Bridge"]
        self.network_combo.clear()
        self.network_combo.addItems(names)

    def _network_names(self) -> list[str]:
        names = [str(item.get("name")) for item in self._networks]
        return names or ["NAT", "Isolated-Net", "Bridge"]

    def _preset_changed(self) -> None:
        self._apply_preset()
        self._plan()

    def _apply_preset(self) -> None:
        preset = self.preset_combo.currentData()
        if preset == "appliance":
            self.vcpu_spin.setValue(1)
            self.memory_spin.setValue(4096)
            self.disk_bus_combo.setCurrentText("virtio")
            self.nic_model_combo.setCurrentText("virtio")
            self.firmware_combo.setCurrentText("bios")
            self.video_combo.setCurrentText("vga")
            self.serial_check.setChecked(True)
            self.data_disk_spin.setValue(0)
        elif preset == "vcsa":
            self.vcpu_spin.setValue(2)
            self.memory_spin.setValue(14336)
            self.disk_bus_combo.setCurrentText("sata")
            self.nic_model_combo.setCurrentText("e1000e")
            self.firmware_combo.setCurrentText("efi")
            self.video_combo.setCurrentText("virtio")
            self.serial_check.setChecked(False)
            self.data_disk_spin.setValue(0)
        else:
            self.vcpu_spin.setValue(2)
            self.memory_spin.setValue(2048)
            self.disk_bus_combo.setCurrentText("sata")
            self.nic_model_combo.setCurrentText("e1000e")
            self.firmware_combo.setCurrentText("bios")
            self.video_combo.setCurrentText("vga")
            self.serial_check.setChecked(False)
            self.data_disk_spin.setValue(0)

    def _browse(self) -> None:
        if self.kind == "ova":
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Choose OVA/ZIP",
                str(Path.home() / "Downloads"),
                "Appliance archives (*.ova *.zip);;All files (*)",
            )
        else:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Choose disk image",
                str(Path.home() / "Downloads"),
                "Disk images (*.qcow2 *.img *.raw *.vmdk *.zip);;All files (*)",
            )
        if not path:
            return
        self.source_edit.setText(path)
        if not self.name_edit.text().strip():
            self.name_edit.setText(Path(path).stem.replace("_", "-")[:64])
        self._plan()

    def _selected_network(self) -> str:
        return self.network_combo.currentText().strip() or "NAT"

    def _plan(self) -> None:
        if self._plan_task is not None or self._import_task is not None:
            return
        source = self.source_edit.text().strip()
        name = self.name_edit.text().strip()
        if not source or not name:
            return
        self.import_button.setEnabled(False)
        self.warning_label.hide()
        self.summary_label.setText("Inspecting source…")
        self.progress.show()
        self.estimate_button.setEnabled(False)
        task = _Task(
            lambda: self.backend.vm_import_plan(
                kind=self.kind,
                source=source,
                name=name,
                preset=self.preset_combo.currentData(),
                network=self._selected_network(),
                video=self.video_combo.currentText(),
            )
        )
        self._plan_task = task
        task.done.connect(self._plan_done)
        task.start()

    def _plan_done(self, result, error) -> None:
        self._plan_task = None
        self.progress.hide()
        self.estimate_button.setEnabled(True)
        if error is not None:
            self.plan = None
            self.summary_label.setText("Estimator failed.")
            self.warning_label.setText(_error_text(error))
            self.warning_label.show()
            return
        self.plan = result or {}
        specs = self.plan.get("specs") or {}
        self.vcpu_spin.setValue(int(specs.get("vcpus", 2)))
        self.memory_spin.setValue(int(specs.get("memory_mib", 2048)))
        self.firmware_combo.setCurrentText(
            str(specs.get("firmware", "bios"))
        )
        self.machine_combo.setCurrentText(str(specs.get("machine", "q35")))
        self.disk_bus_combo.setCurrentText(
            str(specs.get("disk_bus", "sata"))
        )
        self.nic_model_combo.setCurrentText(
            str(specs.get("nic_model", "e1000e"))
        )
        self.video_combo.setCurrentText(str(specs.get("video", "vga")))
        self.serial_check.setChecked(bool(specs.get("serial_console")))
        self._populate_nics(self.plan.get("nics") or [])
        disks = self.plan.get("disks") or []
        self.disk_label.setText(
            "\n".join(
                f"Disk {disk.get('target')}: {disk.get('size_human')} "
                f"{disk.get('bus')} — {disk.get('source') or 'new empty disk'}"
                for disk in disks
            )
        )
        self.summary_label.setText(
            f"Estimated {self.plan.get('estimated_human', '-')} · "
            f"required {self.plan.get('required_human', '-')} · "
            f"available {self.plan.get('available_human', '-')} · "
            f"destination {self.plan.get('destination') or '-'}"
        )
        warnings = list(self.plan.get("warnings") or [])
        reasons = list(self.plan.get("reasons") or [])
        if warnings or reasons:
            self.warning_label.setText(
                "\n".join(
                    [f"• {item}" for item in reasons]
                    + [f"Warning: {item}" for item in warnings]
                )
            )
            self.warning_label.show()
        self.import_button.setEnabled(
            bool(self.plan.get("supported")) and bool(self.plan.get("sufficient"))
        )

    def _populate_nics(self, nics: list[dict[str, Any]]) -> None:
        self.nic_table.setRowCount(0)
        for index, nic in enumerate(nics or [{}]):
            self._add_nic_row(
                index=index,
                network=str(nic.get("network") or self._selected_network()),
                model=str(nic.get("model") or self.nic_model_combo.currentText()),
                mac=str(nic.get("mac") or ""),
            )

    def _add_nic_row(
        self,
        *,
        index: int | None = None,
        network: str | None = None,
        model: str | None = None,
        mac: str = "",
    ) -> None:
        row = self.nic_table.rowCount() if index is None else index
        if index is None or row >= self.nic_table.rowCount():
            self.nic_table.insertRow(row)
        label = QTableWidgetItem(f"NIC {row + 1}")
        label.setFlags(label.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.nic_table.setItem(row, 0, label)
        network_combo = QComboBox()
        network_combo.addItems(self._network_names())
        if network:
            if network_combo.findText(network) < 0:
                network_combo.addItem(network)
            network_combo.setCurrentText(network)
        self.nic_table.setCellWidget(row, 1, network_combo)
        model_combo = QComboBox()
        model_combo.addItems(
            [
                self.nic_model_combo.itemText(i)
                for i in range(self.nic_model_combo.count())
            ]
        )
        if model:
            if model_combo.findText(model) < 0:
                model_combo.addItem(model)
            model_combo.setCurrentText(model)
        self.nic_table.setCellWidget(row, 2, model_combo)
        mac_item = QTableWidgetItem(mac)
        self.nic_table.setItem(row, 3, mac_item)

    def _remove_nic_row(self) -> None:
        row = self.nic_table.currentRow()
        if row < 0:
            row = self.nic_table.rowCount() - 1
        if row >= 0 and self.nic_table.rowCount() > 1:
            self.nic_table.removeRow(row)
            for index in range(self.nic_table.rowCount()):
                item = self.nic_table.item(index, 0)
                if item is not None:
                    item.setText(f"NIC {index + 1}")

    def _collect_nics(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in range(self.nic_table.rowCount()):
            network = self.nic_table.cellWidget(row, 1)
            model = self.nic_table.cellWidget(row, 2)
            mac_item = self.nic_table.item(row, 3)
            result.append(
                {
                    "target": f"nic{row + 1}",
                    "network": network.currentText() if isinstance(network, QComboBox) else self._selected_network(),
                    "model": model.currentText() if isinstance(model, QComboBox) else self.nic_model_combo.currentText(),
                    "mac": (mac_item.text().strip() if mac_item else "") or None,
                }
            )
        return result

    def _import(self) -> None:
        if not self.plan or not self.import_button.isEnabled():
            return
        answer = QMessageBox.question(
            self,
            "Import VM",
            "Create this VM now? The source file is copied/flattened; the "
            "original is left untouched.\n\n"
            + "Presets and parsed OVF values are suggestions only. Verify the "
            "hardware against the vendor documentation.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_controls(False)
        self.progress.show()
        self.summary_label.setText("Importing… do not close this window.")
        task = _Task(
            lambda: self.backend.vm_import(
                kind=self.kind,
                source=self.source_edit.text().strip(),
                name=self.name_edit.text().strip(),
                preset=self.preset_combo.currentData(),
                network=self._selected_network(),
                vcpus=self.vcpu_spin.value(),
                memory_mib=self.memory_spin.value(),
                firmware=self.firmware_combo.currentText(),
                machine=self.machine_combo.currentText(),
                disk_bus=self.disk_bus_combo.currentText(),
                nic_model=self.nic_model_combo.currentText(),
                serial_console=self.serial_check.isChecked(),
                data_disk_gb=self.data_disk_spin.value(),
                nics=self._collect_nics(),
                start=self.start_check.isChecked(),
            )
        )
        self._import_task = task
        task.done.connect(self._import_done)
        task.start()

    def _import_done(self, result, error) -> None:
        self._import_task = None
        self.progress.hide()
        if error is not None:
            self._set_controls(True)
            self.summary_label.setText("Import failed.")
            self.warning_label.setText(_error_text(error))
            self.warning_label.show()
            return
        self.created = result or {}
        lines = [f"{self.created.get('name')} imported to v-EMU."]
        if self.created.get("chain"):
            lines.append(f"Chain: {self.created['chain']}")
        lines.append("")
        lines.append(
            "Follow the vendor's configuration guide for the next steps; "
            "boot, licensing and feature support are not guaranteed."
        )
        QMessageBox.information(self, "Import complete", "\n".join(lines))
        self.accept()

    def _set_controls(self, enabled: bool) -> None:
        for widget in (
            self.source_edit,
            self.name_edit,
            self.preset_combo,
            self.network_combo,
            self.vcpu_spin,
            self.memory_spin,
            self.firmware_combo,
            self.machine_combo,
            self.disk_bus_combo,
            self.nic_model_combo,
            self.video_combo,
            self.data_disk_spin,
            self.serial_check,
            self.start_check,
            self.nic_table,
            self.estimate_button,
            self.cancel_button,
        ):
            widget.setEnabled(enabled)
        ready = bool(
            self.plan
            and self.plan.get("supported")
            and self.plan.get("sufficient")
        )
        self.import_button.setEnabled(enabled and ready)

    def reject(self) -> None:
        if self._import_task is not None or self._plan_task is not None:
            return
        super().reject()
