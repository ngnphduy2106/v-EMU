"""v-EMU daemon: local JSON-RPC over a Unix socket.

The daemon runs as a system service (root) and owns all privileged operations:
qcow2 chains, identity reset, template sealing, storage repair, and libvirt
domain definitions. Clients connect through ``/run/vemu/vemud.sock``.

Protocol: one JSON object per line.

    -> {"id": 1, "method": "ping", "params": {}}
    <- {"id": 1, "result": {"pong": true, "version": "0.1.0"}}

This is intentionally small and dependency-free so it can be audited easily.
"""

from __future__ import annotations

import argparse
import grp
import json
import logging
import os
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from . import __version__
from . import diagnose
from .chain import ChainManager
from .errors import Conflict, ValidationError, VemuError
from . import service
from . import lab
from .backend import LibvirtBackend
from .pathpolicy import PathPolicy, validate_storage_root

DEFAULT_SOCKET = "/run/vemu/vemud.sock"
DEFAULT_SOCKET_MODE = 0o660
MAX_REQUEST_BYTES = 1024 * 1024

LOG = logging.getLogger("vemud")

READ_ONLY_METHODS = {
    "ping",
    "system_info",
    "profile.get",
    "doctor.run",
    "domains.list",
    "domain.interfaces",
    "domain.stats",
    "domain.drift",
    "vm.config_info",
    "vm.import_plan",
    "vm.capabilities",
    "vm.shares",
    "vm.share_status",
    "shares.list",
    "vm.xml",
    "console.info",
    "serial.info",
    "domain.screenshot",
    "chain.show",
    "chain.verify",
    "chain.journals",
    "snapshots.info",
    "storage.inspect",
    "storage.list",
    "storage.registry",
    "storage.files",
    "templates.list",
    "networks.list",
    "network.plan",
    "network.bridge_inventory",
    "network.leases",
    "network.topology",
    "lab.status",
}


def _peer_groups(pid: int) -> set[int]:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return set()
    for line in text.splitlines():
        if line.startswith("Groups:"):
            return {int(value) for value in line.split()[1:] if value.isdigit()}
    return set()


def peer_allowed(
    uid: int,
    gid: int,
    pid: int,
    allowed_gid: int | None,
    daemon_uid: int,
) -> bool:
    """Allow root, the daemon owner, or members of the socket group."""
    if uid == 0 or uid == daemon_uid:
        return True
    if allowed_gid is None:
        return False
    if gid == allowed_gid:
        return True
    return allowed_gid in _peer_groups(pid)


def polkit_check(pid: int) -> bool:
    if not shutil.which("pkcheck"):
        return False
    try:
        result = subprocess.run(
            [
                "pkcheck",
                "--process", str(pid),
                "--action-id", "org.vemu.manage",
                "--allow-user-interaction",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


class VemuDaemon:
    def __init__(
        self,
        uri: str = "qemu:///system",
        config: str | None = None,
        network_config: str | None = None,
        socket_path: str | None = None,
        *,
        polkit: bool = False,
    ) -> None:
        self.uri = uri
        self.config = config
        self.network_config = network_config
        self.socket_path = socket_path
        self.polkit = polkit
        self.allowed_gid: int | None = None
        self.daemon_uid = os.geteuid()
        self._event_thread: threading.Thread | None = None
        self._auto_mounting: set[str] = set()
        self._auto_mount_lock = threading.Lock()
        try:
            import libvirt  # type: ignore

            libvirt.virEventRegisterDefaultImpl()
        except ImportError:
            pass

    # ------------------------------------------------------------------ events

    def start_event_monitor(self) -> None:
        """Start the libvirt reboot watcher (auto-eject installer media)."""
        if self._event_thread is not None:
            return
        self._event_thread = threading.Thread(
            target=self._event_loop,
            name="vemu-events",
            daemon=True,
        )
        self._event_thread.start()

    def _event_loop(self) -> None:
        try:
            import libvirt  # type: ignore
        except ImportError:
            LOG.warning("python3-libvirt missing; reboot auto-eject disabled")
            return
        while True:
            try:
                connection = libvirt.open(self.uri)
                if connection is None:
                    raise RuntimeError("libvirt connection is NULL")
                connection.domainEventRegisterAny(
                    None,
                    libvirt.VIR_DOMAIN_EVENT_ID_REBOOT,
                    self._domain_reboot,
                    None,
                )
                connection.domainEventRegisterAny(
                    None,
                    libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE,
                    self._domain_lifecycle,
                    None,
                )
                LOG.info(
                    "domain event monitor active (auto-eject, VirtioFS mount)"
                )
                while True:
                    libvirt.virEventRunDefaultImpl()
            except Exception as exc:  # noqa: BLE001 - reconnect below
                LOG.warning("domain event monitor: %s; retrying", exc)
                time.sleep(5)

    def _domain_lifecycle(self, _conn, domain, event, _detail, _opaque) -> None:
        try:
            import libvirt  # type: ignore

            wanted = {
                libvirt.VIR_DOMAIN_EVENT_STARTED,
                libvirt.VIR_DOMAIN_EVENT_RESUMED,
            }
        except ImportError:
            return
        if event not in wanted:
            return
        self._schedule_auto_mount(domain.name())

    def _schedule_auto_mount(self, name: str) -> None:
        with self._auto_mount_lock:
            if name in self._auto_mounting:
                return
            self._auto_mounting.add(name)

        def worker() -> None:
            try:
                deadline = time.monotonic() + 120
                ready = False
                while time.monotonic() < deadline:
                    try:
                        with LibvirtBackend(self.uri) as backend:
                            backend.qemu_agent_command(
                                name,
                                {"execute": "guest-ping"},
                                timeout=5,
                            )
                        ready = True
                        break
                    except Exception:
                        time.sleep(3)
                if not ready:
                    LOG.info(
                        "%s: guest agent not ready; skipping auto-mount",
                        name,
                    )
                    return
                result = service.vm_share_auto_mount(name, uri=self.uri)
                if not result.get("skipped"):
                    LOG.info(
                        "%s: auto-mounted VirtioFS shares: %s",
                        name,
                        ", ".join(
                            f"{item.get('tag')}->{item.get('drive')}"
                            for item in result.get("mounts", [])
                            if item.get("mounted")
                        )
                        or "none",
                    )
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                LOG.warning(
                    "%s: VirtioFS auto-mount failed: %s", name, exc
                )
            finally:
                with self._auto_mount_lock:
                    self._auto_mounting.discard(name)

        threading.Thread(
            target=worker, name=f"vemu-auto-mount-{name}", daemon=True
        ).start()

    def _domain_reboot(self, _conn, domain, _opaque) -> None:
        name = domain.name()
        try:
            result = service.vm_eject_installer_media(name, uri=self.uri)
        except Exception as exc:  # noqa: BLE001 - never kill the event loop
            LOG.warning("auto-eject after reboot failed for %s: %s", name, exc)
            return
        ejected = result.get("ejected") or []
        if ejected:
            LOG.info(
                "%s: ejected installer media after reboot: %s",
                name,
                ", ".join(ejected),
            )
        self._schedule_auto_mount(name)

    # ------------------------------------------------------------------ dispatch

    def dispatch(self, method: str, params: Any) -> Any:
        if not isinstance(method, str) or not method:
            raise ValidationError("Method name must be a non-empty string.")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValidationError("params must be a JSON object.")
        handler = getattr(self, "m_" + method.replace(".", "_"), None)
        if handler is None:
            raise ValidationError(f"Unknown method: {method}")
        return handler(params)

    # ------------------------------------------------------------------ system

    def m_ping(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "version": __version__}

    def m_system_info(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": __version__,
            "uri": self.uri,
            "socket": self.socket_path,
            "pid": os.getpid(),
            "uid": os.geteuid(),
            "python": sys.version.split()[0],
        }

    def m_doctor_run(self, params: dict[str, Any]) -> dict[str, Any]:
        return diagnose.run_checks(
            self.uri, config=self.config
        )

    def m_profile_get(self, _params: dict[str, Any]) -> dict[str, Any]:
        return service.runtime_profile_get()

    def m_profile_set(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.runtime_profile_set(
            _require(params, "profile"), uri=self.uri
        )

    # ------------------------------------------------------------------ domains

    def m_domains_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return service.domains_list(self.uri)

    def m_domain_interfaces(
        self, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return service.domain_interfaces(
            _require(params, "domain"), self.uri
        )

    def m_domain_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.domain_stats(
            _require(params, "domain"), self.uri
        )

    def m_domain_drift(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.domain_drift(
            _require(params, "domain"), self.uri
        )

    def m_domain_baseline_refresh(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        return service.domain_baseline_refresh(
            params.get("domain") or None,
            uri=self.uri,
        )

    def m_vm_config_info(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_config_info(
            _require(params, "domain"), self.uri
        )

    def m_vm_clipboard_agent_start(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        return service.vm_clipboard_agent_start(
            _require(params, "domain"), uri=self.uri
        )

    def m_vm_clipboard_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_clipboard_get(
            _require(params, "domain"), uri=self.uri
        )

    def m_vm_clipboard_set(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_clipboard_set(
            _require(params, "domain"),
            str(_require(params, "text")),
            uri=self.uri,
        )

    def m_serial_info(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.serial_info(
            _require(params, "domain"), self.uri
        )

    def m_vm_rename_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_rename_plan(
            _require(params, "domain"),
            _require(params, "new_name"),
            rename_disks=bool(params.get("rename_disks", True)),
            uri=self.uri,
        )

    def m_vm_rename(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_rename(
            _require(params, "domain"),
            _require(params, "new_name"),
            rename_disks=bool(params.get("rename_disks", True)),
            backup_dir=None,
            uri=self.uri,
        )

    def m_vm_adopt(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.adopt_domain(
            _require(params, "domain"),
            str(params.get("mode", "partial")),
            backup_dir=params.get("backup_dir"),
            uri=self.uri,
        )

    def m_vm_migration_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_migration_plan(
            _require(params, "domain"),
            new_firmware=bool(params.get("new_firmware", False)),
            storage_config=self.config,
            import_snapshots=bool(params.get("import_snapshots", False)),
            uri=self.uri,
        )

    def m_vm_migrate_full(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_migrate_full(
            _require(params, "domain"),
            accept_snapshot_loss=bool(
                params.get("accept_snapshot_loss", False)
            ),
            import_snapshots=bool(params.get("import_snapshots", False)),
            start=bool(params.get("start", False)),
            new_firmware=bool(params.get("new_firmware", False)),
            storage_config=self.config,
            backup_dir=None,
            uri=self.uri,
        )

    def m_vm_serial_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_serial_add(
            _require(params, "domain"), uri=self.uri
        )

    def m_vm_xml(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_xml(
            _require(params, "domain"), self.uri
        )

    def m_vm_media_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_media_list(
            _require(params, "domain"), self.uri
        )

    def m_vm_media_change(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_media_change(
            _require(params, "domain"),
            _require(params, "path"),
            target=params.get("target"),
            force=bool(params.get("force", False)),
            uri=self.uri,
        )

    def m_vm_media_eject(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_media_eject(
            _require(params, "domain"),
            target=params.get("target"),
            force=bool(params.get("force", False)),
            uri=self.uri,
        )

    def m_vm_capabilities(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_capabilities(
            params.get("machine"),
            self.uri,
            arch=str(params.get("arch", "x86_64")),
        )

    def m_vm_shares(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_shares(
            _require(params, "domain"), self.uri
        )

    def m_shares_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return service.shares_list()

    def m_shares_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.share_catalog_add(
            _require(params, "name"),
            _require(params, "path"),
            _require(params, "tag"),
            read_only=bool(params.get("read_only", False)),
            description=str(params.get("description", "")),
        )

    def m_shares_update(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.share_catalog_update(
            _require(params, "name"),
            _require(params, "path"),
            _require(params, "tag"),
            read_only=bool(params.get("read_only", False)),
            description=str(params.get("description", "")),
        )

    def m_shares_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.share_catalog_remove(_require(params, "name"))

    def m_vm_share_attach(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_share_attach(
            _require(params, "domain"),
            _require(params, "share"),
            tag=params.get("tag"),
            read_only=params.get("read_only"),
            auto_mount=bool(params.get("auto_mount", False)),
            drive=params.get("drive"),
            mountpoint=params.get("mountpoint"),
            uri=self.uri,
        )

    def m_vm_share_detach(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_share_detach(
            _require(params, "domain"),
            _require(params, "share"),
            uri=self.uri,
        )

    def m_vm_share_options(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_share_options(
            _require(params, "domain"),
            _require(params, "share"),
            auto_mount=params.get("auto_mount"),
            drive=params.get("drive"),
            mountpoint=params.get("mountpoint"),
        )

    def m_vm_share_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_share_status(
            _require(params, "domain"), self.uri
        )

    def m_vm_share_mount(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_share_mount(
            _require(params, "domain"),
            tag=params.get("tag"),
            uri=self.uri,
        )

    def m_vm_share_unmount(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_share_unmount(
            _require(params, "domain"),
            _require(params, "tag"),
            uri=self.uri,
        )

    def m_vm_share_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_share_add(
            _require(params, "domain"),
            _require(params, "tag"),
            _require(params, "source"),
            read_only=bool(params.get("read_only", False)),
            uri=self.uri,
        )

    def m_vm_share_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_share_remove(
            _require(params, "domain"),
            _require(params, "tag"),
            self.uri,
        )

    def m_vm_config_update(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_config_update(
            _require(params, "domain"),
            memory_mib=_optional_int(params, "memory_mib"),
            vcpus=_optional_int(params, "vcpus"),
            vcpus_max=_optional_int(params, "vcpus_max"),
            cpu_mode=params.get("cpu_mode"),
            machine=params.get("machine"),
            video=params.get("video"),
            video_memory_mib=_optional_int(params, "video_memory_mib"),
            disk_bus=params.get("disk_bus"),
            nic_model=params.get("nic_model"),
            os_type=params.get("os_type"),
            uri=self.uri,
        )

    def m_vm_disk_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_disk_add(
            _require(params, "domain"),
            self._safe_path(
                _require(params, "path"), create_parent=True
            ),
            size_gb=int(params.get("size_gb", 10)),
            bus=str(params.get("bus", "virtio")),
            format=str(params.get("format", "qcow2")),
            create_file=bool(params.get("create_file", True)),
            uri=self.uri,
        )

    def m_vm_disk_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_disk_remove(
            _require(params, "domain"),
            self._safe_path(_require(params, "source"), must_exist=True),
            delete_file=bool(params.get("delete_file", False)),
            uri=self.uri,
        )

    def m_vm_disk_resize(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_disk_resize(
            _require(params, "domain"),
            self._safe_path(_require(params, "source"), must_exist=True),
            int(_require(params, "size_gb")),
            uri=self.uri,
        )

    def m_vm_tpm_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_tpm_add(
            _require(params, "domain"),
            version=str(params.get("version", "2.0")),
            uri=self.uri,
        )

    def m_vm_tpm_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_tpm_remove(
            _require(params, "domain"),
            uri=self.uri,
        )

    def m_vm_usb_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_usb_add(
            _require(params, "domain"),
            uri=self.uri,
        )

    def m_usb_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return service.vm_usb_list(
            params.get("domain"),
            uri=self.uri,
        )

    def m_vm_usb_attach(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_usb_attach(
            _require(params, "domain"),
            _require(params, "vendor"),
            _require(params, "product"),
            uri=self.uri,
        )

    def m_vm_usb_detach(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_usb_detach(
            _require(params, "domain"),
            _require(params, "vendor"),
            _require(params, "product"),
            uri=self.uri,
        )

    def m_vm_boot_menu(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_boot_menu(
            _require(params, "domain"),
            timeout_ms=int(params.get("timeout_ms", 5000)),
            uri=self.uri,
        )

    def m_vm_boot_firmware(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_boot_firmware(
            _require(params, "domain"),
            uri=self.uri,
        )

    def m_vm_promote_boot(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_promote_boot(
            params.get("domain") or None,
            uri=self.uri,
        )

    def m_vm_audio_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_audio_add(
            _require(params, "domain"),
            uri=self.uri,
        )

    def m_vm_audio_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_audio_remove(
            _require(params, "domain"),
            uri=self.uri,
        )

    def m_console_info(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.console_info(
            _require(params, "domain"), self.uri
        )

    def m_domain_screenshot(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.domain_screenshot(
            _require(params, "domain"), self.uri
        )

    def m_vm_rescue_start(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.domain_rescue_start(
            _require(params, "domain"),
            backup_dir=params.get("backup_dir"),
            uri=self.uri,
        )

    def m_vm_rescue_shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.domain_rescue_shutdown(
            _require(params, "domain"),
            self.uri,
            force=bool(params.get("force", False)),
            wait=bool(params.get("wait", True)),
        )

    def m_domain_start(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.domain_start(
            _require(params, "domain"), self.uri
        )

    def m_domain_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.domain_resume(
            _require(params, "domain"), self.uri
        )

    def m_domain_autostart(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.domain_set_autostart(
            _require(params, "domain"),
            bool(params.get("enabled", True)),
            self.uri,
        )

    def m_domain_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.domain_delete(
            _require(params, "domain"),
            delete_storage=bool(params.get("delete_storage", False)),
            force=bool(params.get("force", False)),
            uri=self.uri,
        )

    def m_domain_shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.domain_shutdown(
            _require(params, "domain"),
            self.uri,
            force=bool(params.get("force", False)),
            wait=bool(params.get("wait", True)),
        )

    def m_domain_force_off(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.domain_force_off(
            _require(params, "domain"), self.uri
        )

    def m_domain_restart(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.domain_restart(
            _require(params, "domain"), self.uri
        )

    # ------------------------------------------------------------------ snapshots

    def m_snapshots_info(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.snapshots_info(
            _require(params, "domain"), self.uri
        )

    def m_snapshots_create(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.snapshots_create(
            _require(params, "domain"),
            str(params.get("label", "")),
            self.uri,
            description=str(params.get("description", "")),
        )

    def m_snapshots_update(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.snapshots_update(
            _require(params, "domain"),
            _require(params, "layer"),
            label=params.get("label"),
            description=params.get("description"),
            uri=self.uri,
        )

    def m_snapshots_revert(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.snapshots_revert(
            _require(params, "domain"),
            _require(params, "layer"),
            self.uri,
        )

    def m_snapshots_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.snapshots_delete(
            _require(params, "domain"),
            _require(params, "layer"),
            self.uri,
            force=bool(params.get("force", False)),
        )

    def m_clone_create(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.clone_create(
            self._safe_path(
                _require(params, "template"),
                roles=("templates",),
                must_exist=True,
            ),
            self._safe_path(
                _require(params, "dest"), create_parent=True
            ),
            count=int(params.get("count", 1)),
            name=str(params.get("name", "vm-{{n}}")),
            start_at=int(params.get("start_at", 1)),
            define=bool(params.get("define", False)),
            start=bool(params.get("start", False)),
            memory=_optional_int(params, "memory"),
            vcpus=_optional_int(params, "vcpus"),
            network=str(params.get("network", "default")),
            network_config=self.network_config,
            firmware=params.get("firmware"),
            uri=self.uri,
            reset_identity=bool(params.get("reset_identity", False)),
            cloud_init=bool(params.get("cloud_init", False)),
            cloud_init_user=str(params.get("cloud_init_user", "lab")),
            cloud_init_password=params.get("cloud_init_password"),
            cloud_init_ssh_key_file=params.get("cloud_init_ssh_key_file"),
            cloud_init_packages=list(params.get("cloud_init_packages") or []),
            cloud_init_run=list(params.get("cloud_init_run") or []),
            cloud_init_ip=params.get("cloud_init_ip"),
            cloud_init_gateway=params.get("cloud_init_gateway"),
            cloud_init_dns=list(params.get("cloud_init_dns") or []),
            windows_oobe=bool(params.get("windows_oobe", False)),
            windows_admin_password=params.get("windows_admin_password"),
        )

    def m_vm_import_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_import_plan(
            _require(params, "kind"),
            _require(params, "source"),
            _require(params, "name"),
            preset=str(params.get("preset", "generic")),
            network=params.get("network"),
            network_config=self.network_config,
            storage_config=self.config,
            vcpus=params.get("vcpus"),
            memory_mib=params.get("memory_mib"),
            firmware=params.get("firmware"),
            machine=params.get("machine"),
            disk_bus=params.get("disk_bus"),
            nic_model=params.get("nic_model"),
            nic_count=params.get("nic_count"),
            serial_console=params.get("serial_console"),
            data_disk_gb=params.get("data_disk_gb"),
            video=params.get("video"),
            nics=params.get("nics"),
            uri=self.uri,
        )

    def m_vm_import(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_import(
            _require(params, "kind"),
            _require(params, "source"),
            _require(params, "name"),
            preset=str(params.get("preset", "generic")),
            network=params.get("network"),
            network_config=self.network_config,
            storage_config=self.config,
            vcpus=params.get("vcpus"),
            memory_mib=params.get("memory_mib"),
            firmware=params.get("firmware"),
            machine=params.get("machine"),
            disk_bus=params.get("disk_bus"),
            nic_model=params.get("nic_model"),
            nic_count=params.get("nic_count"),
            serial_console=params.get("serial_console"),
            data_disk_gb=params.get("data_disk_gb"),
            video=params.get("video"),
            nics=params.get("nics"),
            start=bool(params.get("start", False)),
            uri=self.uri,
        )

    def m_vm_create(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_create_from_iso(
            _require(params, "name"),
            _require(params, "iso"),
            self._safe_path(
                _require(params, "dest"), create_parent=True
            ),
            disk_size_gb=int(params.get("disk_size_gb", 40)),
            memory_mib=int(params.get("memory", 4096)),
            vcpus=int(params.get("vcpus", 2)),
            network=str(params.get("network", "default")),
            network_config=self.network_config,
            os_type=str(params.get("os_type", "windows")),
            os_variant=str(params.get("os_variant", "")),
            arch=str(params.get("arch", "x86_64")),
            windows_unattend=params.get("windows_unattend"),
            windows_edition=str(params.get("edition", "standard")),
            admin_password=str(
                params.get("admin_password", "Vemu!Lab2026")
            ),
            computer_name=params.get("computer_name"),
            virtio_iso=params.get("virtio_iso"),
            firmware=str(params.get("firmware", "bios")),
            machine=str(params.get("machine", "q35")),
            disk_bus=params.get("disk_bus"),
            iso_bus=params.get("iso_bus"),
            nic_model=params.get("nic_model"),
            video=params.get("video"),
            video_memory_mib=(
                int(params["video_memory_mib"])
                if params.get("video_memory_mib") is not None
                else None
            ),
            hyperv=params.get("hyperv"),
            cpu_mode=str(params.get("cpu_mode", "host-passthrough")),
            tpm=bool(params.get("tpm", False)),
            secure_boot=params.get("secure_boot"),
            start=bool(params.get("start", False)),
            uri=self.uri,
        )

    def m_seal(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.seal_template(
            _require(params, "domain"),
            self._safe_path(
                _require(params, "dest"),
                roles=("templates",),
                create_parent=True,
            ),
            name=params.get("name"),
            description=str(params.get("description", "")),
            sysprepped=bool(params.get("sysprepped", False)),
            shutdown=bool(params.get("shutdown", False)),
            in_place=bool(params.get("in_place", False)),
            accept_bitlocker=bool(params.get("accept_bitlocker", False)),
            uri=self.uri,
        )

    # ------------------------------------------------------------------ chain

    def m_chain_show(self, params: dict[str, Any]) -> dict[str, Any]:
        return ChainManager(_require(params, "chain")).info()

    def m_chain_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        chain = _require(params, "chain")
        service.require_chain_writable(chain, self.uri)
        manager = ChainManager(chain)
        layer = manager.snapshot(str(params.get("label", "")))
        return layer.to_dict()

    def m_chain_revert(self, params: dict[str, Any]) -> dict[str, Any]:
        chain = _require(params, "chain")
        service.require_chain_writable(chain, self.uri)
        manager = ChainManager(chain)
        layer = manager.revert(_require(params, "layer"))
        return layer.to_dict()

    def m_chain_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        chain = _require(params, "chain")
        service.require_chain_writable(chain, self.uri)
        manager = ChainManager(chain)
        manager.delete(
            _require(params, "layer"), force=bool(params.get("force", False))
        )
        return {"deleted": params["layer"]}

    def m_chain_verify(self, params: dict[str, Any]) -> dict[str, Any]:
        manager = ChainManager(_require(params, "chain"))
        issues = manager.verify()
        return {"ok": not issues, "issues": issues}

    def m_chain_recover(self, params: dict[str, Any]) -> dict[str, Any]:
        chain = _require(params, "chain")
        service.require_chain_writable(chain, self.uri)
        return service.chain_recover(chain)

    def m_chain_journals(self, params: dict[str, Any]) -> list[str]:
        return service.chain_journals(_require(params, "root"))

    # ------------------------------------------------------------------ identity

    def m_identity_reset(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.identity_reset(
            _require(params, "chain"),
            _require(params, "hostname"),
            machine_id=params.get("machine_id"),
            dry_run=bool(params.get("dry_run", False)),
        )

    def m_performance_probe(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.performance_probe(
            _require(params, "domain"), self.uri
        )

    def m_performance_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.performance_apply(
            _require(params, "domain"), self.uri
        )

    # ------------------------------------------------------------------ storage

    def m_storage_inspect(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.storage_inspect(
            _require(params, "path"), str(params.get("role", "labs"))
        )

    def m_storage_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.storage_add(
            str(validate_storage_root(_require(params, "path"))),
            str(params.get("role", "labs")),
            config=self.config,
            dry_run=bool(params.get("dry_run", False)),
        )

    def m_storage_repair(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.storage_repair(
            self._safe_path(
                _require(params, "path"),
                roles=("labs", "templates", "isos"),
                must_exist=True,
            ),
            str(params.get("role", "labs")),
            dry_run=bool(params.get("dry_run", False)),
            force_selinux=bool(params.get("force_selinux", False)),
        )

    def m_storage_registry(self, params: dict[str, Any]) -> list[dict[str, str]]:
        return service.storage_registry(self.config)

    def m_storage_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return service.storage_list(self.config)

    def m_storage_remove(self, params: dict[str, Any]) -> dict[str, str]:
        return service.storage_remove(
            _require(params, "path"), self.config
        )

    def m_storage_files(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.storage_files(
            _require(params, "path"),
            config=self.config,
            uri=self.uri,
        )

    def m_storage_delete_file(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.storage_delete_file(
            _require(params, "path"),
            config=self.config,
            uri=self.uri,
        )

    def m_storage_import_file(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.storage_import_file(
            _require(params, "source"),
            _require(params, "location"),
            config=self.config,
        )

    # ------------------------------------------------------------------ networks

    def _safe_path(
        self,
        value: str,
        *,
        roles: tuple[str, ...] = ("labs",),
        must_exist: bool = False,
        create_parent: bool = False,
    ) -> str:
        return str(
            PathPolicy.from_config(self.config).resolve(
                value,
                roles=roles,
                must_exist=must_exist,
                create_parent=create_parent,
            )
        )

    def _safe_lab(self, value: str) -> str:
        spec = lab.load_lab(value)
        PathPolicy.from_config(self.config).resolve(
            spec.dest, roles=("labs",), create_parent=True
        )
        return str(spec.path)

    def _network_config(self, params: dict[str, Any]) -> str | None:
        return self.network_config

    def m_networks_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return service.networks_list(
            self._network_config(params),
            self.uri,
        )

    def m_network_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.network_plan(
            self._network_config(params),
            self.uri,
        )

    def m_network_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.network_add(
            _require(params, "name"),
            str(params.get("mode", "nat")),
            str(params.get("subnet", "auto")),
            bool(params.get("dhcp", True)),
            str(params.get("dns_domain", "vemu.local")),
            str(params.get("description", "")),
            str(params.get("bridge", "")),
            config=self._network_config(params),
        )

    def m_network_bridge_inventory(
        self, _params: dict[str, Any]
    ) -> dict[str, Any]:
        return service.network_bridge_inventory()

    def m_network_bridge_create(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        return service.network_bridge_create(
            _require(params, "name"),
            _require(params, "bridge"),
            _require(params, "uplink"),
            str(params.get("addressing", "dhcp")),
            str(params.get("address", "")),
            str(params.get("gateway", "")),
            str(params.get("dns", "")),
            str(params.get("description", "")),
            config=self._network_config(params),
        )

    def m_network_update(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.network_update(
            _require(params, "name"),
            str(params.get("mode", "nat")),
            str(params.get("subnet", "auto")),
            bool(params.get("dhcp", True)),
            str(params.get("dns_domain", "vemu.local")),
            str(params.get("description", "")),
            config=self._network_config(params),
        )

    def m_network_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.network_remove(
            _require(params, "name"),
            config=self._network_config(params),
            uri=self.uri,
            purge=bool(params.get("purge", False)),
        )

    def m_network_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.network_apply(
            self._network_config(params),
            self.uri,
            dry_run=bool(params.get("dry_run", False)),
        )

    def m_network_restart(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.network_restart(
            _require(params, "name"),
            config=self._network_config(params),
            uri=self.uri,
            force=bool(params.get("force", False)),
        )

    def m_network_leases(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.network_leases(
            _require(params, "name"),
            config=self._network_config(params),
            uri=self.uri,
        )

    def m_network_topology(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.network_topology(
            config=self._network_config(params),
            uri=self.uri,
        )

    def m_network_reservation_add(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        return service.network_reservation_add(
            _require(params, "name"),
            _require(params, "mac"),
            _require(params, "ip"),
            str(params.get("hostname", "")),
            config=self._network_config(params),
        )

    def m_network_reservation_remove(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        return service.network_reservation_remove(
            _require(params, "name"),
            _require(params, "mac"),
            config=self._network_config(params),
        )

    def m_network_port_forward_add(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        return service.network_port_forward_add(
            _require(params, "name"),
            str(params.get("protocol", "tcp")),
            int(params.get("host_port", 0)),
            _require(params, "guest_ip"),
            int(params.get("guest_port", 0)),
            str(params.get("description", "")),
            config=self._network_config(params),
            uri=self.uri,
        )

    def m_network_port_forward_remove(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        return service.network_port_forward_remove(
            _require(params, "name"),
            str(params.get("protocol", "tcp")),
            int(params.get("host_port", 0)),
            config=self._network_config(params),
        )

    def m_network_attach(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.network_attach(
            _require(params, "domain"),
            _require(params, "network"),
            model=str(params.get("model", "virtio")),
            config=self._network_config(params),
            uri=self.uri,
        )

    def m_network_detach(self, params: dict[str, Any]) -> dict[str, str]:
        return service.network_detach(
            _require(params, "domain"),
            _require(params, "mac"),
            self.uri,
        )

    def m_vm_nic_link(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_nic_link(
            _require(params, "domain"),
            _require(params, "mac"),
            bool(params.get("up", False)),
            uri=self.uri,
        )

    def m_vm_nic_network(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.vm_nic_network(
            _require(params, "domain"),
            _require(params, "mac"),
            _require(params, "network"),
            config=self._network_config(params),
            uri=self.uri,
        )

    # --------------------------------------------------------------------- lab

    def m_lab_up(self, params: dict[str, Any]) -> dict[str, Any]:
        return lab.lab_up(
            self._safe_lab(str(params.get("path", "lab.yaml"))),
            uri=self.uri,
            network_config=self._network_config(params),
            start=bool(params.get("start", True)),
            dry_run=bool(params.get("dry_run", False)),
        )

    def m_lab_down(self, params: dict[str, Any]) -> dict[str, Any]:
        return lab.lab_down(
            str(params.get("path", "lab.yaml")),
            uri=self.uri,
            force=bool(params.get("force", False)),
        )

    def m_lab_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return lab.lab_status(
            str(params.get("path", "lab.yaml")),
            uri=self.uri,
            network_config=self._network_config(params),
        )

    def m_lab_export(self, params: dict[str, Any]) -> dict[str, Any]:
        return lab.lab_export(
            str(params.get("path", "lab.yaml")),
            _require(params, "dest"),
            with_templates=bool(params.get("with_templates", False)),
        )

    def m_lab_import(self, params: dict[str, Any]) -> dict[str, Any]:
        return lab.lab_import(
            _require(params, "bundle"),
            self._safe_path(
                _require(params, "dest"), create_parent=True
            ),
            templates_root=params.get("templates_root"),
            overwrite=bool(params.get("overwrite", False)),
        )

    def m_lab_reset(self, params: dict[str, Any]) -> dict[str, Any]:
        return lab.lab_reset(
            self._safe_lab(_require(params, "path")),
            list(params.get("vms") or []),
            force=bool(params.get("force", False)),
            start=bool(params.get("start", True)),
            uri=self.uri,
            network_config=self._network_config(params),
        )

    def m_lab_destroy(self, params: dict[str, Any]) -> dict[str, Any]:
        return lab.lab_destroy(
            self._safe_lab(_require(params, "path")),
            force=bool(params.get("force", False)),
            keep_networks=bool(params.get("keep_networks", False)),
            uri=self.uri,
            network_config=self._network_config(params),
        )

    # ------------------------------------------------------------------ templates

    def m_templates_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return service.templates_list(
            self._safe_path(
                _require(params, "root"),
                roles=("templates",),
                must_exist=True,
            )
        )

    def m_template_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.template_delete(
            self._safe_path(
                _require(params, "template"),
                roles=("templates",),
                must_exist=True,
            ),
            config=self.config,
            uri=self.uri,
        )

    def m_template_import(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.template_import(
            _require(params, "source"),
            self._safe_path(
                _require(params, "dest"),
                roles=("templates",),
                create_parent=True,
            ),
            _require(params, "name"),
            description=str(params.get("description", "")),
            arch=str(params.get("arch", "x86_64")),
            machine=str(params.get("machine", "q35")),
            firmware=str(params.get("firmware", "bios")),
            disk_bus=str(params.get("disk_bus", "virtio")),
            video=str(params.get("video", "virtio")),
            video_memory_mib=_optional_int(params, "video_memory_mib"),
            nic_model=str(params.get("nic_model", "virtio")),
            secure_boot=bool(params.get("secure_boot", False)),
            tpm=bool(params.get("tpm", False)),
            os_family=str(params.get("os_family", "")),
            sysprepped=bool(params.get("sysprepped", False)),
        )

    def m_template_update(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.template_update(
            self._safe_path(
                _require(params, "template"),
                roles=("templates",),
                must_exist=True,
            ),
            os_family=params.get("os_family"),
            sysprepped=params.get("sysprepped"),
        )

    def m_template_export(self, params: dict[str, Any]) -> dict[str, Any]:
        return service.template_export(
            _require(params, "template"),
            _require(params, "dest"),
            format=str(params.get("format", "qcow2")),
        )


def _require(params: dict[str, Any], key: str) -> Any:
    if key not in params or params[key] is None:
        raise ValidationError(f"Missing required parameter: {key}")
    return params[key]


def _optional_int(params: dict[str, Any], key: str) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Parameter '{key}' must be an integer.") from exc


class _Handler(socketserver.StreamRequestHandler):
    def _peer_creds(self) -> tuple[int, int, int] | None:
        try:
            raw = self.connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
        except OSError:
            return None
        pid, uid, gid = struct.unpack("3i", raw)
        return pid, uid, gid

    def handle(self) -> None:
        peer_pid: int | None = None
        creds = self._peer_creds()
        if creds is not None:
            peer_pid, uid, gid = creds
            daemon = self.server.daemon  # type: ignore[attr-defined]
            if not peer_allowed(
                uid, gid, peer_pid, daemon.allowed_gid, daemon.daemon_uid
            ):
                LOG.warning(
                    "denied peer pid=%s uid=%s gid=%s", peer_pid, uid, gid
                )
                self._write(
                    {
                        "id": None,
                        "error": {
                            "type": "PermissionError",
                            "message": "Access denied: user is not in the vemu group.",
                        },
                    }
                )
                return

        while True:
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_REQUEST_BYTES:
                self._write(
                    {
                        "id": None,
                        "error": {
                            "type": "ValidationError",
                            "message": "Request is too large.",
                        },
                    }
                )
                return

            request_id: Any = None
            try:
                request = json.loads(line.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValidationError("Request must be a JSON object.")
                request_id = request.get("id")
                method = request.get("method")
                params = request.get("params") or {}
                daemon = self.server.daemon  # type: ignore[attr-defined]
                if (
                    daemon.polkit
                    and isinstance(method, str)
                    and method not in READ_ONLY_METHODS
                    and peer_pid is not None
                    and not polkit_check(peer_pid)
                ):
                    response = {
                        "id": request_id,
                        "error": {
                            "type": "PermissionError",
                            "message": "polkit authorization failed.",
                        },
                    }
                else:
                    result = daemon.dispatch(method, params)
                    response = {"id": request_id, "result": result}
            except VemuError as exc:
                response = {
                    "id": request_id,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            except Exception as exc:  # noqa: BLE001 - keep the daemon alive
                LOG.error("Unhandled error: %s", exc)
                LOG.debug("%s", traceback.format_exc())
                response = {
                    "id": request_id,
                    "error": {"type": "InternalError", "message": str(exc)},
                }
            self._write(response)

    def _write(self, payload: dict[str, Any]) -> None:
        self.wfile.write(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        )
        self.wfile.flush()


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(
    socket_path: str | Path,
    daemon: VemuDaemon,
    *,
    mode: int = DEFAULT_SOCKET_MODE,
    group: str = "vemu",
) -> _Server:
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.5)
            probe.connect(str(path))
        except OSError:
            path.unlink()
        else:
            raise Conflict(f"A daemon is already listening on {path}.")
        finally:
            probe.close()

    server = _Server(str(path), _Handler)
    server.daemon = daemon  # type: ignore[attr-defined]
    daemon.socket_path = str(path)
    os.chmod(path, mode)
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        gid = None
        LOG.warning("Group '%s' does not exist; socket stays root-owned.", group)
    daemon.allowed_gid = gid
    if os.geteuid() == 0 and gid is not None:
        os.chown(path, 0, gid)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vemud", description="v-EMU daemon")
    parser.add_argument("--socket", default=os.environ.get("VEMU_SOCKET", DEFAULT_SOCKET))
    parser.add_argument("--uri", default="qemu:///system")
    parser.add_argument("--config")
    parser.add_argument("--network-config")
    parser.add_argument("--foreground", action="store_true", help="Run in the foreground.")
    parser.add_argument("--socket-mode", default="0660")
    parser.add_argument("--group", default="vemu")
    parser.add_argument(
        "--polkit",
        action="store_true",
        help="Require polkit authorization for mutating methods.",
    )
    parser.add_argument("--version", action="version", version=f"vemud {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s vemud %(levelname)s %(message)s",
    )
    mode = int(args.socket_mode, 8)
    daemon = VemuDaemon(
        uri=args.uri,
        config=args.config,
        network_config=args.network_config,
        socket_path=args.socket,
        polkit=args.polkit,
    )
    try:
        applied = service.network_apply(args.network_config, args.uri)
        nft = applied.get("nft", {})
        LOG.info(
            "network reconcile: %s profile(s), nft=%s, libvirt-forward=%s",
            len(applied.get("networks", [])),
            nft.get("applied", False),
            nft.get("libvirt", {}).get("applied", False),
        )
    except Exception as exc:  # noqa: BLE001 - daemon must still start
        LOG.warning("network reconcile failed: %s", exc)
    server = make_server(args.socket, daemon, mode=mode, group=args.group)
    daemon.start_event_monitor()

    def stop(*_args: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    LOG.info("listening on %s", args.socket)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            Path(args.socket).unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
