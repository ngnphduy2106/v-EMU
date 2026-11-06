"""Application settings and connection information."""

from __future__ import annotations

import os

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..errors import VemuError
from .components import PageHeader, form_layout
from .tokens import COMPONENT, SPACE
from .widgets import ComboBox


class SettingsPage(QWidget):
    theme_changed = Signal(bool)
    sidebar_changed = Signal(bool)
    inspector_changed = Signal(bool)
    mini_console_changed = Signal(bool)
    auto_refresh_changed = Signal(bool, int)

    def __init__(self, backend, *, dark: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.dark = dark
        self._settings = QSettings("v-EMU", "v-EMU")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(
            COMPONENT.page_margin,
            COMPONENT.page_top,
            COMPONENT.page_margin,
            COMPONENT.page_bottom,
        )
        layout.setSpacing(SPACE.md)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        layout.addWidget(
            PageHeader(
                "Settings",
                "Desktop preferences and daemon connection",
            )
        )

        appearance = QGroupBox("Appearance")
        appearance_form = form_layout(appearance)
        self.theme_combo = ComboBox()
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.addItem("Light", "light")
        self.theme_combo.setCurrentIndex(
            self.theme_combo.findData("dark" if dark else "light")
        )
        self.theme_combo.currentIndexChanged.connect(self._theme_selected)
        appearance_form.addRow("Theme", self.theme_combo)

        self.scale_combo = ComboBox()
        for label, value in (
            ("100%", "1"),
            ("125%", "1.25"),
            ("150%", "1.5"),
            ("200%", "2"),
        ):
            self.scale_combo.addItem(label, value)
        current_scale = (
            os.environ.get("VEMU_UI_SCALE")
            or str(self._settings.value("ui/scale", "1"))
        )
        scale_index = self.scale_combo.findData(current_scale)
        if scale_index < 0:
            scale_index = 0
        self.scale_combo.setCurrentIndex(scale_index)
        self.scale_combo.currentIndexChanged.connect(self._scale_selected)
        appearance_form.addRow("UI scale", self.scale_combo)
        self.scale_note = QLabel(
            "Applied after v-EMU restarts. Use 100% on smaller screens or "
            "desktops; 125% needs at least 1600×900 (FullHD laptops); "
            "150% is intended for 2K+ and 200% for 4K."
        )
        self.scale_note.setObjectName("Muted")
        self.scale_note.setWordWrap(True)
        appearance_form.addRow(self.scale_note)
        layout.addWidget(appearance)

        panels = QGroupBox("Panels")
        panels_layout = QVBoxLayout(panels)
        self.sidebar_check = QCheckBox("Show the sidebar")
        self.sidebar_check.setChecked(
            self._settings.value("ui/sidebar", True, type=bool)
        )
        self.sidebar_check.toggled.connect(self.sidebar_changed.emit)
        panels_layout.addWidget(self.sidebar_check)
        self.inspector_check = QCheckBox("Show the details inspector")
        self.inspector_check.setChecked(
            self._settings.value("ui/inspector", True, type=bool)
        )
        self.inspector_check.toggled.connect(self.inspector_changed.emit)
        panels_layout.addWidget(self.inspector_check)
        self.mini_console_check = QCheckBox("Show the mini console preview")
        self.mini_console_check.setChecked(
            self._settings.value("ui/mini_console", True, type=bool)
        )
        self.mini_console_check.toggled.connect(
            self.mini_console_changed.emit
        )
        panels_layout.addWidget(self.mini_console_check)
        layout.addWidget(panels)

        refresh_group = QGroupBox("Refresh")
        refresh_layout = QVBoxLayout(refresh_group)
        self.auto_refresh_check = QCheckBox(
            "Refresh VM status automatically"
        )
        self.auto_refresh_check.setChecked(
            self._settings.value("ui/auto_refresh", True, type=bool)
        )
        self.auto_refresh_check.toggled.connect(self._auto_refresh_toggled)
        refresh_layout.addWidget(self.auto_refresh_check)
        interval_row = QHBoxLayout()
        interval_label = QLabel("Interval")
        interval_label.setFixedWidth(70)
        self.auto_refresh_interval = QSpinBox()
        self.auto_refresh_interval.setRange(30, 3600)
        self.auto_refresh_interval.setSingleStep(10)
        self.auto_refresh_interval.setSuffix(" s")
        self.auto_refresh_interval.setValue(
            max(
                30,
                self._settings.value(
                    "ui/auto_refresh_interval", 30, type=int
                ),
            )
        )
        self.auto_refresh_interval.setEnabled(
            self.auto_refresh_check.isChecked()
        )
        self.auto_refresh_interval.valueChanged.connect(
            self._auto_refresh_interval_changed
        )
        interval_row.addWidget(interval_label)
        interval_row.addWidget(self.auto_refresh_interval)
        interval_row.addStretch(1)
        refresh_layout.addLayout(interval_row)
        refresh_note = QLabel(
            "Minimum 30 seconds. Automatic refresh keeps the selected VM "
            "and scroll position."
        )
        refresh_note.setObjectName("Muted")
        refresh_note.setWordWrap(True)
        refresh_layout.addWidget(refresh_note)
        layout.addWidget(refresh_group)

        vm_group = QGroupBox("Virtual machines")
        vm_layout = QVBoxLayout(vm_group)
        self.start_check = QCheckBox("Start new VMs after creation")
        self.start_check.setChecked(
            self._settings.value("vm/start_after_create", True, type=bool)
        )
        self.start_check.toggled.connect(self._start_after_create)
        vm_layout.addWidget(self.start_check)
        note = QLabel(
            "The default is used by the New VM wizard. You can still change it "
            "on the summary step."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        vm_layout.addWidget(note)
        layout.addWidget(vm_group)

        resources = QGroupBox("Resource scheduling")
        resources_layout = QVBoxLayout(resources)
        self.profile_combo = ComboBox()
        for key, label in (
            ("standard", "Standard"),
            ("high", "High"),
            ("ultra", "Ultra"),
        ):
            self.profile_combo.addItem(label, key)
        self.profile_combo.currentIndexChanged.connect(
            self._profile_selected
        )
        resources_layout.addWidget(self.profile_combo)
        self.profile_note = QLabel()
        self.profile_note.setObjectName("Muted")
        self.profile_note.setWordWrap(True)
        resources_layout.addWidget(self.profile_note)
        layout.addWidget(resources)

        boot_group = QGroupBox("Boot media")
        boot_layout = QVBoxLayout(boot_group)
        self.prefer_disk_check = QCheckBox(
            "Boot the installed disk before installer media"
        )
        self.prefer_disk_check.setChecked(
            self._settings.value("ui/prefer_installed_disk", True, type=bool)
        )
        self.prefer_disk_check.toggled.connect(self._prefer_installed_disk)
        boot_layout.addWidget(self.prefer_disk_check)
        boot_note = QLabel(
            "Once the primary disk holds an OS, it becomes the boot device "
            "and the installer ISO/CD is unchecked. The media stays attached; "
            "boot it again from the BIOS/firmware boot menu."
        )
        boot_note.setObjectName("Muted")
        boot_note.setWordWrap(True)
        boot_layout.addWidget(boot_note)
        layout.addWidget(boot_group)

        advanced = QGroupBox("Advanced")
        advanced_layout = QVBoxLayout(advanced)
        self.cross_arch_check = QCheckBox(
            "Enable cross-architecture guests (aarch64/riscv64)"
        )
        self.cross_arch_check.setChecked(
            self._settings.value("ui/cross_arch", False, type=bool)
        )
        self.cross_arch_check.toggled.connect(self._cross_arch_toggled)
        advanced_layout.addWidget(self.cross_arch_check)
        self.cross_arch_note = QLabel()
        self.cross_arch_note.setObjectName("Muted")
        self.cross_arch_note.setWordWrap(True)
        advanced_layout.addWidget(self.cross_arch_note)
        self.cross_arch_warning = QLabel(
            "TCG is software emulation. It can keep CPU cores busy for hours, "
            "increase heat and power draw, and is much slower than native KVM. "
            "Use it only for short compatibility tests, and keep the Standard "
            "resource profile on laptops and small hosts."
        )
        self.cross_arch_warning.setObjectName("Warning")
        self.cross_arch_warning.setWordWrap(True)
        advanced_layout.addWidget(self.cross_arch_warning)
        layout.addWidget(advanced)
        self.cross_arch_available: list[str] = []

        connection = QGroupBox("Connection")
        connection_form = form_layout(connection)
        self.mode_label = QLabel("unknown")
        self.socket_label = QLabel("")
        self.socket_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.uri_label = QLabel("")
        self.version_label = QLabel(__version__)
        connection_form.addRow("Mode", self.mode_label)
        connection_form.addRow("Socket", self.socket_label)
        connection_form.addRow("URI", self.uri_label)
        connection_form.addRow("Version", self.version_label)
        layout.addWidget(connection)

        buttons = QHBoxLayout()
        self.copy_socket = QPushButton("Copy socket path")
        self.copy_socket.setObjectName("SecondaryButton")
        self.copy_socket.clicked.connect(self._copy_socket)
        buttons.addWidget(self.copy_socket)
        buttons.addStretch(1)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("SecondaryButton")
        self.refresh_button.clicked.connect(self.refresh_connection)
        buttons.addWidget(self.refresh_button)
        layout.addLayout(buttons)
        layout.addStretch(1)

        self.refresh_connection()
        self.refresh_profile()

    def set_theme(self, dark: bool) -> None:
        self.dark = dark
        self.theme_combo.blockSignals(True)
        index = self.theme_combo.findData("dark" if dark else "light")
        if index >= 0:
            self.theme_combo.setCurrentIndex(index)
        self.theme_combo.blockSignals(False)

    def set_panes(self, sidebar: bool, inspector: bool) -> None:
        self.sidebar_check.blockSignals(True)
        self.sidebar_check.setChecked(sidebar)
        self.sidebar_check.blockSignals(False)
        self.inspector_check.blockSignals(True)
        self.inspector_check.setChecked(inspector)
        self.inspector_check.blockSignals(False)

    def set_mini_console(self, enabled: bool) -> None:
        self.mini_console_check.blockSignals(True)
        self.mini_console_check.setChecked(enabled)
        self.mini_console_check.blockSignals(False)

    def _prefer_installed_disk(self, enabled: bool) -> None:
        self._settings.setValue("ui/prefer_installed_disk", bool(enabled))

    def refresh_profile(self) -> None:
        getter = getattr(self.backend, "runtime_profile_get", None)
        if getter is None:
            self.profile_combo.setEnabled(False)
            self.profile_note.setText(
                "Resource profiles require the vemud daemon."
            )
            return
        try:
            payload = getter()
        except (VemuError, AttributeError) as exc:
            self.profile_combo.setEnabled(False)
            self.profile_note.setText(
                f"Resource profile unavailable: {exc}"
            )
            return
        self.profile_combo.setEnabled(True)
        index = self.profile_combo.findData(
            str(payload.get("profile", "standard"))
        )
        self.profile_combo.blockSignals(True)
        if index >= 0:
            self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)
        self._set_profile_note(payload)

    def _set_profile_note(self, payload: dict) -> None:
        description = str(payload.get("description", "")).strip()
        note = (
            "Best effort: changes CPU/IO weight only. It does not cap CPU, "
            "temperature or power."
        )
        if description:
            note += " " + description
        self.profile_note.setText(note)
        warnings = payload.get("warnings") or []
        if warnings:
            self.profile_note.setText(
                self.profile_note.text() + " " + str(warnings[0])
            )

    def _profile_selected(self, _index: int) -> None:
        setter = getattr(self.backend, "runtime_profile_set", None)
        if setter is None:
            return
        key = str(self.profile_combo.currentData() or "")
        try:
            payload = setter(key)
        except (VemuError, AttributeError) as exc:
            QMessageBox.warning(
                self, "Resource scheduling", str(exc)
            )
            self.refresh_profile()
            return
        self._set_profile_note(payload)

    def _capability_payload(self, arch: str):
        try:
            if hasattr(self.backend, "vm_capabilities"):
                return self.backend.vm_capabilities(arch=arch)
            if hasattr(self.backend, "capabilities"):
                return self.backend.capabilities(arch=arch)
        except (VemuError, AttributeError, TypeError):
            return None
        return None

    def refresh_cross_arch(self) -> None:
        available: list[str] = []
        for arch in ("aarch64", "riscv64"):
            payload = self._capability_payload(arch)
            if payload and payload.get("machines"):
                available.append(arch)
        self.cross_arch_available = available
        enabled = bool(available)
        self.cross_arch_check.blockSignals(True)
        self.cross_arch_check.setEnabled(enabled)
        if not enabled:
            self.cross_arch_check.setChecked(False)
            self._settings.setValue("ui/cross_arch", False)
        self.cross_arch_check.blockSignals(False)
        if enabled:
            missing = [
                arch for arch in ("aarch64", "riscv64")
                if arch not in available
            ]
            text = (
                "Available cross-architecture QEMU: "
                + ", ".join(available)
                + ". TCG is much slower than native x86_64 (no hardware "
                "virtualization or GPU acceleration), and needs the v-EMU UEFI "
                "firmware plus architecture-matched ISOs."
            )
            if missing:
                text += " Not installed: " + ", ".join(missing) + "."
            self.cross_arch_note.setText(text)
            self.cross_arch_check.setToolTip(
                "Cross-arch QEMU detected: " + ", ".join(available)
            )
        else:
            self.cross_arch_note.setText(
                "Cross-architecture QEMU is not installed on this host. "
                "Install it with `./setup.sh --full` on Fedora, or install the "
                "matching QEMU packages for this distribution. The option "
                "stays disabled until then."
            )
            self.cross_arch_check.setToolTip(
                "Install cross-arch QEMU packages to enable this option."
            )

    def _cross_arch_toggled(self, enabled: bool) -> None:
        if enabled and not self.cross_arch_available:
            self.cross_arch_check.blockSignals(True)
            self.cross_arch_check.setChecked(False)
            self.cross_arch_check.blockSignals(False)
            return
        self._settings.setValue("ui/cross_arch", bool(enabled))

    def set_auto_refresh(self, enabled: bool, interval: int) -> None:
        self.auto_refresh_check.blockSignals(True)
        self.auto_refresh_interval.blockSignals(True)
        self.auto_refresh_check.setChecked(enabled)
        self.auto_refresh_interval.setValue(max(30, int(interval)))
        self.auto_refresh_interval.setEnabled(enabled)
        self.auto_refresh_check.blockSignals(False)
        self.auto_refresh_interval.blockSignals(False)

    def refresh_connection(self) -> None:
        info: dict = {}
        client = getattr(self.backend, "client", None)
        if client is not None:
            self.mode_label.setText("vemud daemon")
            self.socket_label.setText(str(client.socket_path))
            try:
                info = self.backend.system_info()
            except VemuError:
                info = {}
        else:
            self.mode_label.setText("direct libvirt")
            self.socket_label.setText("(not used)")
        self.uri_label.setText(
            str(info.get("uri", getattr(self.backend, "uri", "qemu:///system")))
        )
        self.version_label.setText(
            str(info.get("version", __version__))
        )
        self.refresh_cross_arch()

    def _theme_selected(self) -> None:
        self.theme_changed.emit(
            str(self.theme_combo.currentData()) == "dark"
        )

    def _scale_selected(self) -> None:
        value = str(self.scale_combo.currentData() or "1")
        self._settings.setValue("ui/scale", value)
        label = self.scale_combo.currentText()
        self.scale_note.setText(
            f"Restart v-EMU to apply {label}. 125% needs at least "
            "1600×900; use 150% on 2K+ and 200% on 4K."
        )

    def _start_after_create(self, enabled: bool) -> None:
        self._settings.setValue("vm/start_after_create", enabled)

    def _auto_refresh_toggled(self, enabled: bool) -> None:
        self.auto_refresh_interval.setEnabled(enabled)
        self._settings.setValue("ui/auto_refresh", enabled)
        self.auto_refresh_changed.emit(
            enabled, self.auto_refresh_interval.value()
        )

    def _auto_refresh_interval_changed(self, value: int) -> None:
        self._settings.setValue("ui/auto_refresh_interval", value)
        if self.auto_refresh_check.isChecked():
            self.auto_refresh_changed.emit(True, value)

    def _copy_socket(self) -> None:
        QGuiApplication.clipboard().setText(self.socket_label.text())
