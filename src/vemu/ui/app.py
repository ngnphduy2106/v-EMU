"""PySide6 desktop shell for v-EMU (M0 read-only view)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..backend import DomainInfo, LibvirtBackend
from ..client import DaemonClient, socket_available
from ..errors import VemuError
from . import configure_display_environment as _configure_display_environment
from . import configure_webengine_environment as _configure_webengine_environment
from . import strings as s
from .components import PageHeader
from .delegate import NoFocusDelegate
from .dialogs import ConfirmDialog
from .drift import DriftDialog
from .doctor_page import DoctorPage
from .fonts import load_bundled_fonts, ui_font
from .guide import GuidePage
from .help import ContextHelpDialog
from .host_metrics import HostMetrics, format_metrics
from .icons import DEFAULT_SIZE, make_icon
from .labs_page import LabsPage
from .network_map import NetworkMapDialog
from .network_page import NetworkPage
from .onboarding import FirstRunDialog
from .performance import PerformanceTuningDialog
from .notify import notify
from .settings_page import SettingsPage
from .shares_page import SharesPage
from .storage_page import StoragePage
from .templates_page import TemplatesPage
from .theme import ASSETS, DARK, LIGHT, Tokens, stylesheet
from .tokens import TYPE
from .single_instance import SingleInstanceGuard
from .tooltips import (
    attach_tooltip,
    compact_tooltip,
    set_tooltip,
    tip,
)
from .widgets import EmptyStateTable


_STATE_SORT = {
    "running": 0,
    "paused": 1,
    "suspended": 1,
    "stopped": 2,
    "shutdown": 2,
    "crashed": 3,
    "unknown": 4,
}

_OS_LABELS = {
    "windows": "Windows",
    "linux": "Linux",
    "hypervisor": "Hypervisor",
    "generic": "Generic",
}
_OS_SORT = {
    "windows": 0,
    "linux": 1,
    "hypervisor": 2,
    "generic": 3,
}

_NAV_ICONS = {
    "Labs": "labs",
    "Templates": "templates",
    "Virtual machines": "vms",
    "Networks": "network",
    "Storage": "storage",
    "Shared folders": "shares",
    "Guide": "guide",
    "Doctor": "doctor",
    "Settings": "gear",
}

_NAV_SHORTCUTS = {
    "Labs": "Ctrl+1",
    "Templates": "Ctrl+2",
    "Virtual machines": "Ctrl+3",
    "Networks": "Ctrl+4",
    "Storage": "Ctrl+5",
    "Shared folders": "Ctrl+6",
    "Guide": "Ctrl+7",
    "Doctor": "Ctrl+8",
    "Settings": "Ctrl+,",
}


class _SortItem(QTableWidgetItem):
    """Table item with an explicit sort key; the display text is unchanged."""

    def __init__(self, text: str, key: object) -> None:
        super().__init__(text)
        self._key = key

    def __lt__(self, other) -> bool:
        if isinstance(other, _SortItem):
            return self._key < other._key  # type: ignore[operator]
        return super().__lt__(other)


class _BackendTask(QThread):
    """Run a slow backend call off the GUI thread."""

    done = Signal(object, object)

    def __init__(self, operation) -> None:
        super().__init__()
        self._operation = operation

    def run(self) -> None:
        try:
            result = self._operation()
        except Exception as exc:  # noqa: BLE001 - forwarded to the GUI thread
            self.done.emit(None, exc)
        else:
            self.done.emit(result, None)


class DaemonBackend:
    """Read-only domain source backed by vemud."""

    def __init__(self, socket_path: str | None = None) -> None:
        self.client = DaemonClient(socket_path)

    def list_domains(self) -> list[DomainInfo]:
        data = self.client.call("domains.list", {})
        return [
            DomainInfo(
                name=str(item["name"]),
                uuid=str(item["uuid"]),
                state=str(item["state"]),
                disks=[str(path) for path in item.get("disks", [])],
                autostart=bool(item.get("autostart", False)),
                firmware=str(item.get("firmware", "bios")),
                managed_by=str(item.get("managed_by", "External")),
                os_type=str(item.get("os_type", "generic")),
            )
            for item in data
        ]

    def console_info(self, name: str) -> dict:
        return self.client.call("console.info", {"domain": name})

    def screenshot(self, name: str) -> bytes:
        import base64

        payload = self.client.call("domain.screenshot", {"domain": name})
        return base64.b64decode(str(payload.get("data", "")))

    def system_info(self) -> dict:
        return self.client.call("system_info", {})

    def runtime_profile_get(self) -> dict:
        return self.client.call("profile.get", {})

    def runtime_profile_set(self, profile: str) -> dict:
        return self.client.call("profile.set", {"profile": profile})

    def doctor(self) -> dict:
        return self.client.call("doctor.run", {})

    def domain_baseline_refresh(self, name: str) -> dict:
        return self.client.call(
            "domain.baseline_refresh", {"domain": name}
        )

    def domain_drift(self, name: str) -> dict:
        return self.client.call("domain.drift", {"domain": name})

    def performance_probe(self, name: str) -> dict:
        return self.client.call("performance.probe", {"domain": name})

    def performance_apply(self, name: str) -> dict:
        return self.client.call("performance.apply", {"domain": name})

    def lab_status(self, path: str) -> dict:
        return self.client.call("lab.status", {"path": path})

    def lab_up(self, path: str, start: bool = True) -> dict:
        return self.client.call(
            "lab.up", {"path": path, "start": start}
        )

    def lab_down(self, path: str, force: bool = False) -> dict:
        return self.client.call(
            "lab.down", {"path": path, "force": force}
        )

    def lab_export(
        self, path: str, dest: str, with_templates: bool = False
    ) -> dict:
        return self.client.call(
            "lab.export",
            {
                "path": path,
                "dest": dest,
                "with_templates": with_templates,
            },
        )

    def lab_import(
        self,
        bundle: str,
        dest: str,
        templates_root: str | None = None,
        overwrite: bool = False,
    ) -> dict:
        return self.client.call(
            "lab.import",
            {
                "bundle": bundle,
                "dest": dest,
                "templates_root": templates_root,
                "overwrite": overwrite,
            },
        )

    def lab_reset(
        self,
        path: str,
        vms: list[str],
        force: bool = False,
        start: bool = True,
    ) -> dict:
        return self.client.call(
            "lab.reset",
            {
                "path": path,
                "vms": vms,
                "force": force,
                "start": start,
            },
        )

    def lab_destroy(
        self,
        path: str,
        force: bool = False,
        keep_networks: bool = False,
    ) -> dict:
        return self.client.call(
            "lab.destroy",
            {
                "path": path,
                "force": force,
                "keep_networks": keep_networks,
            },
        )

    def start(self, name: str) -> dict:
        return self.client.call("domain.start", {"domain": name})

    def resume(self, name: str) -> None:
        self.client.call("domain.resume", {"domain": name})

    def set_autostart(self, name: str, enabled: bool) -> dict:
        return self.client.call(
            "domain.autostart", {"domain": name, "enabled": enabled}
        )

    def delete_vm(
        self,
        name: str,
        *,
        delete_storage: bool = False,
        force: bool = False,
    ) -> dict:
        return self.client.call(
            "domain.delete",
            {
                "domain": name,
                "delete_storage": delete_storage,
                "force": force,
            },
        )

    def shutdown(
        self,
        name: str,
        timeout: int = 90,
        force: bool = False,
        *,
        wait: bool = True,
    ) -> None:
        self.client.call(
            "domain.shutdown",
            {"domain": name, "force": force, "wait": wait},
        )

    def destroy(self, name: str) -> None:
        self.client.call("domain.force_off", {"domain": name})

    def restart(self, name: str) -> None:
        self.client.call("domain.restart", {"domain": name})

    def snapshots_info(self, name: str) -> dict:
        return self.client.call("snapshots.info", {"domain": name})

    def snapshots_create(
        self, name: str, label: str, description: str = ""
    ) -> dict:
        return self.client.call(
            "snapshots.create",
            {
                "domain": name,
                "label": label,
                "description": description,
            },
        )

    def snapshots_update(
        self,
        name: str,
        layer: str,
        *,
        label: str | None = None,
        description: str | None = None,
    ) -> dict:
        return self.client.call(
            "snapshots.update",
            {
                "domain": name,
                "layer": layer,
                "label": label,
                "description": description,
            },
        )

    def snapshots_revert(self, name: str, layer: str) -> dict:
        return self.client.call(
            "snapshots.revert", {"domain": name, "layer": layer}
        )

    def snapshots_delete(self, name: str, layer: str, force: bool = False) -> dict:
        return self.client.call(
            "snapshots.delete",
            {"domain": name, "layer": layer, "force": force},
        )

    def storage_list(self) -> list[dict]:
        return self.client.call("storage.list", {})

    def storage_registry(self) -> list[dict]:
        return self.client.call("storage.registry", {})

    def storage_inspect(self, path: str, role: str = "labs") -> dict:
        return self.client.call(
            "storage.inspect", {"path": path, "role": role}
        )

    def storage_add(
        self, path: str, role: str = "labs", dry_run: bool = False
    ) -> dict:
        return self.client.call(
            "storage.add",
            {"path": path, "role": role, "dry_run": dry_run},
        )

    def storage_repair(
        self,
        path: str,
        role: str = "labs",
        dry_run: bool = False,
        force_selinux: bool = False,
    ) -> dict:
        return self.client.call(
            "storage.repair",
            {
                "path": path,
                "role": role,
                "dry_run": dry_run,
                "force_selinux": force_selinux,
            },
        )

    def storage_remove(self, path: str) -> dict:
        return self.client.call("storage.remove", {"path": path})

    def storage_files(self, path: str) -> dict:
        return self.client.call("storage.files", {"path": path})

    def storage_delete_file(self, path: str) -> dict:
        return self.client.call("storage.delete_file", {"path": path})

    def storage_import_file(self, source: str, location: str) -> dict:
        return self.client.call(
            "storage.import_file", {"source": source, "location": location}
        )

    def vm_import_plan(self, **params) -> dict:
        return self.client.call("vm.import_plan", params)

    def vm_import(self, **params) -> dict:
        return self.client.call("vm.import", params)

    def vm_create(self, **params) -> dict:
        return self.client.call("vm.create", params)

    def seal(self, **params) -> dict:
        return self.client.call("seal", params)

    def networks_list(self) -> list[dict]:
        return self.client.call("networks.list", {})

    def network_topology(self) -> dict:
        return self.client.call("network.topology", {})

    def domain_interfaces(self, name: str) -> list[dict]:
        return self.client.call("domain.interfaces", {"domain": name})

    def domain_stats(self, name: str) -> dict:
        return self.client.call("domain.stats", {"domain": name})

    def vm_config_info(self, name: str) -> dict:
        return self.client.call("vm.config_info", {"domain": name})

    def vm_clipboard_agent_start(self, domain: str) -> dict:
        return self.client.call(
            "vm.clipboard_agent_start", {"domain": domain}
        )

    def vm_clipboard_get(self, domain: str) -> dict:
        return self.client.call("vm.clipboard_get", {"domain": domain})

    def vm_clipboard_set(self, domain: str, text: str) -> dict:
        return self.client.call(
            "vm.clipboard_set", {"domain": domain, "text": text}
        )

    def vm_media_list(self, name: str) -> dict:
        return self.client.call("vm.media_list", {"domain": name})

    def vm_media_change(
        self,
        name: str,
        path: str,
        target: str | None = None,
        force: bool = False,
    ) -> dict:
        return self.client.call(
            "vm.media_change",
            {
                "domain": name,
                "path": path,
                "target": target,
                "force": force,
            },
        )

    def vm_media_eject(
        self, name: str, target: str | None = None, force: bool = False
    ) -> dict:
        return self.client.call(
            "vm.media_eject",
            {"domain": name, "target": target, "force": force},
        )

    def vm_capabilities(
        self, machine: str | None = None, arch: str = "x86_64"
    ) -> dict:
        return self.client.call(
            "vm.capabilities", {"machine": machine, "arch": arch}
        )

    def vm_shares(self, name: str) -> dict:
        return self.client.call("vm.shares", {"domain": name})

    def vm_share_add(
        self,
        name: str,
        tag: str,
        source: str,
        read_only: bool = False,
    ) -> dict:
        return self.client.call(
            "vm.share_add",
            {
                "domain": name,
                "tag": tag,
                "source": source,
                "read_only": read_only,
            },
        )

    def vm_share_remove(self, name: str, tag: str) -> dict:
        return self.client.call(
            "vm.share_remove", {"domain": name, "tag": tag}
        )

    def shares_list(self) -> list[dict]:
        return self.client.call("shares.list", {})

    def share_catalog_add(self, **params) -> dict:
        return self.client.call("shares.add", params)

    def share_catalog_update(self, **params) -> dict:
        return self.client.call("shares.update", params)

    def share_catalog_remove(self, name: str) -> dict:
        return self.client.call("shares.remove", {"name": name})

    def vm_share_attach(self, **params) -> dict:
        return self.client.call("vm.share_attach", params)

    def vm_share_detach(self, name: str, share: str) -> dict:
        return self.client.call(
            "vm.share_detach", {"domain": name, "share": share}
        )

    def vm_share_options(self, **params) -> dict:
        return self.client.call("vm.share_options", params)

    def vm_share_status(self, name: str) -> dict:
        return self.client.call("vm.share_status", {"domain": name})

    def vm_share_mount(self, name: str, tag: str | None = None) -> dict:
        return self.client.call(
            "vm.share_mount", {"domain": name, "tag": tag}
        )

    def vm_share_unmount(self, name: str, tag: str) -> dict:
        return self.client.call(
            "vm.share_unmount", {"domain": name, "tag": tag}
        )

    def vm_rename_plan(
        self, domain: str, new_name: str, *, rename_disks: bool = True
    ) -> dict:
        return self.client.call(
            "vm.rename_plan",
            {
                "domain": domain,
                "new_name": new_name,
                "rename_disks": rename_disks,
            },
        )

    def vm_rename(
        self,
        domain: str,
        new_name: str,
        *,
        rename_disks: bool = True,
    ) -> dict:
        return self.client.call(
            "vm.rename",
            {
                "domain": domain,
                "new_name": new_name,
                "rename_disks": rename_disks,
            },
        )

    def rescue_start(self, domain: str) -> dict:
        return self.client.call(
            "vm.rescue_start", {"domain": domain}
        )

    def rescue_shutdown(self, domain: str, *, wait: bool = False) -> dict:
        return self.client.call(
            "vm.rescue_shutdown", {"domain": domain, "wait": wait}
        )

    def adopt_domain(self, domain: str, mode: str = "partial") -> dict:
        return self.client.call(
            "vm.adopt", {"domain": domain, "mode": mode}
        )

    def vm_migration_plan(
        self,
        domain: str,
        *,
        new_firmware: bool = False,
        import_snapshots: bool = False,
    ) -> dict:
        return self.client.call(
            "vm.migration_plan",
            {
                "domain": domain,
                "new_firmware": new_firmware,
                "import_snapshots": import_snapshots,
            },
        )

    def vm_migrate_full(
        self,
        domain: str,
        *,
        accept_snapshot_loss: bool = False,
        import_snapshots: bool = False,
        start: bool = False,
        new_firmware: bool = False,
    ) -> dict:
        return self.client.call(
            "vm.migrate_full",
            {
                "domain": domain,
                "accept_snapshot_loss": accept_snapshot_loss,
                "import_snapshots": import_snapshots,
                "start": start,
                "new_firmware": new_firmware,
            },
        )

    def vm_serial_add(self, domain: str) -> dict:
        return self.client.call("vm.serial_add", {"domain": domain})

    def vm_config_update(self, **params) -> dict:
        return self.client.call("vm.config_update", params)

    def vm_disk_add(self, **params) -> dict:
        return self.client.call("vm.disk_add", params)

    def vm_disk_remove(self, **params) -> dict:
        return self.client.call("vm.disk_remove", params)

    def vm_disk_resize(self, **params) -> dict:
        return self.client.call("vm.disk_resize", params)

    def vm_tpm_add(self, **params) -> dict:
        return self.client.call("vm.tpm_add", params)

    def vm_tpm_remove(self, **params) -> dict:
        return self.client.call("vm.tpm_remove", params)

    def vm_usb_add(self, **params) -> dict:
        return self.client.call("vm.usb_add", params)

    def usb_devices(self, domain: str | None = None) -> list[dict]:
        return self.client.call("usb.list", {"domain": domain})

    def vm_usb_attach(
        self, domain: str, vendor: str, product: str
    ) -> dict:
        return self.client.call(
            "vm.usb_attach",
            {"domain": domain, "vendor": vendor, "product": product},
        )

    def vm_usb_detach(
        self, domain: str, vendor: str, product: str
    ) -> dict:
        return self.client.call(
            "vm.usb_detach",
            {"domain": domain, "vendor": vendor, "product": product},
        )

    def vm_boot_menu(self, domain: str, timeout_ms: int = 5000) -> dict:
        return self.client.call(
            "vm.boot_menu", {"domain": domain, "timeout_ms": timeout_ms}
        )

    def vm_boot_firmware(self, domain: str) -> dict:
        return self.client.call("vm.boot_firmware", {"domain": domain})

    def vm_promote_boot(self, domain: str | None = None) -> dict:
        return self.client.call("vm.promote_boot", {"domain": domain})

    def vm_audio_add(self, **params) -> dict:
        return self.client.call("vm.audio_add", params)

    def vm_audio_remove(self, **params) -> dict:
        return self.client.call("vm.audio_remove", params)

    def network_add(
        self,
        name: str,
        mode: str,
        subnet: str,
        dhcp: bool,
        dns_domain: str,
        description: str = "",
        bridge: str = "",
    ) -> dict:
        return self.client.call(
            "network.add",
            {
                "name": name,
                "mode": mode,
                "subnet": subnet,
                "dhcp": dhcp,
                "dns_domain": dns_domain,
                "description": description,
                "bridge": bridge,
            },
        )

    def network_bridge_inventory(self) -> dict:
        return self.client.call("network.bridge_inventory", {})

    def network_bridge_create(
        self,
        name: str,
        bridge: str,
        uplink: str,
        addressing: str = "dhcp",
        address: str = "",
        gateway: str = "",
        dns: str = "",
        description: str = "",
    ) -> dict:
        return self.client.call(
            "network.bridge_create",
            {
                "name": name,
                "bridge": bridge,
                "uplink": uplink,
                "addressing": addressing,
                "address": address,
                "gateway": gateway,
                "dns": dns,
                "description": description,
            },
        )

    def network_apply(self, dry_run: bool = False) -> dict:
        return self.client.call("network.apply", {"dry_run": dry_run})

    def network_update(
        self,
        name: str,
        mode: str,
        subnet: str,
        dhcp: bool,
        dns_domain: str,
        description: str = "",
    ) -> dict:
        return self.client.call(
            "network.update",
            {
                "name": name,
                "mode": mode,
                "subnet": subnet,
                "dhcp": dhcp,
                "dns_domain": dns_domain,
                "description": description,
            },
        )

    def network_restart(self, name: str) -> dict:
        return self.client.call("network.restart", {"name": name})

    def network_reservation_add(
        self, name: str, mac: str, ip: str, hostname: str = ""
    ) -> dict:
        return self.client.call(
            "network.reservation_add",
            {
                "name": name,
                "mac": mac,
                "ip": ip,
                "hostname": hostname,
            },
        )

    def network_reservation_remove(self, name: str, mac: str) -> dict:
        return self.client.call(
            "network.reservation_remove", {"name": name, "mac": mac}
        )

    def network_remove(self, name: str, purge: bool = False) -> dict:
        return self.client.call(
            "network.remove", {"name": name, "purge": purge}
        )

    def network_port_forward_add(
        self,
        name: str,
        protocol: str,
        host_port: int,
        guest_ip: str,
        guest_port: int,
        description: str = "",
    ) -> dict:
        return self.client.call(
            "network.port_forward_add",
            {
                "name": name,
                "protocol": protocol,
                "host_port": host_port,
                "guest_ip": guest_ip,
                "guest_port": guest_port,
                "description": description,
            },
        )

    def network_port_forward_remove(
        self, name: str, protocol: str, host_port: int
    ) -> dict:
        return self.client.call(
            "network.port_forward_remove",
            {
                "name": name,
                "protocol": protocol,
                "host_port": host_port,
            },
        )

    def network_attach(
        self, domain: str, network: str, model: str = "virtio"
    ) -> dict:
        return self.client.call(
            "network.attach",
            {"domain": domain, "network": network, "model": model},
        )

    def network_detach(self, domain: str, mac: str) -> dict:
        return self.client.call(
            "network.detach", {"domain": domain, "mac": mac}
        )

    def vm_nic_link(self, domain: str, mac: str, up: bool) -> dict:
        return self.client.call(
            "vm.nic_link", {"domain": domain, "mac": mac, "up": up}
        )

    def vm_nic_network(
        self, domain: str, mac: str, network: str
    ) -> dict:
        return self.client.call(
            "vm.nic_network",
            {"domain": domain, "mac": mac, "network": network},
        )

    def template_import(self, **params) -> dict:
        return self.client.call("template.import", params)

    def template_update(self, **params) -> dict:
        return self.client.call("template.update", params)

    def template_delete(self, template: str) -> dict:
        return self.client.call(
            "template.delete", {"template": template}
        )

    def template_export(self, **params) -> dict:
        return self.client.call("template.export", params)

    def templates_list(self, root: str) -> list[dict]:
        return self.client.call("templates.list", {"root": root})

    def clone_create(self, **params) -> dict:
        return self.client.call("clone.create", params)

    def close(self) -> None:
        return None


class MainWindow(QMainWindow):
    def __init__(
        self,
        backend: LibvirtBackend,
        *,
        dark: bool = True,
        uri: str = "qemu:///system",
    ) -> None:
        super().__init__()
        self.backend = backend
        self.uri = uri
        self.dark = dark
        self.domains: list[DomainInfo] = []
        self.selected_domain: DomainInfo | None = None
        self._load_error: str | None = None
        self.console_windows: dict[str, object] = {}
        self._boot_task = None
        self.mini_console = None
        self._shutting_down = False
        self._nav_buttons: list[QPushButton] = []
        self._icon_buttons: dict[QPushButton, str] = {}
        self._mini_timer = QTimer(self)
        self._mini_timer.setSingleShot(True)
        self._mini_timer.setInterval(250)
        self._mini_timer.timeout.connect(self._refresh_mini_console_now)
        self._host_metrics = HostMetrics()
        self._metrics_timer = QTimer(self)
        self._metrics_timer.setInterval(2000)
        self._metrics_timer.timeout.connect(self._refresh_metrics)
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(2000)
        self._stats_timer.timeout.connect(self._refresh_guest_stats)
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.timeout.connect(self._auto_refresh)
        self._auto_refresh_busy = False
        self._guest_sample: tuple[str, int, float] | None = None
        self._last_boot_promote = 0.0
        self._first_run_checked = False
        self._labs_ready = True
        self._has_nat = True

        self.setWindowTitle(s.APP_NAME)
        self.resize(1280, 720)
        self.setMinimumSize(1280, 720)

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_top_bar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.sidebar = self._build_sidebar()
        body.addWidget(self.sidebar)
        body.addWidget(self._build_content(), 1)
        self.inspector = self._build_inspector()
        body.addWidget(self.inspector)
        root_layout.addLayout(body, 1)

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.status_daemon = QLabel("vemud")
        self.status_daemon.setObjectName("Muted")
        self.status_daemon.setToolTip("Backend connection")
        self.status_host = QLabel("KVM")
        self.status_host.setObjectName("Muted")
        self.status_host.setToolTip("Local hypervisor")
        self.status_counts = QLabel("0 running · 0 stopped")
        self.status_counts.setObjectName("Muted")
        self.status_metrics = QLabel("CPU — · RAM — · SWAP — · NET —")
        self.status_metrics.setObjectName("Muted")
        self.status_metrics.setToolTip("Host load sampled from /proc")
        self.statusBar().addPermanentWidget(self.status_metrics)
        self.statusBar().addPermanentWidget(self.status_daemon)
        self.statusBar().addPermanentWidget(self.status_host)
        self.statusBar().addPermanentWidget(self.status_counts)
        self.statusBar().showMessage("Ready")

        self._settings = QSettings("v-EMU", "v-EMU")
        self.ui_sidebar_visible = self._settings.value(
            "ui/sidebar", True, type=bool
        )
        self.ui_inspector_visible = self._settings.value(
            "ui/inspector", True, type=bool
        )
        self.mini_console_enabled = self._settings.value(
            "ui/mini_console", True, type=bool
        )
        if os.environ.get("VEMU_DISABLE_MINI_CONSOLE") == "1":
            self.mini_console_enabled = False
        self.auto_refresh_enabled = self._settings.value(
            "ui/auto_refresh", True, type=bool
        )
        self.auto_refresh_interval = max(
            30,
            self._settings.value(
                "ui/auto_refresh_interval", 30, type=int
            ),
        )
        self._install_shortcuts()
        self._apply_pane_state()

        self._apply_theme(self.dark)
        self.refresh()
        self._apply_auto_refresh()
        self._refresh_metrics()
        self._metrics_timer.start()
        self._stats_timer.start()

    # ------------------------------------------------------------------ layout

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(56)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 8, 24, 8)
        layout.setSpacing(12)

        self.sidebar_button = self._icon_button(
            "menu",
            tip("Toggle sidebar", "Ctrl+B"),
            self._toggle_sidebar,
        )
        layout.addWidget(self.sidebar_button)

        brand_icon = QLabel()
        brand_icon.setObjectName("BrandIcon")
        brand_icon.setFixedSize(20, 20)
        brand_icon.setScaledContents(True)
        icon = QIcon(str(ASSETS / "org.vemu.VEMU.svg"))
        if not icon.isNull():
            brand_icon.setPixmap(icon.pixmap(20, 20))
        layout.addWidget(brand_icon)

        brand = QLabel(s.APP_NAME)
        brand.setObjectName("Brand")
        layout.addWidget(brand)

        self.tagline_label = QLabel(s.APP_TAGLINE)
        self.tagline_label.setObjectName("BrandTagline")
        layout.addWidget(self.tagline_label)

        layout.addStretch(1)

        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText(s.SEARCH_PLACEHOLDER)
        self.search.setFixedWidth(300)
        self.search.setFixedHeight(30)
        self.search.textChanged.connect(self._filter_rows)
        layout.addWidget(self.search)

        self.theme_button = self._icon_button(
            "theme",
            tip("Theme", "Switch between light and dark"),
            self._toggle_theme,
        )
        layout.addWidget(self.theme_button)

        self.topology_button = self._icon_button(
            "topology",
            tip("Network map", "Ctrl+Shift+M · Host → networks → VMs"),
            self._open_network_map,
        )
        layout.addWidget(self.topology_button)

        self.cli_button = self._icon_button(
            "terminal",
            tip("CLI console", "Ctrl+Shift+K · Run v-EMU commands"),
            self._open_cli_console,
        )
        layout.addWidget(self.cli_button)

        self.inspector_button = self._icon_button(
            "panel",
            tip("Toggle inspector", "Ctrl+I"),
            self._toggle_inspector,
        )
        layout.addWidget(self.inspector_button)

        self.help_button = self._icon_button(
            "help",
            tip("Help for this page", "F1"),
            self._show_context_help,
        )
        layout.addWidget(self.help_button)

        settings = self._icon_button(
            "gear",
            tip(s.NAV_SETTINGS, "Preferences and connection"),
            self._open_settings,
        )
        layout.addWidget(settings)
        return bar

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(220)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(10, 14, 10, 14)
        layout.setSpacing(2)

        layout.addWidget(self._section_label("WORKBENCH"))
        for label, coming in (
            (s.NAV_LABS, False),
            (s.NAV_TEMPLATES, False),
            (s.NAV_VMS, False),
            (s.NAV_NETWORKS, False),
            (s.NAV_STORAGE, False),
            (s.NAV_SHARES, False),
        ):
            button = self._nav_button(
                label,
                icon=_NAV_ICONS.get(label),
                selected=(label == s.NAV_VMS),
            )
            button.setToolTip(f"{label} ({_NAV_SHORTCUTS[label]})")
            if coming:
                button.setToolTip(f"{label} view is coming later.")
            layout.addWidget(button)

        layout.addStretch(1)
        layout.addWidget(self._section_label("PREFERENCES"))
        for label, coming in (
            (s.NAV_GUIDE, False),
            (s.NAV_DOCTOR, False),
            (s.NAV_SETTINGS, False),
        ):
            button = self._nav_button(label, icon=_NAV_ICONS.get(label))
            button.setToolTip(f"{label} ({_NAV_SHORTCUTS[label]})")
            if coming:
                button.setToolTip(f"{label} is coming later.")
            layout.addWidget(button)
        return sidebar

    def _build_vms_page(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        page_header = PageHeader(s.PAGE_VMS_TITLE, s.PAGE_VMS_SUBTITLE)
        self.vms_subtitle = page_header.subtitle
        header.addWidget(page_header)
        header.addStretch(1)

        self.new_vm_button = QPushButton("New VM")
        self.new_vm_button.setObjectName("PrimaryButton")
        self.new_vm_button.setToolTip(
            "Create a VM from an install ISO (Windows unattended supported). "
            "Ctrl+N"
        )
        self.new_vm_button.setEnabled(hasattr(self.backend, "vm_create"))
        self.new_vm_button.clicked.connect(self._new_vm)
        header.addWidget(self.new_vm_button)

        seal = QPushButton(s.ACTION_SEAL)
        seal.setObjectName("SecondaryButton")
        seal.setToolTip("Seal a VM as a template (stopped, or Sysprep first).")
        seal.setEnabled(False)
        seal.clicked.connect(self._seal_selected)
        self.seal_button = seal
        header.addWidget(seal)

        clone = QPushButton(s.ACTION_CLONE)
        clone.setObjectName("SecondaryButton")
        clone.setToolTip(
            "Clone a sealed template. Opens the Templates page."
        )
        clone.clicked.connect(self._open_templates)
        self.clone_button = clone
        header.addWidget(clone)

        self.import_button = QPushButton("Import…")
        self.import_button.setObjectName("SecondaryButton")
        set_tooltip(
            self.import_button,
            "Import an OVA or qcow2/raw disk. Presets are starting points; "
            "check vendor documentation.",
        )
        import_menu = QMenu(self.import_button)
        import_menu.addAction(
            "OVA appliance…", lambda: self._import_vm("ova")
        )
        import_menu.addAction(
            "Disk image…", lambda: self._import_vm("disk")
        )
        self.import_button.setMenu(import_menu)
        self.import_button.setEnabled(
            hasattr(self.backend, "vm_import_plan")
        )
        header.addWidget(self.import_button)

        refresh = QPushButton(s.ACTION_REFRESH)
        refresh.setObjectName("SecondaryButton")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        layout.addLayout(header)

        layout.addLayout(self._build_kpis())

        self.setup_banner = QFrame()
        self.setup_banner.setObjectName("SetupBanner")
        banner_layout = QHBoxLayout(self.setup_banner)
        banner_layout.setContentsMargins(12, 8, 12, 8)
        banner_layout.setSpacing(8)
        self.setup_banner_label = QLabel()
        self.setup_banner_label.setWordWrap(True)
        banner_layout.addWidget(self.setup_banner_label, 1)
        finish_setup = QPushButton("Finish setup")
        finish_setup.setObjectName("SecondaryButton")
        finish_setup.clicked.connect(self._open_first_run)
        banner_layout.addWidget(finish_setup)
        open_storage = QPushButton("Storage")
        open_storage.setObjectName("SecondaryButton")
        open_storage.clicked.connect(
            lambda: self._open_nav(s.NAV_STORAGE)
        )
        banner_layout.addWidget(open_storage)
        open_networks = QPushButton("Networks")
        open_networks.setObjectName("SecondaryButton")
        open_networks.clicked.connect(
            lambda: self._open_nav(s.NAV_NETWORKS)
        )
        banner_layout.addWidget(open_networks)
        dismiss_setup = QPushButton("Dismiss")
        dismiss_setup.setObjectName("SecondaryButton")
        dismiss_setup.clicked.connect(self._dismiss_setup_banner)
        banner_layout.addWidget(dismiss_setup)
        self.setup_banner.hide()
        layout.addWidget(self.setup_banner)

        self.table = EmptyStateTable(0, 6)
        self.table.setHorizontalHeaderLabels(
            [
                s.TABLE_NAME.upper(),
                s.TABLE_OS.upper(),
                s.TABLE_STATE.upper(),
                s.TABLE_MANAGED.upper(),
                s.TABLE_AUTOSTART.upper(),
                s.TABLE_DISKS.upper(),
            ]
        )
        for column, alignment in enumerate(
            (
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
            )
        ):
            header_item = self.table.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setTextAlignment(
                    alignment | Qt.AlignmentFlag.AlignVCenter
                )
        self.table.setWordWrap(False)
        self.table.setItemDelegate(NoFocusDelegate(self.table))
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.horizontalHeader().setFixedHeight(32)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for column, width in ((1, 130), (2, 110), (3, 130),
                              (4, 90), (5, 60)):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Interactive
            )
            self.table.horizontalHeader().resizeSection(column, width)
        self.table.horizontalHeader().setMinimumSectionSize(60)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().setSortIndicator(
            0, Qt.SortOrder.AscendingOrder
        )
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.table, 1)

        return content

    def _build_content(self) -> QWidget:
        self.stack = QStackedWidget()
        self.vms_page = self._build_vms_page()
        self.labs_page = LabsPage(self.backend, self)
        self.labs_page.status.connect(
            lambda message: self.statusBar().showMessage(message, 4000)
        )
        self.labs_page.lab_changed.connect(self.refresh)
        self.templates_page = TemplatesPage(self.backend, self)
        self.templates_page.status.connect(
            lambda message: self.statusBar().showMessage(message, 4000)
        )
        self.templates_page.vms_changed.connect(self.refresh)
        self.guide_page = GuidePage(self, dark=self.dark)
        self.doctor_page = DoctorPage(self.backend, self)
        self.doctor_page.status.connect(
            lambda message: self.statusBar().showMessage(message, 4000)
        )
        self.settings_page = SettingsPage(
            self.backend, dark=self.dark, parent=self
        )
        self.settings_page.theme_changed.connect(self._set_theme)
        self.settings_page.sidebar_changed.connect(self._set_sidebar_visible)
        self.settings_page.inspector_changed.connect(
            self._set_inspector_visible
        )
        self.settings_page.mini_console_changed.connect(
            self._set_mini_console_enabled
        )
        self.settings_page.auto_refresh_changed.connect(
            self._set_auto_refresh
        )
        self.network_page = NetworkPage(self.backend, self)
        self.network_page.status.connect(
            lambda message: self.statusBar().showMessage(message, 4000)
        )
        self.storage_page = StoragePage(self.backend, self)
        self.storage_page.status.connect(
            lambda message: self.statusBar().showMessage(message, 4000)
        )
        self.storage_page.status.connect(
            lambda _message: self._refresh_storage_ready()
        )
        self.shares_page = SharesPage(self.backend, self)
        self.shares_page.status.connect(
            lambda message: self.statusBar().showMessage(message, 4000)
        )
        self.stack.addWidget(self.vms_page)
        self.stack.addWidget(self.labs_page)
        self.stack.addWidget(self.templates_page)
        self.stack.addWidget(self.network_page)
        self.stack.addWidget(self.storage_page)
        self.stack.addWidget(self.shares_page)
        self.stack.addWidget(self.guide_page)
        self.stack.addWidget(self.doctor_page)
        self.stack.addWidget(self.settings_page)
        return self.stack

    def _build_kpis(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        total_card, self.kpi_total = self._kpi_card(s.KPI_TOTAL, "0")
        running_card, self.kpi_running = self._kpi_card(s.KPI_RUNNING, "0")
        stopped_card, self.kpi_stopped = self._kpi_card(s.KPI_STOPPED, "0")
        disks_card, self.kpi_disks = self._kpi_card(s.KPI_DISKS, "0")
        for card in (total_card, running_card, stopped_card, disks_card):
            row.addWidget(card, 1)
        return row

    def _build_inspector(self) -> QWidget:
        inspector = QFrame()
        inspector.setObjectName("Inspector")
        inspector.setFixedWidth(256)
        layout = QVBoxLayout(inspector)
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(10)

        title = QLabel(s.INSPECTOR_TITLE)
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        self.inspector_body = QLabel(s.INSPECTOR_SELECT)
        self.inspector_body.setObjectName("Muted")
        self.inspector_body.setWordWrap(True)
        self.inspector_body.setTextFormat(Qt.TextFormat.RichText)
        self.inspector_body.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self.inspector_body.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        layout.addWidget(self.inspector_body)

        self.inspector_stats = QLabel()
        self.inspector_stats.setObjectName("Muted")
        self.inspector_stats.setWordWrap(True)
        self.inspector_stats.hide()
        layout.addWidget(self.inspector_stats)

        self.mini_anchor = QWidget()
        self.mini_layout = QVBoxLayout(self.mini_anchor)
        self.mini_layout.setContentsMargins(0, 0, 0, 0)
        self.mini_layout.setSpacing(0)
        layout.addWidget(self.mini_anchor)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addStretch(1)
        self.power_button = self._action_button(
            "play",
            tip("Start", "Boot this VM"),
            self._toggle_power,
        )
        actions.addWidget(self.power_button)
        self.force_button = self._action_button(
            "force",
            tip("Force off", "Power off immediately without guest shutdown"),
            self._force_off_selected,
        )
        self.force_button.setProperty("danger", True)
        actions.addWidget(self.force_button)
        self.snapshots_button = self._action_button(
            "snapshot",
            tip("Snapshots", "Create, revert or delete chain snapshots"),
            self._open_snapshots,
        )
        actions.addWidget(self.snapshots_button)
        self.networks_button = self._action_button(
            "network",
            tip("Networks", "Attach or detach VM network interfaces"),
            self._open_vm_networks,
        )
        actions.addWidget(self.networks_button)
        self.configure_button = self._action_button(
            "hardware",
            tip("Hardware", "Memory, CPU, disks, firmware and devices"),
            self._open_vm_config,
        )
        actions.addWidget(self.configure_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)
        self._inspector_layout = layout
        return inspector

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        selected_name = (
            self.selected_domain.name if self.selected_domain is not None else None
        )
        scroll = self.table.verticalScrollBar().value()
        try:
            self.domains = self.backend.list_domains()
            self._load_error = None
        except VemuError as exc:
            self.domains = []
            self._load_error = str(exc)
        error = self._load_error

        header = self.table.horizontalHeader()
        sort_column = header.sortIndicatorSection()
        sort_order = header.sortIndicatorOrder()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.selected_domain = None
        self._update_actions()
        for domain in self.domains:
            row = self.table.rowCount()
            self.table.insertRow(row)
            name_key = (domain.name.lower(), domain.name.lower())
            os_type = (
                domain.os_type
                if domain.os_type in _OS_LABELS
                else "generic"
            )
            os_key = (_OS_SORT[os_type], domain.name.lower())
            state_key = (
                _STATE_SORT.get(domain.state.lower(), 9),
                domain.name.lower(),
            )
            autostart_key = (
                0 if domain.autostart else 1,
                domain.name.lower(),
            )
            disks_key = (len(domain.disks), domain.name.lower())
            managed_key = (domain.managed_by.lower(), domain.name.lower())
            name_item = _SortItem(domain.name, name_key)
            os_item = _SortItem(_OS_LABELS[os_type], os_key)
            os_tokens = self._tokens()
            os_item.setIcon(
                make_icon(
                    f"os-{os_type}",
                    os_tokens.text if self.dark else os_tokens.muted,
                    os_tokens.text,
                )
            )
            os_item.setToolTip("Guest OS type")
            state_item = _SortItem(domain.state.capitalize(), state_key)
            managed_text = domain.managed_by
            if domain.drift == "drifted":
                managed_text += " · drift"
            managed_item = _SortItem(managed_text, managed_key)
            autostart_item = _SortItem(
                "On" if domain.autostart else "Off", autostart_key
            )
            disks_item = _SortItem(str(len(domain.disks)), disks_key)
            for item in (name_item, os_item, state_item, managed_item,
                         autostart_item, disks_item):
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
            if domain.state == "running":
                state_item.setForeground(QColor(LIGHT.success if not self.dark else DARK.success))
            else:
                state_item.setForeground(QColor(LIGHT.muted if not self.dark else DARK.muted))
            if not domain.autostart:
                autostart_item.setForeground(
                    QColor(LIGHT.muted if not self.dark else DARK.muted)
                )
            if domain.drift == "drifted":
                managed_item.setToolTip(
                    compact_tooltip(
                        "Domain XML changed outside v-EMU; baseline drift "
                        "detected."
                    )
                )
                managed_item.setForeground(
                    QColor(
                        LIGHT.warning if not self.dark else DARK.warning
                    )
                )
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, os_item)
            self.table.setItem(row, 2, state_item)
            self.table.setItem(row, 3, managed_item)
            self.table.setItem(row, 4, autostart_item)
            self.table.setItem(row, 5, disks_item)

        if sort_column < 0:
            sort_column = 0
            sort_order = Qt.SortOrder.AscendingOrder
        self.table.setSortingEnabled(True)
        self.table.sortItems(sort_column, sort_order)

        running = sum(1 for domain in self.domains if domain.state == "running")
        stopped = len(self.domains) - running
        disks = sum(len(domain.disks) for domain in self.domains)
        self._set_kpi(self.kpi_total, str(len(self.domains)))
        self._set_kpi(self.kpi_running, str(running))
        self._set_kpi(self.kpi_stopped, str(stopped))
        self._set_kpi(self.kpi_disks, str(disks))
        self.vms_subtitle.setText(
            f"{len(self.domains)} virtual machines · {running} running"
        )
        self.status_daemon.setText(
            "vemud" if hasattr(self.backend, "client") else "direct"
        )
        self.status_counts.setText(f"{running} running · {stopped} stopped")

        self._refresh_storage_ready()
        self._filter_rows(self.search.text())
        restored = False
        if not error and selected_name:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if (
                    item is not None
                    and item.text() == selected_name
                    and not self.table.isRowHidden(row)
                ):
                    self.table.selectRow(row)
                    restored = True
                    break
        if error:
            self.statusBar().showMessage(error)
            self.inspector_body.setText(
                f"<b>Cannot load libvirt</b><br><span style='color:{self._tokens().danger}'>{error}</span>"
            )
        elif not restored:
            self.statusBar().showMessage("Ready")
            self.inspector_body.setText(s.INSPECTOR_SELECT)
        self.table.verticalScrollBar().setValue(scroll)
        self._schedule_mini_console()
        self._maybe_promote_boot()

    def _maybe_promote_boot(self) -> None:
        """Ask the backend to prefer an installed disk over installer media.

        Runs at most every 30 seconds and is best-effort: older daemons do not
        implement ``vm.promote_boot`` and must keep working.
        """
        settings = QSettings("v-EMU", "v-EMU")
        if not settings.value("ui/prefer_installed_disk", True, type=bool):
            return
        now = time.monotonic()
        if now - self._last_boot_promote < 30:
            return
        self._last_boot_promote = now
        promote = getattr(self.backend, "vm_promote_boot", None)
        if promote is None:
            return
        try:
            result = promote()
        except (VemuError, TypeError):
            return
        if not isinstance(result, dict):
            return
        for name in result.get("promoted") or []:
            logging.getLogger("vemu.ui").info(
                "boot order promoted to the installed disk: %s", name
            )

    def _auto_refresh(self) -> None:
        if self._shutting_down or self._auto_refresh_busy:
            return
        if (
            QApplication.activeModalWidget() is not None
            or QApplication.activePopupWidget() is not None
        ):
            return
        self._auto_refresh_busy = True
        try:
            self.refresh()
        finally:
            self._auto_refresh_busy = False

    def _set_auto_refresh(self, enabled: bool, interval: int) -> None:
        self.auto_refresh_enabled = bool(enabled)
        self.auto_refresh_interval = max(30, int(interval))
        self._settings.setValue("ui/auto_refresh", self.auto_refresh_enabled)
        self._settings.setValue(
            "ui/auto_refresh_interval", self.auto_refresh_interval
        )
        self._apply_auto_refresh()

    def _apply_auto_refresh(self) -> None:
        self._auto_refresh_timer.stop()
        if self.auto_refresh_enabled:
            self._auto_refresh_timer.setInterval(
                self.auto_refresh_interval * 1000
            )
            self._auto_refresh_timer.start()

    def _filter_rows(self, text: str) -> None:
        needle = text.strip().lower()
        visible = 0
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            os_item = self.table.item(row, 1)
            matches = not needle or (
                item is not None and needle in item.text().lower()
            ) or (
                os_item is not None and needle in os_item.text().lower()
            )
            self.table.setRowHidden(row, not matches)
            if matches:
                visible += 1
        if self._load_error:
            self.table.set_empty_state(
                "Cannot load virtual machines", self._load_error
            )
        elif not self.domains:
            if not self._labs_ready:
                self.table.set_empty_state(
                    "No virtual machines yet",
                    "Add a labs storage folder from Storage, then create "
                    "your first VM.",
                )
            elif not self._has_nat:
                self.table.set_empty_state(
                    "No virtual machines yet",
                    "Add a NAT network from Networks for internet access, "
                    "then create your first VM.",
                )
            else:
                self.table.set_empty_state(
                    "No virtual machines yet",
                    "Seal a VM as a template, then clone it from the "
                    "Templates page.",
                )
        elif visible == 0:
            self.table.set_empty_state(
                "No matching virtual machines",
                f'Nothing matches "{text.strip()}". Clear the search to see all VMs.',
            )
        self.table.refresh_empty_state(visible)

    def _selection_changed(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.selected_domain = None
            self._update_actions()
            self.inspector_body.setText(s.INSPECTOR_SELECT)
            self.inspector_stats.hide()
            self._guest_sample = None
            self.statusBar().showMessage("Ready")
            self._schedule_mini_console()
            return
        row = rows[0].row()
        name_item = self.table.item(row, 0)
        if name_item is None:
            return
        name = name_item.text()
        domain = next((item for item in self.domains if item.name == name), None)
        if domain is None:
            return
        self.selected_domain = domain
        self._update_actions()
        tokens = self._tokens()
        state_color = (
            tokens.success if domain.state == "running" else tokens.muted
        )
        inspector = (
            f"<b style='font-size:{TYPE.body_lg}px'>{domain.name}</b><br>"
            f"<span style='color:{state_color}; font-weight:600'>"
            f"{domain.state.capitalize()}</span>"
        )
        if domain.drift == "drifted":
            inspector += (
                f"<br><span style='color:{tokens.warning}'>"
                "XML drift detected outside v-EMU</span>"
            )
        self.inspector_body.setText(inspector)
        self.statusBar().showMessage(
            f"{domain.name} · {domain.state} · {len(domain.disks)} disk(s)"
        )
        self._guest_sample = None
        self._refresh_guest_stats()
        self._schedule_mini_console()
        self._refresh_storage_ready()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._first_run_checked:
            self._first_run_checked = True
            QTimer.singleShot(50, self._maybe_first_run)

    def _maybe_first_run(self) -> None:
        if os.environ.get("VEMU_SKIP_ONBOARDING") == "1":
            self._settings.setValue("onboarding/first_run_done", True)
            return
        if self._settings.value(
            "onboarding/first_run_done", False, type=bool
        ):
            return
        if not hasattr(self.backend, "networks_list"):
            return
        try:
            locations = (
                self.backend.storage_registry()
                if hasattr(self.backend, "storage_registry")
                else self.backend.storage_list()
            )
            networks = self.backend.networks_list()
        except (VemuError, AttributeError):
            return
        has_labs = any(item.get("role") == "labs" for item in locations)
        has_nat = any(item.get("mode") == "nat" for item in networks)
        if has_labs and has_nat:
            self._settings.setValue("onboarding/first_run_done", True)
            return
        dialog = FirstRunDialog(self.backend, self)
        dialog.exec()
        self._settings.setValue("onboarding/first_run_done", True)
        if dialog.skipped:
            self._settings.setValue("onboarding/skipped", True)
            self._settings.setValue("onboarding/banner_dismissed", False)
        elif dialog.storage_done or dialog.network_done:
            self._settings.setValue("onboarding/skipped", False)
            self._settings.setValue("onboarding/banner_dismissed", False)
        self.refresh()
        if dialog.storage_done:
            self.statusBar().showMessage(
                "First-run storage configured", 5000
            )

    def _open_first_run(self) -> None:
        dialog = FirstRunDialog(self.backend, self)
        dialog.exec()
        if dialog.skipped:
            self._settings.setValue("onboarding/skipped", True)
            self._settings.setValue("onboarding/banner_dismissed", False)
        elif dialog.storage_done or dialog.network_done:
            self._settings.setValue("onboarding/skipped", False)
            self._settings.setValue("onboarding/banner_dismissed", False)
        self.refresh()

    def _dismiss_setup_banner(self) -> None:
        self._settings.setValue("onboarding/banner_dismissed", True)
        self.setup_banner.hide()

    def _refresh_storage_ready(self) -> None:
        if not hasattr(self.backend, "storage_registry") and not hasattr(
            self.backend, "storage_list"
        ):
            self._labs_ready = True
        else:
            try:
                locations = (
                    self.backend.storage_registry()
                    if hasattr(self.backend, "storage_registry")
                    else self.backend.storage_list()
                )
                self._labs_ready = any(
                    item.get("role") == "labs" for item in locations
                )
            except (VemuError, AttributeError):
                self._labs_ready = True
        if hasattr(self.backend, "networks_list"):
            try:
                networks = self.backend.networks_list()
                self._has_nat = any(
                    item.get("mode") == "nat" for item in networks
                )
            except (VemuError, AttributeError):
                self._has_nat = True
        can_create = hasattr(self.backend, "vm_create")
        self.new_vm_button.setEnabled(can_create and self._labs_ready)
        self.new_vm_button.setToolTip(
            "Add a labs storage location first (Storage page)."
            if not self._labs_ready
            else "Create a VM from an install ISO (Windows unattended supported)."
        )
        if hasattr(self, "import_button"):
            self.import_button.setEnabled(
                hasattr(self.backend, "vm_import_plan")
                and self._labs_ready
            )
        self._update_setup_banner()

    def _update_setup_banner(self) -> None:
        if not hasattr(self, "setup_banner"):
            return
        if not self._settings.value("onboarding/skipped", False, type=bool):
            self.setup_banner.hide()
            return
        missing: list[str] = []
        if not self._labs_ready:
            missing.append("a labs storage folder")
        if not self._has_nat:
            missing.append("a NAT network")
        if not missing:
            self._settings.setValue("onboarding/skipped", False)
            self._settings.setValue("onboarding/banner_dismissed", False)
            self.setup_banner.hide()
            return
        if self._settings.value(
            "onboarding/banner_dismissed", False, type=bool
        ):
            self.setup_banner.hide()
            return
        if len(missing) == 2:
            text = (
                "Setup is incomplete: add a labs storage folder and a NAT "
                "network."
            )
        else:
            text = f"Setup is incomplete: add {missing[0]}."
        self.setup_banner_label.setText(text)
        self.setup_banner.show()

    def _open_nav(self, label: str) -> None:
        for nav in self._nav_buttons:
            if nav.text() == label:
                self._select_nav(nav)
                return

    def _wake_for_console(self, domain: DomainInfo) -> bool:
        """Wake a paused/suspended guest before the console opens.

        VNC input is queued by QEMU but an S3 guest does not process it until
        an ACPI wake. Opening a frozen console with no visible explanation is
        confusing, so wake first and report failures.
        """

        if domain.state not in {"paused", "suspended"}:
            return True
        if domain.managed_by == "External":
            self.statusBar().showMessage(
                f"{domain.name} is suspended and read-only; wake it with "
                "the host tools.",
                5000,
            )
            return False
        resume = getattr(self.backend, "resume", None)
        if resume is None:
            QMessageBox.warning(
                self,
                "Cannot wake guest",
                f"{domain.name} is suspended and the running v-EMU service "
                "does not support resume. Restart the v-EMU service and try "
                "again.",
            )
            return False
        error: str | None = None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            resume(domain.name)
        except VemuError as exc:
            error = str(exc)
        finally:
            QApplication.restoreOverrideCursor()
        if error is not None:
            QMessageBox.warning(
                self,
                "Cannot wake guest",
                f"{domain.name} is suspended and could not be woken:\n"
                f"{error}\n\nThe console will open, but input may not work "
                "until the guest is resumed.",
            )
            return False
        self.statusBar().showMessage(f"Waking {domain.name}…", 3000)
        return True

    def _open_console(self) -> None:
        if self.selected_domain is None:
            return
        name = self.selected_domain.name
        self._wake_for_console(self.selected_domain)
        try:
            info = self.backend.console_info(name)
        except VemuError as exc:
            QMessageBox.critical(self, "Console unavailable", str(exc))
            return
        window = self.console_windows.get(name)
        if window is None:
            from .console import ConsoleWindow

            window = ConsoleWindow(
                info,
                parent=None,
                backend=self.backend,
                tokens=self._tokens(),
            )
            window.closed.connect(self._console_closed)
            self.console_windows[name] = window
        window.show()
        window.raise_()
        window.activateWindow()

    def _console_closed(self, name: str) -> None:
        self.console_windows.pop(name, None)

    def _blocked_external(self) -> bool:
        domain = self.selected_domain
        if domain is None or domain.managed_by != "External":
            return False
        QMessageBox.information(
            self,
            "Read-only VM",
            "This VM is external/read-only. Adopt it into v-EMU before "
            "changing its state or configuration.",
        )
        return True

    def _open_snapshots(self) -> None:
        if self.selected_domain is None:
            return
        if self._blocked_external():
            return
        if not hasattr(self.backend, "snapshots_info"):
            QMessageBox.information(
                self,
                "Snapshots",
                "Snapshots are available through the v-EMU daemon.",
            )
            return
        from .snapshots import SnapshotsDialog

        name = self.selected_domain.name
        SnapshotsDialog(self.backend, name, self).exec()
        self._refresh_and_select(name)

    def _open_vm_networks(self) -> None:
        if self.selected_domain is None:
            return
        if not hasattr(self.backend, "domain_interfaces"):
            QMessageBox.information(
                self,
                "Networks",
                "NIC management is available through the v-EMU daemon.",
            )
            return
        from .network_page import VmNetworkDialog

        name = self.selected_domain.name
        VmNetworkDialog(self.backend, name, self).exec()
        self._refresh_and_select(name)

    def _performance_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        dialog = PerformanceTuningDialog(self.backend, domain.name, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.changed:
            self._refresh_and_select(domain.name)
            self.statusBar().showMessage(
                f"Performance tuning applied to {domain.name}", 5000
            )

    def _open_vm_config(self) -> None:
        if self.selected_domain is None:
            return
        if self._blocked_external():
            return
        if not hasattr(self.backend, "vm_config_info"):
            QMessageBox.information(
                self,
                "Hardware",
                "Hardware editing is available through the v-EMU daemon.",
            )
            return
        from .vm_config import VmConfigDialog

        name = self.selected_domain.name
        try:
            dialog = VmConfigDialog(
                self.backend, name, self, tokens=self._tokens()
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Hardware", str(exc))
            return
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self._refresh_and_select(name)
            self.statusBar().showMessage(
                "Hardware updated; restart if the VM is running.", 5000
            )

    def _open_virtiofs(self) -> None:
        if self.selected_domain is None:
            return
        if self._blocked_external():
            return
        if not hasattr(self.backend, "vm_shares"):
            QMessageBox.information(
                self,
                "VirtioFS",
                "VirtioFS shares are available through the v-EMU daemon.",
            )
            return
        from .virtiofs import VirtioFsDialog

        VirtioFsDialog(
            self.backend, self.selected_domain.name, self
        ).exec()

    def _open_usb_devices(self) -> None:
        if self.selected_domain is None:
            return
        if self._blocked_external():
            return
        if not hasattr(self.backend, "usb_devices"):
            QMessageBox.information(
                self,
                "USB devices",
                "USB passthrough is available through the v-EMU daemon.",
            )
            return
        from .usb import UsbDevicesDialog

        UsbDevicesDialog(
            self.backend, self.selected_domain.name, self
        ).exec()

    def _run_boot_task(
        self,
        name: str,
        operation,
        title: str,
        success: str,
        *,
        notify_success: bool = False,
    ) -> None:
        if getattr(self, "_boot_task", None) is not None:
            QMessageBox.information(
                self, title, "A boot action is already running."
            )
            return
        self.statusBar().showMessage(f"{title}…")
        task = _BackendTask(operation)
        self._boot_task = task

        def done(_result, error) -> None:
            self._boot_task = None
            if error is not None:
                message = str(error)
                if "Unknown method" in message:
                    message += (
                        "\n\nvemud is outdated. Run `./setup.sh --daemon` "
                        "and restart v-EMU."
                    )
                QMessageBox.critical(self, title, message)
                return
            self._refresh_and_select(name)
            self.statusBar().showMessage(success, 6000)
            if notify_success:
                notify(title, success)

        task.done.connect(done)
        task.start()

    def _boot_menu_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        if self._blocked_external():
            return
        if domain.state != "stopped":
            QMessageBox.information(
                self,
                "Boot menu",
                "Power off the VM before opening the boot menu.",
            )
            return
        if not hasattr(self.backend, "vm_boot_menu"):
            QMessageBox.information(
                self,
                "Boot menu",
                "Boot options are available through the v-EMU daemon.",
            )
            return
        self._run_boot_task(
            domain.name,
            lambda: self.backend.vm_boot_menu(domain.name),
            "Boot menu",
            f"{domain.name} is booting with the boot menu",
        )

    def _boot_firmware_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        if self._blocked_external():
            return
        if domain.state != "stopped":
            QMessageBox.information(
                self,
                "Firmware setup",
                "Power off the VM before entering firmware setup.",
            )
            return
        if domain.firmware != "efi":
            QMessageBox.information(
                self,
                "Firmware setup",
                "Firmware setup is only available for UEFI VMs. "
                "Use the boot menu for BIOS VMs.",
            )
            return
        if not hasattr(self.backend, "vm_boot_firmware"):
            QMessageBox.information(
                self,
                "Firmware setup",
                "Boot options are available through the v-EMU daemon.",
            )
            return
        self._run_boot_task(
            domain.name,
            lambda: self.backend.vm_boot_firmware(domain.name),
            "Firmware setup",
            f"{domain.name}: choose EFI Firmware Setup in the boot menu",
            notify_success=True,
        )

    def _seal_selected(self) -> None:
        if self.selected_domain is None:
            return
        if self._blocked_external():
            return
        if not hasattr(self.backend, "seal"):
            QMessageBox.information(
                self,
                "Seal template",
                "Sealing is available through the v-EMU daemon.",
            )
            return
        from .seal import SealDialog

        domain = self.selected_domain.name
        dialog = SealDialog(
            self.backend, domain, self, tokens=self._tokens()
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.created:
            return
        self.refresh()
        self.statusBar().showMessage(
            f"Sealed {domain} as '{dialog.created.get('name', 'template')}'",
            6000,
        )

    def _new_vm(self) -> None:
        if not hasattr(self.backend, "vm_create"):
            QMessageBox.information(
                self,
                "New VM",
                "Creating VMs is available through the v-EMU daemon.",
            )
            return
        from .vm_create import NewVmDialog

        dialog = NewVmDialog(
            self.backend, self, tokens=self._tokens()
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.created:
            return
        created = dialog.created
        self.refresh()
        self._refresh_and_select(str(created["name"]))
        if created.get("admin_password"):
            QMessageBox.information(
                self,
                "VM created",
                f"{created['name']} is installing Windows "
                f"{created.get('windows_edition_label', '')}.\n\n"
                f"Administrator password: {created['admin_password']}\n\n"
                "The installer runs unattended. Change the password after "
                "the first login.",
            )
        else:
            self.statusBar().showMessage(
                f"Created {created['name']}", 5000
            )

    # ----------------------------------------------------------- mini console

    def _ensure_mini_console(self):
        if self.mini_console is None:
            from .console import MiniConsole

            mini = MiniConsole(self.mini_anchor, backend=self.backend)
            mini.opened.connect(self._open_console)
            mini.set_theme(self._tokens())
            self.mini_layout.addWidget(mini)
            self.mini_console = mini
        return self.mini_console

    def _schedule_mini_console(self) -> None:
        domain = self.selected_domain
        available = (
            self.mini_console_enabled
            and domain is not None
            and self.stack.currentWidget() is self.vms_page
            and self.ui_inspector_visible
        )
        if not available:
            self._mini_timer.stop()
            if self.mini_console is not None:
                self.mini_console.clear()
                self.mini_console.hide()
            return
        if (
            self.mini_console is not None
            and self.mini_console.domain not in {None, domain.name}
        ):
            self.mini_console.clear()
            self.mini_console.hide()
        if domain.state != "running":
            self._mini_timer.stop()
            mini = self._ensure_mini_console()
            if mini is not None:
                mini.show()
                mini.show_no_signal(domain.name)
            return
        self._mini_timer.start()

    def _refresh_mini_console_now(self) -> None:
        if self._shutting_down:
            return
        domain = self.selected_domain
        if (
            not self.mini_console_enabled
            or domain is None
            or self.stack.currentWidget() is not self.vms_page
            or not self.ui_inspector_visible
        ):
            if self.mini_console is not None:
                self.mini_console.clear()
                self.mini_console.hide()
            return
        mini = self._ensure_mini_console()
        if mini is None:
            return
        mini.show()
        if domain.state == "running":
            mini.load({"domain": domain.name})
        else:
            mini.show_no_signal(domain.name)

    # ------------------------------------------------------------------ actions

    def _update_actions(self) -> None:
        domain = self.selected_domain
        running = domain is not None and domain.state == "running"
        suspended = domain is not None and domain.state in {
            "paused",
            "suspended",
        }
        active = running or suspended
        stopped = domain is not None and domain.state == "stopped"
        read_only = domain is not None and domain.managed_by == "External"

        if running:
            self._set_action_icon(
                self.power_button,
                "power",
                tip("Shutdown", "Send an ACPI shutdown request to the guest"),
            )
        elif suspended:
            self._set_action_icon(
                self.power_button,
                "play",
                tip("Resume", "Wake the paused or suspended guest"),
            )
        else:
            self._set_action_icon(
                self.power_button,
                "play",
                tip("Start", "Boot this VM"),
            )
        self.power_button.setEnabled((active or stopped) and not read_only)
        self.force_button.setEnabled(active and not read_only)
        self._set_action_icon(
            self.force_button,
            "force",
            tip(
                "Force off",
                "Power off immediately; unsaved guest data may be lost",
            ),
        )
        snapshots_ok = (
            domain is not None
            and not read_only
            and hasattr(self.backend, "snapshots_info")
        )
        self.snapshots_button.setEnabled(snapshots_ok)
        self._set_action_icon(
            self.snapshots_button,
            "snapshot",
            tip(
                "Snapshots",
                "Create, revert or delete chain snapshots"
                if snapshots_ok
                else "Available through the v-EMU daemon",
            ),
        )
        networks_ok = (
            domain is not None
            and not read_only
            and hasattr(self.backend, "domain_interfaces")
        )
        self.networks_button.setEnabled(networks_ok)
        self._set_action_icon(
            self.networks_button,
            "network",
            tip(
                "Networks",
                "Attach or detach VM network interfaces"
                if networks_ok
                else "Available through the v-EMU daemon",
            ),
        )
        config_ok = (
            domain is not None
            and not read_only
            and hasattr(self.backend, "vm_config_info")
        )
        self.configure_button.setEnabled(config_ok)
        self._set_action_icon(
            self.configure_button,
            "hardware",
            tip(
                "Hardware",
                "Memory, CPU, disks, firmware and devices"
                if config_ok
                else "Available through the v-EMU daemon",
            ),
        )
        self.seal_button.setEnabled(
            domain is not None
            and not read_only
            and hasattr(self.backend, "seal")
        )
        if read_only:
            read_only_tip = (
                "This VM is external/read-only. Adopt it into v-EMU first."
            )
        else:
            read_only_tip = "Seal the selected VM as a template."
        self.seal_button.setToolTip(
            read_only_tip
            if domain is not None and hasattr(self.backend, "seal")
            else "Sealing is available through the v-EMU daemon."
        )

    def _toggle_power(self) -> None:
        if self.selected_domain is None:
            return
        if self.selected_domain.state == "running":
            self._shutdown_selected()
        elif self.selected_domain.state in {"paused", "suspended"}:
            self._resume_selected()
        elif self.selected_domain.state == "stopped":
            self._start_selected()

    def _resume_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        if domain.managed_by == "External":
            QMessageBox.information(
                self,
                "Read-only VM",
                "This VM is external/read-only. Adopt it into v-EMU before "
                "changing its state.",
            )
            return
        self._run_action(
            f"Resuming {domain.name}…",
            lambda: self.backend.resume(domain.name),
            domain.name,
        )

    def _start_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        if domain.managed_by == "External":
            QMessageBox.information(
                self,
                "Read-only VM",
                "This VM is external/read-only. Adopt it into v-EMU before "
                "changing its state or configuration.",
            )
            return
        self._run_action(
            f"Starting {domain.name}…",
            lambda: self.backend.start(domain.name),
            domain.name,
        )

    def _shutdown_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        if domain.managed_by == "External":
            QMessageBox.information(
                self,
                "Read-only VM",
                "This VM is external/read-only. Adopt it into v-EMU before "
                "changing its state or configuration.",
            )
            return
        self._run_action(
            f"Shutdown requested for {domain.name}…",
            lambda: self.backend.shutdown(domain.name, wait=False),
            domain.name,
        )
        QTimer.singleShot(3000, self.refresh)

    def _force_off_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        if domain.managed_by == "External":
            QMessageBox.information(
                self,
                "Read-only VM",
                "This VM is external/read-only. Adopt it into v-EMU before "
                "changing its state or configuration.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Force off",
            s.CONFIRM_FORCE_OFF.format(name=domain.name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run_action(
            f"Forcing {domain.name} off…",
            lambda: self.backend.destroy(domain.name),
            domain.name,
        )

    def _restart_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        if domain.managed_by == "External":
            QMessageBox.information(
                self,
                "Read-only VM",
                "This VM is external/read-only. Adopt it into v-EMU before "
                "changing its state or configuration.",
            )
            return
        self._run_action(
            f"Restarting {domain.name}…",
            lambda: self.backend.restart(domain.name),
            domain.name,
        )

    def _migrate_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        if domain.managed_by == "v-EMU (full)":
            return
        if not hasattr(self.backend, "vm_migration_plan"):
            QMessageBox.information(
                self,
                "Migrate to v-EMU",
                "Migration is available through the v-EMU daemon.",
            )
            return
        from .adopt import AdoptDialog
        from .migrate import MigrateDialog

        if domain.managed_by == "External":
            adopt = AdoptDialog(self.backend, domain.name, self)
            adopt.exec()
            if adopt.choice == "read-only":
                return
            if adopt.changed:
                self._refresh_and_select(domain.name)
                self.statusBar().showMessage(
                    f"{domain.name} adopted as Partial", 6000
                )
                return
            if adopt.choice != "full":
                return
        dialog = MigrateDialog(
            self.backend, domain.name, domain.managed_by, self
        )
        dialog.exec()
        if dialog.migrated:
            self._refresh_and_select(domain.name)
            self.statusBar().showMessage(
                f"{domain.name} migrated to v-EMU", 6000
            )

    def _rescue_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        answer = QMessageBox.question(
            self,
            "Rescue start",
            f"Start {domain.name} without adopting it?\n\n"
            "v-EMU writes an XML backup first. The VM stays External/read-only "
            "and Cockpit/virt-manager may still change it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            payload = self.backend.rescue_start(domain.name)
        except VemuError as exc:
            QMessageBox.critical(self, "Rescue start", str(exc))
            return
        self._refresh_and_select(domain.name)
        self.statusBar().showMessage(
            f"{domain.name} started for rescue (backup: "
            f"{payload.get('backup')})",
            8000,
        )

    def _rescue_shutdown_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        answer = QMessageBox.question(
            self,
            "Rescue shutdown",
            f"Request shutdown of {domain.name}?\n\n"
            "The VM stays External/read-only; this only sends a graceful "
            "shutdown request.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.rescue_shutdown(domain.name)
        except VemuError as exc:
            QMessageBox.critical(self, "Rescue shutdown", str(exc))
            return
        self._refresh_and_select(domain.name)
        self.statusBar().showMessage(
            f"Shutdown requested for {domain.name}", 6000
        )

    def _rename_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        if domain.managed_by == "External":
            QMessageBox.information(
                self,
                "Read-only VM",
                "This VM is external/read-only. Adopt it into v-EMU before "
                "renaming it.",
            )
            return
        new_name, ok = QInputDialog.getText(
            self,
            "Rename VM",
            f"New name for {domain.name}:",
            text=domain.name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == domain.name:
            return
        wants_disks = domain.managed_by == "v-EMU (full)"
        try:
            plan = self.backend.vm_rename_plan(
                domain.name, new_name, rename_disks=wants_disks
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Rename failed", str(exc))
            return
        if not plan.get("supported"):
            QMessageBox.warning(
                self,
                "Cannot rename",
                "\n".join(str(item) for item in plan.get("reasons") or []),
            )
            return
        lines = [f"Rename {domain.name} → {new_name}?"]
        if plan.get("rename_disks"):
            lines.append(
                f"Disk chain: {plan.get('chain_dir')} → "
                f"{plan.get('new_chain_dir')}"
            )
        else:
            lines.append(
                "Only the libvirt domain name will change; disk paths stay "
                "as-is."
            )
        for warning in plan.get("warnings") or []:
            lines.append(f"Warning: {warning}")
        answer = QMessageBox.question(
            self,
            "Rename VM",
            "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.vm_rename(
                domain.name,
                new_name,
                rename_disks=bool(plan.get("rename_disks")),
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Rename failed", str(exc))
            return
        self._refresh_and_select(new_name)
        self.statusBar().showMessage(
            f"Renamed {domain.name} to {new_name}", 4000
        )

    def _run_action(self, message: str, action, name: str) -> None:
        self.statusBar().showMessage(message)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        error: str | None = None
        result = None
        try:
            result = action()
        except VemuError as exc:
            error = str(exc)
        finally:
            QApplication.restoreOverrideCursor()
        if error is not None:
            self.statusBar().showMessage("Action failed", 4000)
            QMessageBox.critical(self, "Action failed", error)
            return
        self._refresh_and_select(name)
        self.statusBar().showMessage(message, 3000)
        removed = result.get("usb_removed") if isinstance(result, dict) else None
        if removed:
            devices = ", ".join(
                f"{item.get('vendor', '')}:{item.get('product', '')}"
                for item in removed
            )
            QMessageBox.warning(
                self,
                "USB devices unavailable",
                f"Started {name} without unavailable USB device(s): "
                f"{devices}.\n\nThey were removed from the VM configuration. "
                "Reconnect the device and attach it again when needed.",
            )

    def _refresh_and_select(self, name: str) -> None:
        self.refresh()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == name:
                self.table.selectRow(row)
                self.table.scrollToItem(item)
                break

    def _build_context_menu(self, domain: DomainInfo) -> QMenu:
        menu = QMenu(self)
        read_only = domain.managed_by == "External"
        active = domain.state in {"running", "paused", "suspended"}
        if active:
            menu.addAction(s.ACTION_CONSOLE, self._open_console)
        if not read_only:
            if domain.state == "running":
                menu.addAction(s.ACTION_SHUTDOWN, self._shutdown_selected)
                menu.addAction(s.ACTION_RESTART, self._restart_selected)
                menu.addAction(s.ACTION_FORCE_OFF, self._force_off_selected)
            elif domain.state in {"paused", "suspended"}:
                menu.addAction(s.ACTION_RESUME, self._resume_selected)
                menu.addAction(s.ACTION_FORCE_OFF, self._force_off_selected)
            else:
                menu.addAction(s.ACTION_START, self._start_selected)
                if hasattr(self.backend, "vm_rename"):
                    menu.addAction(s.ACTION_RENAME, self._rename_selected)
        menu.addSeparator()
        menu.addAction(
            s.ACTION_COPY_NAME,
            lambda: QGuiApplication.clipboard().setText(domain.name),
        )
        if domain.managed_by != "v-EMU (full)":
            migrate = menu.addAction(s.ACTION_MIGRATE, self._migrate_selected)
            migrate.setEnabled(
                domain.state == "stopped"
                and hasattr(self.backend, "vm_migration_plan")
            )
        if read_only and domain.state == "stopped":
            rescue = menu.addAction("Rescue start…", self._rescue_selected)
            rescue.setEnabled(hasattr(self.backend, "rescue_start"))
        if read_only and domain.state == "running":
            stop = menu.addAction(
                "Rescue shutdown…", self._rescue_shutdown_selected
            )
            stop.setEnabled(hasattr(self.backend, "rescue_shutdown"))
        if read_only:
            note = menu.addAction("External · read-only")
            note.setEnabled(False)
            return menu
        snapshots = menu.addAction(s.ACTION_SNAPSHOT)
        snapshots.setEnabled(hasattr(self.backend, "snapshots_info"))
        snapshots.triggered.connect(self._open_snapshots)
        performance = menu.addAction(
            s.ACTION_PERFORMANCE, self._performance_selected
        )
        performance.setEnabled(
            domain.os_type != "windows"
            and hasattr(self.backend, "performance_probe")
        )
        configure = menu.addAction("Configure hardware…", self._open_vm_config)
        configure.setEnabled(hasattr(self.backend, "vm_config_info"))
        if domain.drift == "drifted":
            if hasattr(self.backend, "domain_drift"):
                drift = menu.addAction("Show XML drift…", self._show_drift_selected)
                drift.setEnabled(True)
            if hasattr(self.backend, "domain_baseline_refresh"):
                rebaseline = menu.addAction(
                    "Re-pin XML baseline…", self._rebaseline_selected
                )
        share = menu.addAction(
            "Configure Share (VirtioFS)…", self._open_virtiofs
        )
        share.setEnabled(hasattr(self.backend, "vm_shares"))
        usb = menu.addAction("USB devices…", self._open_usb_devices)
        usb.setEnabled(hasattr(self.backend, "usb_devices"))
        boot = menu.addMenu("Boot options")
        stopped = domain.state == "stopped"
        firmware = boot.addAction("Boot to firmware (UEFI)")
        firmware.setEnabled(
            stopped
            and domain.firmware == "efi"
            and hasattr(self.backend, "vm_boot_firmware")
        )
        firmware.triggered.connect(self._boot_firmware_selected)
        boot_menu = boot.addAction("Boot menu (BIOS)")
        boot_menu.setEnabled(
            stopped
            and domain.firmware != "efi"
            and hasattr(self.backend, "vm_boot_menu")
        )
        boot_menu.triggered.connect(self._boot_menu_selected)
        autostart = menu.addAction("Start on host boot")
        autostart.setCheckable(True)
        autostart.setChecked(domain.autostart)
        autostart.setEnabled(hasattr(self.backend, "set_autostart"))
        autostart.triggered.connect(self._set_autostart)
        menu.addSeparator()
        delete = menu.addAction(s.ACTION_DELETE)
        delete.setEnabled(hasattr(self.backend, "delete_vm"))
        delete.triggered.connect(self._delete_selected)
        return menu

    def _show_drift_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        DriftDialog(self.backend, [domain.name], self).exec()

    def _rebaseline_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        if domain.managed_by != "v-EMU (full)":
            return
        answer = QMessageBox.question(
            self,
            "Re-pin XML baseline",
            "Accept the current libvirt XML as the new v-EMU baseline?\n\n"
            "Only do this after reviewing an intentional external change.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.domain_baseline_refresh(domain.name)
        except VemuError as exc:
            QMessageBox.critical(
                self, "Cannot re-pin baseline", str(exc)
            )
            return
        self.refresh()
        self.statusBar().showMessage(
            f"XML baseline updated for {domain.name}", 4000
        )

    def _set_autostart(self, enabled: bool) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        if self._blocked_external():
            return
        try:
            self.backend.set_autostart(domain.name, enabled)
        except VemuError as exc:
            QMessageBox.critical(self, "Autostart", str(exc))
            return
        self.refresh()
        state = "enabled" if enabled else "disabled"
        self.statusBar().showMessage(
            f"Autostart {state} for {domain.name}", 4000
        )

    def _delete_selected(self) -> None:
        domain = self.selected_domain
        if domain is None:
            return
        if self._blocked_external():
            return
        message = (
            "Delete VM only keeps the disk chain and metadata.\n\n"
            "Delete VM and disks removes v-EMU chain directories, XML "
            "backups, the drift baseline and share assignments. Unmanaged "
            "or Partial disks are kept and reported."
        )
        if domain.state == "running":
            message = (
                "The VM is running and will be forced off first.\n\n"
                + message
            )
        choice = ConfirmDialog(
            self,
            title="Delete VM",
            heading=f"Delete {domain.name}? This cannot be undone.",
            body=message,
            actions=(
                ("Delete VM only", 1, "SecondaryButton"),
                ("Delete VM and disks", 2, "DangerButton"),
                ("Cancel", 0, ""),
            ),
            default_index=2,
        ).exec()
        if choice not in (1, 2):
            return
        delete_storage = choice == 2
        try:
            payload = self.backend.delete_vm(
                domain.name,
                delete_storage=delete_storage,
                force=domain.state == "running",
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Cannot delete VM", str(exc))
            return
        self.refresh()
        suffix = " and its disks" if delete_storage else ""
        self.statusBar().showMessage(
            f"Deleted {domain.name}{suffix}", 5000
        )
        warnings = payload.get("warnings") or []
        if warnings:
            QMessageBox.warning(
                self, "VM deleted", "\n".join(str(item) for item in warnings)
            )

    def _show_context_menu(self, position) -> None:
        row = self.table.rowAt(position.y())
        if row < 0:
            return
        self.table.selectRow(row)
        domain = self.selected_domain
        if domain is None:
            return
        self._build_context_menu(domain).exec(
            self.table.viewport().mapToGlobal(position)
        )

    # ------------------------------------------------------------------ theme

    def _tokens(self) -> Tokens:
        return DARK if self.dark else LIGHT

    def _apply_theme(self, dark: bool) -> None:
        self.dark = dark
        style = stylesheet(DARK if dark else LIGHT)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(style)
        else:
            self.setStyleSheet(style)
        self._refresh_icons()
        if hasattr(self, "inspector_stats"):
            self.inspector_stats.setStyleSheet(
                f"color: {self._tokens().success}; font-weight: 600;"
            )
        if getattr(self, "mini_console", None) is not None:
            self.mini_console.set_theme(self._tokens())
        for window in getattr(self, "console_windows", {}).values():
            window.set_theme(self._tokens())
        if hasattr(self, "guide_page"):
            self.guide_page.set_theme(dark)
        if hasattr(self, "doctor_page"):
            self.doctor_page.set_theme(dark)
        if hasattr(self, "settings_page"):
            self.settings_page.set_theme(dark)

    def _set_theme(self, dark: bool) -> None:
        if dark == self.dark:
            return
        selected = (
            self.selected_domain.name if self.selected_domain else None
        )
        scroll = self.table.verticalScrollBar().value()
        self._apply_theme(dark)
        self._settings.setValue("ui/theme", "dark" if dark else "light")
        self.refresh()
        if selected:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item is not None and item.text() == selected:
                    self.table.selectRow(row)
                    self.table.scrollToItem(item)
                    if self.stack.currentWidget() is self.vms_page:
                        self.table.setFocus(Qt.FocusReason.OtherFocusReason)
                    break
        self.table.verticalScrollBar().setValue(scroll)
        self._filter_rows(self.search.text())
        self._refresh_guest_stats()
        self._schedule_mini_console()

    def _refresh_icons(self) -> None:
        tokens = self._tokens()
        for button, name in self._icon_buttons.items():
            if name == "theme":
                resolved = "sun" if self.dark else "moon"
            else:
                resolved = name
            selected = (
                button in self._nav_buttons
                and bool(button.property("selected"))
            )
            base = tokens.text if self.dark else tokens.muted
            inspector = button.objectName() == "InspectorAction"
            color = tokens.accent if selected else (
                tokens.text if inspector else base
            )
            active = tokens.accent_hover if selected else tokens.text
            button.setIcon(
                make_icon(
                    resolved, color, active,
                    disabled_color=tokens.muted,
                )
            )
        for nav in self._nav_buttons:
            name = nav.property("iconName")
            if not name:
                continue
            selected = bool(nav.property("selected"))
            base = tokens.text if self.dark else tokens.muted
            nav.setIcon(
                make_icon(
                    str(name),
                    tokens.accent if selected else base,
                    tokens.accent_hover if selected else tokens.text,
                )
            )

    def _toggle_theme(self) -> None:
        self._set_theme(not self.dark)

    # ------------------------------------------------------------------ helpers

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("SectionLabel")
        label.setContentsMargins(8, 10, 0, 4)
        return label

    def _icon_button(self, name: str, tooltip: str, callback) -> QPushButton:
        button = QPushButton()
        button.setObjectName("IconButton")
        button.setFixedSize(32, 32)
        button.setIconSize(QSize(DEFAULT_SIZE, DEFAULT_SIZE))
        attach_tooltip(button, tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        self._icon_buttons[button] = name
        return button

    def _action_button(self, name: str, tooltip: str, callback) -> QPushButton:
        button = QPushButton()
        button.setObjectName("InspectorAction")
        button.setFixedSize(34, 34)
        button.setIconSize(QSize(18, 18))
        attach_tooltip(button, tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        self._icon_buttons[button] = name
        tokens = self._tokens()
        button.setIcon(
            make_icon(
                name,
                tokens.text if self.dark else tokens.muted,
                tokens.text,
                disabled_color=tokens.muted,
            )
        )
        return button

    def _set_action_icon(
        self, button: QPushButton, name: str, tooltip: str
    ) -> None:
        self._icon_buttons[button] = name
        tokens = self._tokens()
        button.setIcon(
            make_icon(
                name,
                tokens.text if self.dark else tokens.muted,
                tokens.text,
                disabled_color=tokens.muted,
            )
        )
        set_tooltip(button, tooltip)

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+B"), self).activated.connect(
            self._toggle_sidebar
        )
        QShortcut(QKeySequence("Ctrl+I"), self).activated.connect(
            self._toggle_inspector
        )
        for label in _NAV_SHORTCUTS:
            QShortcut(
                QKeySequence(_NAV_SHORTCUTS[label]), self
            ).activated.connect(lambda label=label: self._open_nav(label))
        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(
            self._context_new
        )
        QShortcut(QKeySequence("Ctrl+R"), self).activated.connect(
            self._context_refresh
        )
        QShortcut(QKeySequence("Ctrl+Shift+M"), self).activated.connect(
            self._open_network_map
        )
        QShortcut(QKeySequence("Ctrl+Shift+K"), self).activated.connect(
            self._open_cli_console
        )
        QShortcut(QKeySequence("F1"), self).activated.connect(
            self._show_context_help
        )

    def _current_nav_label(self) -> str:
        for button in self._nav_buttons:
            if bool(button.property("selected")):
                return button.text()
        return ""

    def _context_new(self) -> None:
        page = self.stack.currentWidget()
        if page is self.vms_page:
            self._new_vm()
        elif page is self.templates_page:
            self.templates_page._import()
        elif page is self.network_page:
            self.network_page._add()
        elif page is self.storage_page:
            self.storage_page._add()
        elif page is self.shares_page:
            self.shares_page._add()
        else:
            self.statusBar().showMessage(
                "No add action on this page.", 2500
            )

    def _context_refresh(self) -> None:
        page = self.stack.currentWidget()
        if page is self.vms_page:
            self.refresh()
        elif hasattr(page, "refresh"):
            page.refresh()
        elif page is self.settings_page:
            self.settings_page.refresh_connection()
        else:
            self.statusBar().showMessage(
                "No refresh action on this page.", 2500
            )

    def _show_context_help(self) -> None:
        label = self._current_nav_label()
        if not label:
            label = s.NAV_VMS
        ContextHelpDialog(label, self).exec()

    def _apply_pane_state(self) -> None:
        self.sidebar.setVisible(self.ui_sidebar_visible)
        self.inspector.setVisible(self.ui_inspector_visible)

    def _toggle_sidebar(self) -> None:
        self._set_sidebar_visible(not self.ui_sidebar_visible)
        self.statusBar().showMessage(
            "Sidebar shown" if self.ui_sidebar_visible else "Sidebar hidden",
            2000,
        )

    def _set_sidebar_visible(self, visible: bool) -> None:
        self.ui_sidebar_visible = visible
        self.sidebar.setVisible(visible)
        self._save_ui_state()

    def _toggle_inspector(self) -> None:
        if self.stack.currentWidget() is not self.vms_page:
            self.statusBar().showMessage(
                "Inspector is available on Virtual machines.", 2500
            )
            return
        self._set_inspector_visible(not self.ui_inspector_visible)
        self.statusBar().showMessage(
            "Inspector shown"
            if self.ui_inspector_visible
            else "Inspector hidden",
            2000,
        )
        self._schedule_mini_console()

    def _set_inspector_visible(self, visible: bool) -> None:
        self.ui_inspector_visible = visible
        if self.stack.currentWidget() is self.vms_page:
            self.inspector.setVisible(visible)
        self._save_ui_state()
        self._schedule_mini_console()

    def _set_mini_console_enabled(self, enabled: bool) -> None:
        self.mini_console_enabled = enabled
        self._settings.setValue("ui/mini_console", enabled)
        if not enabled:
            self._mini_timer.stop()
            if self.mini_console is not None:
                self.mini_console.clear()
                self.mini_console.hide()
        else:
            self._schedule_mini_console()

    def _refresh_metrics(self) -> None:
        metrics = self._host_metrics.sample()
        self.status_metrics.setText(format_metrics(metrics))
        total = metrics.get("mem_total_mib")
        swap_total = metrics.get("swap_total_mib")
        self.status_metrics.setToolTip(
            "Host load sampled every 2 seconds\n"
            f"Memory: {metrics.get('mem_used_mib') or 0} / {total or 0} MiB\n"
            f"Swap: {metrics.get('swap_used_mib') or 0} / {swap_total or 0} MiB"
        )

    def _refresh_guest_stats(self) -> None:
        domain = self.selected_domain
        if (
            domain is None
            or domain.state != "running"
            or self.stack.currentWidget() is not self.vms_page
            or not self.ui_inspector_visible
        ):
            self.inspector_stats.hide()
            self._guest_sample = None
            return
        try:
            stats = self.backend.domain_stats(domain.name)
        except (VemuError, AttributeError):
            self.inspector_stats.hide()
            self._guest_sample = None
            return
        now = time.monotonic()
        cpu_time = int(stats.get("cpu_time_ns") or 0)
        vcpus = max(1, int(stats.get("vcpus") or 1))
        cpu_percent = None
        if self._guest_sample and self._guest_sample[0] == domain.name:
            elapsed = max(0.001, now - self._guest_sample[2])
            delta = max(0, cpu_time - self._guest_sample[1])
            cpu_percent = 100.0 * delta / (elapsed * 1_000_000_000 * vcpus)
        self._guest_sample = (domain.name, cpu_time, now)
        rss = stats.get("memory_rss_mib")
        actual = stats.get("memory_actual_mib")
        available = stats.get("memory_available_mib")
        usable = stats.get("memory_usable_mib")
        parts = []
        if cpu_percent is not None:
            parts.append(f"vCPU {cpu_percent:.0f}%")
        else:
            parts.append("vCPU —")
        memory_note = ""
        total = int(available) if available else int(actual or 0)
        if actual:
            # The guest-visible total (MemTotal) is "available"; "actual" is
            # the balloon allocation and includes memory not visible to the
            # guest. "usable" accounts for reclaimable cache, so total minus
            # usable matches the guest's used figure. "unused" is only free
            # pages and went stale when balloon stats were not collected.
            if usable is not None and total > 0:
                used = max(0, total - int(usable))
                parts.append(f"RAM {used}/{total} MiB")
                memory_note = (
                    "RAM from guest balloon statistics (total minus usable)."
                )
            else:
                # Without balloon statistics actual is only the amount
                # allocated to the VM, not guest usage. Label it explicitly
                # instead of making a full VM look fully used.
                parts.append(f"RAM {actual} MiB allocated")
                memory_note = (
                    "RAM allocated; install the virtio balloon driver "
                    "(viomem) for guest usage."
                )
        self.inspector_stats.setText(" · ".join(parts))
        self.inspector_stats.setStyleSheet(
            f"color: {self._tokens().success}; font-weight: 600;"
        )
        host_rss = (
            f"Host RSS {rss} MiB (includes emulation overhead)."
            if rss is not None
            else ""
        )
        set_tooltip(
            self.inspector_stats,
            tip(
                "Guest stats",
                "vCPU average since the last sample. "
                f"{memory_note} {host_rss}",
            ),
        )
        self.inspector_stats.show()

    def _save_ui_state(self) -> None:
        self._settings.setValue("ui/sidebar", self.ui_sidebar_visible)
        self._settings.setValue("ui/inspector", self.ui_inspector_visible)

    def _nav_button(
        self,
        text: str,
        *,
        icon: str | None = None,
        selected: bool = False,
    ) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("NavButton")
        button.setProperty("selected", selected)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: self._select_nav(button))
        if icon:
            button.setProperty("iconName", icon)
            button.setIconSize(QSize(18, 18))
            tokens = self._tokens()
            button.setIcon(
                make_icon(
                    icon,
                    tokens.accent if selected else (
                        tokens.text if self.dark else tokens.muted
                    ),
                    tokens.text,
                )
            )
        self._nav_buttons.append(button)
        return button

    def _import_vm(self, kind: str) -> None:
        if not hasattr(self.backend, "vm_import_plan"):
            QMessageBox.information(
                self,
                "Import VM",
                "Importing OVA/disk images is available through the v-EMU "
                "daemon.",
            )
            return
        from .import_vm import ImportVmDialog

        dialog = ImportVmDialog(self.backend, kind, self)
        if (
            dialog.exec() != QDialog.DialogCode.Accepted
            or not dialog.created
        ):
            return
        created = dialog.created
        self.refresh()
        self._refresh_and_select(str(created.get("name") or ""))
        self.statusBar().showMessage(
            f"Imported {created.get('name')} · review hardware and vendor docs",
            8000,
        )

    def _open_templates(self) -> None:
        for nav in self._nav_buttons:
            if nav.text() == s.NAV_TEMPLATES:
                self._select_nav(nav)
                return

    def _select_nav(self, button: QPushButton) -> None:
        for nav in self._nav_buttons:
            nav.setProperty("selected", nav is button)
            nav.style().unpolish(nav)
            nav.style().polish(nav)
        label = button.text()
        if label == s.NAV_VMS:
            self.stack.setCurrentWidget(self.vms_page)
            self.inspector.setVisible(self.ui_inspector_visible)
            self.statusBar().showMessage("Virtual machines")
        elif label == s.NAV_LABS:
            self.stack.setCurrentWidget(self.labs_page)
            self.inspector.hide()
            self.labs_page.refresh()
            self.statusBar().showMessage("Labs")
        elif label == s.NAV_TEMPLATES:
            self.stack.setCurrentWidget(self.templates_page)
            self.inspector.hide()
            self.templates_page.refresh()
            self.statusBar().showMessage(
                f"Templates · {len(self.templates_page.templates)} template(s)"
            )
        elif label == s.NAV_STORAGE:
            self.stack.setCurrentWidget(self.storage_page)
            self.inspector.hide()
            self.storage_page.refresh()
            self.statusBar().showMessage(
                f"Storage · {len(self.storage_page.locations)} location(s)"
            )
        elif label == s.NAV_NETWORKS:
            self.stack.setCurrentWidget(self.network_page)
            self.inspector.hide()
            self.network_page.refresh()
            self.statusBar().showMessage(
                f"Networks · {len(self.network_page.profiles)} network(s)"
            )
        elif label == s.NAV_SHARES:
            self.stack.setCurrentWidget(self.shares_page)
            self.inspector.hide()
            self.shares_page.refresh()
            self.statusBar().showMessage(
                f"Shared folders · {len(self.shares_page.shares)} folder(s)"
            )
        elif label == s.NAV_GUIDE:
            self.stack.setCurrentWidget(self.guide_page)
            self.inspector.hide()
            self.statusBar().showMessage("Guide")
        elif label == s.NAV_DOCTOR:
            self.stack.setCurrentWidget(self.doctor_page)
            self.inspector.hide()
            self.doctor_page.refresh()
            self.statusBar().showMessage("Doctor")
        elif label == s.NAV_SETTINGS:
            self.stack.setCurrentWidget(self.settings_page)
            self.inspector.hide()
            self.settings_page.set_panes(
                self.ui_sidebar_visible, self.ui_inspector_visible
            )
            self.settings_page.set_mini_console(self.mini_console_enabled)
            self.settings_page.set_auto_refresh(
                self.auto_refresh_enabled, self.auto_refresh_interval
            )
            self.settings_page.set_theme(self.dark)
            self.settings_page.refresh_connection()
            self.statusBar().showMessage("Settings")
        else:
            self.statusBar().showMessage(f"{label} view is coming later.", 3000)
        self._refresh_icons()
        self._schedule_mini_console()

    def _open_settings(self) -> None:
        for button in self._nav_buttons:
            if button.text() == s.NAV_SETTINGS:
                self._select_nav(button)
                return

    def _open_network_map(self) -> None:
        dialog = NetworkMapDialog(
            self.backend, dark=self.dark, parent=self
        )
        dialog.exec()

    def _open_cli_console(self) -> None:
        from .cli_console import CliConsoleDialog

        CliConsoleDialog(self.backend, self).exec()

    def center_on_screen(self) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        frame = self.frameGeometry()
        frame.moveCenter(screen.availableGeometry().center())
        self.move(frame.topLeft())

    def _kpi_card(self, label: str, value: str) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(2)
        caption = QLabel(label.upper())
        caption.setObjectName("KpiLabel")
        number = QLabel(value)
        number.setObjectName("KpiValue")
        layout.addWidget(caption)
        layout.addWidget(number)
        return card, number

    def _set_kpi(self, label: QLabel, value: str) -> None:
        label.setText(value)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._shutting_down = True
        self._mini_timer.stop()
        self._metrics_timer.stop()
        self._stats_timer.stop()
        self._auto_refresh_timer.stop()
        logging.info(
            "ui close: selected=%s mini=%s consoles=%s",
            getattr(self.selected_domain, "name", None),
            getattr(self.mini_console, "domain", None),
            len(self.console_windows),
        )
        if self.mini_console is not None:
            self.mini_console.shutdown()
        for window in list(self.console_windows.values()):
            window.shutdown()
        self.console_windows.clear()
        self.backend.close()
        super().closeEvent(event)


def _resolve_theme(force_dark: bool = False, force_light: bool = False) -> bool:
    """Return the theme; first run defaults to dark."""

    if force_dark:
        return True
    if force_light:
        return False
    saved = QSettings("v-EMU", "v-EMU").value("ui/theme", "")
    # Invalid or missing values intentionally fall back to dark for the
    # first-run experience. A user choice is persisted by Settings.
    return str(saved) != "light"


def _resolve_ui_scale() -> str:
    """Return the persisted UI scale, with VEMU_UI_SCALE as override."""

    raw = os.environ.get("VEMU_UI_SCALE")
    if not raw:
        raw = QSettings("v-EMU", "v-EMU").value("ui/scale", "1")
    value = str(raw)
    return value if value in {"1", "1.25", "1.5", "2"} else "1"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vemu-ui")
    parser.add_argument("--uri", default="qemu:///system")
    parser.add_argument("--socket", help="vemud socket path.")
    parser.add_argument("--direct", action="store_true", help="Use libvirt directly.")
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--light", action="store_true")
    parser.add_argument(
        "--no-mini-console",
        action="store_true",
        help="Disable the noVNC inspector preview for this run.",
    )
    parser.add_argument("--screenshot", help="Render the window to a PNG and exit.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument(
        "--allow-multiple",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def _configure_logging() -> Path:
    base = Path(
        os.environ.get(
            "XDG_STATE_HOME", str(Path.home() / ".local" / "state")
        )
    ) / "vemu"
    base.mkdir(parents=True, exist_ok=True)
    path = base / "ui.log"
    logging.basicConfig(
        filename=str(path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    def _excepthook(exc_type, exc, tb):
        logging.critical(
            "Unhandled exception", exc_info=(exc_type, exc, tb)
        )
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook
    return path


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _configure_webengine_environment()
    os.environ["QT_SCALE_FACTOR"] = _resolve_ui_scale()
    _configure_display_environment()
    log_path = _configure_logging()
    app = QApplication(sys.argv[:1])
    from PySide6.QtCore import qInstallMessageHandler

    def _qt_message(_mode, _context, message):
        logging.getLogger("qt").warning("%s", message)

    qInstallMessageHandler(_qt_message)
    app.setStyle("Fusion")
    load_bundled_fonts()
    font = ui_font()
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    app.setApplicationName("v-EMU")
    app.setDesktopFileName("org.vemu.VEMU")
    icon_path = Path(__file__).with_name("assets") / "org.vemu.VEMU.svg"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    guard: SingleInstanceGuard | None = None
    if not args.screenshot and not args.allow_multiple:
        guard = SingleInstanceGuard(parent=app)
        if not guard.acquire():
            logging.info("ui already running; raised the existing instance")
            return 0

    dark = _resolve_theme(args.dark, args.light)

    socket_path = args.socket or os.environ.get("VEMU_SOCKET")
    use_daemon = not args.direct and (
        bool(socket_path) or socket_available()
    )
    if use_daemon:
        backend = DaemonBackend(socket_path)
    else:
        try:
            backend = LibvirtBackend(args.uri)
        except VemuError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    window = MainWindow(backend, dark=dark, uri=args.uri)
    if args.no_mini_console:
        window.mini_console_enabled = False
    window.resize(args.width, args.height)
    window.center_on_screen()
    logging.info(
        "ui start: log=%s daemon=%s theme=%s socket=%s",
        log_path,
        use_daemon,
        "dark" if dark else "light",
        socket_path,
    )
    window.show()

    if guard is not None:
        def _raise_existing() -> None:
            window.showNormal()
            window.raise_()
            window.activateWindow()
            handle = window.windowHandle()
            if handle is not None:
                handle.requestActivate()

        guard.show_requested.connect(_raise_existing)

    if args.screenshot:
        path = Path(args.screenshot).expanduser()

        def capture() -> None:
            window.grab().save(str(path))
            app.quit()

        QTimer.singleShot(400, capture)

    result = int(app.exec())
    if guard is not None:
        guard.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
