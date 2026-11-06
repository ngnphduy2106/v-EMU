"""Detect CPU virtualization support and the KVM device.

v-EMU requires working KVM.  This module is deliberately standard-library
only because ``setup.sh`` runs it with the system Python before the project
virtual environment exists.  The same result feeds ``vemu doctor`` so the
installer and the UI cannot disagree about whether the host can run guests.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

_X86_ARCHES = {"x86_64", "amd64", "i386", "i486", "i586", "i686"}
_VIRT_FLAGS = {"vmx": "Intel VT-x", "svm": "AMD-V (SVM)"}


def _read_cpuinfo(path: Path) -> tuple[set[str], str]:
    """Return CPU flags and the vendor string from a cpuinfo-style file."""
    flags: set[str] = set()
    vendor = ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return flags, vendor
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if key in {"flags", "Features"}:
            flags.update(value.split())
        elif key in {"vendor_id", "CPU implementer", "vendor"} and not vendor:
            vendor = value
    return flags, vendor


def _vendor_name(vendor: str, flags: set[str]) -> str:
    if "vmx" in flags:
        return "Intel"
    if "svm" in flags:
        return "AMD"
    lowered = vendor.lower()
    if "intel" in lowered:
        return "Intel"
    if "amd" in lowered or "authenticamd" in lowered:
        return "AMD"
    return vendor or platform.machine()


def _detected_environment() -> str:
    tool = shutil.which("systemd-detect-virt")
    if not tool:
        return "unknown"
    try:
        result = subprocess.run(
            [tool], capture_output=True, text=True, check=False
        )
    except OSError:
        return "unknown"
    value = result.stdout.strip()
    return value or "unknown"


def _device_owner(path: Path) -> str:
    try:
        info = path.stat()
    except OSError:
        return ""
    try:
        import grp
        import pwd

        owner = pwd.getpwuid(info.st_uid).pw_name
        group = grp.getgrgid(info.st_gid).gr_name
    except (ImportError, KeyError):
        owner, group = str(info.st_uid), str(info.st_gid)
    mode = stat.S_IMODE(info.st_mode)
    return f"{owner}:{group} mode {mode:04o}"


def _current_user() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "this user"


def virtualization_status(
    *,
    cpuinfo: str | Path | None = None,
    kvm_path: str | Path | None = None,
    arch: str | None = None,
    virtualization: str | None = None,
) -> dict[str, Any]:
    """Return a diagnostic snapshot for CPU virtualization and /dev/kvm.

    ``ok`` means KVM can be used.  CPU flags are used to explain a missing
    device (BIOS/UEFI disabled versus a missing module/permission), not to
    override a working /dev/kvm.  The optional arguments exist for tests.
    """
    architecture = (arch or platform.machine() or "unknown").lower()
    x86 = architecture in _X86_ARCHES
    cpuinfo_path = Path(cpuinfo or "/proc/cpuinfo")
    device = Path(kvm_path or "/dev/kvm")

    flags, vendor = _read_cpuinfo(cpuinfo_path)
    virt_flags = sorted(flag for flag in flags if flag in _VIRT_FLAGS)
    cpu_virtualization = bool(virt_flags)
    cpu_label = " / ".join(_VIRT_FLAGS[flag] for flag in virt_flags)
    hypervisor = "hypervisor" in flags
    environment = (
        virtualization
        if virtualization is not None
        else _detected_environment()
    )
    nested = hypervisor or environment not in {"none", "unknown", "bare"}

    kvm_device = device.exists()
    kvm_accessible = kvm_device and os.access(device, os.R_OK | os.W_OK)
    owner = _device_owner(device) if kvm_device else ""

    if cpu_virtualization:
        cpu_detail = f"{cpu_label} enabled ({', '.join(virt_flags)})"
    elif x86:
        cpu_detail = "no Intel VT-x (vmx) or AMD-V (svm) flag in /proc/cpuinfo"
    else:
        cpu_detail = f"{architecture} (KVM does not use x86 CPU flags)"

    if x86 and not cpu_virtualization:
        ok = False
        reason = "cpu-disabled"
        detail = (
            "CPU virtualization is disabled: /proc/cpuinfo has no Intel VT-x "
            "(vmx) or AMD-V (svm) flag."
        )
        if kvm_device:
            detail += (
                " /dev/kvm exists, but the CPU flag is not exposed to this "
                "host."
            )
        hint = (
            "Enable Intel VT-x or AMD SVM/AMD-V in BIOS/UEFI, save, reboot, "
            "then rerun."
        )
        if nested:
            hint += (
                f" This host is a {environment} guest; enable nested "
                "virtualization on the host hypervisor first."
            )
        hint += (
            " VT-d/IOMMU is only required for PCI passthrough, not for KVM."
        )
    elif not kvm_device:
        ok = False
        reason = "kvm-missing"
        if x86:
            detail = f"CPU exposes {cpu_label}, but /dev/kvm is missing."
        else:
            detail = (
                f"/dev/kvm is missing on {architecture}; KVM is required."
            )
        hint = (
            "Load the kvm_intel/kvm_amd module, check /dev/kvm permissions, "
            "or enable nested virtualization on the host hypervisor."
        )
    elif not kvm_accessible:
        ok = True
        reason = "kvm-permission"
        detail = (
            f"/dev/kvm exists but {_current_user()} cannot read/write it"
        )
        if owner:
            detail += f" ({owner})"
        hint = (
            "Log out and back in after setup adds you to the kvm group, "
            "or check the device mode/ACL."
        )
    else:
        ok = True
        reason = "ok"
        detail = "/dev/kvm is available"
        if cpu_label:
            detail += f" ({cpu_label})"
        hint = ""

    if kvm_device and kvm_accessible:
        kvm_detail = "available"
    elif kvm_device:
        kvm_detail = (
            f"available but not accessible to {_current_user()}"
            + (f" ({owner})" if owner else "")
        )
    else:
        kvm_detail = "missing"
        if x86 and cpu_virtualization:
            kvm_detail += " (CPU virtualization is enabled; check the module)"
        elif x86:
            kvm_detail += " (CPU virtualization is disabled in BIOS/UEFI)"

    return {
        "arch": architecture,
        "x86": x86,
        "cpu_vendor": _vendor_name(vendor, flags),
        "cpu_flags": virt_flags,
        "cpu_virtualization": cpu_virtualization,
        "cpu_label": cpu_label,
        "cpu_detail": cpu_detail,
        "hypervisor": hypervisor,
        "virtualization": environment,
        "kvm_device": kvm_device,
        "kvm_path": str(device),
        "kvm_accessible": kvm_accessible,
        "kvm_detail": kvm_detail,
        "owner": owner,
        "ok": ok,
        "reason": reason,
        "detail": detail,
        "hint": hint,
    }


def format_status(status: dict[str, Any]) -> str:
    lines = [f"CPU: {status['cpu_detail']}"]
    environment = status["virtualization"]
    if status["hypervisor"] or environment not in {"none", "unknown", "bare"}:
        lines.append(f"Environment: {environment} guest")
    else:
        lines.append(f"Environment: {environment}")
    lines.append(f"/dev/kvm: {status['kvm_detail']}")
    lines.append(
        "Result: "
        + ("OK" if status["ok"] else "FAIL")
        + f" ({status['reason']})"
    )
    if not status["ok"] or status["reason"] == "kvm-permission":
        lines.append(status["detail"])
        if status["hint"]:
            lines.append(f"Hint: {status['hint']}")
    return "\n".join(lines)


def _shell_quote(value: str) -> str:
    quoted = shlex.quote(value)
    if quoted == value:
        return f"'{value}'"
    return quoted


def _shell_output(status: dict[str, Any]) -> str:
    values = {
        "VIRT_OK": "1" if status["ok"] else "0",
        "VIRT_REASON": status["reason"],
        "VIRT_DETAIL": status["detail"],
        "VIRT_HINT": status["hint"],
        "VIRT_ARCH": status["arch"],
        "VIRT_CPU_LABEL": status["cpu_label"],
        "VIRT_CPU_FLAGS": " ".join(status["cpu_flags"]),
        "VIRT_ENV": status["virtualization"],
    }
    return "\n".join(
        f"{key}={_shell_quote(str(value))}" for key, value in values.items()
    )


def _exit_code(status: dict[str, Any], allow_missing_device: bool) -> int:
    if status["ok"]:
        return 0
    if allow_missing_device and status["reason"] == "kvm-missing":
        return 0
    if status["reason"] == "cpu-disabled":
        return 2
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vemu.virtcheck",
        description="Check CPU virtualization and /dev/kvm support.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print a JSON object."
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Print VIRT_* shell assignments (always exits 0).",
    )
    parser.add_argument(
        "--allow-missing-device",
        action="store_true",
        help=(
            "Treat a missing /dev/kvm as non-fatal when CPU virtualization "
            "is enabled; used by setup.sh before host packages are installed."
        ),
    )
    args = parser.parse_args(argv)

    status = virtualization_status()
    if args.shell:
        print(_shell_output(status))
        return 0
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(format_status(status))
    return _exit_code(status, args.allow_missing_device)


if __name__ == "__main__":
    sys.exit(main())
