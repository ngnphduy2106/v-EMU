"""Detect and create host bridges for LAN-connected VMs.

NAT and isolated networks are owned by libvirt. A LAN bridge is different: it
is a host NetworkManager connection that carries a physical uplink, so guests
get addresses from the site router and are reachable from other machines.

Creating a bridge changes host connectivity. This module keeps every step
reversible:

* detect existing bridges first; using one makes no host changes;
* snapshot the uplink's NetworkManager profile before touching it;
* create the bridge and its port as dedicated ``vemu-*`` connections;
* disable (not delete) the previous uplink profile;
* verify the bridge became active and, on failure, recreate the old state.

The daemon runs as root. The UI never invokes ``nmcli`` directly.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import Conflict, ExternalCommandError, ValidationError, VemuError

BRIDGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$")
ADDRESSING = {"dhcp", "static", "none"}
DEFAULT_BACKUP_DIR = "/var/lib/vemu/backups/network"
SYSFS_NET = Path("/sys/class/net")

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run_command(
    run: Runner,
    args: list[str],
    *,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ValidationError(
            f"Required command not found: {args[0]}. "
            "Install NetworkManager (nmcli) or create the bridge manually."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VemuError(f"{args[0]} timed out after {timeout}s.") from exc
    if check and result.returncode != 0:
        raise ExternalCommandError(
            " ".join(args), result.returncode, result.stderr or ""
        )
    return result


def _terse(line: str) -> list[str]:
    """Split one nmcli terse line, honouring escaped colons."""

    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def _connections(run: Runner) -> dict[str, dict[str, Any]]:
    """Return NetworkManager connections keyed by device name."""

    result = _run_command(
        run,
        [
            "nmcli",
            "-t",
            "-f",
            "NAME,UUID,TYPE,DEVICE,STATE",
            "connection",
            "show",
        ],
        check=False,
    )
    by_device: dict[str, dict[str, Any]] = {}
    if result.returncode != 0:
        return by_device
    for line in result.stdout.splitlines():
        fields = _terse(line)
        if len(fields) < 5:
            continue
        name, uuid, kind, device, state = fields[:5]
        if not device:
            continue
        entry = {
            "name": name,
            "uuid": uuid,
            "type": kind,
            "device": device,
            "state": state,
            "active": state in {"activated", "activating"},
        }
        # Prefer the active profile when several connections target a device.
        if device not in by_device or entry["active"]:
            by_device[device] = entry
    return by_device


def _addresses(run: Runner) -> dict[str, list[str]]:
    result = _run_command(run, ["ip", "-j", "address", "show"], check=False)
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    addresses: dict[str, list[str]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("ifname", ""))
        values = []
        for address in item.get("addr_info", []):
            if not isinstance(address, dict):
                continue
            local = address.get("local")
            prefix = address.get("prefixlen")
            if local and prefix is not None:
                values.append(f"{local}/{prefix}")
        if values:
            addresses[name] = values
    return addresses


def _nmcli_available() -> bool:
    return shutil.which("nmcli") is not None


def _bridge_slaves(sysfs: Path) -> dict[str, list[str]]:
    slaves: dict[str, list[str]] = {}
    try:
        entries = list(sysfs.iterdir())
    except OSError:
        return slaves
    for entry in entries:
        master = entry / "master"
        try:
            if master.is_symlink():
                target = master.resolve().name
                slaves.setdefault(target, []).append(entry.name)
        except OSError:
            continue
    return slaves


def collect_inventory(
    *,
    run: Runner = subprocess.run,
    sysfs: Path = SYSFS_NET,
    nmcli: bool | None = None,
) -> dict[str, Any]:
    """List usable host bridges and physical Ethernet uplinks."""

    managed = _nmcli_available() if nmcli is None else nmcli
    connections = _connections(run) if managed else {}
    addresses = _addresses(run)
    slaves = _bridge_slaves(sysfs)
    bridges: list[dict[str, Any]] = []
    uplinks: list[dict[str, Any]] = []

    try:
        devices = sorted(entry.name for entry in sysfs.iterdir())
    except OSError:
        devices = []

    for device in devices:
        path = sysfs / device
        try:
            info = {
                "name": device,
                "state": (path / "operstate").read_text(
                    encoding="utf-8"
                ).strip()
                if (path / "operstate").is_file()
                else "unknown",
                "addresses": addresses.get(device, []),
                "interfaces": sorted(slaves.get(device, [])),
                "connection": connections.get(device),
                "managed": managed and device in connections,
            }
        except OSError:
            continue
        if (path / "bridge").is_dir():
            info["kind"] = "bridge"
            bridges.append(info)
            continue
        # Physical Ethernet only: a device symlink is present for NICs, not
        # for veth/tun/tap/libvirt taps.
        if device == "lo" or not (path / "device").exists():
            continue
        connection = info.get("connection") or {}
        if str(connection.get("type", "")) not in {"", "802-3-ethernet"}:
            continue
        if (path / "master").is_symlink():
            # Already a bridge port; it belongs to the bridge above.
            continue
        info["kind"] = "uplink"
        info["carrier"] = (
            (path / "carrier").read_text(encoding="utf-8").strip() == "1"
            if (path / "carrier").is_file()
            else info["state"] == "up"
        )
        uplinks.append(info)

    return {
        "bridges": bridges,
        "uplinks": uplinks,
        "devices": devices,
        "nmcli": managed,
        "reason": (
            ""
            if managed
            else "NetworkManager (nmcli) is not installed; only existing "
            "bridges can be used."
        ),
    }


def _validate_static(
    address: str, gateway: str, dns: str
) -> tuple[str, str, str]:
    try:
        interface = ipaddress.ip_interface(address)
    except ValueError as exc:
        raise ValidationError(
            "Static address must be an IPv4 CIDR such as 192.168.1.50/24."
        ) from exc
    if interface.version != 4:
        raise ValidationError("Only IPv4 static addresses are supported.")
    if not gateway:
        raise ValidationError("Static addressing requires a gateway.")
    try:
        gateway_ip = ipaddress.ip_address(gateway)
    except ValueError as exc:
        raise ValidationError(f"Invalid gateway: {gateway}.") from exc
    if gateway_ip not in interface.network:
        raise ValidationError(
            f"Gateway {gateway_ip} is outside {interface.network}."
        )
    if gateway_ip == interface.ip:
        raise ValidationError("Gateway cannot be the bridge address itself.")
    for server in [part.strip() for part in dns.split(",") if part.strip()]:
        try:
            ipaddress.ip_address(server)
        except ValueError as exc:
            raise ValidationError(f"Invalid DNS server: {server}.") from exc
    return str(interface), str(gateway_ip), dns.strip()


def plan_bridge(
    name: str,
    uplink: str,
    addressing: str = "dhcp",
    address: str = "",
    gateway: str = "",
    dns: str = "",
    *,
    inventory: dict[str, Any] | None = None,
    run: Runner = subprocess.run,
) -> dict[str, Any]:
    """Validate a requested bridge without changing the host."""

    name = name.strip()
    uplink = uplink.strip()
    addressing = addressing.strip().lower()
    if not BRIDGE_NAME_RE.match(name):
        raise ValidationError(
            "Bridge name must be at most 15 characters, start with a letter "
            "or digit, and use only letters, digits, '.', '_' or '-'."
        )
    if addressing not in ADDRESSING:
        raise ValidationError(
            "Addressing must be dhcp, static or none."
        )
    inventory = inventory or collect_inventory(run=run)
    bridges = {
        str(item.get("name")): item for item in inventory.get("bridges", [])
    }
    if name in bridges or name in set(inventory.get("devices") or []):
        raise Conflict(f"A network device named {name} already exists.")
    uplinks = {
        str(item.get("name")): item
        for item in inventory.get("uplinks", [])
    }
    if uplink not in uplinks:
        raise ValidationError(
            f"{uplink or 'No uplink selected'} is not an unused physical "
            "Ethernet interface. Ports already attached to a bridge cannot "
            "be reused."
        )
    if addressing == "static":
        address, gateway, dns = _validate_static(address, gateway, dns)
    else:
        address = gateway = dns = ""
    old = dict(uplinks[uplink].get("connection") or {})
    if old.get("uuid") and "autoconnect" not in old:
        snapshot = _run_command(
            run,
            [
                "nmcli",
                "-t",
                "-f",
                "connection.autoconnect",
                "connection",
                "show",
                "uuid",
                str(old["uuid"]),
            ],
            check=False,
        )
        if snapshot.returncode == 0:
            value = snapshot.stdout.strip().split(":", 1)[-1]
            old["autoconnect"] = value.strip().lower() == "yes"
    warnings = [
        f"Creating the bridge will detach {uplink} from its current "
        "NetworkManager profile and briefly interrupt host networking. "
        "Use a local console if you are connected over SSH or VNC."
    ]
    if old:
        if old.get("autoconnect", True):
            warnings.append(
                f"The existing profile '{old.get('name', uplink)}' is kept "
                "and its autoconnect is disabled so it cannot fight the "
                "bridge after a reboot. A backup is written before any "
                "change."
            )
        else:
            warnings.append(
                f"The existing profile '{old.get('name', uplink)}' is kept "
                "and will not autoconnect. A backup is written before any "
                "change."
            )
    if addressing == "none":
        warnings.append(
            "Without a host address the bridge still switches guest traffic, "
            "but the host cannot manage the bridge over IPv4."
        )
    return {
        "name": name,
        "uplink": uplink,
        "addressing": addressing,
        "address": address,
        "gateway": gateway,
        "dns": dns,
        "old_connection": old,
        "warnings": warnings,
        "bridge_connection": f"vemu-br-{name}"[:32],
        "port_connection": f"vemu-port-{name}"[:32],
    }


def _backup_root(backup_dir: str | Path | None) -> Path:
    if backup_dir:
        return Path(backup_dir).expanduser()
    configured = os.environ.get("VEMU_BACKUP_DIR")
    if configured:
        return Path(configured).expanduser() / "network"
    if os.geteuid() == 0:
        return Path(DEFAULT_BACKUP_DIR)
    return Path.home() / ".local" / "state" / "vemu" / "backups" / "network"


def _write_backup(plan: dict[str, Any], backup_dir: str | Path | None) -> Path:
    directory = _backup_root(backup_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"{stamp}-{plan['name']}.json"
        path.write_text(
            json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        backups = sorted(directory.glob("*.json"))
        for old in backups[:-40]:
            old.unlink(missing_ok=True)
        return path
    except OSError as exc:
        raise VemuError(
            f"Cannot back up host networking before creating the bridge: {exc}"
        ) from exc


def _bridge_ready(
    connection: str,
    bridge: str,
    addressing: str,
    port_connection: str,
    run: Runner,
) -> bool:
    result = _run_command(
        run,
        [
            "nmcli",
            "-t",
            "-f",
            "GENERAL.STATE",
            "connection",
            "show",
            connection,
        ],
        check=False,
    )
    if result.returncode != 0 or "activated" not in result.stdout:
        return False
    port = _run_command(
        run,
        [
            "nmcli",
            "-t",
            "-f",
            "GENERAL.STATE",
            "connection",
            "show",
            port_connection,
        ],
        check=False,
    )
    if port.returncode != 0 or "activated" not in port.stdout:
        return False
    if addressing == "none":
        return True
    addresses = _addresses(run).get(bridge, [])
    # DHCP/static only configure IPv4; a link-local IPv6 address is not
    # proof that the bridge got its LAN address yet.
    return any("." in value and "/" in value for value in addresses)


def _rollback(
    plan: dict[str, Any],
    run: Runner,
) -> None:
    for connection in (
        plan.get("bridge_connection"),
        plan.get("port_connection"),
    ):
        if not connection:
            continue
        try:
            _run_command(run, ["nmcli", "connection", "delete", connection])
        except VemuError:
            pass
    old = plan.get("old_connection") or {}
    if old.get("uuid") and plan.get("_old_autoconnect_changed"):
        try:
            _run_command(
                run,
                [
                    "nmcli",
                    "connection",
                    "modify",
                    "uuid",
                    str(old["uuid"]),
                    "connection.autoconnect",
                    "yes" if old.get("autoconnect", True) else "no",
                ],
                check=False,
            )
        except VemuError:
            pass
    if old.get("uuid") and old.get("active"):
        try:
            _run_command(
                run,
                [
                    "nmcli",
                    "connection",
                    "up",
                    "uuid",
                    str(old["uuid"]),
                ],
                check=False,
            )
        except VemuError:
            pass


def create_bridge(
    plan: dict[str, Any],
    *,
    run: Runner = subprocess.run,
    backup_dir: str | Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Create a NetworkManager bridge and move the uplink onto it."""

    bridge = plan["name"]
    bridge_connection = plan["bridge_connection"]
    port_connection = plan["port_connection"]
    for connection in (bridge_connection, port_connection):
        existing = _run_command(
            run,
            ["nmcli", "connection", "show", connection],
            check=False,
        )
        if existing.returncode == 0:
            raise Conflict(
                f"NetworkManager connection '{connection}' already exists."
            )
    backup = _write_backup(plan, backup_dir)
    addressing = plan["addressing"]
    old = plan.get("old_connection") or {}
    try:
        if old.get("uuid") and old.get("active"):
            _run_command(
                run,
                [
                    "nmcli",
                    "connection",
                    "down",
                    "uuid",
                    str(old["uuid"]),
                ],
                check=False,
            )
        bridge_args = [
            "nmcli",
            "connection",
            "add",
            "type",
            "bridge",
            "con-name",
            bridge_connection,
            "ifname",
            bridge,
            "bridge.stp",
            "no",
        ]
        if addressing == "dhcp":
            bridge_args += ["ipv4.method", "auto", "ipv6.method", "auto"]
        elif addressing == "static":
            bridge_args += [
                "ipv4.method",
                "manual",
                "ipv4.addresses",
                plan["address"],
                "ipv4.gateway",
                plan["gateway"],
                "ipv6.method", "auto",
            ]
            if plan.get("dns"):
                bridge_args += [
                    "ipv4.dns", plan["dns"].replace(",", " ")
                ]
        else:
            bridge_args += ["ipv4.method", "disabled", "ipv6.method", "ignore"]
        _run_command(run, bridge_args)
        _run_command(
            run,
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "ethernet",
                "con-name",
                port_connection,
                "ifname",
                plan["uplink"],
                "master",
                bridge_connection,
                "slave-type",
                "bridge",
            ],
        )
        _run_command(
            run,
            ["nmcli", "connection", "up", bridge_connection],
            timeout=90,
        )
        if old.get("uuid") and old.get("autoconnect", True):
            _run_command(
                run,
                [
                    "nmcli",
                    "connection",
                    "modify",
                    "uuid",
                    str(old["uuid"]),
                    "connection.autoconnect",
                    "no",
                ],
            )
            plan["_old_autoconnect_changed"] = True
        deadline = time.monotonic() + 30
        while not _bridge_ready(
            bridge_connection,
            bridge,
            addressing,
            port_connection,
            run,
        ):
            if time.monotonic() >= deadline:
                raise VemuError(
                    f"Bridge {bridge} did not become active with an address. "
                    "The previous connection was restored."
                )
            sleep(0.5)
    except Exception as exc:
        _rollback(plan, run)
        if isinstance(exc, VemuError):
            raise
        raise VemuError(
            f"Cannot create bridge {bridge}: {exc}. The previous connection "
            "was restored."
        ) from exc
    return {
        "name": bridge,
        "connection": bridge_connection,
        "port_connection": port_connection,
        "uplink": plan["uplink"],
        "addressing": addressing,
        "address": plan.get("address", ""),
        "gateway": plan.get("gateway", ""),
        "dns": plan.get("dns", ""),
        "backup": str(backup),
        "warnings": plan.get("warnings", []),
    }
