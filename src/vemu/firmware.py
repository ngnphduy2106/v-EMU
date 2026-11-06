"""Discovery of the v-EMU custom UEFI firmware for newly created VMs.

The firmware binaries are installed by ``setup.sh`` under
``/usr/share/vemu/firmware`` (override with ``VEMU_FIRMWARE_DIR``). Existing
VMs keep whatever loader libvirt assigned; only new EFI domains use these.

Layout::

    x64/OVMF_CODE.fd, x64/OVMF_VARS.fd
    x64/OVMF_CODE.secboot.fd, x64/OVMF_VARS.secboot.fd
    aarch64/QEMU_EFI-pflash.raw, aarch64/vars-template.raw
    riscv64/RISCV_VIRT_CODE-pflash.raw, riscv64/vars-template.raw
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path("/usr/share/vemu/firmware")

ARCH_DIRS = {
    "x86_64": "x64",
    "aarch64": "aarch64",
    "riscv64": "riscv64",
}


@dataclass(frozen=True)
class FirmwareFiles:
    loader: Path
    loader_format: str
    nvram_template: Path
    nvram_format: str
    width: int
    height: int


def firmware_root() -> Path:
    value = os.environ.get("VEMU_FIRMWARE_DIR")
    if value:
        return Path(value).expanduser()
    # Debian AppArmor only allows firmware below /usr/share/OVMF. setup.sh
    # installs the custom files there on Debian; prefer that copy when it
    # exists without changing the Fedora/RHEL layout.
    ovmf = Path("/usr/share/OVMF/vemu")
    if ovmf.is_dir():
        return ovmf
    return DEFAULT_ROOT


def secure_boot_available(arch: str = "x86_64") -> bool:
    """True when a Secure Boot firmware pair is installed for the arch."""
    if arch != "x86_64":
        return False
    return firmware_for(arch, secure_boot=True) is not None


def firmware_for(
    arch: str, *, secure_boot: bool = False
) -> FirmwareFiles | None:
    """Return the custom firmware paths for an architecture, if installed.

    ``secure_boot=True`` selects the OVMF Secure Boot build for x86_64. Other
    architectures currently fall back to their normal firmware.
    """
    directory = firmware_root() / ARCH_DIRS.get(arch, "")
    if arch == "x86_64":
        if secure_boot:
            loader = directory / "OVMF_CODE.secboot.fd"
            template = directory / "OVMF_VARS.secboot.fd"
        else:
            loader = directory / "OVMF_CODE.fd"
            template = directory / "OVMF_VARS.fd"
    elif arch == "aarch64":
        loader = directory / "QEMU_EFI-pflash.raw"
        template = directory / "vars-template.raw"
    elif arch == "riscv64":
        loader = directory / "RISCV_VIRT_CODE-pflash.raw"
        template = directory / "vars-template.raw"
    else:
        return None
    if not loader.is_file() or not template.is_file():
        return None
    return FirmwareFiles(
        loader=loader,
        loader_format="raw",
        nvram_template=template,
        nvram_format="raw",
        width=1280,
        height=800,
    )
