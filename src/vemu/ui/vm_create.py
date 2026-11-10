"""Multi-step New VM wizard for ISO installs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..errors import VemuError
from ..virtcheck import domain_type_for, host_arch
from .components import FieldLabel, form_layout
from .theme import LIGHT, Tokens
from ..provision import (
    DEFAULT_ADMIN_PASSWORD,
    OS_FAMILIES,
    OS_VARIANTS,
    WINDOWS_EDITIONS,
    detect_iso_os,
)
from .dialogs import VemuDialog, dialog_layout
from .widgets import ComboBox

STEP_TITLES = (
    "Name and operating system",
    "Hardware",
    "Storage and network",
    "Summary",
)

# Safe hardware defaults per variant. Users can still edit every field.
HARDWARE_PRESETS: dict[str, dict[str, Any]] = {
    "windows-11": {
        "memory": "4",
        "vcpus": "2",
        "disk": "64",
        "firmware": "efi",
        "machine": "q35",
        "disk_bus": "sata",
        "nic": "e1000e",
        "video": "vga",
        "video_memory": 64,
        "tpm": True,
    },
    "windows-server-2025": {
        "memory": "4",
        "vcpus": "2",
        "disk": "64",
        "firmware": "bios",
        "machine": "q35",
        "disk_bus": "sata",
        "nic": "e1000e",
        "video": "vga",
        "video_memory": 64,
        "tpm": False,
    },
    "windows-server-other": {
        "memory": "4",
        "vcpus": "2",
        "disk": "64",
        "firmware": "bios",
        "machine": "q35",
        "disk_bus": "sata",
        "nic": "e1000e",
        "video": "vga",
        "video_memory": 64,
        "tpm": False,
    },
    "windows-10": {
        "memory": "4",
        "vcpus": "2",
        "disk": "64",
        "firmware": "bios",
        "machine": "q35",
        "disk_bus": "sata",
        "nic": "e1000e",
        "video": "vga",
        "video_memory": 64,
        "tpm": False,
    },
    "appliance": {
        "memory": "2",
        "vcpus": "2",
        "disk": "8",
        "firmware": "bios",
        "machine": "q35",
        "disk_bus": "sata",
        "nic": "e1000e",
        "video": "vga",
        "video_memory": 64,
        "tpm": False,
    },
    "windows-client": {
        "memory": "4",
        "vcpus": "2",
        "disk": "64",
        "firmware": "bios",
        "machine": "q35",
        "disk_bus": "sata",
        "nic": "e1000e",
        "video": "vga",
        "video_memory": 64,
        "tpm": False,
    },
    "esxi-8-9": {
        "memory": "8",
        "vcpus": "2",
        "disk": "128",
        "firmware": "efi",
        "machine": "q35",
        "disk_bus": "sata",
        "nic": "e1000e",
        "video": "vga",
        "video_memory": 64,
        "tpm": True,
    },
}

LINUX_PRESET = {
    "memory": "2",
    "vcpus": "2",
    "disk": "64",
    "firmware": "efi",
    "machine": "q35",
    "disk_bus": "virtio",
    "nic": "virtio",
    "video": "vga",
    "video_memory": 64,
    "tpm": False,
}

OTHER_PRESET = {
    "memory": "2",
    "vcpus": "2",
    "disk": "64",
    "firmware": "bios",
    "machine": "q35",
    "disk_bus": "sata",
    "nic": "e1000e",
    "video": "vga",
    "tpm": False,
}


def recommended_emulated_resources() -> tuple[int, float]:
    """Recommend TCG resources relative to the host, not fixed values.

    TCG uses host CPU threads and memory; a fifth of the host RAM with a 12 GB
    ceiling matches the validated ARM64 rig on a typical workstation without
    starving smaller machines.
    """
    cpus = os.cpu_count() or 4
    vcpus = max(2, min(6, cpus // 2))
    total_gb = 8.0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                total_gb = int(line.split()[1]) / 1024 / 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    memory = max(2.0, min(12.0, round(total_gb / 4 * 2) / 2))
    return vcpus, memory


class NewVmDialog(VemuDialog):
    def __init__(
        self, backend, parent=None, *, tokens: Tokens | None = None
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self._theme_tokens = tokens or LIGHT
        self.created: dict[str, Any] | None = None
        self.detected: dict[str, Any] | None = None
        self.step = 0
        self._manual: set[str] = set()
        self._preset_guard = False
        self._hardware_visited = False
        self.setWindowTitle("New virtual machine")
        self.resize(760, 560)

        layout = dialog_layout(self)

        title = QLabel("New virtual machine")
        title.setObjectName("HeroTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Create a VM from an installer ISO. Each step keeps your choices "
            "when you go back and forth."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        self.step_label = QLabel()
        self.step_label.setObjectName("Muted")
        layout.addWidget(self.step_label)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_os_page())
        self.stack.addWidget(self._build_hardware_page())
        self.stack.addWidget(self._build_storage_page())
        self.stack.addWidget(self._build_summary_page())
        layout.addWidget(self.stack, 1)

        buttons = QHBoxLayout()
        self.back_button = QPushButton("Back")
        self.back_button.setObjectName("SecondaryButton")
        self.back_button.clicked.connect(self._back)
        buttons.addWidget(self.back_button)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.next_button = QPushButton("Next")
        self.next_button.setObjectName("PrimaryButton")
        self.next_button.clicked.connect(self._next)
        buttons.addWidget(self.next_button)
        self.create_button = QPushButton("Create VM")
        self.create_button.setObjectName("PrimaryButton")
        self.create_button.clicked.connect(self._create)
        buttons.addWidget(self.create_button)
        layout.addLayout(buttons)

        self._prefill_paths()
        self._load_networks()
        self._family_changed()
        self._detect_iso()
        self._apply_capabilities()
        self._sync_navigation()

    # ------------------------------------------------------------------ pages

    def _build_os_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        form = form_layout()
        self.guest_form = form
        self.form_layout = form

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("win-lab-1")
        form.addRow("Name", self.name_edit)

        self.iso_edit, iso_row = self._path_row(
            "Install ISO", "Choose installer ISO", self._browse_iso
        )
        self.iso_edit.editingFinished.connect(self._detect_iso)
        form.addRow("Install ISO", iso_row)

        self.detected_label = QLabel("No ISO selected")
        self.detected_label.setObjectName("Muted")
        form.addRow("Detected", self.detected_label)

        self.family_combo = ComboBox()
        for key, label in OS_FAMILIES.items():
            self.family_combo.addItem(label, key)
        self.family_combo.currentIndexChanged.connect(self._family_changed)
        form.addRow("OS family", self.family_combo)

        self.variant_combo = ComboBox()
        self.variant_combo.currentIndexChanged.connect(self._variant_changed)
        form.addRow("OS version", self.variant_combo)

        self.unattend_check = QCheckBox(
            "Unattended install (Windows Server 2025 answer file)"
        )
        self.unattend_check.toggled.connect(self._unattend_changed)
        form.addRow("Install mode", self.unattend_check)

        self.edition_combo = ComboBox()
        for key, (_index, label) in WINDOWS_EDITIONS.items():
            self.edition_combo.addItem(label, key)
        self.edition_combo.setCurrentIndex(
            self.edition_combo.findData("standard")
        )
        form.addRow("Windows edition", self.edition_combo)

        self.password_edit = QLineEdit(DEFAULT_ADMIN_PASSWORD)
        form.addRow("Admin password", self.password_edit)

        layout.addLayout(form)
        for field in (
            self.name_edit,
            iso_row,
            self.detected_label,
            self.family_combo,
            self.variant_combo,
            self.unattend_check,
            self.edition_combo,
            self.password_edit,
        ):
            label = form.labelForField(field)
            if label is not None:
                label.setFixedWidth(140)
        self.helper_label = QLabel()
        self.helper_label.setObjectName("Muted")
        self.helper_label.setWordWrap(True)
        layout.addWidget(self.helper_label)
        layout.addStretch(1)
        return page

    def _build_hardware_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        grid = QGridLayout()
        grid.setSpacing(10)

        resources = QGroupBox("CPU and memory")
        resources_form = form_layout(resources)
        self.memory_spin = QDoubleSpinBox()
        self.memory_spin.setRange(0.5, 64.0)
        self.memory_spin.setDecimals(1)
        self.memory_spin.setSingleStep(0.5)
        self.memory_spin.setSuffix(" GB")
        self.memory_spin.setValue(4.0)
        self.memory_spin.valueChanged.connect(
            lambda _value: self._mark_manual("memory")
        )
        resources_form.addRow("Memory", self.memory_spin)
        self.vcpu_edit = QLineEdit("2")
        self.vcpu_edit.setValidator(QIntValidator(1, 64, self))
        self.vcpu_edit.textChanged.connect(
            lambda _text: self._mark_manual("vcpus")
        )
        resources_form.addRow("vCPUs", self.vcpu_edit)
        self.cpu_combo = ComboBox()
        for mode in ("host-passthrough", "host-model", "custom", "maximum"):
            self.cpu_combo.addItem(mode, mode)
        resources_form.addRow("CPU mode", self.cpu_combo)
        grid.addWidget(resources, 0, 0)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        machine_group = QGroupBox("Machine and display")
        machine_form = form_layout(machine_group)
        self.arch_combo = ComboBox()
        self.arch_combo.addItem("x86_64", "x86_64")
        self.arch_combo.currentIndexChanged.connect(self._arch_changed)
        machine_form.addRow("Architecture", self.arch_combo)
        self.firmware_combo = ComboBox()
        self.firmware_combo.addItem("BIOS", "bios")
        self.firmware_combo.addItem("UEFI", "efi")
        machine_form.addRow("Firmware", self.firmware_combo)
        self.machine_combo = ComboBox()
        self.machine_combo.addItem("q35", "q35")
        self.machine_combo.addItem("pc (i440fx)", "pc")
        machine_form.addRow("Machine", self.machine_combo)
        self.video_combo = ComboBox()
        for model in ("vga", "virtio", "ramfb", "qxl", "cirrus", "vmvga", "bochs"):
            self.video_combo.addItem(model, model)
        self.video_label = FieldLabel(
            "Video",
            "",
            kind="info",
            color=self._theme_tokens.muted,
        )
        machine_form.addRow(self.video_label, self.video_combo)
        self.video_memory_combo = ComboBox()
        for value in (64, 128, 256):
            self.video_memory_combo.addItem(f"{value} MB", value)
        self.video_memory_combo.setCurrentIndex(
            self.video_memory_combo.findData(64)
        )
        self.video_memory_combo.setToolTip(
            "Configurable for VGA or QXL. VirtIO and other models use a "
            "different memory model."
        )
        self.video_memory_combo.currentIndexChanged.connect(
            lambda _index: self._mark_manual("video_memory")
        )
        self.video_combo.currentIndexChanged.connect(
            self._update_video_memory
        )
        machine_form.addRow("Video memory", self.video_memory_combo)
        self.video_badge = self.video_label.badge
        self.tpm_check = QCheckBox("TPM 2.0")
        self.tpm_check.toggled.connect(self._update_summary)
        machine_form.addRow("Security", self.tpm_check)
        self.secure_boot_check = QCheckBox("Secure Boot (UEFI, x86_64)")
        self.secure_boot_check.toggled.connect(self._update_summary)
        machine_form.addRow("", self.secure_boot_check)
        self.firmware_combo.currentIndexChanged.connect(
            self._update_security_options
        )
        grid.addWidget(machine_group, 0, 1)

        disk_group = QGroupBox("Primary disk")
        disk_form = form_layout(disk_group)
        self.disk_edit = QLineEdit("64")
        self.disk_edit.setValidator(QIntValidator(8, 1000, self))
        self.disk_edit.textChanged.connect(
            lambda _text: self._mark_manual("disk")
        )
        disk_form.addRow("Disk size (GB)", self.disk_edit)
        self.disk_bus_combo = ComboBox()
        for bus in ("sata", "virtio", "nvme", "scsi", "ide", "usb"):
            self.disk_bus_combo.addItem(bus, bus)
        disk_form.addRow("Disk bus", self.disk_bus_combo)
        grid.addWidget(disk_group, 1, 0, 1, 2)

        layout.addLayout(grid)
        self.arch_note = QLabel("Native x86_64 KVM.")
        self.arch_note.setObjectName("Muted")
        self.arch_note.setWordWrap(True)
        layout.addWidget(self.arch_note)
        self.defaults_note = QLabel(
            "Defaults follow the guest OS. Windows, BSD and ESXi use SATA + "
            "e1000e; Linux uses virtio. NVMe requires q35."
        )
        self.defaults_note.setObjectName("Muted")
        self.defaults_note.setWordWrap(True)
        layout.addWidget(self.defaults_note)
        layout.addStretch(1)
        return page

    def _build_storage_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        storage_group = QGroupBox("Storage")
        storage_form = form_layout(storage_group)
        self.dest_edit, dest_row = self._path_row(
            "Destination", "Choose VM storage directory", self._browse_dest
        )
        storage_form.addRow("Destination", dest_row)
        self.virtio_edit, virtio_layout = self._path_row(
            "Driver ISO", "Choose virtio-win ISO", self._browse_virtio
        )
        self.virtio_edit.setPlaceholderText("")
        # The row is wrapped in a widget so it can be hidden with its label.
        # Remove the layout's default margins; otherwise the field sits lower
        # and taller than the other form rows.
        virtio_layout.setContentsMargins(0, 0, 0, 0)
        self.virtio_row = QWidget()
        self.virtio_row.setLayout(virtio_layout)
        storage_form.addRow("Driver ISO", self.virtio_row)
        self.virtio_label = storage_form.labelForField(self.virtio_row)
        self.iso_bus_combo = ComboBox()
        for bus in ("sata", "usb", "scsi", "ide"):
            self.iso_bus_combo.addItem(bus, bus)
        storage_form.addRow("Installer ISO bus", self.iso_bus_combo)
        layout.addWidget(storage_group)

        network_group = QGroupBox("Network")
        network_form = form_layout(network_group)
        self.network_combo = ComboBox()
        self.network_combo.addItem("default", "default")
        network_form.addRow("Network", self.network_combo)
        self.nic_combo = ComboBox()
        for model in ("e1000e", "virtio", "e1000", "rtl8139", "vmxnet3", "pcnet"):
            self.nic_combo.addItem(model, model)
        network_form.addRow("NIC model", self.nic_combo)
        layout.addWidget(network_group)

        self.storage_note = QLabel()
        self.storage_note.setObjectName("Muted")
        self.storage_note.setWordWrap(True)
        layout.addWidget(self.storage_note)
        layout.addStretch(1)
        return page

    def _build_summary_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        heading = QLabel("Review the new VM")
        heading.setObjectName("CardTitle")
        layout.addWidget(heading)
        self.summary_label = QLabel()
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        self.summary_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.summary_label, 1)

        self.start_check = QCheckBox("Start the VM after creating it")
        self.start_check.setChecked(
            QSettings("v-EMU", "v-EMU").value(
                "vm/start_after_create", True, type=bool
            )
        )
        layout.addWidget(self.start_check)
        return page

    # ------------------------------------------------------------------ navigation

    def _sync_navigation(self) -> None:
        step = min(self.step, len(STEP_TITLES) - 1)
        self.step_label.setText(
            f"Step {step + 1} of {len(STEP_TITLES)} · {STEP_TITLES[step]}"
        )
        self.back_button.setVisible(step > 0)
        self.next_button.setVisible(step < len(STEP_TITLES) - 1)
        self.create_button.setVisible(step == len(STEP_TITLES) - 1)
        if step == len(STEP_TITLES) - 1:
            self._update_summary()

    def _next(self) -> None:
        if not self._validate_step(self.step):
            return
        if self.step < len(STEP_TITLES) - 1:
            if self.step == 0:
                # Resource fields live on the hardware step; once the user has
                # seen them, later OS/ISO changes must not reset their values.
                self._hardware_visited = True
            self.step += 1
            self.stack.setCurrentIndex(self.step)
            self._sync_navigation()

    def _back(self) -> None:
        if self.step > 0:
            self.step -= 1
            self.stack.setCurrentIndex(self.step)
            self._sync_navigation()

    def _validate_step(self, step: int) -> bool:
        if step == 0:
            if not self.name_edit.text().strip():
                QMessageBox.warning(self, "New VM", "A VM name is required.")
                return False
            if not self.iso_edit.text().strip():
                QMessageBox.warning(
                    self, "New VM", "Choose an installer ISO."
                )
                return False
            if self.unattend_check.isChecked():
                if len(self.password_edit.text()) < 8:
                    QMessageBox.warning(
                        self,
                        "New VM",
                        "The Windows administrator password must be at least "
                        "8 characters.",
                    )
                    return False
        elif step == 1:
            try:
                memory_mib = int(round(self.memory_spin.value() * 1024))
                vcpus = int(self.vcpu_edit.text())
                disk = int(self.disk_edit.text())
            except ValueError:
                QMessageBox.warning(
                    self, "New VM", "Memory, vCPU and disk values must be numbers."
                )
                return False
            if memory_mib < 512 or vcpus < 1 or disk < 8:
                QMessageBox.warning(
                    self,
                    "New VM",
                    "Memory must be at least 0.5 GB, vCPU at least 1 and "
                    "disk at least 8 GB.",
                )
                return False
            if (
                self.family_combo.currentData() == "windows"
                and memory_mib < 2048
            ):
                QMessageBox.warning(
                    self, "New VM", "Windows needs at least 2 GB of RAM."
                )
                return False
            if (
                self.family_combo.currentData() == "hypervisor"
                and memory_mib < 4096
            ):
                QMessageBox.warning(
                    self, "New VM", "ESXi needs at least 4 GB of RAM."
                )
                return False
            if (
                self.family_combo.currentData() == "hypervisor"
                and disk < 32
            ):
                QMessageBox.warning(
                    self, "New VM", "ESXi needs a disk of at least 32 GB."
                )
                return False
        elif step == 2:
            if not self.dest_edit.text().strip():
                QMessageBox.warning(
                    self, "New VM", "Choose a destination directory."
                )
                return False
        return True

    def _update_summary(self) -> None:
        unattended = self.unattend_check.isChecked()
        network_text = self.network_combo.currentText()
        lines = [
            f"Name: {self.name_edit.text().strip() or '(not set)'}",
            (
                f"Guest OS: {self.family_combo.currentText()} · "
                f"{self.variant_combo.currentText()}"
            ),
            (
                f"Architecture: {self.arch_combo.currentText()}"
                + (
                    " (KVM)"
                    if domain_type_for(str(self.arch_combo.currentData()))
                    == "kvm"
                    else " (TCG emulation)"
                )
            ),
            f"Install ISO: {self.iso_edit.text().strip() or '(not set)'}",
            f"Unattended install: {'yes' if unattended else 'no'}",
        ]
        if self.family_combo.currentData() == "windows":
            lines.append(
                "Driver ISO: "
                + (self.virtio_edit.text().strip() or "not mounted")
            )
        if unattended:
            lines.append(f"Windows edition: {self.edition_combo.currentText()}")
        lines.extend(
            [
                (
                    "Serial console: yes (x86_64 pty; use Mode → Serial)"
                    if self.arch_combo.currentData() == "x86_64"
                    else "Serial console: no (x86_64 only)"
                ),
                (
                    f"Resources: {self.memory_spin.value():g} GB RAM · "
                    f"{self.vcpu_edit.text()} vCPU · "
                    f"{self.cpu_combo.currentText()}"
                ),
                (
                    f"Machine: {self.firmware_combo.currentText()} · "
                    f"{self.machine_combo.currentText()} · "
                    f"{self.video_combo.currentText()} video"
                    + (
                        f" · {self.video_memory_combo.currentData()} MB"
                        if self.video_combo.currentData() in {"vga", "qxl"}
                        else ""
                    )
                    + " · "
                    f"TPM 2.0: {'yes' if self.tpm_check.isChecked() else 'no'} · "
                    "Secure Boot: "
                    f"{'yes' if self.secure_boot_check.isChecked() else 'no'}"
                ),
                (
                    f"Primary disk: {self.disk_edit.text()} GB · "
                    f"{self.disk_bus_combo.currentText()}"
                ),
                (
                    f"Installer ISO: {self.iso_bus_combo.currentText()} · "
                    f"{self.iso_edit.text().strip() or '(not set)'}"
                ),
                f"Network: {network_text} · {self.nic_combo.currentText()}",
                f"Destination: {self.dest_edit.text().strip() or '(not set)'}",
                (
                    "After creation: "
                    f"{'start the VM' if self.start_check.isChecked() else 'leave it stopped'}"
                ),
            ]
        )
        self.summary_label.setText("\n".join(lines))

    # ------------------------------------------------------------------ fields

    def _apply_capabilities(self) -> None:
        caps = self._fetch_capabilities(host_arch())
        self._populate_architectures(caps)
        self._update_combo_options(caps, host_arch())
        self._arch_changed()

    def _fetch_capabilities(self, arch: str) -> dict[str, Any]:
        try:
            return self.backend.vm_capabilities(arch=arch)
        except (VemuError, AttributeError, TypeError):
            return {}

    def _populate_architectures(self, caps: dict[str, Any]) -> None:
        supported = {"x86_64", "aarch64", "riscv64"}
        names = [
            str(item.get("name"))
            for item in caps.get("arches", [])
            if item.get("name") in supported
        ]
        native = host_arch()
        if native not in names:
            names.insert(0, native)
        names = sorted(
            dict.fromkeys(names), key=lambda item: (item != native, item)
        )
        if not QSettings("v-EMU", "v-EMU").value(
            "ui/cross_arch", False, type=bool
        ):
            names = [native]
        current = str(self.arch_combo.currentData() or native)
        self.arch_combo.blockSignals(True)
        self.arch_combo.clear()
        for name in names:
            self.arch_combo.addItem(name, name)
        index = self.arch_combo.findData(current)
        if index < 0:
            index = 0
        self.arch_combo.setCurrentIndex(index)
        self.arch_combo.blockSignals(False)

    @staticmethod
    def _replace_combo(
        combo: QComboBox, values: list[str], current: str | None = None
    ) -> None:
        chosen = current if current is not None else combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for value in values:
            combo.addItem(str(value), str(value))
        index = combo.findData(chosen)
        if index < 0 and combo.count():
            index = 0
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _update_combo_options(
        self, caps: dict[str, Any], arch: str
    ) -> None:
        emulated = arch != "x86_64"
        order = (
            ["virtio", "nvme", "sata", "scsi", "usb"]
            if emulated
            else ["virtio", "nvme", "sata", "scsi", "ide", "usb"]
        )
        reported = list(caps.get("disk_buses") or [])
        disk_values = (
            [item for item in order if item in reported] if reported else order
        )
        if emulated:
            disk_values = [item for item in disk_values if item != "ide"]
        if "usb" not in disk_values:
            disk_values.append("usb")
        self._replace_combo(self.disk_bus_combo, disk_values)
        nic_values = list(
            caps.get("nic_models", [])
            or ["virtio", "e1000e", "e1000", "rtl8139", "vmxnet3", "pcnet"]
        )
        self._replace_combo(self.nic_combo, nic_values)
        video_values = [
            value
            for value in caps.get("video_models", [])
            if value in {"virtio", "vga", "ramfb", "qxl", "cirrus", "vmvga", "bochs"}
            and (not emulated or value in {"virtio", "vga", "ramfb"})
        ] or (["virtio", "vga", "ramfb"] if emulated else ["virtio", "vga", "qxl", "cirrus", "vmvga", "bochs"])
        self._replace_combo(self.video_combo, video_values)
        tcg = domain_type_for(arch) == "qemu"
        allowed_cpu = (
            {"maximum"} if tcg else {"host-passthrough", "host-model"}
        )
        cpu_values = [
            value
            for value in caps.get("cpu_modes", [])
            if value in allowed_cpu
        ] or (["maximum"] if tcg else ["host-passthrough", "host-model"])
        self._replace_combo(self.cpu_combo, cpu_values)
        machines = ["q35", "pc"] if not emulated else ["virt"]
        self._replace_combo(self.machine_combo, machines)

    def _arch_changed(self) -> None:
        arch = str(self.arch_combo.currentData() or "x86_64")
        caps = self._fetch_capabilities(arch)
        self._update_combo_options(caps, arch)
        self._variant_changed()
        if arch == "x86_64":
            self.firmware_combo.blockSignals(True)
            self.firmware_combo.setEnabled(True)
            if self.firmware_combo.currentData() not in {"bios", "efi"}:
                self.firmware_combo.setCurrentIndex(0)
            self.firmware_combo.blockSignals(False)
            self._select_data(self.machine_combo, "q35")
            self._select_data(self.iso_bus_combo, "sata")
            if domain_type_for(arch) == "kvm":
                self._select_data(self.cpu_combo, "host-passthrough")
                self.arch_note.setText("Native x86_64 KVM acceleration.")
            else:
                self._select_data(self.cpu_combo, "maximum")
                self.arch_note.setText(
                    f"x86_64 emulation uses TCG on this {host_arch()} host: "
                    "much slower than KVM."
                )
            self.defaults_note.setText(
                "Defaults follow the guest OS. Windows, BSD and ESXi use "
                "SATA + e1000e; Linux uses virtio. NVMe requires q35."
            )
        else:
            self.firmware_combo.blockSignals(True)
            self.firmware_combo.setCurrentIndex(
                self.firmware_combo.findData("efi")
            )
            self.firmware_combo.setEnabled(False)
            self.firmware_combo.blockSignals(False)
            self._select_data(self.machine_combo, "virt")
            self._select_data(self.disk_bus_combo, "virtio")
            self._select_data(self.iso_bus_combo, "usb")
            if domain_type_for(arch) == "kvm":
                self._select_data(self.cpu_combo, "host-passthrough")
                self.arch_note.setText(
                    f"Native {arch} KVM acceleration. UEFI firmware is "
                    "required; use an architecture-matched ISO."
                )
            else:
                self._select_data(self.cpu_combo, "maximum")
                self.arch_note.setText(
                    f"{arch} emulation uses TCG on this {host_arch()} host: "
                    "much slower than KVM. UEFI firmware is required; use an "
                    "architecture-matched ISO. Windows unattended install is "
                    "disabled."
                )
            self.defaults_note.setText(
                "TCG defaults: virtio or NVMe disks, USB installer media and "
                "ramfb for Windows/Other; Linux uses virtio-gpu. NVMe is "
                "supported on the virt machine."
            )
        self._update_security_options()
        self._update_summary()

    def _update_video_memory(self) -> None:
        self.video_memory_combo.setEnabled(
            str(self.video_combo.currentData() or "") in {"vga", "qxl"}
        )
        self._update_video_note()

    def _update_video_note(self) -> None:
        family = str(self.family_combo.currentData() or "")
        video = str(self.video_combo.currentData() or "")
        badge = getattr(self, "video_badge", None)
        if badge is None:
            return
        if family != "windows":
            badge.setVisible(False)
            return
        badge.setVisible(True)
        if video == "virtio":
            badge.set_message(
                "Windows needs the virtio-win WDDM driver (viogpudo) for "
                "virtio GPU. Install it in the guest first; keep ramfb or VGA "
                "for the installer if the screen stays black.",
                kind="alert",
                color=self._theme_tokens.warning,
            )
        elif video == "qxl":
            badge.set_message(
                "QXL 128 MB gave the best Windows 11 lab response in testing "
                "when the QXL driver and guest agent were installed. 2D only.",
                kind="info",
                color=self._theme_tokens.muted,
            )
        else:
            badge.set_message(
                "Windows works without a GPU driver. Install viogpudo from "
                "virtio-win, then switch Video to virtio for 2D WDDM. QXL "
                "128 MB is a good lab choice with the QXL driver installed.",
                kind="info",
                color=self._theme_tokens.muted,
            )

    def _update_security_options(self) -> None:
        efi = str(self.firmware_combo.currentData() or "") == "efi"
        x64 = str(self.arch_combo.currentData() or "x86_64") == "x86_64"
        enabled = efi and x64
        self.secure_boot_check.setEnabled(enabled)
        self.secure_boot_check.blockSignals(True)
        if not enabled:
            self.secure_boot_check.setChecked(False)
        elif not self.secure_boot_check.isChecked():
            family = str(self.family_combo.currentData() or "")
            self.secure_boot_check.setChecked(family == "windows")
        self.secure_boot_check.blockSignals(False)
        if hasattr(self, "summary_label"):
            self._update_summary()

    def _path_row(self, _label: str, dialog_title: str, callback):
        row = QHBoxLayout()
        edit = QLineEdit()
        edit.setPlaceholderText(dialog_title)
        row.addWidget(edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.clicked.connect(callback)
        row.addWidget(browse)
        return edit, row

    def _prefill_paths(self) -> None:
        iso_dirs = [Path("/home/ISO")]
        try:
            locations = self.backend.storage_list()
        except (VemuError, AttributeError):
            locations = []
        for location in locations:
            if location.get("role") == "labs":
                self.dest_edit.setText(str(location.get("path", "")))
            elif location.get("role") == "isos":
                path = Path(str(location.get("path", "")))
                if path not in iso_dirs:
                    iso_dirs.insert(0, path)
        self.iso_dir = next(
            (path for path in iso_dirs if path.is_dir()),
            iso_dirs[0],
        )
        for iso_dir in iso_dirs:
            if not iso_dir.is_dir():
                continue
            try:
                candidates = sorted(
                    iso_dir.glob("*windows_server_2025*.iso")
                )
            except OSError:
                candidates = []
            for candidate in candidates:
                self.iso_edit.setText(str(candidate))
                break
            if self.iso_edit.text().strip():
                break

    def _load_networks(self) -> None:
        try:
            profiles = self.backend.networks_list()
        except (VemuError, AttributeError):
            profiles = []
        self.network_combo.clear()
        for profile in profiles:
            self.network_combo.addItem(
                f"{profile['name']} ({profile.get('mode', '')})",
                profile["name"],
            )
        if not profiles:
            self.network_combo.addItem(
                "No network profiles — create one under Networks", None
            )

    def _family_changed(self) -> None:
        family = str(self.family_combo.currentData() or "windows")
        current_variant = self.variant_combo.currentData()
        self.variant_combo.blockSignals(True)
        self.variant_combo.clear()
        for key, label in OS_VARIANTS.get(family, []):
            self.variant_combo.addItem(label, key)
        index = self.variant_combo.findData(current_variant)
        if index < 0:
            index = 0
        self.variant_combo.setCurrentIndex(index)
        self.variant_combo.blockSignals(False)
        self._variant_changed()
        self._update_driver_iso_visibility()

    def _update_driver_iso_visibility(self) -> None:
        row = getattr(self, "virtio_row", None)
        if row is None:
            return
        visible = (
            str(self.family_combo.currentData() or "") == "windows"
        )
        row.setVisible(visible)
        label = getattr(self, "virtio_label", None)
        if label is not None:
            label.setVisible(visible)
        note = getattr(self, "storage_note", None)
        if note is not None:
            note.setText(
                "The VM gets a new qcow2 chain in the destination directory. "
                "The Driver ISO is optional; choose any virtio-win ISO or "
                "leave it blank to skip the driver CD."
                if visible
                else (
                    "The VM gets a new qcow2 chain in the destination "
                    "directory. No driver CD is mounted for non-Windows "
                    "guests."
                )
            )

    def _variant_changed(self) -> None:
        family = str(self.family_combo.currentData() or "windows")
        variant = str(self.variant_combo.currentData() or "")
        arch = str(self.arch_combo.currentData() or "x86_64")
        eligible = (
            arch == "x86_64"
            and family == "windows"
            and variant == "windows-server-2025"
        )
        self.unattend_check.blockSignals(True)
        self.unattend_check.setChecked(eligible)
        self.unattend_check.setEnabled(eligible)
        self.unattend_check.blockSignals(False)
        self._unattend_changed()
        self._apply_os_defaults(family, variant, arch)
        self._update_detection_label()
        self._update_video_note()
        if hasattr(self, "summary_label"):
            self._update_summary()

    def _unattend_changed(self) -> None:
        family = str(self.family_combo.currentData() or "windows")
        arch = str(self.arch_combo.currentData() or "x86_64")
        unattended = (
            self.unattend_check.isChecked()
            and family == "windows"
            and arch == "x86_64"
        )
        self.edition_combo.setEnabled(unattended)
        self.password_edit.setEnabled(unattended)
        self._update_helper()

    def _preset_for(self, family: str, variant: str) -> dict[str, Any]:
        if variant in HARDWARE_PRESETS:
            return HARDWARE_PRESETS[variant]
        return LINUX_PRESET if family == "linux" else OTHER_PRESET

    def _mark_manual(self, field: str) -> None:
        if not self._preset_guard:
            self._manual.add(field)

    def _set_field(self, field: str, apply) -> None:
        if field in self._manual:
            return
        self._preset_guard = True
        try:
            apply()
        finally:
            self._preset_guard = False

    def _apply_os_defaults(
        self, family: str, variant: str = "", arch: str = "x86_64"
    ) -> None:
        preset = self._preset_for(family, variant)
        self._select_data(self.disk_bus_combo, preset["disk_bus"])
        self._select_data(self.nic_combo, preset["nic"])
        self._select_data(self.firmware_combo, preset["firmware"])
        self._select_data(self.machine_combo, preset["machine"])
        if arch == "x86_64" or domain_type_for(arch) == "kvm":
            video = str(preset["video"])
            vcpus = int(preset["vcpus"])
            memory = float(preset["memory"])
            if arch != "x86_64":
                video = "ramfb" if family in {"windows", "other"} else "virtio"
        else:
            # TCG guests get host-relative recommendations; Ramfb keeps
            # Windows/WinPE visible without a virtio-gpu driver.
            vcpus, memory = recommended_emulated_resources()
            if family == "windows":
                memory = max(4.0, memory)
            video = "ramfb" if family in {"windows", "other"} else "virtio"
        self._select_data(self.video_combo, video)
        if "video_memory" not in self._manual:
            index = self.video_memory_combo.findData(
                int(preset.get("video_memory", 64))
            )
            if index >= 0:
                self.video_memory_combo.setCurrentIndex(index)
        self._update_video_memory()
        if not self._hardware_visited:
            self._set_field(
                "disk", lambda: self.disk_edit.setText(str(preset["disk"]))
            )
            self._set_field("memory", lambda: self.memory_spin.setValue(memory))
            self._set_field("vcpus", lambda: self.vcpu_edit.setText(str(vcpus)))
        if hasattr(self, "tpm_check"):
            self.tpm_check.setEnabled(True)
            self.tpm_check.setChecked(bool(preset.get("tpm")))

    def _update_detection_label(self) -> None:
        if self.detected is None:
            return
        detected = self.detected
        family = str(self.family_combo.currentData() or "")
        variant = str(self.variant_combo.currentData() or "")
        matches = (
            family == str(detected["family"])
            and variant == str(detected["variant"])
        )
        unattended = "yes" if detected["unattend"] else "no"
        text = f"Detected: {detected['label']} (unattended: {unattended})"
        if not matches:
            text += " · your selection overrides the detected OS"
        self.detected_label.setText(text)
        name = "Muted" if matches else "Warning"
        if self.detected_label.objectName() != name:
            self.detected_label.setObjectName(name)
            self.detected_label.style().unpolish(self.detected_label)
            self.detected_label.style().polish(self.detected_label)
        self._update_helper()

    def _detect_iso(self) -> None:
        iso = self.iso_edit.text().strip()
        if not iso:
            self.detected = None
            self.detected_label.setText("No ISO selected")
            self._update_helper()
            return
        detected = detect_iso_os(iso)
        self.detected = detected
        family = str(detected["family"])
        variant = str(detected["variant"])
        family_index = self.family_combo.findData(family)
        if family_index >= 0 and family_index != self.family_combo.currentIndex():
            self.family_combo.setCurrentIndex(family_index)
        variant_index = self.variant_combo.findData(variant)
        if variant_index >= 0:
            self.variant_combo.setCurrentIndex(variant_index)
        else:
            self._family_changed()
        self._update_detection_label()

    def _update_helper(self) -> None:
        if not hasattr(self, "helper_label"):
            return
        arch = (
            str(self.arch_combo.currentData())
            if hasattr(self, "arch_combo")
            else "x86_64"
        )
        if domain_type_for(arch) == "qemu":
            self.helper_label.setText(
                f"{arch} runs under TCG emulation: slow but useful for cross-"
                "architecture labs. Use virtio/NVMe disks, USB installer "
                "media and ramfb for Windows. Windows unattended install is "
                "disabled; choose an architecture-matched ISO."
            )
        elif self.family_combo.currentData() == "hypervisor":
            self.helper_label.setText(
                "ESXi 8/9 defaults: UEFI, q35, SATA, e1000e, virtio video, "
                "8 GB RAM, 2 vCPU, 128 GB disk, TPM 2.0 and Secure Boot. "
                "Uncheck Secure Boot only for unsigned custom installers."
            )
        elif self.unattend_check.isChecked():
            self.helper_label.setText(
                "Windows Server 2025 uses SATA + e1000e + VGA and a generated "
                "answer file. The administrator password is shown after "
                "creation; change it after the first login."
            )
        else:
            self.helper_label.setText(
                "The interactive installer runs normally; Windows-specific "
                "options above are disabled. Windows defaults to SATA + "
                "e1000e + VGA; Linux defaults to virtio devices."
            )

    @staticmethod
    def _select_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _browse_iso(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose installer ISO",
            str(getattr(self, "iso_dir", Path("/home/ISO"))),
            "ISO images (*.iso)",
        )
        if path:
            self.iso_edit.setText(path)
            self._detect_iso()

    def _browse_virtio(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose virtio-win ISO",
            str(getattr(self, "iso_dir", Path("/home/ISO"))),
            "ISO images (*.iso)",
        )
        if path:
            self.virtio_edit.setText(path)

    def _browse_dest(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose VM storage directory", self.dest_edit.text()
        )
        if path:
            self.dest_edit.setText(path)

    def _create(self) -> None:
        name = self.name_edit.text().strip()
        iso = self.iso_edit.text().strip()
        dest = self.dest_edit.text().strip()
        if not name or not iso or not dest:
            QMessageBox.warning(
                self, "New VM", "Name, install ISO and destination are required."
            )
            return
        try:
            disk_size = int(self.disk_edit.text())
            memory = int(round(self.memory_spin.value() * 1024))
            vcpus = int(self.vcpu_edit.text())
        except ValueError:
            QMessageBox.warning(self, "New VM", "Numeric fields are invalid.")
            return
        network = self.network_combo.currentData()
        if not network:
            QMessageBox.warning(
                self,
                "New VM",
                "No network profile is registered. Create one under "
                "Networks first.",
            )
            return
        os_type = str(self.family_combo.currentData())
        # The driver CD is a Windows-only optional extra. A stale hidden path
        # must never be attached to another guest family.
        virtio_iso = None
        if os_type == "windows":
            virtio_iso = self.virtio_edit.text().strip() or None
        params = {
            "name": name,
            "iso": iso,
            "dest": dest,
            "disk_size_gb": disk_size,
            "memory": memory,
            "vcpus": vcpus,
            "network": str(self.network_combo.currentData()),
            "os_type": os_type,
            "os_variant": str(self.variant_combo.currentData() or ""),
            "arch": str(self.arch_combo.currentData() or "x86_64"),
            "windows_unattend": self.unattend_check.isChecked(),
            "edition": str(self.edition_combo.currentData()),
            "admin_password": self.password_edit.text(),
            "virtio_iso": virtio_iso,
            "firmware": str(self.firmware_combo.currentData()),
            "machine": str(self.machine_combo.currentData()),
            "disk_bus": str(self.disk_bus_combo.currentData()),
            "iso_bus": str(self.iso_bus_combo.currentData()),
            "nic_model": str(self.nic_combo.currentData()),
            "video": str(self.video_combo.currentData()),
            "video_memory_mib": (
                self.video_memory_combo.currentData()
                if self.video_combo.currentData() in {"vga", "qxl"}
                else None
            ),
            "cpu_mode": str(self.cpu_combo.currentData()),
            "tpm": bool(self.tpm_check.isChecked()),
            "secure_boot": bool(self.secure_boot_check.isChecked()),
            "start": self.start_check.isChecked(),
        }
        self.create_button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            self.created = self.backend.vm_create(**params)
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot create VM", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.create_button.setEnabled(True)
        self.accept()
