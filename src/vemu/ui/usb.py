"""USB host-device passthrough dialog."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .delegate import NoFocusDelegate
from .dialogs import VemuDialog, dialog_layout

class UsbDevicesDialog(VemuDialog):
    def __init__(self, backend, domain_name: str, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domain_name = domain_name
        self.devices: list[dict[str, Any]] = []
        self.changed = False

        self.setWindowTitle(f"USB devices · {domain_name}")
        self.resize(760, 460)
        layout = dialog_layout(self)

        title = QLabel(f"USB passthrough · {domain_name}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        hint = QLabel(
            "Attaching a host USB device removes it from the host and gives "
            "it to the VM. Detach it before unplugging or handing it back. "
            "Entries marked Missing are still in the VM configuration but the "
            "device is not connected; detach them to keep the VM startable."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["DEVICE", "VENDOR", "PRODUCT", "BUS/PORT", "STATUS"]
        )
        self.table.setItemDelegate(NoFocusDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.horizontalHeader().setFixedHeight(30)
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        for column, width in ((0, 210), (1, 140), (2, 150), (3, 75)):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Fixed
            )
            self.table.horizontalHeader().resizeSection(column, width)
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("SecondaryButton")
        self.refresh_button.clicked.connect(self.refresh)
        buttons.addWidget(self.refresh_button)
        self.attach_button = QPushButton("Attach")
        self.attach_button.setObjectName("PrimaryButton")
        self.attach_button.clicked.connect(self._attach)
        buttons.addWidget(self.attach_button)
        self.detach_button = QPushButton("Detach")
        self.detach_button.setObjectName("DangerButton")
        self.detach_button.clicked.connect(self._detach)
        buttons.addWidget(self.detach_button)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("SecondaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.refresh()

    def refresh(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.devices = list(
                self.backend.usb_devices(self.domain_name) or []
            )
        except Exception as exc:  # noqa: BLE001 - show backend error
            message = str(exc)
            if "Unknown method" in message:
                message += (
                    "\n\nvemud is outdated. Run `./setup.sh --daemon` and "
                    "restart v-EMU."
                )
            QMessageBox.critical(self, "Cannot list USB devices", message)
            self.devices = []
        finally:
            QApplication.restoreOverrideCursor()
        self.table.setRowCount(len(self.devices))
        for row, device in enumerate(self.devices):
            attached = bool(device.get("attached"))
            description = str(device.get("description") or "USB device")
            if device.get("hub"):
                description += " (hub)"
            if device.get("missing"):
                status = "Missing"
                description = (
                    f"Missing USB device "
                    f"({device.get('vendor', '')}:{device.get('product', '')})"
                )
            else:
                status = "Attached" if attached else "Host"
            values = [
                description,
                str(device.get("vendor_name") or ""),
                f"{device.get('vendor', '')} / {device.get('product', '')}",
                f"{device.get('bus', '')}/{device.get('device', '')}",
                status,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, device)
                self.table.setItem(row, column, item)
        self._update_buttons()

    def _selected(self) -> dict[str, Any] | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        device = item.data(Qt.ItemDataRole.UserRole)
        return device if isinstance(device, dict) else None

    def _update_buttons(self) -> None:
        device = self._selected()
        attached = bool(device and device.get("attached"))
        hub = bool(device and device.get("hub"))
        missing = bool(device and device.get("missing"))
        self.attach_button.setEnabled(
            device is not None and not attached and not hub and not missing
        )
        self.detach_button.setEnabled(
            device is not None and (attached or missing)
        )

    def _attach(self) -> None:
        device = self._selected()
        if device is None:
            return
        try:
            payload = self.backend.vm_usb_attach(
                self.domain_name,
                str(device["vendor"]),
                str(device["product"]),
            )
        except Exception as exc:  # noqa: BLE001 - show backend error
            QMessageBox.critical(self, "Cannot attach USB device", str(exc))
            return
        self.changed = True
        warning = payload.get("warning") if isinstance(payload, dict) else None
        if warning:
            QMessageBox.warning(self, "USB device saved", str(warning))
        self.refresh()

    def _detach(self) -> None:
        device = self._selected()
        if device is None:
            return
        try:
            self.backend.vm_usb_detach(
                self.domain_name,
                str(device["vendor"]),
                str(device["product"]),
            )
        except Exception as exc:  # noqa: BLE001 - show backend error
            QMessageBox.critical(self, "Cannot detach USB device", str(exc))
            return
        self.changed = True
        self.refresh()
