"""Offline Linux guest performance tuning for the per-VM wizard.

This is deliberately separate from identity reset: it changes the guest's
systemd sleep policy, adds a small boot-time tuning unit and can switch the
domain video model to virtio-gpu.  Everything happens while the VM is shut
down, using the same libguestfs tooling as identity reset.
"""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import identity
from .errors import ExternalCommandError, NotFound, ValidationError
from .vmconfig import apply_config

# Distros exercised end-to-end during the 2026-11 spike.  Other supported
# systemd distributions are allowed with an "untested" warning, but immutable
# images and non-systemd guests are rejected.
TESTED_OS_IDS = {
    "alma",
    "almalinux",
    "debian",
    "fedora",
    "opensuse-leap",
    "sles",
}

SLEEP_CONF = """[Sleep]
AllowSuspend=no
AllowHibernation=no
AllowSuspendThenHibernate=no
AllowHybridSleep=no
"""

HIGH_PERFORMANCE_UNIT = """[Unit]
Description=v-EMU guest high-performance tuning
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do [ -w "$f" ] && echo performance > "$f" 2>/dev/null || true; done; for f in /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference; do [ -w "$f" ] && echo performance > "$f" 2>/dev/null || true; done'

[Install]
WantedBy=multi-user.target
"""

SYSCTL_CONF = """vm.swappiness = 10
vm.dirty_background_ratio = 5
vm.dirty_ratio = 15
"""

SLEEP_UNITS = (
    "sleep.target",
    "suspend.target",
    "hibernate.target",
    "hybrid-sleep.target",
)


@dataclass
class PerformanceProbe:
    domain: str
    disk: str
    supported: bool
    reason: str = ""
    distro: str = ""
    os_id: str = ""
    systemd: bool = False
    immutable: bool = False
    tested: bool = False
    video: str = ""
    needs_video_change: bool = False
    tuned: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "disk": self.disk,
            "supported": self.supported,
            "reason": self.reason,
            "distro": self.distro,
            "os_id": self.os_id,
            "systemd": self.systemd,
            "immutable": self.immutable,
            "tested": self.tested,
            "video": self.video,
            "needs_video_change": self.needs_video_change,
            "tuned": self.tuned,
            "warnings": list(self.warnings),
        }


def _virt_tools(
    *,
    virt_cat: str = "virt-cat",
    virt_customize: str = "virt-customize",
    virt_ls: str = "virt-ls",
) -> tuple[str, str, str, dict[str, str]]:
    cat = identity._require(virt_cat)
    custom = identity._require(virt_customize)
    ls = identity._require(virt_ls)
    return cat, custom, ls, identity._libguestfs_env("direct")


def _has_systemd(
    disk: str,
    virt_ls: str,
    env: dict[str, str],
) -> bool:
    for directory in ("/usr/lib/systemd", "/lib/systemd"):
        try:
            listing = identity._run(
                [virt_ls, "-a", disk, directory], env=env
            ).stdout.splitlines()
        except ExternalCommandError:
            continue
        if "systemd" in listing:
            return True
    return False


def _has_tuned(
    disk: str,
    virt_ls: str,
    env: dict[str, str],
) -> bool:
    for directory in ("/usr/bin", "/usr/sbin"):
        try:
            listing = identity._run(
                [virt_ls, "-a", disk, directory], env=env
            ).stdout.splitlines()
        except ExternalCommandError:
            continue
        if "tuned-adm" in listing:
            return True
    return False


def _video_model(xml: str) -> str:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    model = root.find("./devices/video/model")
    return str(model.get("type", "")) if model is not None else ""


def probe(
    domain: str,
    disk: str,
    xml: str,
    *,
    virt_cat: str = "virt-cat",
    virt_ls: str = "virt-ls",
) -> PerformanceProbe:
    video = _video_model(xml)
    result = PerformanceProbe(
        domain=domain,
        disk=disk,
        supported=False,
        video=video,
        needs_video_change=video != "virtio",
    )
    try:
        cat, _custom, ls, env = _virt_tools(
            virt_cat=virt_cat,
            virt_ls=virt_ls,
        )
    except NotFound as exc:
        result.reason = str(exc)
        return result
    os_release = identity._detect_os(disk, cat, env)
    if not os_release:
        result.reason = "This guest has no readable /etc/os-release."
        return result
    result.os_id = os_release.get("ID", "").strip().lower()
    result.distro = (
        os_release.get("PRETTY_NAME")
        or os_release.get("NAME")
        or result.os_id
        or "Linux"
    )
    result.systemd = _has_systemd(disk, ls, env)
    result.tuned = _has_tuned(disk, ls, env)
    result.immutable = identity._is_immutable(os_release)
    result.tested = result.os_id in TESTED_OS_IDS
    result.supported = (
        identity._supported_os(os_release)
        and result.systemd
        and not result.immutable
    )
    if result.immutable:
        result.reason = (
            f"{result.distro} is immutable; its /etc cannot be changed "
            "offline."
        )
    elif not result.systemd:
        result.reason = (
            f"{result.distro} does not appear to use systemd; this tuning "
            "needs systemd."
        )
    elif not identity._supported_os(os_release):
        result.reason = (
            f"{result.distro} is not in the supported guest list."
        )
    elif not result.tested:
        result.warnings.append(
            f"{result.distro} is systemd-based but was not part of the "
            "tested distro set."
        )
    return result


def _write_tuning(disk: str, custom: str, env: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="vemu-tuning-") as tmp:
        root = Path(tmp)
        (root / "90-vemu-no-sleep.conf").write_text(
            SLEEP_CONF, encoding="utf-8"
        )
        (root / "vemu-high-performance.service").write_text(
            HIGH_PERFORMANCE_UNIT, encoding="utf-8"
        )
        (root / "90-vemu-high-performance.conf").write_text(
            SYSCTL_CONF, encoding="utf-8"
        )
        command = [
            custom,
            "-a",
            disk,
            "--mkdir",
            "/etc/systemd/sleep.conf.d",
            "--mkdir",
            "/etc/systemd/system/multi-user.target.wants",
            "--upload",
            f"{root / '90-vemu-no-sleep.conf'}:"
            "/etc/systemd/sleep.conf.d/90-vemu-no-sleep.conf",
            "--upload",
            f"{root / 'vemu-high-performance.service'}:"
            "/etc/systemd/system/vemu-high-performance.service",
            "--upload",
            f"{root / '90-vemu-high-performance.conf'}:"
            "/etc/sysctl.d/90-vemu-high-performance.conf",
            "--run-command",
            "ln -sf /etc/systemd/system/vemu-high-performance.service "
            "/etc/systemd/system/multi-user.target.wants/"
            "vemu-high-performance.service",
            "--run-command",
            "for u in "
            + " ".join(SLEEP_UNITS)
            + '; do ln -sf /dev/null "/etc/systemd/system/$u"; done',
        ]
        identity._run(command, env=env)


def _verify_tuning(
    disk: str,
    virt_cat: str,
    virt_ls: str,
    env: dict[str, str],
) -> None:
    required = (
        "/etc/systemd/sleep.conf.d/90-vemu-no-sleep.conf",
        "/etc/systemd/system/vemu-high-performance.service",
        "/etc/sysctl.d/90-vemu-high-performance.conf",
    )
    for path in required:
        identity._run([virt_cat, "-a", disk, path], env=env)
    listing = identity._run(
        [virt_ls, "-a", disk, "/etc/systemd/system"],
        env=env,
    ).stdout.splitlines()
    for unit in SLEEP_UNITS:
        if unit not in listing:
            raise ValidationError(
                f"Guest tuning verification failed: {unit} was not created."
            )
    wants = identity._run(
        [virt_ls, "-a", disk,
         "/etc/systemd/system/multi-user.target.wants"],
        env=env,
    ).stdout.splitlines()
    if "vemu-high-performance.service" not in wants:
        raise ValidationError(
            "Guest tuning verification failed: service was not enabled."
        )


def apply(
    domain: str,
    disk: str,
    xml: str,
    *,
    virt_cat: str = "virt-cat",
    virt_customize: str = "virt-customize",
    virt_ls: str = "virt-ls",
) -> dict[str, Any]:
    probe_result = probe(
        domain,
        disk,
        xml,
        virt_cat=virt_cat,
        virt_ls=virt_ls,
    )
    if not probe_result.supported:
        raise ValidationError(probe_result.reason or "Guest is not supported.")
    _cat, custom, _ls, env = _virt_tools(
        virt_cat=virt_cat,
        virt_customize=virt_customize,
        virt_ls=virt_ls,
    )
    _write_tuning(disk, custom, env)
    _verify_tuning(disk, _cat, _ls, env)
    updated = (
        apply_config(xml, video="virtio")
        if probe_result.needs_video_change
        else xml
    )
    return {
        "domain": domain,
        "disk": disk,
        "probe": probe_result.as_dict(),
        "xml_changed": updated != xml,
        "video_changed": probe_result.needs_video_change,
        "xml": updated,
    }
