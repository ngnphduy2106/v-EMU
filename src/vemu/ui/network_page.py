"""Networks page: profiles, libvirt state, and NAT port forwarding."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QIntValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..errors import VemuError
from .delegate import NoFocusDelegate
from .dialogs import VemuDialog, dialog_layout
from .widgets import ComboBox, EmptyStateTable
from .components import form_layout, PageHeader
from .tokens import TYPE


def _runtime_state(profile: dict[str, Any]) -> str:
    runtime = profile.get("runtime", {})
    if runtime.get("error"):
        return "unavailable"
    if runtime.get("active"):
        return "active"
    if runtime.get("defined"):
        return "defined"
    return "not applied"


def _format_profile(profile: dict[str, Any]) -> str:
    runtime = profile.get("runtime", {})
    if profile.get("dhcp"):
        dhcp = "DHCP/DNS on"
        if profile.get("dns_domain"):
            dhcp += f" ({profile['dns_domain']})"
    else:
        dhcp = "no DHCP/DNS"
    gateway = profile.get("gateway") or "no host IP"
    lines = [
        f"<b style='font-size:{TYPE.body_lg}px'>{profile.get('name', '')}</b><br>",
        f"{profile.get('mode', '').upper()} · {profile.get('subnet', '')} · "
        f"{dhcp}<br>",
        f"Gateway: {gateway} · bridge {profile.get('bridge', '')}<br>",
        f"libvirt network: <span style='font-family:\"JetBrains Mono\",monospace'>"
        f"{profile.get('libvirt_name', '')}</span><br>",
        f"State: {_runtime_state(profile)}",
    ]
    reservations = profile.get("host_reservations") or []
    if reservations:
        lines.append(
            f"<br>DHCP reservations: {len(reservations)}"
        )
    if profile.get("description"):
        lines.append(f"<br><br>{profile['description']}")
    error = runtime.get("error")
    if error:
        lines.append(f"<br><br><b>libvirt</b>: {error}")
    return "".join(lines)


class NetworkAddDialog(VemuDialog):
    def __init__(
        self,
        backend,
        parent=None,
        profile: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.profile: dict[str, Any] | None = None
        self._editing = profile is not None
        self.setWindowTitle(
            f"Edit network · {profile.get('name', '')}"
            if profile
            else "Add network"
        )
        self.resize(520, 300)

        layout = dialog_layout(self)
        title = QLabel(self.windowTitle())
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        form = form_layout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("lab-net")
        form.addRow("Name", self.name_edit)

        self.mode_combo = ComboBox()
        self.mode_combo.addItem("NAT — internet access", "nat")
        self.mode_combo.addItem("Isolated — lab only", "isolated")
        self.mode_combo.addItem("Bridge — existing host bridge", "bridge")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        form.addRow("Mode", self.mode_combo)

        self.subnet_edit = QLineEdit("auto")
        self.subnet_edit.setPlaceholderText("auto or 10.213.0.0/24")
        form.addRow("Subnet", self.subnet_edit)

        self.bridge_edit = QLineEdit()
        self.bridge_edit.setPlaceholderText("br0 or bridge0")
        self.bridge_edit.setEnabled(False)
        form.addRow("Host bridge", self.bridge_edit)

        self.dhcp_check = QCheckBox("DHCP and DNS (uses a host IP)")
        self.dhcp_check.setChecked(True)
        form.addRow("Services", self.dhcp_check)

        self.dns_edit = QLineEdit("vemu.local")
        form.addRow("DNS domain", self.dns_edit)

        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("Optional description")
        form.addRow("Description", self.description_edit)
        layout.addLayout(form)
        layout.addStretch(1)

        helper = QLabel(
            "NAT networks get an internet route through the host. Isolated "
            "networks without DHCP get no host IP, so labs can reuse subnets. "
            "Bridge networks attach VMs to an existing host bridge such as "
            "br0 or bridge0; DHCP/DNS stay on that network."
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
        create = QPushButton("Save" if profile else "Create")
        create.setObjectName("PrimaryButton")
        create.clicked.connect(self._create)
        buttons.addWidget(create)
        layout.addLayout(buttons)

        if profile:
            self._load_profile(profile)

    def _load_profile(self, profile: dict[str, Any]) -> None:
        self.name_edit.setText(str(profile.get("name", "")))
        self.name_edit.setEnabled(False)
        mode = str(profile.get("mode", "nat"))
        self.mode_combo.blockSignals(True)
        index = self.mode_combo.findData(mode)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)
        self.mode_combo.blockSignals(False)
        # Changing the mode of an existing profile would require recreating
        # its libvirt network and bridge attachment; keep it immutable.
        self.mode_combo.setEnabled(False)
        self.subnet_edit.setText(str(profile.get("subnet", "auto")))
        self.bridge_edit.setText(str(profile.get("bridge", "")))
        self.dhcp_check.setChecked(bool(profile.get("dhcp")))
        self.dns_edit.setText(str(profile.get("dns_domain", "vemu.local")))
        self.description_edit.setText(str(profile.get("description", "")))
        self._sync_mode_fields()

    def _sync_mode_fields(self) -> None:
        mode = str(self.mode_combo.currentData())
        bridge = mode == "bridge"
        self.bridge_edit.setEnabled(bridge and not self._editing)
        self.subnet_edit.setEnabled(not bridge)
        self.dhcp_check.setEnabled(not bridge)
        self.dns_edit.setEnabled(not bridge)

    def _mode_changed(self) -> None:
        mode = str(self.mode_combo.currentData())
        self.dhcp_check.setChecked(mode == "nat")
        if mode == "bridge":
            self.subnet_edit.setText("auto")
            self.dhcp_check.setChecked(False)
        self._sync_mode_fields()

    def _create(self) -> None:
        name = self.name_edit.text().strip()
        mode = str(self.mode_combo.currentData())
        subnet = self.subnet_edit.text().strip() or "auto"
        dhcp = self.dhcp_check.isChecked()
        dns = self.dns_edit.text().strip()
        description = self.description_edit.text().strip()
        try:
            if self._editing:
                update = getattr(self.backend, "network_update", None)
                if update is None:
                    QMessageBox.critical(
                        self,
                        "Cannot edit network",
                        "Editing networks requires vemud.",
                    )
                    return
                self.profile = update(
                    name, mode, subnet, dhcp, dns, description
                )
            else:
                self.profile = self.backend.network_add(
                    name,
                    mode,
                    subnet,
                    dhcp,
                    dns,
                    description,
                    self.bridge_edit.text().strip(),
                )
        except VemuError as exc:
            QMessageBox.critical(
                self,
                "Cannot edit network" if self._editing else "Cannot add network",
                str(exc),
            )
            return
        self.accept()


class PortForwardDialog(VemuDialog):
    def __init__(self, backend, profile: dict[str, Any], parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.profile = profile
        self.forward: dict[str, Any] | None = None
        self.setWindowTitle(f"Port forward · {profile.get('name', '')}")
        self.resize(460, 280)

        layout = dialog_layout(self)
        title = QLabel("Add port forward")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        form = form_layout()
        self.protocol_combo = ComboBox()
        self.protocol_combo.addItems(["tcp", "udp"])
        form.addRow("Protocol", self.protocol_combo)
        self.host_port = QLineEdit("2222")
        self.host_port.setValidator(QIntValidator(1, 65535, self))
        form.addRow("Host port", self.host_port)
        self.guest_ip = QLineEdit()
        self.guest_ip.setPlaceholderText("10.213.0.10")
        form.addRow("Guest IP", self.guest_ip)
        self.guest_port = QLineEdit("22")
        self.guest_port.setValidator(QIntValidator(1, 65535, self))
        form.addRow("Guest port", self.guest_port)
        self.description = QLineEdit()
        self.description.setPlaceholderText("Optional, e.g. SSH")
        form.addRow("Description", self.description)
        layout.addLayout(form)
        layout.addStretch(1)

        helper = QLabel(
            "The guest IP must be inside the network subnet. Rules are kept "
            "in the v-EMU nft table, separate from firewalld."
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
        create = QPushButton("Add forward")
        create.setObjectName("PrimaryButton")
        create.clicked.connect(self._create)
        buttons.addWidget(create)
        layout.addLayout(buttons)

    def _create(self) -> None:
        try:
            host_port = int(self.host_port.text())
            guest_port = int(self.guest_port.text())
        except ValueError:
            QMessageBox.warning(
                self, "Invalid port", "Host and guest ports must be numbers."
            )
            return
        try:
            payload = self.backend.network_port_forward_add(
                self.profile["name"],
                self.protocol_combo.currentText(),
                host_port,
                self.guest_ip.text().strip(),
                guest_port,
                self.description.text().strip(),
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot add forward", str(exc))
            return
        self.forward = payload
        if payload.get("warning"):
            QMessageBox.warning(self, "Port forward", payload["warning"])
        self.accept()


class VmNetworkDialog(VemuDialog):
    """List, attach and detach NICs for one VM."""

    def __init__(self, backend, domain: str, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domain = domain
        self.interfaces: list[dict[str, Any]] = []
        self.setWindowTitle(f"Networks · {domain}")
        self.resize(660, 380)

        layout = dialog_layout(self)

        title = QLabel(f"Network interfaces · {domain}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        helper = QLabel(
            "Changes are applied to the running VM and the persistent "
            "definition. Detaching a NIC disconnects that network immediately."
        )
        helper.setObjectName("Muted")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        self.table = EmptyStateTable(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["MAC", "NETWORK", "MODEL", "SOURCE"]
        )
        for column in range(4):
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
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.horizontalHeader().setFixedHeight(30)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(0, 160)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(2, 80)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(3, 120)
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
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.attach_button = QPushButton("Attach network")
        self.attach_button.setObjectName("PrimaryButton")
        self.attach_button.clicked.connect(self._attach)
        buttons.addWidget(self.attach_button)
        self.detach_button = QPushButton("Detach selected")
        self.detach_button.setObjectName("SecondaryButton")
        self.detach_button.setEnabled(False)
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
        try:
            self.interfaces = self.backend.domain_interfaces(self.domain)
        except VemuError as exc:
            self.interfaces = []
            self.table.setRowCount(0)
            self.table.set_empty_state("Cannot load interfaces", str(exc))
            self.table.refresh_empty_state(0)
            self.detach_button.setEnabled(False)
            return

        self.table.setRowCount(0)
        for interface in self.interfaces:
            row = self.table.rowCount()
            self.table.insertRow(row)
            source = (
                interface.get("network")
                or interface.get("bridge")
                or interface.get("target")
                or interface.get("type")
                or ""
            )
            values = [
                interface.get("mac", ""),
                interface.get("network") or interface.get("bridge") or "—",
                interface.get("model") or "—",
                source,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, column, item)
        if not self.interfaces:
            self.table.set_empty_state(
                "No network interfaces",
                "Attach a network profile to give this VM connectivity.",
            )
        self.table.refresh_empty_state()
        self.detach_button.setEnabled(False)

    def _selection_changed(self) -> None:
        self.detach_button.setEnabled(bool(self.table.selectionModel().selectedRows()))

    def _attach(self) -> None:
        try:
            profiles = self.backend.networks_list()
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot list networks", str(exc))
            return
        names = [profile["name"] for profile in profiles]
        if not names:
            QMessageBox.information(
                self,
                "Attach network",
                "No network profiles yet. Create one on the Networks page first.",
            )
            return
        name, accepted = QInputDialog.getItem(
            self, "Attach network", "Network:", names, 0, False
        )
        if not accepted:
            return
        try:
            self.backend.network_attach(self.domain, name)
        except VemuError as exc:
            QMessageBox.critical(self, "Attach failed", str(exc))
            return
        self.refresh()

    def _detach(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return
        mac = item.text()
        answer = QMessageBox.question(
            self,
            "Detach interface",
            f"Detach {mac} from {self.domain}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.network_detach(self.domain, mac)
        except VemuError as exc:
            QMessageBox.critical(self, "Detach failed", str(exc))
            return
        self.refresh()


class ReservationAddDialog(VemuDialog):
    def __init__(self, backend, profile: dict[str, Any], parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.profile = profile
        self.reservation: dict[str, Any] | None = None
        self.setWindowTitle(f"DHCP reservation · {profile.get('name', '')}")
        self.resize(460, 260)

        layout = dialog_layout(self)
        title = QLabel("Reserve DHCP address")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        form = form_layout()
        self.mac_edit = QLineEdit()
        self.mac_edit.setPlaceholderText("52:54:00:aa:bb:cc")
        form.addRow("MAC address", self.mac_edit)
        self.ip_edit = QLineEdit()
        self.ip_edit.setPlaceholderText("10.213.0.20")
        form.addRow("IP address", self.ip_edit)
        self.hostname_edit = QLineEdit()
        self.hostname_edit.setPlaceholderText("Optional hostname")
        form.addRow("Hostname", self.hostname_edit)
        layout.addLayout(form)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        create = QPushButton("Reserve")
        create.setObjectName("PrimaryButton")
        create.clicked.connect(self._create)
        buttons.addWidget(create)
        layout.addLayout(buttons)

    def _create(self) -> None:
        try:
            payload = self.backend.network_reservation_add(
                self.profile["name"],
                self.mac_edit.text().strip(),
                self.ip_edit.text().strip(),
                self.hostname_edit.text().strip(),
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot reserve address", str(exc))
            return
        self.reservation = payload
        self.accept()


class DhcpReservationDialog(VemuDialog):
    def __init__(self, backend, profile: dict[str, Any], parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.profile = profile
        self.changed = False
        self.setWindowTitle(f"DHCP reservations · {profile.get('name', '')}")
        self.resize(620, 380)

        layout = dialog_layout(self)

        title = QLabel(
            f"DHCP reservations · {profile.get('name', '')}"
        )
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        helper = QLabel(
            "Reserved addresses are pinned to a MAC. Restart the network "
            "after adding or removing a reservation."
        )
        helper.setObjectName("Muted")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        self.table = EmptyStateTable(0, 3)
        self.table.setHorizontalHeaderLabels(["MAC", "IP", "HOSTNAME"])
        for column in range(3):
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
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.horizontalHeader().setFixedHeight(30)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.table.horizontalHeader().resizeSection(0, 180)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
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
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        self.hint = QLabel("")
        self.hint.setObjectName("Muted")
        layout.addWidget(self.hint)

        buttons = QHBoxLayout()
        add = QPushButton("Add reservation")
        add.setObjectName("PrimaryButton")
        add.clicked.connect(self._add)
        buttons.addWidget(add)
        self.remove_button = QPushButton("Remove selected")
        self.remove_button.setObjectName("SecondaryButton")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("SecondaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.refresh()

    def refresh(self) -> None:
        try:
            profiles = self.backend.networks_list()
        except VemuError as exc:
            self.table.setRowCount(0)
            self.table.set_empty_state("Cannot load reservations", str(exc))
            self.table.refresh_empty_state(0)
            self._selection_changed()
            return
        for item in profiles:
            if item["name"] == self.profile["name"]:
                self.profile = item
                break
        reservations = self.profile.get("host_reservations", [])
        self.table.setRowCount(0)
        for reservation in reservations:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                reservation.get("mac", ""),
                reservation.get("ip", ""),
                reservation.get("hostname", ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, column, item)
        if not reservations:
            self.table.set_empty_state(
                "No reservations",
                "Reserve a fixed address for a MAC, useful for router VMs "
                "and port-forward targets.",
            )
        self.table.refresh_empty_state()
        self._selection_changed()

    def _selection_changed(self) -> None:
        self.remove_button.setEnabled(
            bool(self.table.selectionModel().selectedRows())
        )

    def _add(self) -> None:
        dialog = ReservationAddDialog(self.backend, self.profile, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.refresh()
        self.hint.setText("Reservation added. Restart the network to apply.")
        self.changed = True

    def _remove(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return
        mac = item.text()
        answer = QMessageBox.question(
            self,
            "Remove reservation",
            f"Remove the reservation for {mac}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.network_reservation_remove(
                self.profile["name"], mac
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot remove reservation", str(exc))
            return
        self.refresh()
        self.hint.setText("Reservation removed. Restart the network to apply.")
        self.changed = True


class LanBridgeWizard(VemuDialog):
    """Create a LAN bridge or register an existing one.

    Existing bridges are used as-is. Creating one goes through the daemon,
    which snapshots the uplink profile and rolls back on failure.
    """

    def __init__(self, backend, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.inventory: dict[str, Any] = {}
        self.profile: dict[str, Any] | None = None
        self.bridge_name = ""
        self.created_host_bridge = False
        self.setWindowTitle("Set up a LAN bridge")
        self.resize(660, 560)

        layout = dialog_layout(self)
        title = QLabel("Set up a LAN bridge")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        helper = QLabel(
            "A LAN bridge puts VMs on the same physical network as the host, "
            "so other machines can reach them. Use an existing bridge when "
            "one is already present."
        )
        helper.setObjectName("Muted")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        self.form = form_layout()
        self.mode_combo = ComboBox()
        self.mode_combo.addItem("Use an existing host bridge", "existing")
        self.mode_combo.addItem("Create a new bridge", "create")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.form.addRow("Setup", self.mode_combo)

        self.existing_combo = ComboBox()
        self.existing_combo.currentIndexChanged.connect(
            self._existing_changed
        )
        self.form.addRow("Host bridge", self.existing_combo)

        self.profile_edit = QLineEdit("lan-bridge")
        self.profile_edit.setPlaceholderText("lan-bridge")
        self.form.addRow("Network name", self.profile_edit)

        self.bridge_edit = QLineEdit("br-vemu")
        self.bridge_edit.setPlaceholderText("br-vemu")
        self.form.addRow("New bridge", self.bridge_edit)

        self.uplink_combo = ComboBox()
        self.form.addRow("Ethernet port", self.uplink_combo)

        self.addressing_combo = ComboBox()
        self.addressing_combo.addItem(
            "DHCP from the LAN (recommended)", "dhcp"
        )
        self.addressing_combo.addItem("Static IPv4", "static")
        self.addressing_combo.addItem("No host address", "none")
        self.addressing_combo.currentIndexChanged.connect(
            self._addressing_changed
        )
        self.form.addRow("Host address", self.addressing_combo)

        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("192.168.1.50/24")
        self.form.addRow("IPv4 address", self.address_edit)
        self.gateway_edit = QLineEdit()
        self.gateway_edit.setPlaceholderText("192.168.1.1")
        self.form.addRow("Gateway", self.gateway_edit)
        self.dns_edit = QLineEdit()
        self.dns_edit.setPlaceholderText("1.1.1.1, 9.9.9.9")
        self.form.addRow("DNS servers", self.dns_edit)

        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("Optional, e.g. lab LAN")
        self.form.addRow("Description", self.description_edit)
        layout.addLayout(self.form)

        self.warning = QLabel("")
        self.warning.setObjectName("Warning")
        self.warning.setWordWrap(True)
        self.warning.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.warning)
        self.hint = QLabel("")
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        layout.addStretch(1)
        self._base_hint = ""

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.create_button = QPushButton("Create network")
        self.create_button.setObjectName("PrimaryButton")
        self.create_button.clicked.connect(self._create)
        buttons.addWidget(self.create_button)
        layout.addLayout(buttons)

        self._load_inventory()
        self._mode_changed()

    # ---------------------------------------------------------------- helpers

    def _set_row_visible(self, field, visible: bool) -> None:
        label = self.form.labelForField(field)
        if label is not None:
            label.setVisible(visible)
        field.setVisible(visible)

    @staticmethod
    def _display_addresses(item: dict[str, Any]) -> str:
        values = [str(value) for value in item.get("addresses") or []]
        ipv4 = [value for value in values if ":" not in value]
        chosen = ipv4[:2] or values[:1]
        return ", ".join(chosen) or "no address"

    def _bridge_label(self, item: dict[str, Any]) -> str:
        addresses = self._display_addresses(item)
        ports = ", ".join(item.get("interfaces") or []) or "no ports"
        state = item.get("state") or "unknown"
        return f"{item.get('name', '')} · {addresses} · {state} · {ports}"

    def _uplink_label(self, item: dict[str, Any]) -> str:
        addresses = self._display_addresses(item)
        state = item.get("state") or "unknown"
        connection = (item.get("connection") or {}).get("name") or "unmanaged"
        return f"{item.get('name', '')} · {state} · {addresses} · {connection}"

    def _load_inventory(self) -> None:
        try:
            inventory = self.backend.network_bridge_inventory()
        except (VemuError, AttributeError) as exc:
            self.inventory = {"bridges": [], "uplinks": []}
            self.hint.setText(str(exc))
            self.warning.setText(
                "Host bridge discovery requires a current vemud. You can "
                "still type an existing bridge name manually under Add "
                "network."
            )
        else:
            self.inventory = inventory or {}
        bridges = self.inventory.get("bridges") or []
        uplinks = self.inventory.get("uplinks") or []
        bridges = sorted(
            bridges,
            key=lambda item: (
                not bool(item.get("interfaces")),
                str(item.get("state")) != "up",
                str(item.get("name", "")),
            ),
        )
        self.existing_combo.clear()
        for item in bridges:
            self.existing_combo.addItem(
                self._bridge_label(item), item.get("name")
            )
        if not bridges:
            self.existing_combo.addItem(
                "No host bridge detected — create one below", None
            )
        self.uplink_combo.clear()
        for item in uplinks:
            self.uplink_combo.addItem(
                self._uplink_label(item), item.get("name")
            )
        if not uplinks:
            self.uplink_combo.addItem(
                "No unused Ethernet port — disconnect it from its bridge "
                "first", None
            )
        hints = []
        reason = str(self.inventory.get("reason") or "")
        if reason:
            hints.append(reason)
        if self.inventory.get("nmcli") is False:
            hints.append(
                "New bridges need NetworkManager. Install nmcli or create "
                "the bridge with your distribution tools."
            )
        self._base_hint = " ".join(hints)
        self._bridge_items = {
            str(item.get("name")): item for item in bridges
        }
        preferred = "existing" if bridges else "create"
        index = self.mode_combo.findData(preferred)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

    def _mode_changed(self) -> None:
        create = str(self.mode_combo.currentData()) == "create"
        self._set_row_visible(self.existing_combo, not create)
        self._set_row_visible(self.bridge_edit, create)
        self._set_row_visible(self.uplink_combo, create)
        self._set_row_visible(self.addressing_combo, create)
        self._addressing_changed()
        self.create_button.setText(
            "Create bridge and network" if create else "Create network"
        )
        if create:
            uplink = self.uplink_combo.currentData()
            detail = f" on {uplink}" if uplink else ""
            self.warning.setText(
                f"Creating a bridge{detail} detaches the port from its "
                "current NetworkManager profile and briefly interrupts host "
                "networking. The old profile is kept and disabled; a backup "
                "is written first. Use a local console if you are connected "
                "over SSH or VNC."
            )
            self.hint.setText(self._base_hint)
        else:
            self._existing_changed()
            hint = (
                "Existing bridges make no host networking changes; DHCP and "
                "DNS stay on the LAN."
            )
            self.hint.setText(
                f"{hint} {self._base_hint}".strip()
            )

    def _existing_changed(self) -> None:
        if str(self.mode_combo.currentData()) != "existing":
            return
        name = str(self.existing_combo.currentData() or "")
        item = getattr(self, "_bridge_items", {}).get(name, {})
        ports = item.get("interfaces") or []
        state = str(item.get("state") or "")
        if name and not ports:
            self.warning.setText(
                f"{name} has no physical uplink. VMs attached to it can reach "
                "other guests, but not the LAN. Choose a bridge with an "
                "Ethernet port or create a new bridge."
            )
        elif name and state and state != "up":
            self.warning.setText(
                f"{name} is {state}. Attach it to an uplink and bring it up "
                "before starting VMs, or choose another bridge."
            )
        else:
            self.warning.setText("")

    def _addressing_changed(self) -> None:
        create = str(self.mode_combo.currentData()) == "create"
        static = create and (
            str(self.addressing_combo.currentData()) == "static"
        )
        for field in (
            self.address_edit,
            self.gateway_edit,
            self.dns_edit,
        ):
            self._set_row_visible(field, static)

    # ----------------------------------------------------------------- action

    def _create(self) -> None:
        name = self.profile_edit.text().strip()
        description = self.description_edit.text().strip()
        create = str(self.mode_combo.currentData()) == "create"
        if not name:
            QMessageBox.warning(
                self, "Network name required", "Enter a name for the network."
            )
            return
        if create:
            bridge = self.bridge_edit.text().strip()
            uplink = str(self.uplink_combo.currentData() or "")
            if not bridge:
                QMessageBox.warning(
                    self,
                    "Bridge name required",
                    "Enter a name for the new host bridge.",
                )
                return
            if not uplink:
                QMessageBox.warning(
                    self,
                    "Ethernet port required",
                    "No unused physical Ethernet port is available. Disconnect "
                    "a port from its current bridge or create the bridge "
                    "manually.",
                )
                return
            try:
                payload = self.backend.network_bridge_create(
                    name,
                    bridge,
                    uplink,
                    str(self.addressing_combo.currentData()),
                    self.address_edit.text().strip(),
                    self.gateway_edit.text().strip(),
                    self.dns_edit.text().strip(),
                    description,
                )
            except VemuError as exc:
                QMessageBox.critical(
                    self, "Cannot create LAN bridge", str(exc)
                )
                return
            self.profile = payload.get("profile")
            self.bridge_name = str(payload.get("name") or bridge)
            self.created_host_bridge = True
        else:
            bridge = str(self.existing_combo.currentData() or "")
            if not bridge:
                QMessageBox.warning(
                    self,
                    "Host bridge required",
                    "No existing host bridge was detected. Choose Create a new "
                    "bridge or create one with your distribution tools.",
                )
                return
            try:
                self.profile = self.backend.network_add(
                    name,
                    "bridge",
                    "auto",
                    False,
                    "",
                    description,
                    bridge,
                )
            except VemuError as exc:
                QMessageBox.critical(
                    self, "Cannot register bridge", str(exc)
                )
                return
            self.bridge_name = bridge
        apply_error = ""
        apply = getattr(self.backend, "network_apply", None)
        if apply is not None:
            try:
                apply()
            except VemuError as exc:
                apply_error = str(exc)
        if apply_error:
            QMessageBox.warning(
                self,
                "Network registered, apply failed",
                "The profile was saved, but applying it to libvirt failed:\n"
                f"{apply_error}\n\n"
                "Press Apply on the Networks page to retry.",
            )
        self.accept()


class NetworkPage(QWidget):
    status = Signal(str)

    def __init__(self, backend, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.profiles: list[dict[str, Any]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.addWidget(
            PageHeader(
                "Networks",
                "Lab segments, DHCP/DNS, and NAT port forwarding.",
            )
        )
        header.addStretch(1)

        self.bridge_button = QPushButton("LAN bridge…")
        self.bridge_button.setObjectName("SecondaryButton")
        self.bridge_button.setToolTip(
            "Set up a host bridge so VMs are reachable from other devices "
            "on your LAN."
        )
        self.bridge_button.clicked.connect(self._lan_bridge)
        header.addWidget(self.bridge_button)

        self.add_button = QPushButton("Add network")
        self.add_button.setToolTip("Add network (Ctrl+N)")
        self.add_button.setObjectName("PrimaryButton")
        self.add_button.clicked.connect(self._add)
        header.addWidget(self.add_button)

        self.apply_button = QPushButton("Apply")
        self.apply_button.setObjectName("SecondaryButton")
        self.apply_button.setToolTip(
            "Reconcile profiles into libvirt and the v-EMU nft table."
        )
        self.apply_button.clicked.connect(self._apply)
        header.addWidget(self.apply_button)

        refresh = QPushButton("Refresh")
        refresh.setObjectName("SecondaryButton")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        layout.addLayout(header)

        self.table = EmptyStateTable(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["NAME", "MODE", "SUBNET", "DHCP", "BRIDGE", "STATE", "FORWARDS"]
        )
        for column in range(7):
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
        for column, width in ((1, 90), (2, 140), (3, 70), (4, 120), (5, 100), (6, 90)):
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

        self.details = QLabel("Select a network to inspect it.")
        self.details.setObjectName("Muted")
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        layout.addWidget(self.details)

        forward_header = QHBoxLayout()
        forward_label = QLabel("PORT FORWARDS")
        forward_label.setObjectName("SectionLabel")
        forward_header.addWidget(forward_label)
        forward_header.addStretch(1)
        self.add_forward_button = QPushButton("Add forward")
        self.add_forward_button.setObjectName("SecondaryButton")
        self.add_forward_button.setEnabled(False)
        self.add_forward_button.clicked.connect(self._add_forward)
        forward_header.addWidget(self.add_forward_button)
        self.remove_forward_button = QPushButton("Remove forward")
        self.remove_forward_button.setObjectName("SecondaryButton")
        self.remove_forward_button.setEnabled(False)
        self.remove_forward_button.clicked.connect(self._remove_forward)
        forward_header.addWidget(self.remove_forward_button)
        layout.addLayout(forward_header)

        self.forwards = EmptyStateTable(0, 4)
        self.forwards.setHorizontalHeaderLabels(
            ["PROTOCOL", "HOST PORT", "GUEST", "DESCRIPTION"]
        )
        for column in range(4):
            header_item = self.forwards.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
        self.forwards.setItemDelegate(NoFocusDelegate(self.forwards))
        self.forwards.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.forwards.verticalHeader().setVisible(False)
        self.forwards.verticalHeader().setDefaultSectionSize(32)
        self.forwards.horizontalHeader().setFixedHeight(30)
        self.forwards.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.forwards.horizontalHeader().resizeSection(0, 90)
        self.forwards.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Fixed
        )
        self.forwards.horizontalHeader().resizeSection(1, 100)
        self.forwards.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.forwards.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.forwards.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.forwards.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.forwards.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.forwards.setShowGrid(False)
        self.forwards.setMaximumHeight(150)
        self.forwards.itemSelectionChanged.connect(self._forward_selection)
        layout.addWidget(self.forwards)

        self.remove_button = QPushButton("Remove network")
        self.remove_button.setObjectName("SecondaryButton")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove)
        self.edit_button = QPushButton("Edit network")
        self.edit_button.setObjectName("SecondaryButton")
        self.edit_button.setEnabled(False)
        self.edit_button.setToolTip(
            "Change subnet, DHCP/DNS and the description without losing "
            "port forwards or reservations."
        )
        self.edit_button.clicked.connect(self._edit)
        self.reservations_button = QPushButton("DHCP reservations…")
        self.reservations_button.setObjectName("SecondaryButton")
        self.reservations_button.setEnabled(False)
        self.reservations_button.setToolTip(
            "Manage static DHCP reservations. Restart the network to apply."
        )
        self.reservations_button.clicked.connect(self._manage_reservations)
        self.restart_button = QPushButton("Restart network")
        self.restart_button.setObjectName("SecondaryButton")
        self.restart_button.setEnabled(False)
        self.restart_button.setToolTip(
            "Apply live libvirt changes. Attached VMs briefly lose connectivity."
        )
        self.restart_button.clicked.connect(self._restart)
        actions = QHBoxLayout()
        actions.addWidget(self.reservations_button)
        actions.addWidget(self.restart_button)
        actions.addWidget(self.edit_button)
        actions.addWidget(self.remove_button)
        actions.addStretch(1)
        layout.addLayout(actions)

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        if not hasattr(self.backend, "networks_list"):
            self.profiles = []
            self.add_button.setEnabled(False)
            self.apply_button.setEnabled(False)
            self.table.setRowCount(0)
            self.table.set_empty_state(
                "Networks need vemud",
                "Start the v-EMU daemon to manage libvirt networks.",
            )
            self.table.refresh_empty_state(0)
            self._selection_changed()
            return
        selected = self._selected_name()
        try:
            self.profiles = self.backend.networks_list()
        except VemuError as exc:
            self.profiles = []
            self.table.setRowCount(0)
            self.table.set_empty_state("Cannot load networks", str(exc))
            self.table.refresh_empty_state(0)
            self._selection_changed()
            return

        self.table.setRowCount(0)
        for profile in self.profiles:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                profile.get("name", ""),
                profile.get("mode", "").upper(),
                profile.get("subnet", ""),
                "on" if profile.get("dhcp") else "off",
                profile.get("bridge", ""),
                _runtime_state(profile),
                str(len(profile.get("port_forwards", []))),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, column, item)
        if not self.profiles:
            self.table.set_empty_state(
                "No networks yet",
                "Create a NAT network for internet access or an isolated "
                "network for lab-only segments.",
            )
        self.table.refresh_empty_state()
        if selected:
            self._select_name(selected)
        else:
            self._selection_changed()

    def _selected_name(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.text() if item is not None else None

    def _selected_profile(self) -> dict[str, Any] | None:
        name = self._selected_name()
        return next(
            (profile for profile in self.profiles if profile["name"] == name),
            None,
        )

    def _select_name(self, name: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == name:
                self.table.selectRow(row)
                return

    def _selection_changed(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.details.setText("Select a network to inspect it.")
            self.remove_button.setEnabled(False)
            self.edit_button.setEnabled(False)
            self.restart_button.setEnabled(False)
            self.reservations_button.setEnabled(False)
            self.add_forward_button.setEnabled(False)
            self.forwards.setRowCount(0)
            self.forwards.set_empty_state(
                "No forward selected", "Select a NAT network."
            )
            self.forwards.refresh_empty_state(0)
            self.remove_forward_button.setEnabled(False)
            return

        self.details.setText(_format_profile(profile))
        self.remove_button.setEnabled(True)
        self.edit_button.setEnabled(True)
        self.restart_button.setEnabled(True)
        self.reservations_button.setEnabled(bool(profile.get("dhcp")))
        self.add_forward_button.setEnabled(profile.get("mode") == "nat")
        self.remove_forward_button.setEnabled(False)

        forwards = profile.get("port_forwards", [])
        self.forwards.setRowCount(0)
        for forward in forwards:
            row = self.forwards.rowCount()
            self.forwards.insertRow(row)
            values = [
                forward.get("protocol", "").upper(),
                str(forward.get("host_port", "")),
                f"{forward.get('guest_ip', '')}:{forward.get('guest_port', '')}",
                forward.get("description", ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.forwards.setItem(row, column, item)
        if forwards:
            self.forwards.set_empty_state("", "")
        else:
            self.forwards.set_empty_state(
                "No port forwards",
                "Add one to reach a guest service from the host.",
            )
        self.forwards.refresh_empty_state()

    def _forward_selection(self) -> None:
        rows = self.forwards.selectionModel().selectedRows()
        self.remove_forward_button.setEnabled(bool(rows))

    # ------------------------------------------------------------------ actions

    def _run(self, message: str, action) -> Any:
        self.status.emit(message)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            return action()
        except VemuError as exc:
            QMessageBox.critical(self, "Network error", str(exc))
            return None
        finally:
            QApplication.restoreOverrideCursor()

    def _lan_bridge(self) -> None:
        dialog = LanBridgeWizard(self.backend, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.refresh()
        name = (dialog.profile or {}).get("name") or ""
        if dialog.created_host_bridge:
            self.status.emit(
                f"Created host bridge {dialog.bridge_name} and network {name}."
            )
        else:
            self.status.emit(
                f"Registered existing bridge {dialog.bridge_name} as {name}."
            )

    def _add(self) -> None:
        if not hasattr(self.backend, "network_add"):
            self.status.emit("Networks require vemud.")
            return
        dialog = NetworkAddDialog(self.backend, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.profile["name"] if dialog.profile else ""
        result = self._run(
            f"Applying network {name}…", self.backend.network_apply
        )
        self.refresh()
        if name:
            self._select_name(name)
        if result is not None:
            self.status.emit(f"Network created and applied: {name}")

    def _edit(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        if not hasattr(self.backend, "network_update"):
            self.status.emit("Editing networks requires vemud.")
            return
        dialog = NetworkAddDialog(self.backend, self, profile)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.profile["name"] if dialog.profile else profile["name"]
        result = self._run(
            f"Applying network {name}…", self.backend.network_apply
        )
        self.refresh()
        self._select_name(name)
        if result is not None:
            self.status.emit(f"Network updated and applied: {name}")

    def _apply(self) -> None:
        if not hasattr(self.backend, "network_apply"):
            self.status.emit("Networks require vemud.")
            return
        result = self._run(
            "Applying networks to libvirt and nft…",
            self.backend.network_apply,
        )
        self.refresh()
        if result is not None:
            count = len(result.get("networks", []))
            nft = result.get("nft", {})
            libvirt = nft.get("libvirt", {})
            if nft.get("applied"):
                suffix = "port forwards applied"
                if libvirt and not libvirt.get("applied"):
                    suffix += (
                        " · guest firewall: "
                        f"{libvirt.get('reason', 'not applied')}"
                    )
            else:
                suffix = nft.get("reason", "no port-forward changes")
            self.status.emit(f"Applied {count} network(s) · {suffix}")

    def _manage_reservations(self) -> None:
        if not hasattr(self.backend, "network_reservation_add"):
            self.status.emit("DHCP reservations require vemud.")
            return
        profile = self._selected_profile()
        if profile is None:
            return
        dialog = DhcpReservationDialog(self.backend, profile, self)
        dialog.exec()
        self.refresh()
        self._select_name(profile["name"])
        if dialog.changed:
            self.status.emit(
                "DHCP reservations updated · restart the network to apply"
            )

    def _restart(self) -> None:
        if not hasattr(self.backend, "network_restart"):
            self.status.emit("Networks require vemud.")
            return
        profile = self._selected_profile()
        if profile is None:
            return
        answer = QMessageBox.question(
            self,
            "Restart network",
            f"Restart '{profile['name']}'? Shut down attached VMs first; "
            "v-EMU blocks the restart while they are running so their "
            "network is not dropped.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = self._run(
            f"Restarting network {profile['name']}…",
            lambda: self.backend.network_restart(profile["name"]),
        )
        self.refresh()
        self._select_name(profile["name"])
        if result is not None:
            self.status.emit(f"Network restarted: {profile['name']}")

    def _remove(self) -> None:
        if not hasattr(self.backend, "network_remove"):
            self.status.emit("Networks require vemud.")
            return
        profile = self._selected_profile()
        if profile is None:
            return
        answer = QMessageBox.question(
            self,
            "Remove network",
            f"Remove '{profile['name']}' from v-EMU and stop/undefine its "
            "libvirt network? VMs still attached will lose connectivity.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = self._run(
            f"Removing network {profile['name']}…",
            lambda: self.backend.network_remove(profile["name"], purge=True),
        )
        self.refresh()
        if result is not None:
            self.status.emit(f"Network removed: {profile['name']}")

    def _add_forward(self) -> None:
        if not hasattr(self.backend, "network_port_forward_add"):
            self.status.emit("Port forwards require vemud.")
            return
        profile = self._selected_profile()
        if profile is None:
            return
        dialog = PortForwardDialog(self.backend, profile, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.refresh()
        self._select_name(profile["name"])
        self.status.emit(f"Port forward added to {profile['name']}")

    def _remove_forward(self) -> None:
        if not hasattr(self.backend, "network_port_forward_remove"):
            self.status.emit("Port forwards require vemud.")
            return
        profile = self._selected_profile()
        row = self.forwards.currentRow()
        if profile is None or row < 0:
            return
        protocol_item = self.forwards.item(row, 0)
        host_item = self.forwards.item(row, 1)
        if protocol_item is None or host_item is None:
            return
        protocol = protocol_item.text().lower()
        host_port = int(host_item.text())
        result = self._run(
            f"Removing {protocol}/{host_port}…",
            lambda: self.backend.network_port_forward_remove(
                profile["name"], protocol, host_port
            ),
        )
        self.refresh()
        self._select_name(profile["name"])
        if result is not None:
            self.status.emit(
                f"Removed forward {protocol}/{host_port} from {profile['name']}"
            )

    def _build_context_menu(self, profile: dict[str, Any] | None) -> QMenu:
        menu = QMenu(self)
        menu.addAction("Add network…", self._add)
        edit = menu.addAction("Edit network…", self._edit)
        edit.setEnabled(
            profile is not None and hasattr(self.backend, "network_update")
        )
        menu.addAction("Apply all networks", self._apply)
        menu.addSeparator()
        forward = menu.addAction("Add port forward…", self._add_forward)
        forward.setEnabled(
            profile is not None and profile.get("mode") == "nat"
        )
        copy = menu.addAction(
            "Copy name",
            lambda: QGuiApplication.clipboard().setText(
                profile.get("name", "") if profile else ""
            ),
        )
        copy.setEnabled(profile is not None)
        menu.addSeparator()
        reservations = menu.addAction(
            "DHCP reservations…", self._manage_reservations
        )
        reservations.setEnabled(
            profile is not None and bool(profile.get("dhcp"))
        )
        restart = menu.addAction("Restart network…", self._restart)
        restart.setEnabled(profile is not None)
        remove = menu.addAction("Remove network…", self._remove)
        remove.setEnabled(profile is not None)
        return menu

    def _show_context_menu(self, position) -> None:
        row = self.table.rowAt(position.y())
        if row < 0:
            return
        self.table.selectRow(row)
        self._build_context_menu(self._selected_profile()).exec(
            self.table.viewport().mapToGlobal(position)
        )
