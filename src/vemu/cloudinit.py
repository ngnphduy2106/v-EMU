"""NoCloud cloud-init seed generation for Linux clones."""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from .errors import ExternalCommandError, NotFound, ValidationError
from .provision import _iso_tool


@dataclass
class CloudInitConfig:
    hostname: str
    user: str = "lab"
    password: str | None = None
    ssh_keys: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    runcmd: list[str] = field(default_factory=list)
    instance_id: str = ""
    mac: str | None = None
    static_ip: str | None = None
    gateway: str | None = None
    dns: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.hostname:
            raise ValidationError("cloud-init hostname must not be empty.")
        if not self.user:
            raise ValidationError("cloud-init user must not be empty.")
        if not self.ssh_keys and not self.password:
            raise ValidationError(
                "cloud-init needs an SSH public key or a password."
            )
        if self.static_ip and not self.mac:
            raise ValidationError(
                "Static cloud-init networking requires a MAC address."
            )
        if self.static_ip and "/" not in self.static_ip:
            raise ValidationError(
                "Static cloud-init IP must include a prefix, e.g. 10.0.0.5/24."
            )


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_meta_data(config: CloudInitConfig) -> str:
    instance = config.instance_id or f"iid-{uuid.uuid4().hex}"
    return (
        "#cloud-config\n"
        f"instance-id: {_q(instance)}\n"
        f"local-hostname: {_q(config.hostname)}\n"
    )


def render_user_data(config: CloudInitConfig) -> str:
    config.validate()
    lines = [
        "#cloud-config",
        f"hostname: {_q(config.hostname)}",
        "manage_etc_hosts: true",
        "disable_root: true",
        f"ssh_pwauth: {'true' if config.password else 'false'}",
        "users:",
        f"  - name: {_q(config.user)}",
        "    shell: /bin/bash",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        "    lock_passwd: false",
    ]
    if config.ssh_keys:
        lines.append("    ssh_authorized_keys:")
        for key in config.ssh_keys:
            lines.append(f"      - {_q(key.strip())}")
    if config.password:
        lines.extend(
            [
                "chpasswd:",
                "  expire: false",
                "  users:",
                f"    - name: {_q(config.user)}",
                f"      password: {_q(config.password)}",
                "      type: text",
            ]
        )
    if config.packages:
        lines.append("package_update: true")
        lines.append("packages:")
        for package in config.packages:
            lines.append(f"  - {_q(package)}")
    if config.runcmd:
        lines.append("runcmd:")
        for command in config.runcmd:
            lines.append(f"  - {_q(command)}")
    return "\n".join(lines) + "\n"


def render_network_config(config: CloudInitConfig) -> str | None:
    if not config.static_ip:
        return None
    lines = [
        "#cloud-config",
        "version: 2",
        "ethernets:",
        "  id0:",
        "    match:",
        f"      macaddress: {_q(config.mac or '')}",
        "    set-name: eth0",
        "    dhcp4: false",
        "    addresses:",
        f"      - {_q(config.static_ip)}",
    ]
    if config.gateway:
        lines.extend(
            [
                "    routes:",
                "      - to: default",
                f"        via: {_q(config.gateway)}",
            ]
        )
    if config.dns:
        lines.append("    nameservers:")
        lines.append("      addresses:")
        for server in config.dns:
            lines.append(f"        - {_q(server)}")
    return "\n".join(lines) + "\n"


def build_cloud_init_iso(
    chain_dir: str | Path, config: CloudInitConfig
) -> Path:
    config.validate()
    chain_path = Path(chain_dir).expanduser().resolve()
    source = chain_path / "cloud-init"
    source.mkdir(parents=True, exist_ok=True)
    (source / "meta-data").write_text(
        render_meta_data(config), encoding="utf-8"
    )
    (source / "user-data").write_text(
        render_user_data(config), encoding="utf-8"
    )
    network = render_network_config(config)
    if network is not None:
        (source / "network-config").write_text(network, encoding="utf-8")
    elif (source / "network-config").exists():
        (source / "network-config").unlink()

    output = chain_path / "cloud-init.iso"
    command, _tool = _iso_tool("cidata")
    result = subprocess.run(
        [*command, "-o", str(output), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ExternalCommandError(
            " ".join([*command, "-o", str(output), str(source)]),
            result.returncode,
            result.stderr or result.stdout,
        )
    if not output.is_file() or output.stat().st_size == 0:
        raise NotFound("The cloud-init seed ISO was not created.")
    return output
