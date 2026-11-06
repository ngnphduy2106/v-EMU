"""Shared XML drift dialog for the VM page and Doctor."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..errors import VemuError
from .dialogs import VemuDialog, dialog_layout
from .widgets import ComboBox


class DriftDialog(VemuDialog):
    """Show canonical XML drift and optionally re-pin the baseline."""

    def __init__(
        self, backend, domains: list[str], parent=None
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domains = [name for name in domains if name]
        self.payload: dict[str, Any] = {}
        self.setWindowTitle("XML drift")
        self.resize(780, 520)
        self.setMinimumSize(640, 420)

        layout = dialog_layout(self)
        title = QLabel("XML drift")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        note = QLabel(
            "The current libvirt XML differs from the pinned v-EMU baseline. "
            "Review the changes before editing or re-pinning."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        if len(self.domains) > 1:
            picker = QHBoxLayout()
            picker.addWidget(QLabel("VM"))
            self.domain_combo = ComboBox()
            for name in self.domains:
                self.domain_combo.addItem(name, name)
            self.domain_combo.currentIndexChanged.connect(self._load)
            picker.addWidget(self.domain_combo, 1)
            layout.addLayout(picker)
        else:
            self.domain_combo = None

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("Muted")
        self.meta_label.setWordWrap(True)
        layout.addWidget(self.meta_label)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.text, 1)

        buttons = QHBoxLayout()
        self.repin_button = QPushButton("Re-pin baseline…")
        self.repin_button.setObjectName("SecondaryButton")
        self.repin_button.clicked.connect(self._repin)
        buttons.addWidget(self.repin_button)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("SecondaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._load()

    def _current_domain(self) -> str:
        if self.domain_combo is not None:
            return str(self.domain_combo.currentData() or "")
        return self.domains[0] if self.domains else ""

    def _load(self) -> None:
        domain = self._current_domain()
        if not domain:
            self.text.setPlainText("No domain selected.")
            self.repin_button.setEnabled(False)
            return
        try:
            self.payload = self.backend.domain_drift(domain)
        except (VemuError, AttributeError) as exc:
            self.payload = {}
            self.meta_label.setText(str(exc))
            self.text.setPlainText(str(exc))
            self.repin_button.setEnabled(False)
            return
        status = str(self.payload.get("status", "unknown"))
        changes = list(self.payload.get("changes") or [])
        baseline = str(self.payload.get("baseline") or "-")
        current = str(self.payload.get("current") or "-")
        self.meta_label.setText(
            f"{domain} · {status} · baseline "
            f"{baseline[:12]} · current {current[:12]}"
        )
        self.meta_label.setToolTip(
            f"Baseline: {baseline}\nCurrent: {current}"
        )
        if changes:
            self.text.setPlainText("\n".join(str(item) for item in changes))
        elif status == "drifted":
            self.text.setPlainText(
                "The hashes differ, but no baseline XML sidecar is available "
                "for a detailed diff. Re-pin only after reviewing the domain "
                "XML in the Hardware editor or with virsh."
            )
        else:
            self.text.setPlainText("No XML drift.")
        self.repin_button.setEnabled(status == "drifted")

    def _repin(self) -> None:
        domain = self._current_domain()
        if not domain:
            return
        answer = QMessageBox.question(
            self,
            "Re-pin XML baseline",
            f"Accept the current XML of {domain} as the new v-EMU baseline?\n\n"
            "Only do this after reviewing an intentional external change.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.domain_baseline_refresh(domain)
        except (VemuError, AttributeError) as exc:
            QMessageBox.critical(self, "Cannot re-pin baseline", str(exc))
            return
        self._load()
        QMessageBox.information(
            self, "Baseline updated", f"XML baseline updated for {domain}."
        )
