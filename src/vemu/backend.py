"""Thin libvirt helpers.

Only the operations needed by M0 are implemented. Domain definitions are
created by the daemon in the next milestone; this module focuses on reading
state, locating disks, and safe shutdown/start.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_network
from pathlib import Path
from typing import Any

from . import drift
from .virtcheck import domain_type_for
from .errors import Conflict, MutationError, NotFound, ValidationError, VemuError
from .vmconfig import (
    add_usb_controller,
    cdrom_info,
    ensure_nvme_on_root_port,
    ensure_suspend_disabled,
    ensure_tcg_tuning,
    ensure_usb_keyboard,
    find_cdrom,
    parse_capabilities,
    promote_installed_disk,
    set_cdrom_media,
)

_MEMORY_STATS_PERIOD: set[tuple[str, str, int, str]] = set()

STATE_NAMES = {
    0: "unknown",
    1: "running",
    2: "blocked",
    3: "paused",
    4: "shutting down",
    5: "stopped",
    6: "crashed",
    7: "suspended",
}

# A guest disk with this much data is treated as holding an installed OS.
# ESXi 8 writes a small (~700 MiB) bootbank/datastore footprint before its
# first reboot, so a 1 GiB threshold missed it. 512 MiB still ignores an
# empty overlay while catching ESXi, Linux and Windows installs.
INSTALLED_DISK_MIN_BYTES = 512 * 1024**2


def _find_interface(root: ET.Element, mac: str) -> ET.Element:
    wanted = mac.lower()
    for interface in root.findall("./devices/interface"):
        address = interface.find("mac")
        if (
            address is not None
            and (address.get("address") or "").lower() == wanted
        ):
            return interface
    raise NotFound(f"Interface {mac} was not found.")


@dataclass
class DomainInfo:
    name: str
    uuid: str
    state: str
    disks: list[str] = field(default_factory=list)
    autostart: bool = False
    firmware: str = "bios"
    managed_by: str = "External"
    drift: str = "unknown"
    os_type: str = "generic"


def _import_libvirt():
    try:
        import libvirt  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on host packages
        raise NotFound(
            "libvirt Python bindings are not installed. "
            "Install python3-libvirt."
        ) from exc
    return libvirt


def ensure_vnc_websocket(xml: str) -> str:
    """Add an autoport WebSocket to a VNC graphics device when missing."""
    root = ET.fromstring(xml)
    graphics = root.find("./devices/graphics[@type='vnc']")
    if graphics is None or graphics.get("websocket") is not None:
        return xml
    graphics.set("websocket", "-1")
    return ET.tostring(root, encoding="unicode")


def parse_vnc_graphics(xml: str) -> dict[str, object]:
    """Extract the live VNC/WebSocket endpoints from a domain XML."""
    root = ET.fromstring(xml)
    graphics = root.find("./devices/graphics[@type='vnc']")
    if graphics is None:
        raise NotFound("This domain has no VNC console.")
    port = graphics.get("port")
    websocket = graphics.get("websocket")
    if port in {None, "-1"} or websocket in {None, "-1"}:
        raise Conflict(
            "VNC ports are not allocated yet. Is the domain running?"
        )
    try:
        port_number = int(port)
        websocket_number = int(websocket)
    except (TypeError, ValueError) as exc:
        raise Conflict(f"Invalid VNC port in domain XML: {port}/{websocket}") from exc
    host = graphics.get("listen") or "127.0.0.1"
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    return {
        "host": host,
        "port": port_number,
        "websocket": websocket_number,
        "password": graphics.get("passwd"),
    }


class LibvirtBackend:
    def __init__(self, uri: str = "qemu:///system") -> None:
        self.uri = uri
        self._libvirt = _import_libvirt()
        try:
            self.connection = self._libvirt.open(uri)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(f"Cannot connect to libvirt at {uri}: {exc}") from exc
        if self.connection is None:
            raise VemuError(f"Cannot connect to libvirt at {uri}.")

    # ------------------------------------------------------------------ queries

    def list_domains(self) -> list[DomainInfo]:
        domains = self.connection.listAllDomains(0)
        result = [self._describe(domain) for domain in domains]
        return sorted(result, key=lambda item: item.name.lower())

    def domain(self, name: str):
        try:
            return self.connection.lookupByName(name)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise NotFound(f"Domain '{name}' was not found.") from exc

    def info(self, name: str) -> DomainInfo:
        return self._describe(self.domain(name))

    def domain_stats(self, name: str) -> dict[str, Any]:
        domain = self.domain(name)
        raw = domain.info()
        state = STATE_NAMES.get(raw[0], "unknown")
        result: dict[str, Any] = {
            "name": name,
            "state": state,
            "vcpus": int(raw[3]),
            "cpu_time_ns": int(raw[4]),
        }
        if state != "running":
            return result
        if state == "running":
            # Balloon free/usable stats are only refreshed when a collection
            # period is set. Without this, libvirt keeps returning the values
            # sampled shortly after boot. The daemon opens a short-lived
            # backend per RPC, so remember the setting process-wide.
            try:
                domain_id = int(domain.ID())
                uuid = domain.UUIDString()
            except self._libvirt.libvirtError:  # type: ignore[attr-defined]
                domain_id = -1
                uuid = ""
            key = (self.uri, name, domain_id, uuid)
            if domain_id >= 0 and key not in _MEMORY_STATS_PERIOD:
                try:
                    domain.setMemoryStatsPeriod(
                        5, self._libvirt.VIR_DOMAIN_AFFECT_LIVE
                    )
                    _MEMORY_STATS_PERIOD.add(key)
                except self._libvirt.libvirtError:  # type: ignore[attr-defined]
                    pass
        try:
            memory = domain.memoryStats()
        except self._libvirt.libvirtError:  # type: ignore[attr-defined]
            memory = {}
        try:
            cpu = domain.getCPUStats(True)
            if cpu:
                result["cpu_time_ns"] = int(cpu[0].get("cpu_time", raw[4]))
        except self._libvirt.libvirtError:  # type: ignore[attr-defined]
            pass
        for key, output in (
            ("actual", "memory_actual_mib"),
            ("rss", "memory_rss_mib"),
            ("available", "memory_available_mib"),
            ("unused", "memory_unused_mib"),
            ("usable", "memory_usable_mib"),
        ):
            if key in memory:
                result[output] = int(memory[key]) // 1024
        return result

    def apply_resource_profile(
        self, name: str, profile: str | None = None
    ) -> dict[str, Any]:
        """Apply best-effort CPU/IO weights to a managed domain."""
        from .runtime_profile import profile_payload

        payload = profile_payload(profile)
        domain = self.domain(name)
        warnings: list[str] = []
        flags_list = [self._libvirt.VIR_DOMAIN_AFFECT_CONFIG]
        if domain.isActive():
            flags_list.append(self._libvirt.VIR_DOMAIN_AFFECT_LIVE)
        for label, setter, params in (
            (
                "CPU weight",
                domain.setSchedulerParametersFlags,
                {"cpu_shares": int(payload["cpu_shares"])},
            ),
            (
                "IO weight",
                domain.setBlkioParameters,
                {"weight": int(payload["io_weight"])},
            ),
        ):
            for flags in flags_list:
                try:
                    setter(params, flags)
                except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
                    scope = "live" if flags == self._libvirt.VIR_DOMAIN_AFFECT_LIVE else "config"
                    warnings.append(f"{label} ({scope}) not applied: {exc}")
        # Keep the drift baseline aligned with the scheduler attributes we just
        # added; otherwise every VM would report XML drift immediately.
        self._normalize_baseline(domain)
        return {
            "domain": name,
            "profile": payload["profile"],
            "warnings": warnings,
        }

    def runtime_profile_get(self) -> dict[str, Any]:
        from .runtime_profile import profile_payload

        return profile_payload()

    def runtime_profile_set(self, profile: str) -> dict[str, Any]:
        from . import service

        return service.runtime_profile_set(profile, uri=self.uri)

    def xml(self, name: str) -> str:
        return self.domain(name).XMLDesc(0)

    def live_xml(self, name: str) -> str:
        return self.domain(name).XMLDesc(0)

    def snapshot_details(self, name: str) -> list[dict[str, Any]]:
        """Return libvirt snapshot metadata without changing anything."""
        domain = self.domain(name)
        try:
            snapshots = domain.listAllSnapshots()
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(
                f"Cannot list snapshots for '{name}': {exc}"
            ) from exc
        current_name = ""
        current_method = getattr(domain, "snapshotCurrent", None)
        if current_method is not None:
            try:
                current_snapshot = current_method()
                if current_snapshot is not None:
                    current_name = str(current_snapshot.getName())
            except self._libvirt.libvirtError:  # type: ignore[attr-defined]
                current_name = ""
        result: list[dict[str, Any]] = []
        for snapshot in snapshots:
            try:
                root = ET.fromstring(snapshot.getXMLDesc(0))
            except (self._libvirt.libvirtError, ET.ParseError):
                continue
            disks: list[dict[str, str]] = []
            for disk in root.findall("./disks/disk"):
                source = disk.find("source")
                disks.append(
                    {
                        "target": str(disk.get("name", "")),
                        "snapshot": str(disk.get("snapshot", "")),
                        "source": (
                            str(source.get("file", ""))
                            if source is not None
                            else ""
                        ),
                    }
                )
            state_sources: dict[str, str] = {}
            for disk in root.findall("./domain/devices/disk"):
                if disk.get("device") != "disk":
                    continue
                target_element = disk.find("target")
                source_element = disk.find("source")
                if target_element is None or source_element is None:
                    continue
                state_sources[str(target_element.get("dev", ""))] = str(
                    source_element.get("file", "")
                )
            external = any(
                item["snapshot"] == "external" for item in disks
            )
            memory = root.find("memory")
            name = str(root.findtext("name") or snapshot.getName())
            result.append(
                {
                    "name": name,
                    "kind": "external" if external else "internal",
                    "parent": str(root.findtext("parent/name") or ""),
                    "state": str(root.findtext("state") or ""),
                    "created_at": str(root.findtext("creationTime") or ""),
                    "current": "yes" if name == current_name else "",
                    "memory": (
                        str(memory.get("snapshot", "no"))
                        if memory is not None
                        else "no"
                    ),
                    "disks": disks,
                    "state_sources": state_sources,
                }
            )
        return result

    def snapshot_names(self, name: str) -> list[str]:
        domain = self.domain(name)
        try:
            return [snapshot.getName() for snapshot in domain.listAllSnapshots()]
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(
                f"Cannot list snapshots for '{name}': {exc}"
            ) from exc

    def config_xml(self, name: str) -> str:
        domain = self.domain(name)
        flag = getattr(self._libvirt, "VIR_DOMAIN_XML_INACTIVE", 1)
        try:
            return domain.XMLDesc(flag)
        except self._libvirt.libvirtError:  # type: ignore[attr-defined]
            return domain.XMLDesc(0)

    def rename_domain(self, name: str, new_name: str) -> DomainInfo:
        domain = self.domain(name)
        if domain.state()[0] != 5:
            raise Conflict(f"Domain '{name}' must be shut down first.")
        try:
            domain.rename(new_name, 0)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(
                f"Cannot rename domain '{name}' to '{new_name}': {exc}"
            ) from exc
        return self._describe(self.domain(new_name))

    def redefine_domain(self, name: str, xml: str) -> DomainInfo:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise ValidationError(f"Invalid domain XML: {exc}") from exc
        xml_name = root.findtext("name")
        if xml_name != name:
            raise MutationError(
                f"Domain XML name '{xml_name}' does not match '{name}'."
            )
        current = self.config_xml(name)
        self._backup_xml(name, current, reason="redefine")
        xml = drift.set_baseline(xml)
        try:
            domain = self.connection.defineXML(xml)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(f"Cannot update domain '{name}': {exc}") from exc
        if domain is None:
            raise VemuError(f"libvirt refused to update domain '{name}'.")
        domain = self._normalize_baseline(domain)
        return self._describe(domain)

    def _normalize_baseline(self, domain):
        """Recompute the baseline from libvirt's normalized inactive XML."""
        flag = getattr(self._libvirt, "VIR_DOMAIN_XML_INACTIVE", 1)
        try:
            actual = domain.XMLDesc(flag)
        except self._libvirt.libvirtError:  # type: ignore[attr-defined]
            return domain
        if drift.baseline(actual) is None or drift.status(actual) == "clean":
            drift.store_baseline(domain.name(), actual)
            return domain
        fixed = drift.set_baseline(actual)
        try:
            updated = self.connection.defineXML(fixed)
        except self._libvirt.libvirtError:  # type: ignore[attr-defined]
            return domain
        drift.store_baseline(domain.name(), fixed)
        return updated or domain

    @staticmethod
    def _backup_root() -> Path:
        configured = os.environ.get("VEMU_BACKUP_DIR")
        if configured:
            return Path(configured).expanduser()
        if os.geteuid() == 0:
            return Path("/var/lib/vemu/backups")
        return Path.home() / ".local" / "state" / "vemu" / "backups"

    def _backup_xml(
        self, name: str, xml: str, *, reason: str = "edit"
    ) -> Path:
        directory = self._backup_root() / name
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%S%fZ"
            )
            path = directory / f"{stamp}-{reason}.xml"
            path.write_text(xml, encoding="utf-8")
            backups = sorted(directory.glob("*.xml"))
            for old in backups[:-40]:
                old.unlink(missing_ok=True)
            return path
        except OSError as exc:
            raise VemuError(
                f"Cannot back up domain '{name}' before edit: {exc}"
            ) from exc

    def capabilities(
        self,
        *,
        machine: str | None = None,
        arch: str = "x86_64",
        virttype: str | None = None,
    ) -> dict[str, Any]:
        if virttype is None:
            virttype = domain_type_for(arch)
        try:
            domain_caps = self.connection.getDomainCapabilities(
                None, arch, machine, virttype
            )
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(f"Cannot read domain capabilities: {exc}") from exc
        caps = self.connection.getCapabilities()
        return parse_capabilities(domain_caps, caps, arch=arch)

    def interfaces(self, name: str) -> list[dict[str, object]]:
        root = ET.fromstring(self.xml(name))
        result: list[dict[str, object]] = []
        for interface in root.findall("./devices/interface"):
            mac = interface.find("mac")
            source = interface.find("source")
            model = interface.find("model")
            target = interface.find("target")
            result.append(
                {
                    "mac": mac.get("address") if mac is not None else "",
                    "type": interface.get("type", ""),
                    "network": (
                        source.get("network") if source is not None else None
                    ),
                    "bridge": (
                        source.get("bridge") if source is not None else None
                    ),
                    "model": (
                        model.get("type") if model is not None else None
                    ),
                    "target": (
                        target.get("dev") if target is not None else None
                    ),
                }
            )
        return result

    def define(self, xml: str) -> DomainInfo:
        """Define a new domain from XML. Existing names are rejected."""
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise ValidationError(f"Invalid domain XML: {exc}") from exc
        name = root.findtext("name")
        if not name:
            raise ValidationError("Domain XML is missing <name>.")
        existing = {domain.name() for domain in self.connection.listAllDomains(0)}
        if name in existing:
            raise Conflict(f"Domain '{name}' already exists.")

        xml = drift.set_baseline(xml)
        try:
            domain = self.connection.defineXML(xml)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(f"Cannot define domain '{name}': {exc}") from exc
        if domain is None:
            raise VemuError(f"libvirt refused to define domain '{name}'.")
        domain = self._normalize_baseline(domain)
        return self._describe(domain)

    def undefine(self, name: str, *, force: bool = False) -> None:
        domain = self.domain(name)
        if domain.state()[0] != 5:
            if not force:
                raise Conflict(f"Domain '{name}' must be shut down first.")
            try:
                domain.destroy()
            except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
                raise VemuError(
                    f"Cannot stop domain '{name}': {exc}"
                ) from exc
        nvram = getattr(self._libvirt, "VIR_DOMAIN_UNDEFINE_NVRAM", 0)
        try:
            if nvram and hasattr(domain, "undefineFlags"):
                domain.undefineFlags(nvram)
            else:
                domain.undefine()
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            if nvram:
                try:
                    domain.undefine()
                    return
                except self._libvirt.libvirtError:
                    pass
            raise VemuError(f"Cannot undefine domain '{name}': {exc}") from exc

    def used_macs(self) -> set[str]:
        macs: set[str] = set()
        for domain in self.connection.listAllDomains(0):
            root = ET.fromstring(domain.XMLDesc(0))
            for mac in root.findall("./devices/interface/mac"):
                address = mac.get("address")
                if address:
                    macs.add(address.lower())
        return macs

    def unique_mac(self) -> str:
        from .domain import random_mac

        used = self.used_macs()
        for _ in range(50):
            candidate = random_mac()
            if candidate not in used:
                return candidate
        raise Conflict("Could not generate a unique MAC address.")

    def state(self, name: str) -> str:
        domain = self.domain(name)
        return STATE_NAMES.get(domain.state()[0], "unknown")

    def autostart(self, name: str) -> bool:
        return bool(self.domain(name).autostart())

    def set_autostart(self, name: str, enabled: bool) -> None:
        try:
            self.domain(name).setAutostart(1 if enabled else 0)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(
                f"Cannot set autostart for '{name}': {exc}"
            ) from exc

    def disk_paths(self, name: str) -> list[str]:
        return self.info(name).disks

    def console_info(self, name: str) -> dict[str, object]:
        domain = self.domain(name)
        state = STATE_NAMES.get(domain.state()[0], "unknown")
        if state not in {"running", "paused", "suspended"}:
            raise Conflict(f"Domain '{name}' is not active.")
        live_xml = domain.XMLDesc(0)
        root = ET.fromstring(live_xml)
        has_serial = bool(
            root.findall("./devices/serial")
            or root.findall("./devices/console")
        )
        try:
            info = parse_vnc_graphics(live_xml)
        except Conflict:
            # Older/imported domains may have no websocket attribute at all.
            # Add it to the persistent definition so the next full restart
            # allocates a port, and explain that a restart is required.
            config = self.config_xml(name)
            updated = ensure_vnc_websocket(config)
            if updated != config:
                self.redefine_domain(name, updated)
                raise Conflict(
                    "VNC WebSocket was added to this VM definition. "
                    "Restart the VM once (full stop/start, not a guest "
                    "reboot), then open the console again."
                ) from None
            raise
        return {"domain": name, **info, "serial": has_serial}

    def serial_info(self, name: str) -> dict[str, object]:
        domain = self.domain(name)
        if domain.state()[0] != 1:
            raise Conflict(f"Domain '{name}' is not running.")
        root = ET.fromstring(domain.XMLDesc(0))
        has_serial = bool(
            root.findall("./devices/serial")
            or root.findall("./devices/console")
        )
        if not has_serial:
            raise NotFound(
                f"Domain '{name}' has no serial/console device."
            )
        return {"domain": name, "serial": True}

    def qemu_agent_command(
        self, name: str, command: dict, *, timeout: int = 30
    ) -> object:
        virsh = shutil.which("virsh")
        if not virsh:
            raise NotFound("virsh was not found; it is required for QGA access.")
        try:
            result = subprocess.run(
                [
                    virsh,
                    "-c",
                    self.uri,
                    "qemu-agent-command",
                    name,
                    "--timeout",
                    str(max(1, int(timeout))),
                    json.dumps(command),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise VemuError(
                f"Guest agent command timed out for '{name}'."
            ) from exc
        if result.returncode != 0:
            raise VemuError(
                f"Guest agent command failed for '{name}': "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise VemuError(
                f"Invalid guest agent response: {result.stdout.strip()}"
            ) from exc
        if isinstance(payload, dict) and "error" in payload:
            error = payload["error"]
            message = (
                error.get("desc") if isinstance(error, dict) else str(error)
            )
            raise VemuError(f"Guest agent error: {message}")
        if isinstance(payload, dict) and "return" in payload:
            return payload["return"]
        return payload

    # ------------------------------------------------------------------ USB

    @staticmethod
    def _usb_attached_set(xml: str) -> set[tuple[str, str]]:
        root = ET.fromstring(xml)
        attached: set[tuple[str, str]] = set()
        for hostdev in root.findall("./devices/hostdev[@type='usb']"):
            source = hostdev.find("source")
            if source is None:
                continue
            vendor = source.find("vendor")
            product = source.find("product")
            if vendor is not None and product is not None:
                attached.add(
                    (
                        str(vendor.get("id", "")).lower(),
                        str(product.get("id", "")).lower(),
                    )
                )
        return attached

    def _usb_host_devices(self) -> list[Any]:
        try:
            return list(self.connection.listAllDevices(0))
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            text = str(exc)
            if "virtnodedevd" in text or "Failed to connect socket" in text:
                raise Conflict(
                    "libvirt USB device service is unavailable. "
                    "Run `vemu doctor`, then on modular libvirt start "
                    "virtnodedevd.socket (for example: "
                    "sudo systemctl start virtnodedevd.socket). "
                    "Do not run libvirtd and virtqemud at the same time."
                ) from exc
            raise VemuError(f"Cannot list USB devices: {exc}") from exc

    def _usb_present_ids(self) -> set[tuple[str, str]]:
        present: set[tuple[str, str]] = set()
        for device in self._usb_host_devices():
            try:
                root = ET.fromstring(device.XMLDesc(0))
            except (ET.ParseError, self._libvirt.libvirtError):
                continue
            capability = root.find("capability[@type='usb_device']")
            if capability is None:
                continue
            vendor = capability.find("vendor")
            product = capability.find("product")
            if vendor is not None and product is not None:
                present.add(
                    (
                        str(vendor.get("id", "")).lower(),
                        str(product.get("id", "")).lower(),
                    )
                )
        return present

    def usb_devices(self, name: str | None = None) -> list[dict[str, Any]]:
        """Return USB host devices, optionally marking those passed to *name*."""
        attached = (
            self._usb_attached_set(self.config_xml(name))
            if name is not None
            else set()
        )
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for device in self._usb_host_devices():
            try:
                root = ET.fromstring(device.XMLDesc(0))
            except (ET.ParseError, self._libvirt.libvirtError):
                continue
            capability = root.find("capability[@type='usb_device']")
            if capability is None:
                continue
            vendor_element = capability.find("vendor")
            product_element = capability.find("product")
            if vendor_element is None or product_element is None:
                continue
            vendor = str(vendor_element.get("id", "")).lower()
            product = str(product_element.get("id", "")).lower()
            if not vendor or not product or vendor == "0x1d6b":
                # Root hubs are not useful passthrough targets.
                continue
            seen.add((vendor, product))
            description = (product_element.text or "").strip()
            is_hub = "hub" in description.lower()
            is_hid = False
            path_text = root.findtext("path", "")
            if path_text:
                sys_path = Path(path_text)
                try:
                    for interface in sys_path.glob("*/bInterfaceClass"):
                        if interface.read_text().strip().lower() in {
                            "03",
                            "3",
                        }:
                            is_hid = True
                            break
                    device_class = sys_path / "bDeviceClass"
                    if device_class.is_file():
                        is_hub = (
                            device_class.read_text().strip().lower()
                            in {"09", "9"}
                        ) or is_hub
                except OSError:
                    pass
            if not is_hid:
                lowered = description.lower()
                is_hid = any(
                    word in lowered
                    for word in ("mouse", "keyboard", "gaming kb", " hid")
                )
            if is_hid or is_hub:
                # Input devices stay on the host; hubs are not useful for
                # passthrough and only clutter the picker.
                continue
            result.append(
                {
                    "name": device.name(),
                    "vendor": vendor,
                    "product": product,
                    "description": description or "USB device",
                    "vendor_name": (vendor_element.text or "").strip(),
                    "bus": capability.findtext("bus", ""),
                    "device": capability.findtext("device", ""),
                    "attached": (vendor, product) in attached,
                    "present": True,
                    "missing": False,
                    "hub": is_hub,
                }
            )
        for vendor, product in sorted(attached - seen):
            result.append(
                {
                    "name": "",
                    "vendor": vendor,
                    "product": product,
                    "description": "Missing USB device",
                    "vendor_name": "Not connected",
                    "bus": "",
                    "device": "",
                    "attached": True,
                    "present": False,
                    "missing": True,
                    "hub": False,
                }
            )
        result.sort(
            key=lambda item: (
                0 if item.get("present", True) else 1,
                int(item["bus"] or 0),
                int(item["device"] or 0),
                str(item["description"]),
            )
        )
        return result

    def missing_usb_devices(self, name: str) -> list[dict[str, str]]:
        """List configured USB hostdevs whose device is no longer present."""
        configured = self._usb_attached_set(self.config_xml(name))
        if not configured:
            return []
        try:
            present = self._usb_present_ids()
        except VemuError:
            # Cannot verify; let libvirt report the state on start.
            return []
        return [
            {"vendor": vendor, "product": product}
            for vendor, product in sorted(configured - present)
        ]

    def _remove_usb_hostdevs(
        self, name: str, wanted: set[tuple[str, str]]
    ) -> list[dict[str, str]]:
        if not wanted:
            return []
        root = ET.fromstring(self.config_xml(name))
        devices = root.find("devices")
        if devices is None:
            return []
        removed: list[dict[str, str]] = []
        for hostdev in list(devices.findall("hostdev[@type='usb']")):
            source = hostdev.find("source")
            if source is None:
                continue
            vendor_element = source.find("vendor")
            product_element = source.find("product")
            if vendor_element is None or product_element is None:
                continue
            key = (
                str(vendor_element.get("id", "")).lower(),
                str(product_element.get("id", "")).lower(),
            )
            if key in wanted:
                devices.remove(hostdev)
                removed.append({"vendor": key[0], "product": key[1]})
        if removed:
            self.redefine_domain(
                name, ET.tostring(root, encoding="unicode")
            )
        return removed

    def drop_missing_usb_devices(self, name: str) -> list[dict[str, str]]:
        """Remove stale USB hostdevs from the persistent domain definition."""
        missing = self.missing_usb_devices(name)
        wanted = {(item["vendor"], item["product"]) for item in missing}
        return self._remove_usb_hostdevs(name, wanted)

    def attach_usb(self, name: str, vendor: str, product: str) -> dict[str, Any]:
        vendor = vendor.lower()
        product = product.lower()
        if not vendor.startswith("0x") or not product.startswith("0x"):
            raise ValidationError("USB vendor/product IDs must be 0x-prefixed.")
        domain = self.domain(name)
        state = domain.state()[0]
        config = self.config_xml(name)
        if (vendor, product) in self._usb_attached_set(config):
            raise Conflict(
                f"USB device {vendor}:{product} is already attached to '{name}'."
            )
        if (vendor, product) not in self._usb_present_ids():
            raise NotFound(
                f"USB device {vendor}:{product} is not connected to this host."
            )
        root = ET.fromstring(config)
        has_controller = (
            root.find("./devices/controller[@type='usb']") is not None
        )
        if not has_controller:
            self.redefine_domain(name, add_usb_controller(config))
        hostdev = ET.Element(
            "hostdev", mode="subsystem", type="usb", managed="yes"
        )
        source = ET.SubElement(hostdev, "source")
        ET.SubElement(source, "vendor", id=vendor)
        ET.SubElement(source, "product", id=product)
        xml = ET.tostring(hostdev, encoding="unicode")
        flags = self._libvirt.VIR_DOMAIN_AFFECT_CONFIG
        live = False
        warning = None
        if state == 1 and has_controller:
            try:
                domain.attachDeviceFlags(
                    xml, flags | self._libvirt.VIR_DOMAIN_AFFECT_LIVE
                )
                live = True
            except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
                warning = (
                    f"Live attach failed ({exc}); saved for the next start."
                )
                domain.attachDeviceFlags(xml, flags)
        else:
            domain.attachDeviceFlags(xml, flags)
            if state == 1:
                warning = (
                    "A USB controller was added; the device is saved for the "
                    "next start."
                )
        return {
            "domain": name,
            "vendor": vendor,
            "product": product,
            "live": live,
            "pending_restart": state == 1 and not live,
            "warning": warning,
        }

    def detach_usb(
        self, name: str, vendor: str, product: str
    ) -> dict[str, Any]:
        vendor = vendor.lower()
        product = product.lower()
        domain = self.domain(name)
        config = self.config_xml(name)
        root = ET.fromstring(config)
        target = None
        for hostdev in root.findall("./devices/hostdev[@type='usb']"):
            source = hostdev.find("source")
            if source is None:
                continue
            vendor_element = source.find("vendor")
            product_element = source.find("product")
            if (
                vendor_element is not None
                and product_element is not None
                and str(vendor_element.get("id", "")).lower() == vendor
                and str(product_element.get("id", "")).lower() == product
            ):
                target = hostdev
                break
        if target is None:
            raise NotFound(
                f"USB device {vendor}:{product} is not attached to '{name}'."
            )
        flags = self._libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if domain.state()[0] == 1:
            try:
                domain.detachDeviceFlags(
                    ET.tostring(target, encoding="unicode"),
                    flags | self._libvirt.VIR_DOMAIN_AFFECT_LIVE,
                )
            except self._libvirt.libvirtError:
                # The device may exist only in the persistent definition
                # (for example after a controller-less live attach).
                domain.detachDeviceFlags(
                    ET.tostring(target, encoding="unicode"), flags
                )
        else:
            domain.detachDeviceFlags(
                ET.tostring(target, encoding="unicode"), flags
            )
        return {"domain": name, "vendor": vendor, "product": product}

    def _bootmenu_xml(
        self, xml: str, enabled: bool, timeout_ms: int
    ) -> str:
        root = ET.fromstring(xml)
        os_element = root.find("os")
        if os_element is None:
            raise MutationError("Domain XML has no <os>.")
        bootmenu = os_element.find("bootmenu")
        if not enabled:
            if bootmenu is not None:
                os_element.remove(bootmenu)
        else:
            if bootmenu is None:
                bootmenu = ET.Element("bootmenu")
                index = 0
                for position, child in enumerate(list(os_element)):
                    if child.tag in {
                        "type",
                        "loader",
                        "nvram",
                        "kernel",
                        "initrd",
                        "cmdline",
                    }:
                        index = position + 1
                os_element.insert(index, bootmenu)
            bootmenu.set("enable", "yes")
            bootmenu.set("timeout", str(timeout_ms))
        return ET.tostring(root, encoding="unicode")

    def boot_menu(self, name: str, timeout_ms: int = 5000) -> dict[str, Any]:
        """Start/restart a VM and open the SeaBIOS boot menu."""
        info = self.info(name)
        self.redefine_domain(
            name,
            self._bootmenu_xml(
                self.config_xml(name), True, max(timeout_ms, 10000)
            ),
        )
        try:
            if info.state == "running":
                self.shutdown(name)
            self.start(name)
            # SeaBIOS displays the boot menu after Esc during the splash.
            time.sleep(2.0)
            domain = self.domain(name)
            keycodes = getattr(
                self._libvirt, "VIR_KEYCODE_SET_LINUX", 0
            )
            try:
                domain.sendKey(keycodes, 50, [1], 1)  # Esc
            except self._libvirt.libvirtError:
                pass
        finally:
            try:
                self.redefine_domain(
                    name,
                    self._bootmenu_xml(
                        self.config_xml(name), False, timeout_ms
                    ),
                )
            except (VemuError, ET.ParseError):
                pass
        return {
            "domain": name,
            "boot_menu": True,
            "state": self.info(name).state,
        }

    def boot_firmware(self, name: str) -> dict[str, Any]:
        """Start a UEFI VM paused and send the firmware-setup key.

        This is the host-side fallback used when guest agents block
        ``guest-exec``. OVMF shows its setup UI on Esc/F2 during POST.
        """
        info = self.info(name)
        if info.firmware != "efi":
            raise ValidationError(
                "Firmware setup is only available for UEFI VMs."
            )
        if info.state == "running":
            self.shutdown(name)
        domain = self.domain(name)
        paused = getattr(self._libvirt, "VIR_DOMAIN_START_PAUSED", 1)
        try:
            domain.createWithFlags(paused)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(f"Cannot start '{name}' paused: {exc}") from exc
        time.sleep(0.2)
        try:
            domain.resume()
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(f"Cannot resume '{name}': {exc}") from exc
        keycodes = getattr(self._libvirt, "VIR_KEYCODE_SET_LINUX", 0)
        # Open the persistent Boot Manager Menu and leave it there. Extra F2
        # presses are harmless once the menu is open; unlike the front-page
        # countdown, the Boot Manager Menu waits for the user indefinitely.
        # TCG POST is much slower than KVM (especially on aarch64 with TPM),
        # so keep sending F2 long enough for the menu to appear.
        is_tcg = "type='qemu'" in (domain.XMLDesc(0) or "")
        presses = 90 if is_tcg else 20
        time.sleep(0.2)
        for _ in range(presses):
            try:
                domain.sendKey(keycodes, 50, [60], 1)  # F2
            except self._libvirt.libvirtError:
                pass
            time.sleep(0.5)
        return {
            "domain": name,
            "firmware": "efi",
            "state": self.info(name).state,
        }

    # ------------------------------------------------------------------ networks

    def _network_info(self, network) -> dict[str, object]:
        root = ET.fromstring(network.XMLDesc(0))
        bridge = root.find("bridge")
        ip_element = root.find("ip")
        subnet = None
        if ip_element is not None:
            address = ip_element.get("address")
            prefix = ip_element.get("prefix") or "24"
            if address:
                try:
                    subnet = str(
                        ip_network(f"{address}/{prefix}", strict=False)
                    )
                except ValueError:
                    subnet = None
        dhcp = root.find("./ip/dhcp") is not None
        domain = root.find("domain")
        return {
            "name": network.name(),
            "uuid": network.UUIDString(),
            "active": bool(network.isActive()),
            "autostart": bool(network.autostart()),
            "bridge": bridge.get("name") if bridge is not None else None,
            "subnet": subnet,
            "dhcp": dhcp,
            "dns_domain": domain.get("name") if domain is not None else None,
            "forward": (
                root.find("forward").get("mode", "isolated")
                if root.find("forward") is not None
                else "isolated"
            ),
        }

    def list_networks(self) -> list[dict[str, object]]:
        networks = [
            self._network_info(network)
            for network in self.connection.listAllNetworks(0)
        ]
        return sorted(networks, key=lambda item: str(item["name"]).lower())

    def network(self, name: str):
        try:
            return self.connection.networkLookupByName(name)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise NotFound(f"Network '{name}' was not found.") from exc

    def define_network(self, xml: str) -> dict[str, object]:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise ValidationError(f"Invalid network XML: {exc}") from exc
        name = root.findtext("name")
        if not name:
            raise ValidationError("Network XML is missing <name>.")
        try:
            existing = self.connection.networkLookupByName(name)
        except self._libvirt.libvirtError:  # type: ignore[attr-defined]
            existing = None
        if existing is not None and root.find("uuid") is None:
            uuid = ET.Element("uuid")
            uuid.text = existing.UUIDString()
            root.insert(1, uuid)
            xml = ET.tostring(root, encoding="unicode")
        try:
            network = self.connection.networkDefineXML(xml)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(f"Cannot define network: {exc}") from exc
        if network is None:
            raise VemuError("libvirt refused to define the network.")
        return self._network_info(network)

    def start_network(self, name: str, autostart: bool = True) -> dict[str, object]:
        network = self.network(name)
        if not network.isActive():
            try:
                network.create()
            except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
                raise VemuError(f"Cannot start network '{name}': {exc}") from exc
        if autostart:
            network.setAutostart(1)
        return self._network_info(network)

    def dhcp_leases(self, name: str) -> list[dict[str, object]]:
        network = self.network(name)
        if not network.isActive():
            return []
        try:
            leases = network.DHCPLeases()
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(f"Cannot read DHCP leases for '{name}': {exc}") from exc
        result: list[dict[str, object]] = []
        for lease in leases or []:
            result.append(
                {
                    "mac": str(lease.get("mac", "")).lower(),
                    "ip": str(lease.get("ipaddr", "")),
                    "hostname": str(lease.get("hostname", "")),
                    "client_id": str(lease.get("clientid", "")),
                    "expires": lease.get("expirytime"),
                    "type": lease.get("type"),
                }
            )
        return result

    def stop_network(self, name: str, *, undefine: bool = False) -> None:
        network = self.network(name)
        if network.isActive():
            try:
                network.destroy()
            except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
                raise VemuError(f"Cannot stop network '{name}': {exc}") from exc
        if undefine:
            try:
                network.undefine()
            except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
                raise VemuError(f"Cannot undefine network '{name}': {exc}") from exc

    def attach_interface(
        self,
        name: str,
        network_name: str,
        mac: str,
        *,
        model: str = "virtio",
    ) -> None:
        domain = self.domain(name)
        interface = ET.Element("interface", type="network")
        ET.SubElement(interface, "source", network=network_name)
        ET.SubElement(interface, "mac", address=mac)
        ET.SubElement(interface, "model", type=model)
        xml = ET.tostring(interface, encoding="unicode")
        flags = self._libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if domain.state()[0] == 1:
            flags |= self._libvirt.VIR_DOMAIN_AFFECT_LIVE
        try:
            domain.attachDeviceFlags(xml, flags)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(f"Cannot attach interface: {exc}") from exc

    def set_interface_link(
        self, name: str, mac: str, *, up: bool
    ) -> None:
        """Connect or disconnect a NIC (live when the VM is running)."""
        domain = self.domain(name)
        running = domain.state()[0] == 1
        root = ET.fromstring(
            domain.XMLDesc(0)
            if running
            else domain.XMLDesc(self._libvirt.VIR_DOMAIN_XML_INACTIVE)
        )
        interface = _find_interface(root, mac)
        link = interface.find("link")
        if link is None:
            link = ET.SubElement(interface, "link")
        link.set("state", "up" if up else "down")
        xml = ET.tostring(interface, encoding="unicode")
        flags = self._libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if running:
            flags |= self._libvirt.VIR_DOMAIN_AFFECT_LIVE
        try:
            domain.updateDeviceFlags(xml, flags)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(
                f"Cannot {'connect' if up else 'disconnect'} NIC {mac} on "
                f"'{name}': {exc}"
            ) from exc

    def set_interface_network(
        self, name: str, mac: str, network_name: str
    ) -> None:
        """Change the libvirt network behind a NIC (VM must be stopped)."""
        domain = self.domain(name)
        if domain.state()[0] == 1:
            raise Conflict(
                "Stop the VM before changing its network."
            )
        root = ET.fromstring(self.config_xml(name))
        interface = _find_interface(root, mac)
        if interface.get("type") not in {"network", "bridge"}:
            raise ValidationError(
                "Only network- or bridge-backed interfaces can change their "
                "network."
            )
        # An imported/older clone may use the host bridge directly. Convert it
        # to a libvirt network so it follows the same v-EMU profile (and DHCP)
        # as the template/source VM.
        interface.set("type", "network")
        source = interface.find("source")
        if source is None:
            source = ET.SubElement(interface, "source")
        source.attrib.clear()
        source.set("network", network_name)
        target = interface.find("target")
        if target is not None:
            interface.remove(target)
        self.redefine_domain(name, ET.tostring(root, encoding="unicode"))

    def set_memory_live(self, name: str, memory_mib: int) -> None:
        domain = self.domain(name)
        root = ET.fromstring(domain.XMLDesc(0))
        if root.find("./devices/memballoon") is None:
            raise VemuError(
                "This VM has no memory balloon device; add one or restart "
                "with the new memory."
            )
        try:
            domain.setMemoryFlags(
                memory_mib * 1024, self._libvirt.VIR_DOMAIN_AFFECT_LIVE
            )
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(
                f"Cannot adjust memory on '{name}': {exc}"
            ) from exc

    def set_vcpus_live(self, name: str, vcpus: int) -> None:
        """Hotplug vCPUs into a running guest.

        The domain must have been defined with a maximum vCPU count larger
        than the current count; libvirt/QEMU exposes the extra CPUs through
        ACPI hotplug.
        """
        domain = self.domain(name)
        try:
            domain.setVcpusFlags(
                vcpus, self._libvirt.VIR_DOMAIN_AFFECT_LIVE
            )
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(
                f"Cannot hotplug vCPUs on '{name}': {exc}. The boot maximum "
                "must be higher than the current count."
            ) from exc

    def block_resize(self, name: str, target: str, size_bytes: int) -> None:
        domain = self.domain(name)
        flag = getattr(
            self._libvirt, "VIR_DOMAIN_BLOCK_RESIZE_BYTES", 1
        )
        try:
            domain.blockResize(target, size_bytes, flag)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(
                f"Cannot resize disk {target} on '{name}': {exc}. "
                "Shut the VM down and retry, or check the disk bus."
            ) from exc

    def update_media(
        self,
        name: str,
        path: str | None,
        target: str | None = None,
        *,
        force: bool = False,
    ) -> dict[str, str | bool | None]:
        """Change or eject a CD/DVD medium (live when the VM is running)."""
        domain = self.domain(name)
        running = domain.state()[0] == 1
        root = ET.fromstring(
            domain.XMLDesc(0) if running else domain.XMLDesc(
                self._libvirt.VIR_DOMAIN_XML_INACTIVE
            )
        )
        disk = find_cdrom(root, target)
        before = cdrom_info(disk)
        set_cdrom_media(disk, path)
        flags = self._libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if running:
            flags |= self._libvirt.VIR_DOMAIN_AFFECT_LIVE
            if force:
                flags |= getattr(
                    self._libvirt, "VIR_DOMAIN_DEVICE_MODIFY_FORCE", 4
                )
        try:
            domain.updateDeviceFlags(
                ET.tostring(disk, encoding="unicode"), flags
            )
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            action = "eject" if path is None else "change"
            hint = (
                " Use Force eject to override a locked tray."
                if path is None and not force
                else (
                    " Use Force change to override a locked tray."
                    if path is not None and not force
                    else ""
                )
            )
            raise VemuError(
                f"Cannot {action} media on '{name}': {exc}. "
                "Eject it from inside the guest, shut the VM down, or retry."
                f"{hint}"
            ) from exc
        return {
            **before,
            "source": path,
            "ejected": path is None,
            "live": running,
            "forced": force,
        }

    def detach_interface(self, name: str, mac: str) -> None:
        domain = self.domain(name)
        root = ET.fromstring(domain.XMLDesc(0))
        target = None
        for interface in root.findall("./devices/interface"):
            element = interface.find("mac")
            if element is not None and element.get("address", "").lower() == mac.lower():
                target = interface
                break
        if target is None:
            raise NotFound(f"Interface {mac} was not found on '{name}'.")
        flags = self._libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if domain.state()[0] == 1:
            flags |= self._libvirt.VIR_DOMAIN_AFFECT_LIVE
        try:
            domain.detachDeviceFlags(
                ET.tostring(target, encoding="unicode"), flags
            )
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(f"Cannot detach interface: {exc}") from exc

    def update_disk_source(
        self,
        name: str,
        new_path: str,
        old_path: str | None = None,
        *,
        allow_running: bool = False,
    ) -> DomainInfo:
        """Point the VM's first disk at a new overlay.

        When ``allow_running`` is true only the inactive definition is updated,
        which is what a live snapshot needs after QEMU has pivoted.
        """
        domain = self.domain(name)
        state = domain.state()[0]
        if state != 5 and not allow_running:
            raise Conflict(f"Domain '{name}' must be shut down first.")
        flags = 1 if allow_running else 0
        root = ET.fromstring(domain.XMLDesc(flags))
        updated = False
        for disk in root.findall("./devices/disk"):
            if disk.get("device") != "disk":
                continue
            source = disk.find("source")
            if source is None:
                continue
            current = source.get("file")
            if old_path is not None and current != old_path:
                continue
            source.set("file", new_path)
            updated = True
            break
        if not updated:
            raise NotFound(
                f"No disk matching {old_path or 'the first disk'} on '{name}'."
            )
        return self.redefine_domain(
            name, ET.tostring(root, encoding="unicode")
        )

    def update_disk_sources(
        self,
        name: str,
        changes: dict[str, str],
        *,
        allow_running: bool = False,
    ) -> DomainInfo:
        """Update several data disk sources in one XML redefine."""
        if not changes:
            return self.info(name)
        domain = self.domain(name)
        state = domain.state()[0]
        if state != 5 and not allow_running:
            raise Conflict(f"Domain '{name}' must be shut down first.")
        flags = 1 if allow_running else 0
        root = ET.fromstring(domain.XMLDesc(flags))
        seen: set[str] = set()
        for disk in root.findall("./devices/disk"):
            if disk.get("device") != "disk":
                continue
            source = disk.find("source")
            if source is None:
                continue
            current = source.get("file")
            if current in changes:
                source.set("file", changes[current])
                seen.add(current)
        missing = set(changes) - seen
        if missing:
            raise NotFound(
                "No disk source matched: " + ", ".join(sorted(missing))
            )
        defined = self.redefine_domain(
            name, ET.tostring(root, encoding="unicode")
        )
        return defined

    # ------------------------------------------------------------------ actions

    def block_info(self, name: str, target: str) -> dict[str, int]:
        """Return capacity/allocation/physical for a domain disk."""
        domain = self.domain(name)
        try:
            info = domain.blockInfo(target, 0)
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(
                f"Cannot read block info for '{target}' of '{name}': {exc}"
            ) from exc
        return {
            "capacity": int(info[0]),
            "allocation": int(info[1]),
            "physical": int(info[2]),
        }

    def installed_disk_target(self, name: str) -> str | None:
        """Return the primary disk target when it already holds an OS."""
        config = self.config_xml(name)
        root = ET.fromstring(config)
        for disk in root.findall("./devices/disk"):
            if disk.get("device", "disk") != "disk":
                continue
            target = disk.find("target")
            if target is None or not target.get("dev"):
                continue
            device = target.get("dev", "")
            try:
                info = self.block_info(name, device)
            except VemuError:
                return None
            if info["allocation"] >= INSTALLED_DISK_MIN_BYTES:
                return device
            return None
        return None

    def promote_boot_order(self, name: str) -> bool:
        """Put an installed disk ahead of attached installer media."""
        target = self.installed_disk_target(name)
        if target is None:
            return False
        config = self.config_xml(name)
        updated = promote_installed_disk(config)
        if updated == config:
            return False
        self.redefine_domain(name, updated)
        return True

    def vm_promote_boot(self, name: str | None = None) -> dict[str, object]:
        if name:
            if self.info(name).managed_by == "External":
                return {"promoted": []}
            names = [name]
        else:
            names = [
                item.name
                for item in self.list_domains()
                if item.state == "running"
                and item.managed_by != "External"
            ]
        promoted: list[str] = []
        for item in names:
            try:
                if self.promote_boot_order(item):
                    promoted.append(item)
            except (VemuError, NotFound):
                continue
        return {"promoted": promoted}

    def eject_installer_media(self, name: str) -> list[str]:
        """Eject CD media when the guest disk already contains an OS.

        An installer may finish with a shutdown instead of a reboot, so the
        libvirt reboot event does not fire.  OVMF keeps its DVD boot entry in
        NVRAM; leaving the ISO attached then loops back into the installer on
        the next Start.  Ejecting the tray makes firmware fall through to the
        installed disk.
        """
        root = ET.fromstring(self.config_xml(name))
        ejected: list[str] = []
        for disk in root.findall("./devices/disk"):
            if disk.get("device") != "cdrom":
                continue
            source = disk.find("source")
            if source is None or not source.get("file"):
                continue
            target = disk.find("target")
            device = target.get("dev") if target is not None else None
            try:
                self.update_media(name, None, device, force=True)
                ejected.append(device or "cdrom")
            except VemuError:
                continue
        return ejected

    def start(self, name: str) -> list[dict[str, str]]:
        domain = self.domain(name)
        if domain.state()[0] == 1:
            raise Conflict(f"Domain '{name}' is already running.")
        removed_usb = self.drop_missing_usb_devices(name)
        installed_target = self.installed_disk_target(name)
        if installed_target is not None:
            self.eject_installer_media(name)
        config = self.config_xml(name)
        updated = ensure_usb_keyboard(ensure_vnc_websocket(config))
        updated = ensure_suspend_disabled(updated)
        if installed_target is not None:
            updated = promote_installed_disk(updated)
        updated = ensure_nvme_on_root_port(updated)
        tuned = ensure_tcg_tuning(updated)
        applied = config
        if tuned != updated:
            try:
                self.redefine_domain(name, tuned)
                updated = tuned
                applied = tuned
            except VemuError:
                # libvirt older than the tb-cache XML element: keep running
                # with the default TCG cache rather than failing the start.
                pass
        if updated != applied:
            self.redefine_domain(name, updated)
        try:
            domain.create()
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            text = str(exc)
            retry = set()
            for vendor, product in re.findall(
                r"Did not find USB device ([0-9a-fA-F]{4}):([0-9a-fA-F]{4})",
                text,
            ):
                retry.add((f"0x{vendor.lower()}", f"0x{product.lower()}"))
            if retry:
                removed = self._remove_usb_hostdevs(name, retry)
                if removed:
                    domain = self.domain(name)
                    try:
                        domain.create()
                    except self._libvirt.libvirtError as retry_exc:
                        if "Did not find USB device" in str(retry_exc):
                            raise Conflict(
                                "A configured USB device is no longer "
                                "connected. Reconnect it, or open USB Devices "
                                "and detach the missing entry before starting."
                            ) from retry_exc
                        raise
                    return (removed_usb or []) + removed
            if "virtnodedevd" in text or "Failed to connect socket" in text:
                raise Conflict(
                    "libvirt USB device service is unavailable. Run "
                    "`vemu doctor`, then on modular libvirt start "
                    "virtnodedevd.socket and try again."
                ) from exc
            raise
        return removed_usb

    def shutdown(
        self,
        name: str,
        timeout: int = 90,
        force: bool = False,
        *,
        wait: bool = True,
    ) -> None:
        domain = self.domain(name)
        state = domain.state()[0]
        if state == 5:
            return
        if state != 1:
            raise Conflict(f"Domain '{name}' is not running.")
        try:
            domain.shutdown()
        except self._libvirt.libvirtError:
            if not force:
                raise
            domain.destroy()
            return
        if not wait:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if domain.state()[0] == 5:
                return
            time.sleep(1)
        if force:
            domain.destroy()
            return
        raise Conflict(
            f"Domain '{name}' did not shut down within {timeout} seconds. "
            "Use --force to destroy it."
        )

    def resume(self, name: str) -> None:
        domain = self.domain(name)
        state = domain.state()[0]
        try:
            if state == 7:
                # Guest-initiated S3 sleep (pmsuspended): inject an ACPI wake.
                domain.pMWakeup(0)
            elif state == 3:
                domain.resume()
            else:
                raise Conflict(
                    f"Domain '{name}' is not paused or suspended."
                )
        except self._libvirt.libvirtError as exc:  # type: ignore[attr-defined]
            raise VemuError(f"Cannot resume '{name}': {exc}") from exc

    def destroy(self, name: str) -> None:
        domain = self.domain(name)
        if domain.state()[0] == 5:
            return
        domain.destroy()

    def restart(self, name: str) -> None:
        self.shutdown(name)
        self.start(name)

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception:  # noqa: BLE001 - close is best effort
            pass

    # ------------------------------------------------------------------ helpers

    def _describe(self, domain) -> DomainInfo:
        xml = domain.XMLDesc(0)
        root = ET.fromstring(xml)
        disks: list[str] = []
        for disk in root.findall("./devices/disk"):
            if disk.get("device") != "disk":
                continue
            target = disk.find("target")
            if target is not None and target.get("bus") == "usb":
                # Removable answer media is not a data disk.
                continue
            source = disk.find("source")
            if source is None:
                continue
            path = source.get("file") or source.get("dev")
            if path:
                disks.append(path)
        try:
            autostart = bool(domain.autostart())
        except self._libvirt.libvirtError:  # type: ignore[attr-defined]
            autostart = False
        os_element = root.find("os")
        firmware = "bios"
        if os_element is not None:
            loader = os_element.find("loader")
            if (
                os_element.get("firmware") == "efi"
                or (loader is not None and loader.get("type") == "pflash")
            ):
                firmware = "efi"
        managed_by = "External"
        os_type = "generic"
        metadata = root.find(
            ".//{https://vemu.local/management}management"
        )
        if metadata is not None:
            managed_by = metadata.get("managed_by") or {
                "partial": "v-EMU (partial)",
                "full": "v-EMU (full)",
            }.get(metadata.get("mode", ""), "External")
            os_type = metadata.get("os_type") or "generic"
        if os_type == "generic":
            features = root.find("features")
            if features is not None and features.find("hyperv") is not None:
                os_type = "windows"
            elif managed_by != "External":
                os_type = "linux"
        flag = getattr(self._libvirt, "VIR_DOMAIN_XML_INACTIVE", 1)
        try:
            config_xml = domain.XMLDesc(flag)
        except self._libvirt.libvirtError:  # type: ignore[attr-defined]
            config_xml = xml
        return DomainInfo(
            os_type=os_type,
            name=domain.name(),
            uuid=str(domain.UUIDString()),
            state=STATE_NAMES.get(domain.state()[0], "unknown"),
            disks=disks,
            autostart=autostart,
            firmware=firmware,
            managed_by=managed_by,
            drift=drift.status(config_xml),
        )

    def __enter__(self) -> "LibvirtBackend":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
