"""Template library: import Packer images, export, and clone labs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..virtcheck import host_arch
from ..errors import VemuError
from .delegate import NoFocusDelegate
from .dialogs import VemuDialog, dialog_layout
from .tooltips import set_tooltip
from .widgets import ComboBox, EmptyStateTable
from .components import form_layout, PageHeader


def _size_text(value: int) -> str:
    return f"{value / (1024 ** 3):.1f} GB" if value else "—"


def _security_text(template: dict[str, Any]) -> str:
    parts = [
        "UEFI" if template.get("firmware") == "efi" else "BIOS",
        f"Secure Boot: {'on' if template.get('secure_boot') else 'off'}",
        f"TPM: {'on' if template.get('tpm') else 'off'}",
    ]
    return " · ".join(parts)


class TemplateImportDialog(VemuDialog):
    def __init__(self, backend, root: str, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.root = root
        self.created: dict[str, Any] | None = None
        self.setWindowTitle("Import golden image")
        self.resize(640, 600)
        self.setMinimumSize(560, 480)

        layout = dialog_layout(self)
        title = QLabel("Import a Packer / golden qcow2")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        helper = QLabel(
            "The image is flattened into base.qcow2 and becomes immutable. "
            "Clones only create overlays."
        )
        helper.setObjectName("Muted")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)
        form = form_layout()
        self.source_edit = QLineEdit()
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.clicked.connect(self._browse)
        source_row.addWidget(browse)
        form.addRow("Source qcow2", source_row)

        self.name_edit = QLineEdit()
        form.addRow("Template name", self.name_edit)
        self.description_edit = QLineEdit()
        form.addRow("Description", self.description_edit)

        self.os_family_combo = ComboBox()
        self.os_family_combo.addItem("Linux", "linux")
        self.os_family_combo.addItem("Windows", "windows")
        self.os_family_combo.currentIndexChanged.connect(
            self._os_family_changed
        )
        form.addRow("Guest OS", self.os_family_combo)
        os_helper = QLabel(
            "Linux templates use cloud-init on clone; Windows templates use "
            "Sysprep/OOBE."
        )
        os_helper.setObjectName("Muted")
        os_helper.setWordWrap(True)
        form.addRow("", os_helper)

        self.arch_combo = ComboBox()
        self.arch_combo.addItems(["x86_64", "aarch64"])
        self.arch_combo.setCurrentText(host_arch())
        form.addRow("Architecture", self.arch_combo)

        self.firmware_combo = ComboBox()
        self.firmware_combo.addItems(["bios", "efi"])
        form.addRow("Firmware", self.firmware_combo)

        self.disk_combo = ComboBox()
        self.disk_combo.addItems(["virtio", "sata", "scsi", "ide", "nvme"])
        form.addRow("Disk bus", self.disk_combo)

        self.video_combo = ComboBox()
        self.video_combo.addItems(["virtio", "vga", "qxl"])
        form.addRow("Video", self.video_combo)

        self.video_memory_combo = ComboBox()
        for value in (64, 128, 256):
            self.video_memory_combo.addItem(f"{value} MB", value)
        self.video_combo.currentIndexChanged.connect(
            self._update_video_memory
        )
        self._update_video_memory()
        form.addRow("Video memory", self.video_memory_combo)

        self.nic_combo = ComboBox()
        self.nic_combo.addItems(["virtio", "e1000e", "e1000"])
        form.addRow("NIC model", self.nic_combo)

        self.secure_boot_check = QCheckBox(
            "Secure Boot (UEFI x86_64)"
        )
        form.addRow("", self.secure_boot_check)
        self.tpm_check = QCheckBox("TPM 2.0")
        form.addRow("", self.tpm_check)
        self.sysprepped_check = QCheckBox(
            "Windows image is already sysprepped"
        )
        form.addRow("", self.sysprepped_check)
        content_layout.addLayout(form)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self._os_family_changed()

        self.root_label = QLabel(f"Template root: {root}")
        self.root_label.setObjectName("Muted")
        layout.addWidget(self.root_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.import_button = QPushButton("Import")
        self.import_button.setToolTip("Import golden image (Ctrl+N)")
        self.import_button.setObjectName("PrimaryButton")
        self.import_button.clicked.connect(self._import)
        buttons.addWidget(self.import_button)
        layout.addLayout(buttons)

    def _os_family_changed(self) -> None:
        windows = self.os_family_combo.currentData() == "windows"
        self.sysprepped_check.setVisible(windows)
        self.sysprepped_check.setEnabled(windows)
        if not windows:
            self.sysprepped_check.setChecked(False)

    def _update_video_memory(self) -> None:
        self.video_memory_combo.setEnabled(
            self.video_combo.currentText() in {"vga", "qxl"}
        )

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose qcow2", "/home/ISO", "QEMU images (*.qcow2 *.img)"
        )
        if path:
            self.source_edit.setText(path)
            if not self.name_edit.text():
                self.name_edit.setText(Path(path).stem.replace("_", "-")[:64])

    def _import(self) -> None:
        source = self.source_edit.text().strip()
        name = self.name_edit.text().strip()
        if not source or not name:
            QMessageBox.warning(
                self, "Import template", "Source and name are required."
            )
            return
        self.import_button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            self.created = self.backend.template_import(
                source=source,
                dest=self.root,
                name=name,
                description=self.description_edit.text().strip(),
                arch=self.arch_combo.currentText(),
                machine="q35" if self.arch_combo.currentText() == "x86_64" else "virt",
                firmware=self.firmware_combo.currentText(),
                disk_bus=self.disk_combo.currentText(),
                video=self.video_combo.currentText(),
                video_memory_mib=(
                    self.video_memory_combo.currentData()
                    if self.video_combo.currentText() in {"vga", "qxl"}
                    else None
                ),
                nic_model=self.nic_combo.currentText(),
                secure_boot=self.secure_boot_check.isChecked(),
                tpm=self.tpm_check.isChecked(),
                os_family=str(
                    self.os_family_combo.currentData() or "linux"
                ),
                sysprepped=self.sysprepped_check.isChecked(),
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.import_button.setEnabled(True)
        self.accept()


class TemplateCloneDialog(VemuDialog):
    def __init__(
        self,
        backend,
        template: dict[str, Any],
        template_root: str,
        labs_root: str,
        networks: list[dict[str, Any]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.template = template
        self.template_root = template_root
        self.labs_root = labs_root
        self.created: list[dict[str, Any]] = []
        self.setWindowTitle(f"Clone template · {template['name']}")
        self.resize(620, 660)
        self.setMinimumSize(560, 500)

        layout = dialog_layout(self)
        title = QLabel(f"Clone {template['name']}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        helper = QLabel(
            "Linked clones share the template base. Linux clones can receive "
            "a cloud-init seed. Windows OOBE USB is optional and only needed "
            "for the Server 2025 unattended/sysprepped flow."
        )
        helper.setObjectName("Muted")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        self.os_family = str(template.get("os_family") or "")
        self.inferred_os_family = False
        if self.os_family not in {"linux", "windows"}:
            self.os_family = (
                "windows" if template.get("sysprepped") else "linux"
            )
            self.inferred_os_family = True
        self.os_label = QLabel(
            f"Guest OS: {self.os_family.capitalize()}"
            + (" (inferred from legacy metadata)" if self.inferred_os_family else "")
        )
        self.os_label.setObjectName(
            "Warning" if self.inferred_os_family else "Muted"
        )
        layout.addWidget(self.os_label)
        self.security_label = QLabel(_security_text(template))
        self.security_label.setObjectName("Muted")
        layout.addWidget(self.security_label)
        if self.inferred_os_family:
            self.legacy_warning = QLabel(
                "This template has no OS family. Set Linux or Windows from "
                "Templates → Edit template metadata before relying on "
                "automatic clone options."
            )
            self.legacy_warning.setObjectName("Warning")
            self.legacy_warning.setWordWrap(True)
            layout.addWidget(self.legacy_warning)
        if template.get("tpm"):
            tpm_warning = QLabel(
                "TPM: this template has a TPM. Every clone receives a new TPM "
                "state. BitLocker or LUKS-TPM volumes may enter recovery; "
                "keep the recovery key or disable/suspend encryption before "
                "sealing."
            )
            tpm_warning.setObjectName("Warning")
            tpm_warning.setWordWrap(True)
            layout.addWidget(tpm_warning)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)
        form = form_layout()
        self.count_edit = QLineEdit("1")
        form.addRow("Count", self.count_edit)
        self.name_edit = QLineEdit(
            f"{template['name']}-" + "{{n}}"
        )
        form.addRow("Name pattern", self.name_edit)
        self.dest_edit = QLineEdit(labs_root)
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.clicked.connect(self._browse)
        dest_row.addWidget(browse)
        form.addRow("Destination", dest_row)

        self.network_combo = ComboBox()
        for profile in networks:
            self.network_combo.addItem(
                f"{profile['name']} ({profile.get('mode', '')})",
                profile["name"],
            )
        if not networks:
            self.network_combo.addItem(
                "No network profiles — create one under Networks", None
            )
        form.addRow("Network", self.network_combo)

        self.memory_edit = QLineEdit(str(template.get("memory_mib", 2048)))
        form.addRow("Memory (MiB)", self.memory_edit)
        self.vcpu_edit = QLineEdit(str(template.get("vcpus", 2)))
        form.addRow("vCPUs", self.vcpu_edit)
        self.start_check = QCheckBox("Start clones after creation")
        self.start_check.setChecked(True)
        form.addRow("", self.start_check)

        self.cloud_init_check = QCheckBox(
            "Attach cloud-init seed (Linux, NoCloud)"
        )
        self.cloud_init_check.setEnabled(self.os_family == "linux")
        form.addRow("", self.cloud_init_check)
        self.ci_user_edit = QLineEdit("lab")
        form.addRow("cloud-init user", self.ci_user_edit)
        self.ci_key_edit = QLineEdit()
        key_row = QHBoxLayout()
        key_row.addWidget(self.ci_key_edit, 1)
        key_browse = QPushButton("Browse…")
        key_browse.setObjectName("SecondaryButton")
        key_browse.clicked.connect(self._browse_key)
        key_row.addWidget(key_browse)
        form.addRow("SSH public key", key_row)
        self.ci_packages_edit = QLineEdit()
        self.ci_packages_edit.setPlaceholderText("vim, tcpdump")
        form.addRow("Packages", self.ci_packages_edit)
        self.ci_ip_edit = QLineEdit()
        self.ci_ip_edit.setPlaceholderText("optional, e.g. 192.168.50.11/24")
        form.addRow("Static IP", self.ci_ip_edit)
        self.ci_gateway_edit = QLineEdit()
        form.addRow("Gateway", self.ci_gateway_edit)

        self.windows_oobe_check = QCheckBox(
            "Attach Windows OOBE answer USB (sysprepped image)"
        )
        self.windows_oobe_check.setEnabled(
            self.os_family == "windows" and bool(template.get("sysprepped"))
        )
        set_tooltip(
            self.windows_oobe_check,
            "Enable only for the v-EMU Server 2025 unattended/OOBE clone flow.",
        )
        self.windows_oobe_check.toggled.connect(self._windows_oobe_toggled)
        form.addRow("", self.windows_oobe_check)
        self.windows_password_edit = QLineEdit("Vemu!Lab2026")
        self.windows_password_edit.setPlaceholderText(
            "8+ characters; used only when OOBE answer is attached"
        )
        self.windows_password_edit.setEnabled(False)
        form.addRow("Windows OOBE password", self.windows_password_edit)
        content_layout.addLayout(form)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.clone_button = QPushButton("Clone")
        self.clone_button.setObjectName("PrimaryButton")
        self.clone_button.clicked.connect(self._clone)
        buttons.addWidget(self.clone_button)
        layout.addLayout(buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose lab directory", self.dest_edit.text()
        )
        if path:
            self.dest_edit.setText(path)

    def _windows_oobe_toggled(self, checked: bool) -> None:
        self.windows_password_edit.setEnabled(checked)
        if checked and not self.windows_password_edit.text().strip():
            self.windows_password_edit.setText("Vemu!Lab2026")

    def _browse_key(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose SSH public key",
            str(Path.home() / ".ssh"),
            "Public keys (*.pub);;All files (*)",
        )
        if path:
            self.ci_key_edit.setText(path)

    def _clone(self) -> None:
        try:
            count = int(self.count_edit.text())
            memory = int(self.memory_edit.text())
            vcpus = int(self.vcpu_edit.text())
        except ValueError:
            QMessageBox.warning(self, "Clone", "Numeric fields are invalid.")
            return
        if count < 1:
            QMessageBox.warning(self, "Clone", "Count must be at least 1.")
            return
        network = self.network_combo.currentData()
        if not network:
            QMessageBox.warning(
                self,
                "Clone",
                "Create a network profile under Networks first.",
            )
            return
        cloud_init = self.cloud_init_check.isChecked()
        windows_oobe = bool(
            self.windows_oobe_check.isChecked()
            and self.os_family == "windows"
            and self.template.get("sysprepped")
        )
        windows_password = self.windows_password_edit.text().strip()
        if windows_oobe and len(windows_password) < 8:
            QMessageBox.warning(
                self,
                "Clone",
                "The Windows OOBE password must be at least 8 characters.",
            )
            return
        packages = [
            item.strip()
            for item in self.ci_packages_edit.text().split(",")
            if item.strip()
        ]
        params = {
            "template": str(
                Path(self.template_root) / self.template["name"]
            ),
            "dest": self.dest_edit.text().strip(),
            "count": count,
            "name": self.name_edit.text().strip(),
            "define": True,
            "start": self.start_check.isChecked(),
            "memory": memory,
            "vcpus": vcpus,
            "network": str(network),
            "reset_identity": not cloud_init
            and self.os_family == "linux",
            "cloud_init": cloud_init,
            "cloud_init_user": self.ci_user_edit.text().strip() or "lab",
            "cloud_init_ssh_key_file": self.ci_key_edit.text().strip()
            or None,
            "cloud_init_packages": packages,
            "cloud_init_ip": self.ci_ip_edit.text().strip() or None,
            "cloud_init_gateway": self.ci_gateway_edit.text().strip()
            or None,
            "windows_oobe": windows_oobe,
            "windows_admin_password": windows_password if windows_oobe else None,
        }
        self.clone_button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            payload = self.backend.clone_create(**params)
            self.created = list(payload.get("created", []))
            warnings = list(payload.get("warnings", []))
            if warnings:
                QMessageBox.warning(
                    self,
                    "Clone warning",
                    "\n\n".join(str(item) for item in warnings),
                )
        except VemuError as exc:
            QMessageBox.critical(self, "Clone failed", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.clone_button.setEnabled(True)
        self.accept()


class TemplateEditDialog(VemuDialog):
    """Edit OS family/sysprep metadata without touching base images."""

    def __init__(self, parent, template: dict[str, Any]) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit template · {template.get('name', '')}")
        self.resize(520, 340)
        layout = dialog_layout(self)
        title = QLabel(f"Edit {template.get('name', 'template')}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        helper = QLabel(
            "The guest OS controls clone behaviour: Linux uses cloud-init, "
            "Windows uses Sysprep/OOBE. This changes metadata only."
        )
        helper.setObjectName("Muted")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        if not template.get("os_family"):
            warning = QLabel(
                "This template has no OS family. Choose the correct guest OS "
                "before cloning."
            )
            warning.setObjectName("Warning")
            warning.setWordWrap(True)
            layout.addWidget(warning)

        form = form_layout()
        self.os_family_combo = ComboBox()
        self.os_family_combo.addItem("Linux", "linux")
        self.os_family_combo.addItem("Windows", "windows")
        current = str(template.get("os_family") or "")
        if current not in {"linux", "windows"}:
            current = (
                "windows" if template.get("sysprepped") else "linux"
            )
        index = self.os_family_combo.findData(current)
        if index >= 0:
            self.os_family_combo.setCurrentIndex(index)
        self.os_family_combo.currentIndexChanged.connect(
            self._os_family_changed
        )
        form.addRow("Guest OS", self.os_family_combo)
        self.sysprepped_check = QCheckBox("Windows image is sysprepped")
        self.sysprepped_check.setChecked(
            bool(template.get("sysprepped"))
        )
        form.addRow("", self.sysprepped_check)
        layout.addLayout(form)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("Save")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self.accept)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        self._os_family_changed()

    def _os_family_changed(self) -> None:
        windows = self.os_family_combo.currentData() == "windows"
        self.sysprepped_check.setVisible(windows)
        self.sysprepped_check.setEnabled(windows)
        if not windows:
            self.sysprepped_check.setChecked(False)

    def values(self) -> tuple[str, bool]:
        return (
            str(self.os_family_combo.currentData() or "linux"),
            bool(self.sysprepped_check.isChecked()),
        )


class TemplatesPage(QWidget):
    status = Signal(str)
    vms_changed = Signal()

    def __init__(self, backend, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.templates: list[dict[str, Any]] = []
        self._settings = QSettings("v-EMU", "v-EMU")
        self.root_manual = self._settings.value(
            "templates/root_manual", False, type=bool
        )
        self.root = self._default_root()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.addWidget(
            PageHeader(
                "Templates",
                "Golden images, Packer imports, and linked-clone sources.",
            )
        )
        header.addStretch(1)

        self.import_button = QPushButton("Import golden image")
        self.import_button.setObjectName("PrimaryButton")
        self.import_button.clicked.connect(self._import)
        header.addWidget(self.import_button)
        refresh = QPushButton("Refresh")
        refresh.setObjectName("SecondaryButton")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        layout.addLayout(header)

        root_row = QHBoxLayout()
        root_row.addWidget(QLabel("Template root"))
        self.root_edit = QLineEdit(self.root)
        self.root_edit.textEdited.connect(self._root_edited)
        root_row.addWidget(self.root_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.clicked.connect(self._browse_root)
        root_row.addWidget(browse)
        layout.addLayout(root_row)

        self.table = EmptyStateTable(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "NAME",
                "ARCH",
                "OS",
                "FIRMWARE",
                "SECURITY",
                "DISK",
                "VIDEO",
                "SIZE",
            ]
        )
        for column in range(8):
            header_item = self.table.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
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
        for column, width in (
            (1, 80),
            (2, 90),
            (3, 70),
            (4, 240),
            (5, 70),
            (6, 80),
            (7, 90),
        ):
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
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.clone_button = QPushButton("Clone selected")
        self.clone_button.setObjectName("PrimaryButton")
        self.clone_button.setEnabled(False)
        self.clone_button.clicked.connect(self._clone)
        actions.addWidget(self.clone_button)
        self.edit_button = QPushButton("Edit metadata…")
        self.edit_button.setObjectName("SecondaryButton")
        self.edit_button.setEnabled(False)
        self.edit_button.clicked.connect(self._edit_metadata)
        actions.addWidget(self.edit_button)
        self.export_button = QPushButton("Export")
        self.export_button.setObjectName("SecondaryButton")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        actions.addWidget(self.export_button)
        self.delete_button = QPushButton("Delete template…")
        self.delete_button.setObjectName("DangerButton")
        self.delete_button.setToolTip(
            "Delete the template base image. VMs or linked clones must not "
            "still depend on it."
        )
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._delete)
        actions.addWidget(self.delete_button)
        self.copy_button = QPushButton("Copy path")
        self.copy_button.setObjectName("SecondaryButton")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy_path)
        actions.addWidget(self.copy_button)
        actions.addStretch(1)
        layout.addLayout(actions)

    def _registered_template_roots(self) -> list[str]:
        try:
            if hasattr(self.backend, "storage_registry"):
                locations = self.backend.storage_registry()
            else:
                locations = self.backend.storage_list()
        except (VemuError, AttributeError):
            return []
        return [
            str(location.get("path", ""))
            for location in locations
            if location.get("role") == "templates"
            and str(location.get("path", ""))
        ]

    def _default_root(self) -> str:
        roots = self._registered_template_roots()
        saved = self._settings.value("templates/root", "", type=str)
        if saved and saved in roots:
            return saved
        if roots:
            return roots[0]
        if saved:
            return saved
        return str(Path.home() / "vemu-templates")

    def _root_edited(self, _text: str) -> None:
        self.root_manual = True
        self._settings.setValue("templates/root_manual", True)

    def _browse_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose template root", self.root_edit.text()
        )
        if path:
            self.root_manual = True
            self._settings.setValue("templates/root_manual", True)
            self.root_edit.setText(path)
            self.root = path
            self._settings.setValue("templates/root", path)
            self.refresh()

    def refresh(self) -> None:
        root = self.root_edit.text().strip()
        roots = self._registered_template_roots()
        if not self.root_manual and roots and (
            not root or root not in roots
        ):
            root = roots[0]
            self.root_edit.setText(root)
        self.root = root
        self._settings.setValue("templates/root", root)
        if not hasattr(self.backend, "templates_list"):
            self.templates = []
            self.table.setRowCount(0)
            self.table.set_empty_state(
                "Templates need vemud",
                "Start the v-EMU daemon to manage golden images.",
            )
            self.table.refresh_empty_state(0)
            self.import_button.setEnabled(False)
            self._selection_changed()
            return
        if not root or not Path(root).is_dir():
            self.templates = []
            self.table.setRowCount(0)
            self.table.set_empty_state(
                "No template root",
                "Choose a template directory, then import a golden image.",
            )
            self.table.refresh_empty_state(0)
            self._selection_changed()
            return
        try:
            self.templates = self.backend.templates_list(root)
        except VemuError as exc:
            self.templates = []
            self.table.setRowCount(0)
            self.table.set_empty_state("Cannot load templates", str(exc))
            self.table.refresh_empty_state(0)
            self._selection_changed()
            return
        self.table.setRowCount(0)
        for template in self.templates:
            row = self.table.rowCount()
            self.table.insertRow(row)
            os_family = str(template.get("os_family") or "")
            if os_family not in {"linux", "windows"}:
                os_family = (
                    "windows"
                    if template.get("sysprepped")
                    else "linux"
                )
            os_text = os_family.capitalize()
            if (
                os_family == "windows"
                and template.get("sysprepped")
            ):
                os_text += " · sysprep"
            values = [
                template.get("name", ""),
                template.get("arch", ""),
                os_text,
                template.get("firmware", ""),
                _security_text(template),
                template.get("disk_bus", ""),
                template.get("video", ""),
                _size_text(int(template.get("base_size", 0))),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, column, item)
        if not self.templates:
            self.table.set_empty_state(
                "No templates yet",
                "Import a Packer/golden qcow2 or seal a VM as a template.",
            )
        self.table.refresh_empty_state()
        self._selection_changed()

    def _selected_template(self) -> dict[str, Any] | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return None
        return next(
            (
                template
                for template in self.templates
                if template["name"] == item.text()
            ),
            None,
        )

    def _selection_changed(self) -> None:
        selected = self._selected_template() is not None
        self.clone_button.setEnabled(selected)
        self.edit_button.setEnabled(
            selected and hasattr(self.backend, "template_update")
        )
        self.export_button.setEnabled(selected)
        self.delete_button.setEnabled(
            selected and hasattr(self.backend, "template_delete")
        )
        self.copy_button.setEnabled(selected)

    def _labs_root(self) -> str:
        try:
            locations = self.backend.storage_list()
        except (VemuError, AttributeError):
            locations = []
        for location in locations:
            if location.get("role") == "labs":
                return str(location.get("path", ""))
        return str(Path.home() / "vemu-labs")

    def _networks(self) -> list[dict[str, Any]]:
        try:
            return self.backend.networks_list()
        except (VemuError, AttributeError):
            return []

    def _import(self) -> None:
        if not hasattr(self.backend, "template_import"):
            self.status.emit("Templates require vemud.")
            return
        root = self.root_edit.text().strip()
        if not root:
            QMessageBox.warning(
                self, "Import template", "Choose a template root first."
            )
            return
        Path(root).mkdir(parents=True, exist_ok=True)
        dialog = TemplateImportDialog(self.backend, root, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.refresh()
        if dialog.created:
            self.status.emit(
                f"Imported template {dialog.created.get('name', '')}"
            )

    def _clone(self) -> None:
        template = self._selected_template()
        if template is None:
            return
        if not hasattr(self.backend, "clone_create"):
            self.status.emit("Cloning requires vemud.")
            return
        dialog = TemplateCloneDialog(
            self.backend,
            template,
            self.root,
            self._labs_root(),
            self._networks(),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.status.emit(f"Created {len(dialog.created)} clone(s)")
        self.vms_changed.emit()

    def _export(self) -> None:
        template = self._selected_template()
        if template is None:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export template",
            f"{template['name']}.qcow2",
            "QEMU images (*.qcow2);;Raw images (*.raw)",
        )
        if not path:
            return
        try:
            payload = self.backend.template_export(
                template=str(Path(self.root) / template["name"]),
                dest=path,
                format="raw" if path.endswith(".raw") else "qcow2",
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.status.emit(f"Exported {payload['path']}")

    def _edit_metadata(self) -> None:
        template = self._selected_template()
        if template is None:
            return
        if not hasattr(self.backend, "template_update"):
            self.status.emit("Editing template metadata requires vemud.")
            return
        dialog = TemplateEditDialog(self, template)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        os_family, sysprepped = dialog.values()
        try:
            self.backend.template_update(
                template=str(Path(self.root) / template["name"]),
                os_family=os_family,
                sysprepped=sysprepped,
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Template update failed", str(exc))
            return
        self.refresh()
        self.status.emit(
            f"Updated {template['name']}: OS={os_family}, "
            f"sysprepped={'yes' if sysprepped else 'no'}"
        )

    def _delete(self) -> None:
        template = self._selected_template()
        if template is None:
            return
        if not hasattr(self.backend, "template_delete"):
            self.status.emit("Deleting templates requires vemud.")
            return
        answer = QMessageBox.question(
            self,
            "Delete template",
            (
                f"Delete template '{template['name']}' and its base image?\n\n"
                "VMs or linked clones must not still depend on it. "
                "This cannot be undone."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            payload = self.backend.template_delete(
                str(Path(self.root) / template["name"])
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot delete template", str(exc))
            return
        self.refresh()
        self.status.emit(f"Deleted template {payload.get('template', '')}")

    def _copy_path(self) -> None:
        template = self._selected_template()
        if template is None:
            return
        QGuiApplication.clipboard().setText(str(Path(self.root) / template["name"]))
        self.status.emit("Template path copied")

    def _build_context_menu(self, template: dict[str, Any] | None) -> QMenu:
        menu = QMenu(self)
        clone = menu.addAction("Clone VMs…", self._clone)
        clone.setEnabled(template is not None)
        edit = menu.addAction("Edit template metadata…", self._edit_metadata)
        edit.setEnabled(
            template is not None
            and hasattr(self.backend, "template_update")
        )
        export = menu.addAction("Export…", self._export)
        export.setEnabled(template is not None)
        delete = menu.addAction("Delete template…", self._delete)
        delete.setEnabled(
            template is not None
            and hasattr(self.backend, "template_delete")
        )
        copy = menu.addAction(
            "Copy path",
            lambda: QGuiApplication.clipboard().setText(
                str(Path(self.root) / template["name"])
                if template
                else ""
            ),
        )
        copy.setEnabled(template is not None)
        menu.addSeparator()
        menu.addAction("Import golden image…", self._import)
        return menu

    def _show_context_menu(self, position) -> None:
        row = self.table.rowAt(position.y())
        if row >= 0:
            self.table.selectRow(row)
        self._build_context_menu(self._selected_template()).exec(
            self.table.viewport().mapToGlobal(position)
        )
