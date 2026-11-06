"""Offline identity reset for cloned disks.

The reset is deterministic and does not rely on cloud-init or on systemd
regenerating machine-id on boot. It runs with the VM shut down and expects the
image to be writable by the caller (the daemon runs as root).

Verified by spike S3a: see ``docs/spikes/S3a-chain-identity.md``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field

from .errors import ExternalCommandError, NotFound, ValidationError

LOG = logging.getLogger(__name__)

HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
MACHINE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Linux identity reset is intentionally limited to distributions v-EMU can
# validate.  ID_LIKE is accepted for derivatives (for example Linux Mint).
IMMUTABLE_VARIANTS = {
    "atomic",
    "bootc",
    "core",
    "coreos",
    "flatcar",
    "iot",
    "kinoite",
    "microos",
    "nixos",
    "silverblue",
}
IMMUTABLE_IDS = {
    "coreos",
    "flatcar",
    "nixos",
    "opensuse-microos",
    "talos",
    "ubuntu-core",
}
SUPPORTED_OS_IDS = {
    "alma",
    "almalinux",
    "arch",
    "centos",
    "debian",
    "fedora",
    "opensuse",
    "opensuse-leap",
    "opensuse-tumbleweed",
    "rhel",
    "rocky",
    "sled",
    "sles",
    "ubuntu",
}


def _libguestfs_env(backend: str) -> dict[str, str]:
    env = os.environ.copy()
    env["LIBGUESTFS_BACKEND"] = backend
    return env


@dataclass
class IdentityResult:
    disk: str
    hostname: str
    machine_id: str
    executed: bool = False
    skipped: bool = False
    reason: str = ""
    ssh_host_key: str | None = None
    command: list[str] = field(default_factory=list)


def new_machine_id() -> str:
    """Return a new random 32-hex machine-id."""
    return uuid.uuid4().hex


def validate_machine_id(value: str) -> str:
    value = value.strip().lower()
    if not MACHINE_ID_RE.match(value):
        raise ValidationError(
            "machine-id must be exactly 32 lowercase hexadecimal characters."
        )
    return value


def validate_hostname(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValidationError("Hostname must not be empty.")
    if not HOSTNAME_RE.match(value):
        raise ValidationError(
            "Hostname must start and end with a letter or digit, contain only "
            "letters, digits and '-', and be at most 63 characters."
        )
    return value


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise NotFound(
            f"Required tool '{tool}' was not found. Install guestfs-tools "
            "(for virt-customize / virt-cat)."
        )
    return path


def _parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _detect_os(
    disk: str, virt_cat: str, env: dict[str, str]
) -> dict[str, str]:
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            text = _run([virt_cat, "-a", disk, path], env=env).stdout
        except ExternalCommandError as exc:
            LOG.warning("Cannot read %s from %s: %s", path, disk, exc)
            continue
        if text.strip():
            return _parse_os_release(text)
    return {}


def _supported_os(values: dict[str, str]) -> bool:
    ids = {values.get("ID", "").strip().lower()}
    ids.update(values.get("ID_LIKE", "").strip().lower().split())
    return bool(ids & SUPPORTED_OS_IDS)


def _is_immutable(values: dict[str, str]) -> bool:
    variant = values.get("VARIANT_ID", "").strip().lower()
    if variant in IMMUTABLE_VARIANTS:
        return True
    ids = {values.get("ID", "").strip().lower()}
    ids.update(values.get("ID_LIKE", "").strip().lower().split())
    return bool(ids & IMMUTABLE_IDS)


def _run(
    command: list[str],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {}
    if os.geteuid() == 0:
        # vemud runs as root but with Group=vemu.  Debian's supermin/libguestfs
        # helper expects a root primary group; running as root:vemu makes
        # virt-cat/virt-customize fail before the appliance starts.
        kwargs["group"] = 0
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        **kwargs,
    )
    if result.returncode != 0:
        raise ExternalCommandError(
            " ".join(command),
            result.returncode,
            result.stderr or result.stdout,
        )
    return result


def reset_linux(
    disk: str,
    hostname: str,
    machine_id: str | None = None,
    *,
    dry_run: bool = False,
    allow_unsupported: bool = False,
    virt_customize: str = "virt-customize",
    virt_cat: str = "virt-cat",
    virt_ls: str = "virt-ls",
    libguestfs_backend: str = "direct",
) -> IdentityResult:
    """Reset hostname, machine-id and SSH host keys on a Linux disk image.

    ``libguestfs_backend="direct"`` is the default because the libvirt backend
    runs the appliance as the ``qemu`` user, which cannot traverse a ``0700``
    home directory. The daemon runs as root, so the direct backend is safe.
    """
    hostname = validate_hostname(hostname)
    machine_id = validate_machine_id(machine_id or new_machine_id())

    command = [
        virt_customize,
        "-a", disk,
        "--hostname", hostname,
        "--run-command", f"echo {machine_id} > /etc/machine-id",
        "--run-command", f"printf '%s\\n' {hostname} > /etc/hostname",
        "--run-command", f"printf '%s\\n' {hostname} > /etc/HOSTNAME",
        "--run-command", "rm -f /etc/ssh/ssh_host_*",
        "--run-command", "ssh-keygen -A",
        # Debian uses ssh.service and treats sshd.service as a linked alias;
        # Fedora/Alma/openSUSE use sshd.service.  Enabling SSH is desirable
        # but must never abort identity reset.
        "--run-command",
        "systemctl enable ssh 2>/dev/null || "
        "systemctl enable sshd 2>/dev/null || true",
    ]
    result = IdentityResult(
        disk=disk,
        hostname=hostname,
        machine_id=machine_id,
        command=command,
    )
    if dry_run:
        return result

    _require(virt_customize)
    env = _libguestfs_env(libguestfs_backend)
    os_release = _detect_os(disk, virt_cat, env)
    reason = ""
    if not os_release:
        reason = (
            "Could not read /etc/os-release from the clone; "
            "offline identity reset is unsupported for this image."
        )
    else:
        name = (
            os_release.get("PRETTY_NAME")
            or os_release.get("NAME")
            or os_release.get("ID")
            or "unknown Linux distribution"
        )
        if _is_immutable(os_release):
            reason = (
                "Offline identity reset is not supported for immutable "
                f"distribution {name}."
            )
        elif not _supported_os(os_release):
            reason = (
                f"Linux identity reset is not supported for {name}."
            )
    if reason:
        if allow_unsupported:
            result.skipped = True
            result.reason = reason
            return result
        raise ValidationError(
            reason + " Use a cloud-init/Ignition seed or clone with "
            "--no-identity-reset."
        )
    _run(command, env=env)

    reset_machine_id = _run(
        [virt_cat, "-a", disk, "/etc/machine-id"], env=env
    ).stdout.strip()
    if reset_machine_id != machine_id:
        raise ValidationError(
            f"machine-id verification failed: expected {machine_id}, "
            f"found {reset_machine_id or '<empty>'}."
        )

    def read_hostname(path: str) -> str:
        try:
            return _run([virt_cat, "-a", disk, path], env=env).stdout.strip()
        except ExternalCommandError:
            return ""

    # Debian/Fedora use /etc/hostname; openSUSE uses /etc/HOSTNAME.  Both are
    # written above, but accept either during verification for old images.
    hostname_values = {
        "hostname": read_hostname("/etc/hostname"),
        "HOSTNAME": read_hostname("/etc/HOSTNAME"),
    }
    if hostname not in hostname_values.values():
        raise ValidationError(
            f"Hostname verification failed: expected {hostname}, "
            f"found /etc/hostname={hostname_values['hostname'] or '<empty>'}, "
            f"/etc/HOSTNAME={hostname_values['HOSTNAME'] or '<empty>'}."
        )

    ssh_files = _run([virt_ls, "-a", disk, "/etc/ssh"], env=env).stdout.splitlines()
    if not any(name.startswith("ssh_host_") for name in ssh_files):
        raise ValidationError("SSH host keys were not generated in the guest.")

    result.executed = True
    result.ssh_host_key = next(
        (name for name in ssh_files if name.startswith("ssh_host_")), None
    )
    return result


def reset_windows(*_args: object, **_kwargs: object) -> IdentityResult:
    """Windows identity reset is intentionally out of scope for M0."""
    raise ValidationError(
        "Windows identity reset requires a sysprepped template. "
        "Seal a sysprepped Windows VM and clone it; do not reset identity offline."
    )
