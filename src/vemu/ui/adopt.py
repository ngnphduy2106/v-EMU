"""Adopt/Migrate mode chooser for external VMs."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from .dialogs import VemuDialog, dialog_layout


class AdoptDialog(VemuDialog):
    """Choose Read-only, Partial or Full for an External VM.

    Partial is executed here; Full is handed to MigrateDialog by the caller.
    """

    def __init__(self, backend, domain_name: str, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domain_name = domain_name
        self.choice = "read-only"
        self.changed = False
        self.result: dict | None = None

        self.setWindowTitle(f"Adopt {domain_name}")
        self.setMinimumWidth(560)
        self.resize(600, 380)

        layout = dialog_layout(self)

        title = QLabel(f"Adopt {domain_name} into v-EMU")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        intro = QLabel(
            "This VM is currently external/read-only. Choose how v-EMU should "
            "manage it."
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.group = QButtonGroup(self)
        self.read_only = QRadioButton("Read-only — view only")
        self.partial = QRadioButton("Partial — manage XML, keep disks")
        self.full = QRadioButton("Full — copy disks into a v2 chain")
        for index, button in enumerate(
            (self.read_only, self.partial, self.full)
        ):
            self.group.addButton(button, index)
            layout.addWidget(button)
        self.read_only.setChecked(True)

        self.description = QLabel()
        self.description.setObjectName("Muted")
        self.description.setWordWrap(True)
        self.description.setMinimumHeight(58)
        layout.addWidget(self.description)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.continue_button = QPushButton("Continue")
        self.continue_button.setObjectName("PrimaryButton")
        self.continue_button.clicked.connect(self._continue)
        buttons.addWidget(self.continue_button)
        layout.addLayout(buttons)

        self.group.idToggled.connect(self._sync_description)
        self._sync_description()

    def _sync_description(self) -> None:
        if self.read_only.isChecked():
            self.choice = "read-only"
            self.description.setText(
                "No XML or disk changes. Use this for Cockpit/virt-manager "
                "VMs that v-EMU should only inspect."
            )
        elif self.partial.isChecked():
            self.choice = "partial"
            self.description.setText(
                "v-EMU manages XML and hardware. Disks stay in place and an "
                "XML backup is written first."
            )
        else:
            self.choice = "full"
            self.description.setText(
                "Flatten the disks into a v-EMU v2 chain. The VM must be shut "
                "down; the Full migration dialog opens next."
            )

    def _continue(self) -> None:
        self._sync_description()
        if self.choice != "partial":
            self.accept()
            return
        try:
            payload = self.backend.adopt_domain(
                self.domain_name, mode="partial"
            )
        except Exception as exc:  # noqa: BLE001 - show UI error
            QMessageBox.critical(self, "Adoption failed", str(exc))
            return
        self.result = payload if isinstance(payload, dict) else None
        self.changed = True
        backup = (self.result or {}).get("backup")
        text = "Partial adoption completed."
        if backup:
            text += f"\n\nXML backup: {backup}"
        QMessageBox.information(self, "Adopted", text)
        self.accept()
