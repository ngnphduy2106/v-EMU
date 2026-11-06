"""VM hardware editor backed by libvirt domain XML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIntValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..errors import VemuError
from .delegate import NoFocusDelegate
from .components import FieldLabel, form_layout
from .dialogs import VemuDialog, dialog_layout
from .tooltips import compact_tooltip, set_tooltip
from .theme import LIGHT, Tokens
from .widgets import ComboBox, EmptyStateTable


class DiskAddDialog(VemuDialog):
    def __init__(
        self,
        backend,
        domain: str,
        default_dir: str,
        parent=None,
        *,
        reserved_paths: set[Path] | None = None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domain = domain
        self.spec: dict[str, Any] | None = None
        self.setWindowTitle(f"Add disk · {domain}")
        self.resize(540, 340)
        self._default_dir = Path(default_dir)
        self._reserved_paths = reserved_paths or set()

        layout = dialog_layout(self)
        title = QLabel(f"Add disk · {domain}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        form = QFormLayout()
        form.setSpacing(8)
        self.path_edit = QLineEdit(str(self._next_path()))
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        form.addRow("Path", path_row)
        self.size_edit = QLineEdit("10")
        form.addRow("Size (GB)", self.size_edit)
        self.bus_combo = ComboBox()
        for bus in ("virtio", "sata", "scsi", "ide", "nvme", "usb"):
            self.bus_combo.addItem(bus, bus)
        form.addRow("Bus", self.bus_combo)
        self.format_combo = ComboBox()
        self.format_combo.addItems(["qcow2", "raw"])
        form.addRow("Format", self.format_combo)
        self.existing_check = QCheckBox(
            "Use an existing image file (do not create a new file)"
        )
        self.existing_check.toggled.connect(self._existing_toggled)
        form.addRow("", self.existing_check)
        layout.addLayout(form)
        helper = QLabel(
            "The disk is added only when you click Save hardware. Cancel "
            "discards the pending disk. The file is kept if you remove the "
            "disk later."
        )
        helper.setObjectName("Muted")
        helper.setWordWrap(True)
        layout.addWidget(helper)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.add_button = QPushButton("Add disk")
        self.add_button.setObjectName("PrimaryButton")
        self.add_button.clicked.connect(self._add)
        buttons.addWidget(self.add_button)
        layout.addLayout(buttons)

    def _next_path(self) -> Path:
        for index in range(1, 1000):
            candidate = self._default_dir / f"data-disk-{index}.qcow2"
            if (
                not candidate.exists()
                and candidate not in self._reserved_paths
            ):
                return candidate
        raise RuntimeError("No free default disk filename.")

    def _existing_toggled(self, enabled: bool) -> None:
        self.size_edit.setEnabled(not enabled)
        if enabled:
            self.path_edit.setToolTip(
                "Choose an existing qcow2/raw image to attach."
            )
        else:
            self.path_edit.setToolTip("")

    def _browse(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Choose disk file",
            self.path_edit.text(),
            "QEMU images (*.qcow2);;Raw images (*.raw)",
        )
        if path:
            self.path_edit.setText(path)

    def _add(self) -> None:
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "Add disk", "Choose a disk path.")
            return
        create_file = not self.existing_check.isChecked()
        if create_file:
            try:
                size = int(self.size_edit.text())
            except ValueError:
                QMessageBox.warning(
                    self, "Add disk", "Size must be a number."
                )
                return
            if Path(path).expanduser().exists():
                QMessageBox.warning(
                    self,
                    "Add disk",
                    "That file already exists. Choose another name or enable "
                    "'Use an existing image file'.",
                )
                return
        else:
            if not Path(path).expanduser().is_file():
                QMessageBox.warning(
                    self,
                    "Add disk",
                    "The selected existing image was not found.",
                )
                return
            size = 0
        self.spec = {
            "path": path,
            "size_gb": size,
            "bus": str(self.bus_combo.currentData()),
            "format": self.format_combo.currentText(),
            "create_file": create_file,
        }
        self.accept()


class NicAddDialog(VemuDialog):
    def __init__(
        self,
        backend,
        domain: str,
        networks: list[dict[str, Any]],
        models: list[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domain = domain
        self.created: dict[str, Any] | None = None
        self.setWindowTitle(f"Add NIC · {domain}")
        self.resize(460, 240)
        layout = dialog_layout(self)
        title = QLabel(f"Add network interface · {domain}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.network_combo = ComboBox()
        for network in networks:
            self.network_combo.addItem(
                f"{network['name']} ({network.get('mode', '')})",
                network["name"],
            )
        form.addRow("Network", self.network_combo)
        self.model_combo = ComboBox()
        for model in models:
            self.model_combo.addItem(model, model)
        form.addRow("Model", self.model_combo)
        layout.addLayout(form)
        helper = QLabel(
            "NICs are hot-plugged into the running VM (and kept in the "
            "persistent definition) when supported by QEMU."
        )
        helper.setObjectName("Muted")
        helper.setWordWrap(True)
        layout.addWidget(helper)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        add = QPushButton("Add NIC")
        add.setObjectName("PrimaryButton")
        add.clicked.connect(self._add)
        buttons.addWidget(add)
        layout.addLayout(buttons)

    def _add(self) -> None:
        try:
            self.created = self.backend.network_attach(
                self.domain,
                str(self.network_combo.currentData()),
                model=str(self.model_combo.currentData()),
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot add NIC", str(exc))
            return
        self.accept()


class VmConfigDialog(VemuDialog):
    def __init__(
        self,
        backend,
        domain: str,
        parent=None,
        *,
        tokens: Tokens | None = None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domain = domain
        self._theme_tokens = tokens or LIGHT
        self.updated: dict[str, Any] | None = None
        self._pending_disks: list[dict[str, Any]] = []
        self.setWindowTitle(f"Hardware · {domain}")
        self.resize(860, 660)

        self.payload = backend.vm_config_info(domain)

        config = self.payload["config"]
        arch = str(config.get("arch", "x86_64"))
        try:
            capabilities = backend.vm_capabilities(
                config.get("machine"), arch=arch
            )
        except (VemuError, AttributeError, TypeError):
            capabilities = {
                "cpu_modes": ["host-passthrough", "host-model", "custom", "maximum"],
                "disk_buses": ["virtio", "sata", "scsi", "ide", "nvme", "usb"],
                "video_models": ["virtio", "vga", "qxl", "cirrus", "vmvga", "bochs", "ramfb"],
                "nic_models": ["virtio", "e1000e", "e1000", "rtl8139", "vmxnet3", "pcnet"],
            }
        self.capabilities = capabilities
        self.arch = arch
        self.has_v2_chain = bool(self.payload.get("has_v2_chain"))
        layout = dialog_layout(self)
        title = QLabel(f"Hardware · {domain}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        self.uuid_label = QLabel(f"UUID: {config.get('uuid') or '(unknown)'}")
        self.uuid_label.setObjectName("Muted")
        self.uuid_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.uuid_label.setWordWrap(True)
        layout.addWidget(self.uuid_label)
        secure = "on" if config.get("secure_boot") else "off"
        state = QLabel(
            f"State: {self.payload.get('state', '')} · "
            f"Firmware: {config.get('firmware', '')} · "
            f"Secure Boot: {secure}"
        )
        state.setObjectName("Muted")
        layout.addWidget(state)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_core_tab(config), "Core")
        self.tabs.addTab(self._build_storage_tab(config), "Storage")
        self.tabs.addTab(self._build_network_tab(config), "Network")
        self.tabs.addTab(self._build_devices_tab(config), "Devices")
        layout.addWidget(self.tabs, 1)

        helper = QLabel(
            "Live changes: NIC hotplug/link toggle, disk expand, CD/DVD "
            "media, memory balloon within the boot maximum, and vCPU hotplug "
            "up to the vCPU boot maximum. Changing a NIC's network/vSwitch "
            "requires the VM to be stopped. Other changes to a running VM are "
            "saved for the next start. Firmware is not changed for an "
            "existing VM."
        )
        helper.setObjectName("Muted")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = QPushButton("Save hardware")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    def _build_core_tab(self, config: dict[str, Any]) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        form = form_layout()

        self.arch_label = QLabel(
            self.arch
            + (
                " · TCG emulation (slower)"
                if self.arch != "x86_64"
                else " · native KVM"
            )
        )
        self.arch_label.setObjectName("Muted")
        form.addRow("Architecture", self.arch_label)

        self.os_combo = ComboBox()
        for key, label in (
            ("windows", "Windows"),
            ("linux", "Linux"),
            ("hypervisor", "Hypervisor"),
            ("generic", "Generic"),
        ):
            self.os_combo.addItem(label, key)
        index = self.os_combo.findData(config.get("os_type", "generic"))
        if index >= 0:
            self.os_combo.setCurrentIndex(index)
        self.os_combo.setToolTip(
            "Guest OS badge for the VM list. Metadata only; it does not "
            "change the installed guest."
        )
        form.addRow("Guest OS", self.os_combo)

        self.memory_spin = QDoubleSpinBox()
        self.memory_spin.setRange(0.5, 1024.0)
        self.memory_spin.setDecimals(1)
        self.memory_spin.setSingleStep(0.5)
        self.memory_spin.setSuffix(" GB")
        current_mib = int(config.get("memory_mib", 2048))
        max_memory = int(
            config.get("memory_max_mib", config.get("memory_mib", 2048))
        )
        self.memory_spin.setValue(round(current_mib / 1024, 1))
        set_tooltip(
            self.memory_spin,
            compact_tooltip(
                f"Current {current_mib} MiB; maximum {max_memory} MiB. "
                "Decrease can use the balloon; increase needs a restart."
            ),
        )
        form.addRow("Memory", self.memory_spin)
        self.vcpu_edit = QLineEdit(str(config.get("vcpus", 1)))
        self.vcpu_edit.setValidator(QIntValidator(1, 256, self))
        self.vcpu_edit.setToolTip(
            "vCPUs now; below the boot maximum changes apply live, "
            "otherwise after restart."
        )
        form.addRow("vCPUs", self.vcpu_edit)
        self.vcpu_max_edit = QLineEdit(
            str(config.get("vcpus_max", config.get("vcpus", 1)))
        )
        self.vcpu_max_edit.setValidator(QIntValidator(1, 256, self))
        self.vcpu_max_edit.setToolTip(
            "Boot maximum for live hotplug; changes apply after restart."
        )
        form.addRow("vCPU boot maximum", self.vcpu_max_edit)
        self.cpu_combo = ComboBox()
        if self.arch == "x86_64":
            allowed_cpu = ("host-passthrough", "host-model")
        else:
            allowed_cpu = ("maximum",)
        cpu_modes = [
            item
            for item in self.capabilities.get("cpu_modes", [])
            if item in allowed_cpu
        ] or list(allowed_cpu)
        self._populate(
            self.cpu_combo,
            cpu_modes,
            config.get("cpu_mode", ""),
            allowed_cpu,
        )
        if str(config.get("cpu_mode", "")) not in allowed_cpu:
            self.cpu_combo.setToolTip(
                f"CPU mode '{config.get('cpu_mode', '')}' is legacy for this "
                f"{self.arch} VM; supported modes are "
                + ", ".join(allowed_cpu)
                + "."
            )
        form.addRow("CPU mode", self.cpu_combo)
        self.machine_edit = QLineEdit(str(config.get("machine", "q35")))
        self.machine_edit.setToolTip(
            "libvirt machine type, e.g. q35, pc, pc-q35-10.2."
        )
        form.addRow("Machine", self.machine_edit)
        self.video_combo = ComboBox()
        video_models = list(self.capabilities.get("video_models", []))
        if self.arch != "x86_64":
            video_models = [
                item
                for item in video_models
                if item in {"virtio", "vga", "ramfb"}
            ] or ["virtio", "vga", "ramfb"]
        self._populate(
            self.video_combo,
            video_models,
            config.get("video", "vga"),
            ("virtio", "vga", "ramfb")
            if self.arch != "x86_64"
            else ("virtio", "vga", "qxl", "cirrus", "vmvga", "bochs", "ramfb"),
        )
        self.video_label = FieldLabel(
            "Video",
            "",
            kind="info",
            color=self._theme_tokens.muted,
        )
        form.addRow(self.video_label, self.video_combo)
        self.video_memory_combo = ComboBox()
        for value in (64, 128, 256):
            self.video_memory_combo.addItem(f"{value} MB", value)
        current_memory = int(config.get("video_memory_mib") or 64)
        index = self.video_memory_combo.findData(current_memory)
        if index < 0:
            index = self.video_memory_combo.findData(64)
        self.video_memory_combo.setCurrentIndex(index)
        self.video_memory_combo.setToolTip(
            "Configurable for VGA or QXL. VirtIO and other models use a "
            "different memory model."
        )
        self.video_combo.currentIndexChanged.connect(
            self._update_video_memory_config
        )
        form.addRow("Video memory", self.video_memory_combo)
        self.video_badge = self.video_label.badge
        self._update_video_memory_config()
        layout.addLayout(form)
        note = QLabel(
            "Memory can be adjusted live through the balloon when the new "
            "value is within the boot maximum. vCPUs can be hotplugged up "
            "to the boot maximum; video and video-memory changes apply after "
            "a restart."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _build_storage_tab(self, config: dict[str, Any]) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.disks = EmptyStateTable(0, 6)
        self.disks.setHorizontalHeaderLabels(
            ["DEVICE", "BUS", "TARGET", "FORMAT", "SIZE", "SOURCE"]
        )
        self._setup_table(self.disks, (80, 70, 70, 70, 80, 0))
        self.disks.setMinimumHeight(160)
        self.disks.itemSelectionChanged.connect(self._disk_selection)
        layout.addWidget(self.disks, 1)

        disk_actions = QHBoxLayout()
        self.add_disk_button = QPushButton("Add disk")
        self.add_disk_button.setObjectName("SecondaryButton")
        self.add_disk_button.clicked.connect(self._add_disk)
        disk_actions.addWidget(self.add_disk_button)
        self.remove_disk_button = QPushButton("Remove disk")
        self.remove_disk_button.setObjectName("SecondaryButton")
        self.remove_disk_button.setEnabled(False)
        self.remove_disk_button.clicked.connect(self._remove_disk)
        disk_actions.addWidget(self.remove_disk_button)
        self.resize_disk_button = QPushButton("Expand disk…")
        self.resize_disk_button.setObjectName("SecondaryButton")
        self.resize_disk_button.setEnabled(False)
        self.resize_disk_button.clicked.connect(self._resize_disk)
        disk_actions.addWidget(self.resize_disk_button)
        self.change_media_button = QPushButton("Change ISO")
        self.change_media_button.setObjectName("SecondaryButton")
        self.change_media_button.setEnabled(False)
        self.change_media_button.clicked.connect(self._change_media)
        disk_actions.addWidget(self.change_media_button)
        self.eject_media_button = QPushButton("Eject ISO")
        self.eject_media_button.setObjectName("SecondaryButton")
        self.eject_media_button.setEnabled(False)
        self.eject_media_button.clicked.connect(self._eject_media)
        disk_actions.addWidget(self.eject_media_button)
        disk_actions.addStretch(1)
        layout.addLayout(disk_actions)
        self.pending_label = QLabel("")
        self.pending_label.setObjectName("Muted")
        self.pending_label.setWordWrap(True)
        layout.addWidget(self.pending_label)
        self._fill_disks(config.get("disks", []))

        note = QLabel(
            "Add disk is staged until Save hardware. Remove and expand apply "
            "immediately. Choose the bus in Add disk; it is fixed after the "
            "disk is created. Expand only grows the virtual disk; shrinking "
            "is not supported. Grow the guest partition and filesystem "
            "afterwards. Select a CD/DVD row to change or eject its media "
            "live; Force change/eject is offered if the guest has locked the "
            "tray."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        return page

    def _build_network_tab(self, config: dict[str, Any]) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        top = QFormLayout()
        top.setSpacing(8)
        self.nic_combo = ComboBox()
        nic_models = {nic.get("model") for nic in config.get("nics", [])}
        current_nic = next(iter(nic_models), "virtio")
        self._populate(
            self.nic_combo,
            self.capabilities.get("nic_models", []),
            current_nic,
            ("virtio", "e1000e", "e1000", "rtl8139", "vmxnet3", "pcnet"),
        )
        top.addRow("NIC model", self.nic_combo)
        layout.addLayout(top)

        self.nics = EmptyStateTable(0, 4)
        self.nics.setHorizontalHeaderLabels(
            ["MAC", "NETWORK", "MODEL", "LINK"]
        )
        self._setup_table(self.nics, (170, 0, 100, 90))
        self.nics.setMinimumHeight(150)
        self.nics.itemSelectionChanged.connect(self._nic_selection)
        layout.addWidget(self.nics, 1)

        nic_actions = QHBoxLayout()
        self.add_nic_button = QPushButton("Add NIC")
        self.add_nic_button.setObjectName("SecondaryButton")
        self.add_nic_button.clicked.connect(self._add_nic)
        nic_actions.addWidget(self.add_nic_button)
        self.remove_nic_button = QPushButton("Remove NIC")
        self.remove_nic_button.setObjectName("SecondaryButton")
        self.remove_nic_button.setEnabled(False)
        self.remove_nic_button.clicked.connect(self._remove_nic)
        nic_actions.addWidget(self.remove_nic_button)
        self.nic_link_button = QPushButton("Disconnect")
        self.nic_link_button.setObjectName("SecondaryButton")
        self.nic_link_button.setEnabled(False)
        self.nic_link_button.setToolTip(
            "Connect or disconnect this NIC without removing it."
        )
        self.nic_link_button.clicked.connect(self._toggle_nic_link)
        nic_actions.addWidget(self.nic_link_button)
        self.change_network_button = QPushButton("Change network…")
        self.change_network_button.setObjectName("SecondaryButton")
        self.change_network_button.setEnabled(False)
        self.change_network_button.setToolTip(
            "Move this NIC to another v-EMU network profile."
        )
        self.change_network_button.clicked.connect(self._change_nic_network)
        nic_actions.addWidget(self.change_network_button)
        nic_actions.addStretch(1)
        layout.addLayout(nic_actions)
        self._fill_nics(config.get("nics", []))

        note = QLabel(
            "Connect/Disconnect changes the link state live when the VM is "
            "running. Changing the network/vSwitch requires the VM to be "
            "stopped; the MAC address is preserved. Create network profiles, "
            "reservations and port forwards on the Networks page."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        return page

    def _build_devices_tab(self, config: dict[str, Any]) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        device_actions = QHBoxLayout()
        self.tpm_button = QPushButton("Add TPM 2.0")
        self.tpm_button.setObjectName("SecondaryButton")
        self.tpm_button.clicked.connect(self._toggle_tpm)
        device_actions.addWidget(self.tpm_button)
        self.usb_button = QPushButton("Add USB xHCI")
        self.usb_button.setObjectName("SecondaryButton")
        self.usb_button.clicked.connect(self._add_usb)
        device_actions.addWidget(self.usb_button)
        self.audio_button = QPushButton("Add audio")
        self.audio_button.setObjectName("SecondaryButton")
        self.audio_button.clicked.connect(self._toggle_audio)
        device_actions.addWidget(self.audio_button)
        self.serial_button = QPushButton("Add serial console")
        self.serial_button.setObjectName("SecondaryButton")
        self.serial_button.clicked.connect(self._add_serial)
        device_actions.addWidget(self.serial_button)
        device_actions.addStretch(1)
        layout.addLayout(device_actions)
        self._update_device_buttons()

        self.devices = EmptyStateTable(0, 2)
        self.devices.setHorizontalHeaderLabels(["TYPE", "DETAILS"])
        self._setup_table(self.devices, (130, 0))
        self.devices.setMinimumHeight(180)
        layout.addWidget(self.devices, 1)
        self._fill_devices(config)

        note = QLabel(
            "Read-only inventory from the domain XML. PCI controllers are "
            "summarised. TPM, USB and audio changes apply after a restart."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        return page

    def _fill_disks(self, disks: list[dict[str, Any]]) -> None:
        self.disks.setRowCount(0)
        for disk in disks:
            row = self.disks.rowCount()
            self.disks.insertRow(row)
            for column, value in enumerate(
                (
                    disk.get("device", ""),
                    disk.get("bus", ""),
                    disk.get("target", ""),
                    disk.get("format", ""),
                    (
                        f"{disk['size_gb']} GB"
                        if disk.get("size_gb") is not None
                        else ""
                    ),
                    disk.get("source", ""),
                )
            ):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.disks.setItem(row, column, item)
        for pending in self._pending_disks:
            row = self.disks.rowCount()
            self.disks.insertRow(row)
            values = (
                "disk (pending)",
                pending.get("bus", ""),
                "-",
                pending.get("format", ""),
                (
                    f"{pending['size_gb']} GB"
                    if pending.get("create_file")
                    else "existing"
                ),
                pending.get("path", ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.disks.setItem(row, column, item)
        self._update_pending_label()
        self.disks.refresh_empty_state()
        self.remove_disk_button.setEnabled(False)
        self.resize_disk_button.setEnabled(False)
        self.change_media_button.setEnabled(False)
        self.eject_media_button.setEnabled(False)
        # The caller may restore the row that triggered a reload.
        target = getattr(self, "_restore_disk_target", None)
        if target:
            for row in range(self.disks.rowCount()):
                item = self.disks.item(row, 2)
                if item is not None and item.text() == target:
                    self.disks.selectRow(row)
                    break
        self._restore_disk_target = None
        self._disk_selection()

    def _update_pending_label(self) -> None:
        if not hasattr(self, "pending_label"):
            return
        if not self._pending_disks:
            self.pending_label.setText("")
            return
        lines = [
            f"{item['path']} ({item['bus']}, "
            + (
                f"{item['size_gb']} GB"
                if item.get("create_file")
                else "existing image"
            )
            + ")"
            for item in self._pending_disks
        ]
        self.pending_label.setText(
            "Pending disks — added when you click Save hardware:\n"
            + "\n".join(f"• {line}" for line in lines)
        )

    def _disk_selection(self) -> None:
        row = self.disks.currentRow()
        device = ""
        source = ""
        size = ""
        if row >= 0:
            item = self.disks.item(row, 0)
            device = item.text() if item is not None else ""
            item = self.disks.item(row, 4)
            size = item.text() if item is not None else ""
            item = self.disks.item(row, 5)
            source = item.text() if item is not None else ""
        self.remove_disk_button.setEnabled(device == "disk")
        self.resize_disk_button.setEnabled(device == "disk" and bool(size))
        cdrom = device == "cdrom"
        self.change_media_button.setEnabled(cdrom)
        self.eject_media_button.setEnabled(cdrom and bool(source))

    def _add_disk(self) -> None:
        disks = self.payload["config"].get("disks", [])
        default_dir = str(Path.home() / "vemu-labs")
        for disk in disks:
            if disk.get("device") == "disk" and disk.get("source"):
                default_dir = str(Path(disk["source"]).parent)
                break
        reserved = {
            Path(item["path"]).expanduser()
            for item in self._pending_disks
        }
        dialog = DiskAddDialog(
            self.backend,
            self.domain,
            default_dir,
            self,
            reserved_paths=reserved,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.spec:
            self._pending_disks.append(dialog.spec)
            self._fill_disks(disks)

    def _remove_disk(self) -> None:
        row = self.disks.currentRow()
        if row < 0:
            return
        item = self.disks.item(row, 5)
        if item is None:
            return
        source = item.text()
        answer = QMessageBox.question(
            self,
            "Remove disk",
            f"Detach {source}? The disk file is kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            payload = self.backend.vm_disk_remove(
                domain=self.domain, source=source, delete_file=False
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot remove disk", str(exc))
            return
        self._reload_disks()
        if payload.get("pending_restart"):
            QMessageBox.information(
                self,
                "Disk removed",
                "The disk was detached from the persistent definition. "
                "Restart the VM to apply the change.",
            )

    def _resize_disk(self) -> None:
        row = self.disks.currentRow()
        if row < 0:
            return
        source_item = self.disks.item(row, 5)
        size_item = self.disks.item(row, 4)
        if source_item is None or size_item is None:
            return
        source = source_item.text()
        disk = next(
            (
                item
                for item in self.payload.get("config", {}).get("disks", [])
                if item.get("source") == source
            ),
            None,
        )
        current = float(disk.get("size_gb") or 0) if disk else 0
        default = int(current) + 10 if current else 20
        minimum = max(1, int(current))
        size, ok = QInputDialog.getInt(
            self,
            "Expand disk",
            (
                f"New size (GB) for {Path(source).name} "
                f"(current: {current:g} GB).\n\n"
                "Only increasing is supported; shrinking needs offline "
                "guest-level preparation."
            ),
            default,
            minimum,
            65536,
        )
        if not ok:
            return
        answer = QMessageBox.question(
            self,
            "Expand disk",
            "This expands the virtual disk. You still need to grow the "
            "partition and filesystem inside the guest.\n\n"
            "Shrinking is not supported. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            payload = self.backend.vm_disk_resize(
                domain=self.domain, source=source, size_gb=size
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot expand disk", str(exc))
            return
        self._reload_disks()
        if payload.get("changed"):
            where = "live" if payload.get("live") else "for the next start"
            QMessageBox.information(
                self,
                "Disk expanded",
                f"{payload['disk']['target']} is now {size} GB ({where}). "
                "Grow the partition and filesystem inside the guest.",
            )

    def _change_media(self) -> None:
        row = self.disks.currentRow()
        if row < 0:
            return
        target_item = self.disks.item(row, 2)
        source_item = self.disks.item(row, 5)
        target = target_item.text() if target_item is not None else ""
        current = source_item.text() if source_item is not None else ""
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose media",
            current or "/home/ISO",
            "ISO images (*.iso);;All files (*)",
        )
        if not path:
            return
        try:
            payload = self.backend.vm_media_change(
                self.domain, path, target or None
            )
        except VemuError as exc:
            answer = QMessageBox.question(
                self,
                "Cannot change media",
                f"{exc}\n\nForce change the tray anyway?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                payload = self.backend.vm_media_change(
                    self.domain, path, target or None, force=True
                )
            except VemuError as forced_exc:
                QMessageBox.critical(
                    self, "Cannot force change media", str(forced_exc)
                )
                return
        self._restore_disk_target = target
        self._reload_disks()
        if not payload.get("live"):
            QMessageBox.information(
                self,
                "Media changed",
                "The media was updated in the persistent definition. "
                "Start the VM to use it.",
            )

    def _eject_media(self) -> None:
        row = self.disks.currentRow()
        if row < 0:
            return
        target_item = self.disks.item(row, 2)
        source_item = self.disks.item(row, 5)
        target = target_item.text() if target_item is not None else ""
        source = source_item.text() if source_item is not None else ""
        answer = QMessageBox.question(
            self,
            "Eject media",
            f"Eject {source or 'the current media'}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            payload = self.backend.vm_media_eject(
                self.domain, target or None
            )
        except VemuError as exc:
            answer = QMessageBox.question(
                self,
                "Cannot eject media",
                f"{exc}\n\nForce eject the tray anyway?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                payload = self.backend.vm_media_eject(
                    self.domain, target or None, force=True
                )
            except VemuError as forced_exc:
                QMessageBox.critical(
                    self, "Cannot force eject media", str(forced_exc)
                )
                return
        self._restore_disk_target = target
        self._reload_disks()
        if not payload.get("live"):
            QMessageBox.information(
                self,
                "Media ejected",
                "The change is saved for the next start.",
            )

    def _update_device_buttons(self) -> None:
        config = self.payload.get("config", {})
        self.tpm_button.setText(
            "Remove TPM" if config.get("tpm") else "Add TPM 2.0"
        )
        usb = bool(config.get("usb"))
        self.usb_button.setEnabled(not usb)
        self.usb_button.setText(
            "USB xHCI present" if usb else "Add USB xHCI"
        )
        audio = bool(config.get("audio"))
        self.audio_button.setText(
            "Remove audio" if audio else "Add audio"
        )
        serial = bool(config.get("serial"))
        stopped = self.payload.get("state") == "stopped"
        self.serial_button.setEnabled(not serial and stopped)
        self.serial_button.setText(
            "Serial console present" if serial else "Add serial console"
        )
        self.serial_button.setToolTip(
            "Add a pty serial/console pair; the VM must be stopped."
        )

    def _toggle_tpm(self) -> None:
        has_tpm = bool(self.payload.get("config", {}).get("tpm"))
        if has_tpm:
            answer = QMessageBox.question(
                self,
                "Remove TPM",
                "Remove the TPM device from this VM?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            if has_tpm:
                payload = self.backend.vm_tpm_remove(domain=self.domain)
            else:
                payload = self.backend.vm_tpm_add(domain=self.domain)
        except VemuError as exc:
            QMessageBox.critical(self, "TPM", str(exc))
            return
        self._reload_disks()
        if payload.get("pending_restart"):
            QMessageBox.information(
                self,
                "TPM",
                "TPM change saved. Restart the VM to apply it.",
            )

    def _add_serial(self) -> None:
        try:
            payload = self.backend.vm_serial_add(self.domain)
        except VemuError as exc:
            QMessageBox.critical(self, "Serial console", str(exc))
            return
        self._reload_disks()
        if payload.get("changed"):
            QMessageBox.information(
                self,
                "Serial console",
                "A serial console was added. Start the VM, then use "
                "Mode → Serial in the console window.",
            )

    def _add_usb(self) -> None:
        try:
            payload = self.backend.vm_usb_add(domain=self.domain)
        except VemuError as exc:
            QMessageBox.critical(self, "USB", str(exc))
            return
        self._reload_disks()
        if payload.get("pending_restart"):
            QMessageBox.information(
                self,
                "USB",
                "USB controller added. Restart the VM to apply it.",
            )

    def _toggle_audio(self) -> None:
        has_audio = bool(self.payload.get("config", {}).get("audio"))
        if has_audio:
            answer = QMessageBox.question(
                self,
                "Remove audio",
                "Remove the audio device from this VM?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            if has_audio:
                payload = self.backend.vm_audio_remove(domain=self.domain)
            else:
                payload = self.backend.vm_audio_add(domain=self.domain)
        except VemuError as exc:
            QMessageBox.critical(self, "Audio", str(exc))
            return
        self._reload_disks()
        if payload.get("pending_restart"):
            QMessageBox.information(
                self,
                "Audio",
                "Audio change saved. Restart the VM to apply it.",
            )

    def _fill_devices(self, config: dict[str, Any]) -> None:
        rows: list[tuple[str, str]] = []
        pci_count = 0
        for controller in config.get("controllers", []):
            controller_type = str(controller.get("type", ""))
            if controller_type == "pci":
                pci_count += 1
                continue
            detail = controller_type
            if controller.get("model"):
                detail += f" {controller['model']}"
            if controller.get("index"):
                detail += f" index={controller['index']}"
            if controller.get("serial"):
                detail += f" serial={controller['serial']}"
            rows.append(("controller", detail))
        if pci_count:
            rows.append(("controller", f"pci: {pci_count} controllers"))
        for device in config.get("other_devices", []):
            rows.append(
                (str(device.get("type", "")), str(device.get("detail", "")))
            )
        self.devices.setRowCount(0)
        for device_type, detail in rows:
            row = self.devices.rowCount()
            self.devices.insertRow(row)
            for column, value in enumerate((device_type, detail)):
                item = QTableWidgetItem(value)
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.devices.setItem(row, column, item)
        self.devices.refresh_empty_state()

    def _fill_nics(self, nics: list[dict[str, Any]]) -> None:
        self.nics.setRowCount(0)
        for nic in nics:
            row = self.nics.rowCount()
            self.nics.insertRow(row)
            up = str(nic.get("link", "up")).lower() != "down"
            values = (
                nic.get("mac", ""),
                nic.get("network", "") or nic.get("bridge", ""),
                nic.get("model", ""),
                "Up" if up else "Down",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                if column == 3 and not up:
                    item.setForeground(QColor("#e5a50a"))
                self.nics.setItem(row, column, item)
        self.nics.refresh_empty_state()
        self.nics.clearSelection()
        self._nic_selection()

    def _selected_nic(self) -> dict[str, Any] | None:
        row = self.nics.currentRow()
        if row < 0:
            return None
        nics = self.payload.get("config", {}).get("nics", [])
        if row >= len(nics):
            return None
        return nics[row]

    def _nic_selection(self) -> None:
        nic = self._selected_nic()
        selected = nic is not None
        self.remove_nic_button.setEnabled(selected)
        self.nic_link_button.setEnabled(selected)
        if not selected:
            self.nic_link_button.setText("Disconnect")
            self.change_network_button.setEnabled(False)
            return
        up = str(nic.get("link", "up")).lower() != "down"
        self.nic_link_button.setText("Disconnect" if up else "Connect")
        running = self.payload.get("state") == "running"
        self.change_network_button.setEnabled(not running)
        self.change_network_button.setToolTip(
            "Stop the VM before changing its network."
            if running
            else "Move this NIC to another v-EMU network profile."
        )

    def _toggle_nic_link(self) -> None:
        nic = self._selected_nic()
        if nic is None:
            return
        up = str(nic.get("link", "up")).lower() == "down"
        try:
            self.backend.vm_nic_link(
                self.domain, str(nic.get("mac", "")), up
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot change link", str(exc))
            return
        self._reload_disks()

    def _change_nic_network(self) -> None:
        nic = self._selected_nic()
        if nic is None:
            return
        if self.payload.get("state") == "running":
            QMessageBox.information(
                self,
                "Change network",
                "Stop the VM before changing its network.",
            )
            return
        try:
            networks = self.backend.networks_list()
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot list networks", str(exc))
            return
        if not networks:
            QMessageBox.information(
                self,
                "Change network",
                "No network profiles. Create one on the Networks page first.",
            )
            return
        labels = [
            f"{network['name']} ({network.get('mode', '')})"
            for network in networks
        ]
        current = str(nic.get("network", ""))
        index = next(
            (
                position
                for position, network in enumerate(networks)
                if str(network.get("name", "")) == current
            ),
            0,
        )
        choice, accepted = QInputDialog.getItem(
            self,
            "Change network",
            f"Network for {nic.get('mac', '')}",
            labels,
            index,
            False,
        )
        if not accepted:
            return
        position = labels.index(choice)
        try:
            self.backend.vm_nic_network(
                self.domain,
                str(nic.get("mac", "")),
                str(networks[position]["name"]),
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot change network", str(exc))
            return
        self._reload_disks()

    def _add_nic(self) -> None:
        try:
            networks = self.backend.networks_list()
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot list networks", str(exc))
            return
        if not networks:
            QMessageBox.information(
                self,
                "Add NIC",
                "No network profiles. Create one on the Networks page first.",
            )
            return
        models = self.capabilities.get(
            "nic_models",
            ["virtio", "e1000e", "e1000", "rtl8139", "vmxnet3", "pcnet"],
        )
        dialog = NicAddDialog(
            self.backend, self.domain, networks, models, self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._reload_disks()

    def _remove_nic(self) -> None:
        row = self.nics.currentRow()
        if row < 0:
            return
        item = self.nics.item(row, 0)
        if item is None:
            return
        mac = item.text()
        answer = QMessageBox.question(
            self,
            "Remove NIC",
            f"Detach interface {mac}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.network_detach(self.domain, mac)
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot remove NIC", str(exc))
            return
        self._reload_disks()

    def _reload_disks(self) -> None:
        try:
            self.payload = self.backend.vm_config_info(self.domain)
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot reload hardware", str(exc))
            return
        self._fill_disks(self.payload["config"].get("disks", []))
        self._fill_nics(self.payload["config"].get("nics", []))
        self._fill_devices(self.payload["config"])
        self._update_device_buttons()

    def _update_video_memory_config(self) -> None:
        video = str(self.video_combo.currentData() or "")
        self.video_memory_combo.setEnabled(video in {"vga", "qxl"})
        badge = getattr(self, "video_badge", None)
        if badge is None:
            return
        if video == "virtio":
            badge.set_message(
                "Windows guests need the virtio-win WDDM driver (viogpudo) "
                "for virtio GPU. Without it, keep ramfb or VGA.",
                kind="alert",
                color=self._theme_tokens.warning,
            )
        elif video == "qxl":
            badge.set_message(
                "QXL 128 MB is a good Windows lab choice when the QXL driver "
                "and guest agent are installed. 2D only.",
                kind="info",
                color=self._theme_tokens.muted,
            )
        else:
            badge.set_message(
                "Windows can use this model without a driver. After "
                "installing viogpudo from virtio-win, switch to virtio for "
                "2D WDDM; QXL 128 MB is also a good lab choice.",
                kind="info",
                color=self._theme_tokens.muted,
            )
        badge.setVisible(bool(badge.toolTip()))

    def _populate(
        self,
        combo: QComboBox,
        values: list[str],
        current: str,
        fallback: tuple[str, ...],
    ) -> None:
        ordered: list[str] = []
        for value in [*values, *fallback, current]:
            if value and value not in ordered:
                ordered.append(value)
        for value in ordered:
            combo.addItem(value, value)
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _setup_table(self, table: EmptyStateTable, widths) -> None:
        table.setItemDelegate(NoFocusDelegate(table))
        table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(28)
        table.horizontalHeader().setFixedHeight(28)
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
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)

    def _save(self) -> None:
        try:
            memory = int(round(self.memory_spin.value() * 1024))
            vcpus = int(self.vcpu_edit.text())
            vcpus_max = int(self.vcpu_max_edit.text())
        except ValueError:
            QMessageBox.warning(self, "Hardware", "Memory/vCPU values are invalid.")
            return
        if memory < 512:
            QMessageBox.warning(
                self, "Hardware", "Memory must be at least 0.5 GB."
            )
            return
        if vcpus_max < vcpus:
            QMessageBox.warning(
                self,
                "Hardware",
                "The vCPU boot maximum must not be lower than the current "
                "count.",
            )
            return
        self.save_button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            self.updated = self.backend.vm_config_update(
                domain=self.domain,
                memory_mib=memory,
                vcpus=vcpus,
                vcpus_max=vcpus_max,
                cpu_mode=str(self.cpu_combo.currentData()),
                machine=self.machine_edit.text().strip() or None,
                video=str(self.video_combo.currentData()),
                video_memory_mib=(
                    self.video_memory_combo.currentData()
                    if self.video_combo.currentData() in {"vga", "qxl"}
                    else None
                ),
                nic_model=str(self.nic_combo.currentData()),
                os_type=str(self.os_combo.currentData()),
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot update hardware", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.save_button.setEnabled(True)
        created: list[str] = []
        for pending in self._pending_disks:
            try:
                payload = self.backend.vm_disk_add(
                    domain=self.domain,
                    path=str(pending["path"]),
                    size_gb=int(pending.get("size_gb") or 0),
                    bus=str(pending.get("bus") or "virtio"),
                    format=str(pending.get("format") or "qcow2"),
                    create_file=bool(pending.get("create_file", True)),
                )
            except VemuError as exc:
                rollback_errors: list[str] = []
                for source in reversed(created):
                    try:
                        self.backend.vm_disk_remove(
                            domain=self.domain,
                            source=source,
                            delete_file=True,
                        )
                    except VemuError as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                message = str(exc)
                if rollback_errors:
                    message += "\n\nRollback issue: " + "; ".join(
                        rollback_errors
                    )
                QMessageBox.critical(self, "Cannot add disk", message)
                return
            created.append(str(payload["disk"]["source"]))
        if created:
            self._pending_disks.clear()
        warnings = self.updated.get("warnings") or []
        if warnings:
            QMessageBox.warning(
                self, "Hardware saved", "\n\n".join(str(item) for item in warnings)
            )
        live_notes: list[str] = []
        if self.updated.get("live_memory"):
            live_notes.append(
                "The memory balloon was adjusted. The guest must have the "
                "balloon driver for the change to take effect."
            )
        if self.updated.get("live_vcpus"):
            live_notes.append(
                "The vCPUs were hotplugged into the running guest."
            )
        if live_notes:
            QMessageBox.information(
                self,
                "Hardware updated live",
                "\n\n".join(live_notes),
            )
        elif self.updated.get("pending_restart"):
            QMessageBox.information(
                self,
                "Hardware saved",
                "The VM is running. Changes are saved to the persistent "
                "definition and take effect after a restart.",
            )
        if created:
            QMessageBox.information(
                self,
                "Disks added",
                f"Added {len(created)} disk(s) to the persistent definition. "
                "Restart the VM if it is running.",
            )
        self.accept()
