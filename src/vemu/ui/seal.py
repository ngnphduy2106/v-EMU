"""Seal a VM as a template from the desktop UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..errors import VemuError
from .components import form_layout, FieldLabel, InfoBadge
from .dialogs import VemuDialog, dialog_layout
from .theme import LIGHT, Tokens
from .widgets import ComboBox


class SealDialog(VemuDialog):
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
        self.created: dict[str, Any] | None = None
        self.setWindowTitle(f"Seal template · {domain}")
        self.resize(620, 580)
        self.setMinimumSize(600, 540)

        layout = dialog_layout(self)

        title = QLabel(f"Seal {domain} as a template")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        helper = QLabel(
            "The VM disk becomes an immutable base. Clones only create small "
            "overlays. In-place seal avoids copying the disk."
        )
        helper.setObjectName("Muted")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        form = form_layout()
        self.name_edit = QLineEdit(f"{domain}-base")
        form.addRow("Template name", self.name_edit)

        self.dest_edit = QLineEdit()
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.clicked.connect(self._browse)
        dest_row.addWidget(browse)
        form.addRow("Template root", dest_row)

        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("Optional description")
        form.addRow("Description", self.description_edit)

        self.in_place_check = QCheckBox(
            "In-place: use the current disk as the base (no large copy)"
        )
        form.addRow("", self.in_place_check)

        self.os_family_combo = ComboBox()
        self.os_family_combo.addItem("Linux", "linux")
        self.os_family_combo.addItem("Windows", "windows")
        self.os_family_combo.currentIndexChanged.connect(
            self._os_family_changed
        )
        form.addRow(
            FieldLabel(
                "Guest OS",
                "Linux uses cloud-init; Windows clones use OOBE when the "
                "image was sysprepped.",
                kind="info",
                color=self._theme_tokens.muted,
            ),
            self.os_family_combo,
        )
        layout.addLayout(form)

        self.windows_options = QGroupBox("Windows template options")
        windows_layout = QVBoxLayout(self.windows_options)
        windows_layout.setSpacing(8)
        sysprep_row = QHBoxLayout()
        self.sysprepped_check = QCheckBox(
            "This VM is already sysprepped (recommended for Windows)"
        )
        sysprep_row.addWidget(self.sysprepped_check)
        self.sysprep_badge = InfoBadge(
            "Run Sysprep /generalize /oobe /shutdown manually inside Windows "
            "before sealing. v-EMU does not run Sysprep.",
            kind="info",
            color=self._theme_tokens.muted,
        )
        sysprep_row.addWidget(self.sysprep_badge)
        sysprep_row.addStretch(1)
        windows_layout.addLayout(sysprep_row)
        self.sysprep_warning = QLabel(
            "Sealing without Sysprep: clones keep the same SID and machine "
            "identity. Microsoft recommends Sysprep /generalize before "
            "cloning. Continue only if you accept duplicate SIDs."
        )
        self.sysprep_warning.setObjectName("Warning")
        self.sysprep_warning.setWordWrap(True)
        windows_layout.addWidget(self.sysprep_warning)
        self.sysprepped_check.toggled.connect(self._update_bitlocker_gate)

        self.bitlocker_warning = QLabel(
            "BitLocker/FDE: v-EMU does not copy TPM state. Clones receive a "
            "new TPM, so encrypted Windows images may enter recovery. "
            "Disable or suspend encryption before sealing, or keep the "
            "recovery key."
        )
        self.bitlocker_warning.setObjectName("Warning")
        self.bitlocker_warning.setWordWrap(True)
        windows_layout.addWidget(self.bitlocker_warning)
        self.bitlocker_check = QCheckBox(
            "I confirm BitLocker/FDE is off or suspended for this Windows base"
        )
        self.bitlocker_check.toggled.connect(self._update_bitlocker_gate)
        windows_layout.addWidget(self.bitlocker_check)
        layout.addWidget(self.windows_options)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.seal_button = QPushButton("Seal template")
        self.seal_button.setObjectName("PrimaryButton")
        self.seal_button.clicked.connect(self._seal)
        buttons.addWidget(self.seal_button)
        layout.addLayout(buttons)

        self._os_family_changed()
        self._prefill()

    def _prefill(self) -> None:
        try:
            locations = self.backend.storage_list()
        except (VemuError, AttributeError):
            locations = []
        for location in locations:
            if location.get("role") == "templates":
                self.dest_edit.setText(str(location.get("path", "")))
                break
        if not self.dest_edit.text():
            for location in locations:
                if location.get("role") == "labs":
                    parent = Path(str(location.get("path", ""))).parent
                    self.dest_edit.setText(str(parent / "templates"))
                    break

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose template root", self.dest_edit.text()
        )
        if path:
            self.dest_edit.setText(path)

    def _os_family_changed(self) -> None:
        windows = self.os_family_combo.currentData() == "windows"
        self.windows_options.setVisible(windows)
        if not windows:
            self.sysprepped_check.setChecked(False)
            self.bitlocker_check.setChecked(False)
        self._update_bitlocker_gate()

    def _update_bitlocker_gate(self) -> None:
        windows = self.os_family_combo.currentData() == "windows"
        self.sysprep_warning.setVisible(
            windows and not self.sysprepped_check.isChecked()
        )
        self.bitlocker_check.setEnabled(windows)
        self.bitlocker_warning.setVisible(windows)
        if not windows:
            self.bitlocker_check.setChecked(False)
        self.seal_button.setEnabled(
            not windows or self.bitlocker_check.isChecked()
        )

    def _seal(self) -> None:
        name = self.name_edit.text().strip()
        dest = self.dest_edit.text().strip()
        if not name or not dest:
            QMessageBox.warning(
                self, "Seal template", "Template name and root are required."
            )
            return
        os_family = str(self.os_family_combo.currentData() or "")
        windows = os_family == "windows"
        if windows and not self.bitlocker_check.isChecked():
            QMessageBox.warning(
                self,
                "Seal template",
                "Confirm that BitLocker/FDE is off or suspended before "
                "sealing a Windows base image.",
            )
            return
        self.seal_button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            self.created = self.backend.seal(
                domain=self.domain,
                dest=dest,
                name=name,
                description=self.description_edit.text().strip(),
                os_family=os_family,
                in_place=self.in_place_check.isChecked(),
                sysprepped=self.sysprepped_check.isChecked(),
                accept_bitlocker=self.bitlocker_check.isChecked(),
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Seal failed", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.seal_button.setEnabled(True)
        removed = (
            self.created.get("removed_shares", [])
            if isinstance(self.created, dict)
            else []
        )
        if removed:
            QMessageBox.information(
                self,
                "VirtioFS shares removed",
                "These shares were removed from the source VM before sealing "
                "and are not part of the template: "
                + ", ".join(str(item) for item in removed)
                + ". Reattach them from Shared folders if needed.",
            )
        self.accept()
