"""One-time first-run setup for storage and the default NAT network."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..errors import VemuError
from .dialogs import VemuDialog, dialog_layout
from .theme import ASSETS, DARK, LIGHT


class FirstRunDialog(VemuDialog):
    """Create the minimum usable v-EMU configuration.

    The dialog is intentionally small: one `labs` location and one NAT
    network. Templates/ISOs, isolated networks and bridges are configured
    later from their normal pages.
    """

    def __init__(self, backend, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.skipped = False
        self.storage_done = False
        self.network_done = False
        self.step = 0
        self._existing_labs = ""
        self._existing_networks: list[str] = []
        self._existing_nat = False
        self.setWindowTitle("v-EMU first-run setup")
        self.resize(700, 360)
        self.setMinimumSize(640, 340)

        self._load_existing()
        self.storage_path = QLineEdit(str(Path.home() / "vemu-labs"))
        self._icons = {
            0: ASSETS / "onboarding-welcome.svg",
            1: ASSETS / "onboarding-storage.svg",
            2: ASSETS / "onboarding-network.svg",
            3: ASSETS / "onboarding-ready.svg",
        }

        layout = dialog_layout(self)

        header = QHBoxLayout()
        header.setSpacing(12)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(64, 64)
        self.icon_label.setAlignment(Qt.AlignCenter)
        dark = bool(getattr(self._owner, "dark", False))
        tokens = DARK if dark else LIGHT
        self.icon_label.setStyleSheet(
            f"background: {tokens.accent_subtle};"
            f" border: 1px solid {tokens.border};"
            " border-radius: 14px;"
        )
        header.addWidget(self.icon_label, 0, Qt.AlignVCenter)
        self.title_label = QLabel("Welcome to v-EMU")
        self.title_label.setObjectName("HeroTitle")
        header.addWidget(self.title_label, 1, Qt.AlignVCenter)
        self.step_label = QLabel()
        self.step_label.setObjectName("Muted")
        self.step_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(
            self.step_label, 0, Qt.AlignVCenter | Qt.AlignRight
        )
        layout.addLayout(header)
        self.progress = QProgressBar()
        self.progress.setRange(0, 4)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        layout.addWidget(self.progress)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._welcome_page())
        self.stack.addWidget(self._storage_page())
        self.stack.addWidget(self._network_page())
        self.stack.addWidget(self._summary_page())
        layout.addWidget(self.stack, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.back_button = QPushButton("Back")
        self.back_button.setObjectName("SecondaryButton")
        self.back_button.clicked.connect(self._back)
        buttons.addWidget(self.back_button)
        buttons.addStretch(1)
        self.skip_button = QPushButton("Skip for now")
        self.skip_button.setObjectName("SecondaryButton")
        self.skip_button.clicked.connect(self._skip)
        buttons.addWidget(self.skip_button)
        # Reserve the Skip slot on the last step so Next/Finish does not move.
        self.skip_spacer = QWidget()
        buttons.addWidget(self.skip_spacer)
        self.next_button = QPushButton("Continue")
        self.next_button.setObjectName("PrimaryButton")
        self.next_button.clicked.connect(self._next)
        buttons.addWidget(self.next_button)
        action_buttons = (
            self.back_button,
            self.skip_button,
            self.next_button,
        )
        button_width = max(
            button.sizeHint().width() for button in action_buttons
        )
        button_height = max(
            max(button.sizeHint().height() for button in action_buttons),
            32,
        )
        for button in action_buttons:
            button.setFixedSize(button_width, button_height)
        self.skip_spacer.setFixedWidth(button_width)
        self.skip_spacer.setVisible(False)
        layout.addLayout(buttons)
        self._show_step()

    # ------------------------------------------------------------------ data

    def _load_existing(self) -> None:
        try:
            if hasattr(self.backend, "storage_registry"):
                locations = self.backend.storage_registry()
            else:
                locations = self.backend.storage_list()
        except (VemuError, AttributeError):
            locations = []
        self._existing_labs = next(
            (
                str(item.get("path", ""))
                for item in locations
                if item.get("role") == "labs"
            ),
            "",
        )
        try:
            networks = self.backend.networks_list()
        except (VemuError, AttributeError):
            networks = []
        self._existing_networks = [
            str(item.get("name", ""))
            for item in networks
            if item.get("name")
        ]
        self._existing_nat = any(
            str(item.get("mode", "")) == "nat" for item in networks
        )

    # ------------------------------------------------------------------ pages

    def _welcome_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        text = QLabel(
            "Two quick choices before the first VM:\n\n"
            "• LAB storage\n"
            "• NAT network\n\n"
            "Both can be changed later."
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        note = QLabel(
            "This wizard only configures storage and networking; "
            "host packages are installed by setup.sh."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _storage_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.storage_info = QLabel()
        self.storage_info.setWordWrap(True)
        layout.addWidget(self.storage_info)
        self.storage_edit = self.storage_path
        self.storage_edit.setPlaceholderText("~/vemu-labs")
        layout.addWidget(self.storage_edit)
        row = QHBoxLayout()
        row.addWidget(QLabel("Suggested path; you can choose another location."))
        row.addStretch(1)
        self.storage_browse = QPushButton("Browse…")
        self.storage_browse.setObjectName("SecondaryButton")
        self.storage_browse.clicked.connect(self._browse_storage)
        row.addWidget(self.storage_browse)
        layout.addLayout(row)
        self.storage_hint = QLabel(
            "Default: ~/vemu-labs. Only the labs role is created here."
        )
        self.storage_hint.setObjectName("Muted")
        self.storage_hint.setWordWrap(True)
        layout.addWidget(self.storage_hint)
        layout.addStretch(1)
        if self._existing_labs:
            self.storage_edit.setText(self._existing_labs)
            self.storage_edit.setEnabled(False)
            self.storage_browse.setEnabled(False)
            self.storage_info.setText(
                f"Storage is already configured: {self._existing_labs}"
            )
            self.storage_done = True
        else:
            self.storage_info.setText("Choose where lab disks will live.")
        return page

    def _network_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.network_info = QLabel()
        self.network_info.setWordWrap(True)
        layout.addWidget(self.network_info)
        self.network_detail = QLabel(
            "lab-nat · NAT · automatic subnet · DHCP + DNS"
        )
        self.network_detail.setObjectName("Muted")
        layout.addWidget(self.network_detail)
        self.network_hint = QLabel(
            "Add isolated or bridge networks later from Networks."
        )
        self.network_hint.setObjectName("Muted")
        self.network_hint.setWordWrap(True)
        layout.addWidget(self.network_hint)
        layout.addStretch(1)
        if self._existing_nat:
            self.network_done = True
            self.network_info.setText(
                "NAT network is already configured: "
                + ", ".join(self._existing_networks)
            )
            self.network_detail.hide()
            self.network_hint.setText(
                "You can add or edit profiles from the Networks page."
            )
        else:
            if self._existing_networks:
                self.network_info.setText(
                    "No NAT profile found; existing networks: "
                    + ", ".join(self._existing_networks)
                    + ". A default lab-nat will be added."
                )
            else:
                self.network_info.setText(
                    "Create a default NAT network for the first lab."
                )
        return page

    def _summary_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        note = QLabel("Change these later from Storage and Networks.")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ actions

    def _browse_storage(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose lab storage directory", self.storage_edit.text()
        )
        if chosen:
            self.storage_edit.setText(chosen)

    def _apply_storage(self) -> bool:
        if self.storage_done:
            return True
        path = self.storage_edit.text().strip()
        if not path:
            QMessageBox.warning(
                self, "Storage", "Choose a directory for lab disks."
            )
            return False
        try:
            self.backend.storage_add(path, "labs")
        except (VemuError, AttributeError) as exc:
            QMessageBox.critical(self, "Storage setup failed", str(exc))
            return False
        self.storage_done = True
        self.storage_edit.setEnabled(False)
        self.storage_browse.setEnabled(False)
        self.storage_info.setText(f"Storage ready: {path}")
        return True

    def _apply_network(self) -> bool:
        if self.network_done:
            return True
        try:
            try:
                self.backend.network_add(
                    "lab-nat",
                    "nat",
                    "auto",
                    True,
                    "vemu.local",
                    "Created by first-run setup",
                )
            except VemuError as exc:
                if "already exists" not in str(exc).lower():
                    raise
            self.backend.network_apply()
        except (VemuError, AttributeError) as exc:
            QMessageBox.critical(self, "Network setup failed", str(exc))
            return False
        self.network_done = True
        self.network_info.setText("Network ready: lab-nat (NAT)")
        self.network_detail.hide()
        return True

    def _show_step(self) -> None:
        self.stack.setCurrentIndex(self.step)
        titles = (
            "Welcome to v-EMU",
            "Lab storage",
            "Default network",
            "Ready to use v-EMU",
        )
        self.title_label.setText(titles[self.step])
        self.step_label.setText(
            f"Step {self.step + 1} of 4"
        )
        self.progress.setValue(self.step + 1)
        icon_path = self._icons.get(self.step)
        if icon_path is not None:
            self.icon_label.setPixmap(
                QPixmap(str(icon_path)).scaled(
                    44,
                    44,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        self.back_button.setVisible(self.step > 0)
        self.skip_button.setVisible(self.step < 3)
        self.skip_spacer.setVisible(self.step >= 3)
        if self.step == 3:
            self.next_button.setText("Finish")
            storage = self._existing_labs or self.storage_edit.text().strip()
            network = (
                ", ".join(self._existing_networks)
                if self._existing_nat
                else ("lab-nat (NAT)" if self.network_done else "not configured")
            )
            self.summary_label.setText(
                f"Storage (labs):\n{storage or 'not configured'}\n\n"
                f"Network:\n{network}\n\n"
                "Next: open Virtual machines and create your first VM."
            )
        else:
            self.next_button.setText("Continue")

    def _next(self) -> None:
        if self.step == 0:
            self.step = 1
        elif self.step == 1:
            if not self._apply_storage():
                return
            self.step = 2
        elif self.step == 2:
            if not self._apply_network():
                return
            self.step = 3
        else:
            self.accept()
            return
        self._show_step()

    def _back(self) -> None:
        if self.step > 0:
            self.step -= 1
            self._show_step()

    def _skip(self) -> None:
        self.skipped = True
        self.accept()
