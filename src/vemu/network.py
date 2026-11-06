"""Network profiles, libvirt network XML, and NAT port forwarding.

The profile is the source of truth. libvirt owns the bridge, DHCP/DNS and
firewall for NAT; v-EMU owns the profile, subnet allocation, and the port
forward table in its own nft table (``inet vemu``) so a firewalld reload does
not remove the rules.

Isolated networks without DHCP get no host IP. That is what allows two labs to
reuse the same subnet without clashing on the host routing table. A profile
that needs a host IP (all NAT networks and isolated networks with DHCP) must
use a unique subnet.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import Conflict, NotFound, ValidationError

MODES = {"nat", "isolated", "bridge"}
PROTOCOLS = {"tcp", "udp"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
DNS_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
DEFAULT_CONFIG = "/etc/vemu/networks.json"
SUBNET_POOL = ipaddress.ip_network("10.213.0.0/16")
NFT_TABLE = "vemu"


def _libvirt_name(name: str) -> str:
    return f"vemu-{name}"


def _bridge_name(name: str) -> str:
    digest = hashlib.sha1(_libvirt_name(name).encode("utf-8")).hexdigest()
    return f"vbr-{digest[:8]}"


@dataclass
class PortForward:
    protocol: str
    host_port: int
    guest_ip: str
    guest_port: int
    description: str = ""

    def validate(self) -> None:
        if self.protocol not in PROTOCOLS:
            raise ValidationError("Port forward protocol must be tcp or udp.")
        for label, value in (
            ("host port", self.host_port),
            ("guest port", self.guest_port),
        ):
            if not 1 <= int(value) <= 65535:
                raise ValidationError(f"Invalid {label}: {value}.")
        try:
            ipaddress.ip_address(self.guest_ip)
        except ValueError as exc:
            raise ValidationError(
                f"Invalid guest IP address: {self.guest_ip}."
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "host_port": self.host_port,
            "guest_ip": self.guest_ip,
            "guest_port": self.guest_port,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PortForward":
        return cls(
            protocol=str(data.get("protocol", "tcp")),
            host_port=int(data.get("host_port", 0)),
            guest_ip=str(data.get("guest_ip", "")),
            guest_port=int(data.get("guest_port", 0)),
            description=str(data.get("description", "")),
        )


@dataclass
class HostReservation:
    mac: str
    ip: str
    hostname: str = ""

    def validate(self) -> None:
        if not MAC_RE.match(self.mac.lower()):
            raise ValidationError(f"Invalid MAC address: {self.mac}.")
        try:
            ipaddress.ip_address(self.ip)
        except ValueError as exc:
            raise ValidationError(f"Invalid IP address: {self.ip}.") from exc
        if self.hostname and not HOSTNAME_RE.match(self.hostname):
            raise ValidationError(
                f"Invalid hostname: {self.hostname}."
            )

    def to_dict(self) -> dict[str, str]:
        return {"mac": self.mac.lower(), "ip": self.ip, "hostname": self.hostname}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HostReservation":
        return cls(
            mac=str(data.get("mac", "")),
            ip=str(data.get("ip", "")),
            hostname=str(data.get("hostname", "")),
        )


@dataclass
class NetworkProfile:
    name: str
    mode: str = "nat"
    subnet: str = "auto"
    dhcp: bool = True
    dns_domain: str = "vemu.local"
    description: str = ""
    bridge_interface: str = ""
    port_forwards: list[PortForward] = field(default_factory=list)
    host_reservations: list[HostReservation] = field(default_factory=list)

    @property
    def libvirt_name(self) -> str:
        return _libvirt_name(self.name)

    @property
    def bridge_name(self) -> str:
        if self.mode == "bridge" and self.bridge_interface:
            return self.bridge_interface
        return _bridge_name(self.name)

    @property
    def has_host_ip(self) -> bool:
        return self.mode == "nat" or (self.mode == "isolated" and self.dhcp)

    @property
    def network(self) -> ipaddress.IPv4Network | None:
        if self.mode == "bridge" or self.subnet == "auto":
            return None
        return ipaddress.ip_network(self.subnet, strict=False)

    @property
    def gateway(self) -> str | None:
        network = self.network
        if network is None or not self.has_host_ip:
            return None
        return str(network.network_address + 1)

    def validate(self) -> None:
        if not NAME_RE.match(self.name):
            raise ValidationError(
                "Network name must start with a letter or digit, contain only "
                "letters, digits, '.', '_' or '-', and be at most 32 characters."
            )
        if self.mode not in MODES:
            raise ValidationError(
                "Network mode must be nat, isolated or bridge."
            )
        if self.mode == "bridge":
            if not self.bridge_interface:
                raise ValidationError(
                    "Bridge mode requires an existing host bridge name."
                )
            if not NAME_RE.match(self.bridge_interface):
                raise ValidationError(
                    f"Invalid host bridge name: {self.bridge_interface}."
                )
            if self.dhcp:
                raise ValidationError(
                    "Bridge networks use the host bridge's DHCP/DNS; "
                    "v-EMU cannot run DHCP on them."
                )
            if self.port_forwards:
                raise ValidationError(
                    "Port forwarding is managed by v-EMU only on NAT networks."
                )
            if self.host_reservations:
                raise ValidationError(
                    "DHCP reservations are not supported on bridge networks."
                )
            return
        if self.subnet != "auto":
            try:
                network = ipaddress.ip_network(self.subnet, strict=False)
            except ValueError as exc:
                raise ValidationError(f"Invalid subnet: {self.subnet}.") from exc
            if network.version != 4:
                raise ValidationError("Only IPv4 subnets are supported.")
            if network.prefixlen > 30:
                raise ValidationError("Subnet must be /30 or larger.")
            if self.has_host_ip and network.num_addresses < 4:
                raise ValidationError("Subnet is too small for a gateway.")
        if self.dns_domain and not DNS_RE.match(self.dns_domain):
            raise ValidationError(f"Invalid DNS domain: {self.dns_domain}.")
        if self.mode == "isolated" and self.port_forwards:
            raise ValidationError(
                "Port forwarding requires NAT mode. Isolated networks have no "
                "route to the host."
            )
        seen: set[tuple[str, int]] = set()
        for forward in self.port_forwards:
            forward.validate()
            key = (forward.protocol, forward.host_port)
            if key in seen:
                raise ValidationError(
                    f"Duplicate {forward.protocol} host port {forward.host_port}."
                )
            seen.add(key)
            network = self.network
            if network is not None:
                guest = ipaddress.ip_address(forward.guest_ip)
                if guest not in network:
                    raise ValidationError(
                        f"Guest IP {guest} is outside subnet {network}."
                    )
        if self.host_reservations and not self.dhcp:
            raise ValidationError(
                "DHCP reservations require DHCP to be enabled."
            )
        reserved_macs: set[str] = set()
        reserved_ips: set[str] = set()
        for reservation in self.host_reservations:
            reservation.validate()
            mac = reservation.mac.lower()
            if mac in reserved_macs:
                raise ValidationError(
                    f"Duplicate reservation MAC address: {mac}."
                )
            if reservation.ip in reserved_ips:
                raise ValidationError(
                    f"Duplicate reservation IP address: {reservation.ip}."
                )
            reserved_macs.add(mac)
            reserved_ips.add(reservation.ip)
            network = self.network
            if network is not None:
                ip = ipaddress.ip_address(reservation.ip)
                if ip not in network:
                    raise ValidationError(
                        f"Reservation IP {ip} is outside subnet {network}."
                    )
                if ip == network.network_address:
                    raise ValidationError("Cannot reserve the network address.")
                if ip == network.broadcast_address:
                    raise ValidationError("Cannot reserve the broadcast address.")
                if str(ip) == self.gateway:
                    raise ValidationError("Cannot reserve the gateway address.")

    def to_dict(self) -> dict[str, Any]:
        network = self.network
        return {
            "name": self.name,
            "mode": self.mode,
            "subnet": str(network) if network is not None else "auto",
            "dhcp": self.dhcp,
            "dns_domain": self.dns_domain,
            "description": self.description,
            "bridge_interface": self.bridge_interface,
            "libvirt_name": self.libvirt_name,
            "bridge": self.bridge_name,
            "gateway": self.gateway,
            "has_host_ip": self.has_host_ip,
            "port_forwards": [item.to_dict() for item in self.port_forwards],
            "host_reservations": [
                item.to_dict() for item in self.host_reservations
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NetworkProfile":
        return cls(
            name=str(data.get("name", "")),
            mode=str(data.get("mode", "nat")),
            subnet=str(data.get("subnet", "auto")),
            dhcp=bool(data.get("dhcp", True)),
            dns_domain=str(data.get("dns_domain", "vemu.local")),
            description=str(data.get("description", "")),
            bridge_interface=str(data.get("bridge_interface", "")),
            port_forwards=[
                PortForward.from_dict(item)
                for item in data.get("port_forwards", [])
            ],
            host_reservations=[
                HostReservation.from_dict(item)
                for item in data.get("host_reservations", [])
            ],
        )

    def libvirt_xml(self) -> str:
        self.validate()
        root = ET.Element("network")
        ET.SubElement(root, "name").text = self.libvirt_name
        if self.mode == "bridge":
            ET.SubElement(root, "forward", mode="bridge")
            ET.SubElement(root, "bridge", name=self.bridge_interface)
            ET.indent(root, space="  ")
            return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
                root, encoding="unicode"
            )
        network = self.network
        if network is None:
            raise ValidationError("Resolve the subnet before rendering XML.")

        ET.SubElement(
            root, "forward", mode="nat" if self.mode == "nat" else "none"
        )
        ET.SubElement(
            root,
            "bridge",
            name=self.bridge_name,
            stp="on",
            delay="0",
        )
        if self.has_host_ip:
            ip = ET.SubElement(
                root,
                "ip",
                address=self.gateway or "",
                prefix=str(network.prefixlen),
            )
            if self.dhcp:
                dhcp = ET.SubElement(ip, "dhcp")
                start = network.network_address + 2
                end = network.broadcast_address - 1
                if int(end) <= int(start):
                    raise ValidationError(
                        "Subnet is too small for a DHCP range."
                    )
                ET.SubElement(
                    dhcp, "range", start=str(start), end=str(end)
                )
                for reservation in self.host_reservations:
                    ET.SubElement(
                        dhcp,
                        "host",
                        mac=reservation.mac.lower(),
                        ip=reservation.ip,
                        name=reservation.hostname or reservation.mac.lower(),
                    )
            if self.dns_domain and self.dhcp:
                ET.SubElement(
                    root,
                    "domain",
                    name=self.dns_domain,
                    localOnly="yes",
                )
        ET.indent(root, space="  ")
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
            root, encoding="unicode"
        )


def allocate_subnet(profiles: list[NetworkProfile]) -> str:
    used = [
        profile.network
        for profile in profiles
        if profile.network is not None
    ]
    for candidate in SUBNET_POOL.subnets(new_prefix=24):
        if not any(candidate.overlaps(network) for network in used):
            return str(candidate)
    raise Conflict("The subnet pool is exhausted.")


def _check_subnet_conflict(
    profile: NetworkProfile, profiles: list[NetworkProfile]
) -> None:
    if not profile.has_host_ip or profile.network is None:
        return
    for other in profiles:
        if other.name == profile.name:
            continue
        if not other.has_host_ip or other.network is None:
            continue
        if profile.network.overlaps(other.network):
            raise Conflict(
                f"Subnet {profile.network} overlaps with network "
                f"'{other.name}' ({other.network}). Use an isolated network "
                "without DHCP or choose another subnet."
            )


def validate_port_forward_conflicts(
    profiles: list[NetworkProfile],
) -> None:
    seen: dict[tuple[str, int], str] = {}
    for profile in profiles:
        for forward in profile.port_forwards:
            key = (forward.protocol, forward.host_port)
            other = seen.get(key)
            if other is not None and other != profile.name:
                raise Conflict(
                    f"Host port {forward.protocol}/{forward.host_port} is "
                    f"already used by network '{other}'."
                )
            seen[key] = profile.name


def resolve_profile(
    profile: NetworkProfile, profiles: list[NetworkProfile]
) -> NetworkProfile:
    if profile.mode != "bridge" and profile.subnet == "auto":
        profile.subnet = allocate_subnet(profiles)
    profile.validate()
    _check_subnet_conflict(profile, profiles)
    return profile


def nft_ruleset(
    profiles: list[NetworkProfile], table: str = NFT_TABLE
) -> str:
    lines = [f"table inet {table} {{", "  chain prerouting {"]
    lines.append(
        "    type nat hook prerouting priority dstnat; policy accept;"
    )
    for profile in profiles:
        if profile.mode != "nat":
            continue
        for forward in profile.port_forwards:
            lines.append(
                f"    {forward.protocol} dport "
                f"{forward.host_port} dnat ip to "
                f"{forward.guest_ip}:{forward.guest_port} "
                f'comment "vemu:{profile.name}"'
            )
    lines.append("  }")
    lines.append("  chain forward {")
    lines.append(
        "    type filter hook forward priority filter; policy accept;"
    )
    lines.append("    ct state established,related accept")
    lines.append("    ct status dnat accept")
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _run_nft(
    command: list[str], input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )


def apply_libvirt_forward_rules(
    profiles: list[NetworkProfile],
    *,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run_nft,
) -> dict[str, Any]:
    """Allow DNAT-ed connections through libvirt's own network firewall.

    libvirt's nft table only accepts established inbound traffic. A new
    connection created by our port-forward DNAT rule needs an explicit accept
    in ``guest_input``. The rule is re-inserted after every apply/restart
    because libvirt recreates the table with the network.
    """
    desired = {
        profile.name: (profile.bridge_name, str(profile.network))
        for profile in profiles
        if profile.mode == "nat"
        and profile.port_forwards
        and profile.network is not None
    }
    if dry_run:
        return {
            "applied": False,
            "desired": sorted(desired),
        }
    if not shutil.which("nft"):
        return {"applied": False, "reason": "nft is not installed."}
    if os.geteuid() != 0:
        return {"applied": False, "reason": "requires root."}

    listing = runner(
        [
            "nft",
            "-a",
            "list",
            "chain",
            "ip",
            "libvirt_network",
            "guest_input",
        ]
    )
    if listing.returncode != 0:
        return {
            "applied": False,
            "reason": "libvirt guest_input chain is not present.",
        }

    existing: dict[str, list[int]] = {}
    for line in listing.stdout.splitlines():
        marker = 'comment "vemu:forward:'
        if marker not in line:
            continue
        name = line.split(marker, 1)[1].split('"', 1)[0]
        handle = line.rsplit("# handle ", 1)[-1].strip()
        if handle.isdigit():
            existing.setdefault(name, []).append(int(handle))

    changed = False
    for name, handles in existing.items():
        if name not in desired:
            for handle in handles:
                result = runner(
                    [
                        "nft",
                        "delete",
                        "rule",
                        "ip",
                        "libvirt_network",
                        "guest_input",
                        "handle",
                        str(handle),
                    ]
                )
                if result.returncode != 0:
                    return {
                        "applied": False,
                        "reason": result.stderr.strip(),
                    }
                changed = True

    for name, (bridge, subnet) in desired.items():
        if existing.get(name):
            continue
        result = runner(
            [
                "nft",
                "insert",
                "rule",
                "ip",
                "libvirt_network",
                "guest_input",
                "oifname",
                bridge,
                "ip",
                "daddr",
                subnet,
                "ct",
                "status",
                "dnat",
                "accept",
                "comment",
                f'"vemu:forward:{name}"',
            ]
        )
        if result.returncode != 0:
            return {"applied": False, "reason": result.stderr.strip()}
        changed = True

    return {"applied": True, "changed": changed}


def apply_nft(
    profiles: list[NetworkProfile],
    *,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run_nft,
) -> dict[str, Any]:
    ruleset = nft_ruleset(profiles)
    if dry_run:
        return {"applied": False, "ruleset": ruleset}
    if not shutil.which("nft"):
        return {
            "applied": False,
            "reason": "nft is not installed; port forwards were not applied.",
            "ruleset": ruleset,
        }
    if os.geteuid() != 0:
        raise ValidationError("Applying port forwards requires root.")
    exists = runner(["nft", "list", "table", "inet", NFT_TABLE])
    if exists.returncode != 0:
        created = runner(["nft", "add", "table", "inet", NFT_TABLE])
        if created.returncode != 0:
            raise ValidationError(
                f"Cannot create the nft table: {created.stderr.strip()}"
            )
    flushed = runner(["nft", "flush", "table", "inet", NFT_TABLE])
    if flushed.returncode != 0:
        raise ValidationError(
            f"Cannot flush the nft table: {flushed.stderr.strip()}"
        )
    applied = runner(["nft", "-f", "-"], input_text=ruleset)
    if applied.returncode != 0:
        raise ValidationError(
            f"Cannot apply nft rules: {applied.stderr.strip()}"
        )
    return {"applied": True, "ruleset": ruleset}


class NetworkStore:
    """Persistent list of network profiles (JSON)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(
            path
            or os.environ.get("VEMU_NETWORK_CONFIG")
            or DEFAULT_CONFIG
        ).expanduser()

    def load(self) -> list[NetworkProfile]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [
            NetworkProfile.from_dict(item)
            for item in data.get("networks", [])
        ]

    def save(self, profiles: list[NetworkProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "version": 1,
                    "networks": [profile.to_dict() for profile in profiles],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def list(self) -> list[dict[str, Any]]:
        return [profile.to_dict() for profile in self.load()]

    def get(self, name: str) -> NetworkProfile:
        for profile in self.load():
            if profile.name == name:
                return profile
        raise NotFound(f"Network '{name}' is not registered.")

    def add(self, profile: NetworkProfile) -> dict[str, Any]:
        profiles = self.load()
        if any(item.name == profile.name for item in profiles):
            raise Conflict(f"Network '{profile.name}' already exists.")
        resolve_profile(profile, profiles)
        profiles.append(profile)
        validate_port_forward_conflicts(profiles)
        self.save(profiles)
        return profile.to_dict()

    def update(self, profile: NetworkProfile) -> dict[str, Any]:
        profiles = self.load()
        for index, item in enumerate(profiles):
            if item.name == profile.name:
                others = [p for p in profiles if p.name != profile.name]
                resolve_profile(profile, others)
                profiles[index] = profile
                validate_port_forward_conflicts(profiles)
                self.save(profiles)
                return profile.to_dict()
        raise NotFound(f"Network '{profile.name}' is not registered.")

    def remove(self, name: str) -> dict[str, Any]:
        profiles = self.load()
        for profile in profiles:
            if profile.name == name:
                profiles.remove(profile)
                self.save(profiles)
                return profile.to_dict()
        raise NotFound(f"Network '{name}' is not registered.")

    def find_libvirt_name(self, name: str) -> str | None:
        for profile in self.load():
            if profile.name == name:
                return profile.libvirt_name
        return None
