"""Guest-side Windows VirtioFS mount helper over QEMU guest agent.

This module never installs drivers. It only detects an existing WinFsp +
VirtIO-FS installation, configures the WinFsp launcher when requested, and
mounts/dismounts shares with fixed command templates.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from typing import Any

from .backend import LibvirtBackend
from .errors import Conflict, VemuError

WINDOWS_DIR = r"C:\Windows"
LAUNCHCTL = r"C:\Program Files (x86)\WinFsp\bin\launchctl-x64.exe"
FSREG = r"C:\Program Files (x86)\WinFsp\bin\fsreg.bat"
POWERSHELL = rf"{WINDOWS_DIR}\System32\WindowsPowerShell\v1.0\powershell.exe"
CMD = rf"{WINDOWS_DIR}\System32\cmd.exe"
SC = rf"{WINDOWS_DIR}\System32\sc.exe"
REG = rf"{WINDOWS_DIR}\System32\reg.exe"
SH = "/bin/sh"
VIRTIOFS_FALLBACKS = (
    r"C:\Program Files\Virtio-Win\VioFS\virtiofs.exe",
    r"C:\Program Files\VirtioFs\virtiofs.exe",
)
REGISTRY_KEY = r"HKLM\Software\WinFsp\Services\virtiofs"


def _run(
    backend: LibvirtBackend,
    domain: str,
    path: str,
    args: list[str],
    *,
    timeout: int = 30,
) -> tuple[int | None, str, str]:
    payload = backend.qemu_agent_command(
        domain,
        {
            "execute": "guest-exec",
            "arguments": {
                "path": path,
                "arg": args,
                "capture-output": True,
            },
        },
        timeout=15,
    )
    if not isinstance(payload, dict) or "pid" not in payload:
        raise VemuError(f"Guest did not start {path}.")
    pid = payload["pid"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = backend.qemu_agent_command(
            domain,
            {
                "execute": "guest-exec-status",
                "arguments": {"pid": pid},
            },
            timeout=15,
        )
        if isinstance(status, dict) and status.get("exited"):
            import base64

            out = base64.b64decode(status.get("out-data", "")).decode(
                "utf-8", "replace"
            )
            err = base64.b64decode(status.get("err-data", "")).decode(
                "utf-8", "replace"
            )
            return status.get("exitcode"), out, err
        time.sleep(0.25)
    raise VemuError(f"Guest command timed out: {path}")


def _powershell(
    backend: LibvirtBackend,
    domain: str,
    script: str,
    *,
    timeout: int = 30,
) -> tuple[int | None, str, str]:
    return _run(
        backend,
        domain,
        POWERSHELL,
        ["-NoProfile", "-NonInteractive", "-Command", script],
        timeout=timeout,
    )


def _service_state(
    backend: LibvirtBackend, domain: str, service: str
) -> dict[str, Any]:
    code, out, err = _run(backend, domain, SC, ["query", service])
    text = f"{out}\n{err}"
    if code != 0 or "SERVICE_NAME" not in text:
        return {"exists": False, "running": False}
    return {
        "exists": True,
        "running": "RUNNING" in text,
        "state": (
            "running"
            if "RUNNING" in text
            else "stopped"
            if "STOPPED" in text
            else "unknown"
        ),
    }


def _service_binary(
    backend: LibvirtBackend, domain: str, service: str
) -> str | None:
    code, out, err = _run(backend, domain, SC, ["qc", service])
    if code != 0:
        return None
    match = re.search(
        r"BINARY_PATH_NAME\s*:\s*(.+)", out, re.IGNORECASE
    )
    if not match:
        return None
    value = match.group(1).strip().strip('"')
    return value or None


def _exists(backend: LibvirtBackend, domain: str, path: str) -> bool:
    code, out, _err = _powershell(
        backend,
        domain,
        f"if (Test-Path -LiteralPath {json.dumps(path)}) {{ 'yes' }}",
        timeout=20,
    )
    return code == 0 and "yes" in out.lower()


def _guest_os(backend: LibvirtBackend, domain: str) -> str:
    info = backend.qemu_agent_command(
        domain, {"execute": "guest-get-osinfo"}, timeout=15
    )
    return str(info.get("id", "")).lower() if isinstance(info, dict) else ""


def _shell(
    backend: LibvirtBackend,
    domain: str,
    command: str,
    *,
    timeout: int = 30,
) -> tuple[int | None, str, str]:
    return _run(backend, domain, SH, ["-c", command], timeout=timeout)


def detect(
    backend: LibvirtBackend, domain: str
) -> dict[str, Any]:
    """Return guest VirtioFS prerequisites without changing the guest."""
    try:
        backend.qemu_agent_command(
            domain, {"execute": "guest-ping"}, timeout=10
        )
    except VemuError as exc:
        raise Conflict(
            "The guest agent is not responding. Install/start qemu-guest-agent "
            "before mounting VirtioFS shares."
        ) from exc
    os_id = _guest_os(backend, domain)
    if os_id and os_id != "mswindows":
        fs_code, fs_out, _fs_err = _shell(
            backend, domain, "grep -qw virtiofs /proc/filesystems",
            timeout=15,
        )
        if fs_code != 0:
            mod_code, mod_out, mod_err = _shell(
                backend, domain, "modprobe virtiofs", timeout=30
            )
            if mod_code != 0:
                raise Conflict(
                    "The Linux guest does not provide the virtiofs kernel "
                    f"filesystem: {(mod_err or mod_out).strip()}"
                )
        missing: list[str] = []
        for command, label in (("mount", "mount"), ("umount", "umount")):
            code, _out, _err = _shell(
                backend, domain, f"command -v {label}", timeout=15
            )
            if code != 0:
                missing.append(
                    f"{label} (or the guest-agent SELinux policy blocks it)"
                )
        return {
            "guest_os": os_id,
            "ready": not missing,
            "missing": missing,
            "mode": "linux",
        }
    if not os_id:
        raise Conflict(
            "Automatic VirtioFS mounting is only supported for Windows and "
            "Linux guests with qemu-guest-agent."
        )

    launchctl_exists = _exists(backend, domain, LAUNCHCTL)
    fsreg_exists = _exists(backend, domain, FSREG)
    launchctl = LAUNCHCTL if launchctl_exists else ""
    fsreg = FSREG if fsreg_exists else ""
    virtiofs = _service_binary(backend, domain, "VirtioFsSvc")
    if not virtiofs:
        for candidate in VIRTIOFS_FALLBACKS:
            if _exists(backend, domain, candidate):
                virtiofs = candidate
                break
    driver = _service_state(backend, domain, "VirtioFsDrv")
    launcher = _service_state(backend, domain, "WinFsp.Launcher")
    single = _service_state(backend, domain, "VirtioFsSvc")
    reg_code, _reg_out, _reg_err = _run(
        backend, domain, REG, ["query", REGISTRY_KEY, "/reg:32"]
    )
    missing: list[str] = []
    if not launchctl:
        missing.append("WinFsp launchctl-x64.exe")
    if not virtiofs:
        missing.append("virtiofs.exe")
    if not driver["exists"]:
        missing.append("VirtioFsDrv")
    if not launcher["exists"]:
        missing.append("WinFsp.Launcher service")
    return {
        "guest_os": os_id,
        "ready": not missing,
        "missing": missing,
        "mode": "windows",
        "launchctl": launchctl,
        "fsreg": fsreg,
        "virtiofs": virtiofs or "",
        "driver": driver,
        "launcher": launcher,
        "single_service": single,
        "launcher_registered": reg_code == 0,
    }


def _configure_launcher(
    backend: LibvirtBackend, domain: str, info: dict[str, Any]
) -> None:
    single = info.get("single_service") or {}
    if single.get("running"):
        _run(backend, domain, SC, ["stop", "VirtioFsSvc"])
    if single.get("exists"):
        _run(backend, domain, SC, ["config", "VirtioFsSvc", "start=", "demand"])
    executable = str(info["virtiofs"])
    entries = (
        ("Executable", "REG_SZ", executable),
        ("CommandLine", "REG_SZ", "-t %1 -m %2"),
        ("JobControl", "REG_DWORD", "1"),
    )
    for name, kind, value in entries:
        code, out, err = _run(
            backend,
            domain,
            REG,
            [
                "add",
                REGISTRY_KEY,
                "/v",
                name,
                "/t",
                kind,
                "/d",
                value,
                "/f",
                "/reg:32",
            ],
            timeout=30,
        )
        if code != 0:
            raise VemuError(
                "Cannot register the WinFsp virtiofs launcher: "
                f"{(err or out).strip()}"
            )
    launcher = info.get("launcher") or {}
    if launcher.get("exists") and not launcher.get("running"):
        _run(backend, domain, SC, ["start", "WinFsp.Launcher"])


def _drives(backend: LibvirtBackend, domain: str) -> set[str]:
    code, out, err = _powershell(
        backend,
        domain,
        "Get-PSDrive -PSProvider FileSystem | Select-Object -ExpandProperty Name",
        timeout=20,
    )
    if code != 0:
        raise VemuError(f"Cannot list guest drives: {(err or out).strip()}")
    return {
        line.strip().rstrip(":").upper()
        for line in out.splitlines()
        if line.strip()
    }


def _instance(tag: str) -> str:
    digest = hashlib.sha1(tag.encode("utf-8")).hexdigest()[:10]
    return f"vemu{digest}"


def _windows_mounts(
    backend: LibvirtBackend, domain: str
) -> dict[str, dict[str, Any]]:
    if not _exists(backend, domain, LAUNCHCTL):
        return {}
    code, out, _err = _run(
        backend, domain, LAUNCHCTL, ["list"], timeout=20
    )
    if code != 0:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] != "virtiofs":
            continue
        instance = parts[1]
        info_code, info_out, _info_err = _run(
            backend,
            domain,
            LAUNCHCTL,
            ["info", "virtiofs", instance],
            timeout=20,
        )
        if info_code != 0:
            continue
        tag_match = re.search(r'-t\s+"?([^"\s]+)"?', info_out)
        drive_match = re.search(
            r'-m\s+"?(?:\\\\\.\\)?([A-Za-z]):"?', info_out
        )
        if tag_match is None:
            continue
        result[tag_match.group(1)] = {
            "drive": f"{drive_match.group(1)}:"
            if drive_match is not None
            else None,
            "instance": instance,
        }
    return result


def mount_status(
    backend: LibvirtBackend, domain: str
) -> dict[str, Any]:
    """Return currently mounted tags without a full driver preflight."""
    try:
        os_id = _guest_os(backend, domain)
    except VemuError:
        return {"guest_os": "", "mode": "unknown", "mounts": {}}
    if os_id == "mswindows":
        try:
            mounts = _windows_mounts(backend, domain)
        except VemuError:
            mounts = {}
        return {"guest_os": os_id, "mode": "windows", "mounts": mounts}
    if os_id and os_id != "mswindows":
        mounts: dict[str, dict[str, Any]] = {}
        try:
            code, out, _err = _shell(
                backend, domain, "cat /proc/mounts", timeout=15
            )
        except VemuError:
            code, out = 1, ""
        if code == 0:
            for line in out.splitlines():
                fields = line.split()
                if len(fields) < 3 or fields[2] != "virtiofs":
                    continue
                mounts[fields[0]] = {"path": fields[1], "instance": ""}
        return {"guest_os": os_id, "mode": "linux", "mounts": mounts}
    return {"guest_os": os_id, "mode": "unknown", "mounts": {}}


def _linux_mountpoint(tag: str, value: str | None = None) -> str:
    if value:
        path = value.strip()
        if not path.startswith("/"):
            raise Conflict("Linux mount point must be an absolute path.")
        return path
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", tag).strip("-") or "share"
    return f"/mnt/vemu/{safe}"


def mount(
    backend: LibvirtBackend,
    domain: str,
    shares: list[dict[str, Any]],
    *,
    configure: bool = True,
) -> dict[str, Any]:
    """Mount shares for Windows (WinFsp) or Linux (kernel virtiofs)."""
    info = detect(backend, domain)
    if not info["ready"]:
        raise Conflict(
            "VirtioFS prerequisites are missing: "
            + ", ".join(info["missing"])
            + ". Install the guest VirtioFS driver first."
        )
    existing = mount_status(backend, domain).get("mounts", {})
    mode = str(info.get("mode", "windows"))
    results: list[dict[str, Any]] = []
    if mode == "windows":
        if configure:
            _configure_launcher(backend, domain, info)
        used = _drives(backend, domain)
        free = [
            letter for letter in "ZYXWVUTSRQPONMLKJIHGFED"
            if letter not in used
        ]
        for share in shares:
            tag = str(share.get("tag") or "")
            if not tag:
                results.append(
                    {"tag": tag, "mounted": False, "error": "Missing guest tag."}
                )
                continue
            if tag in existing:
                results.append(
                    {
                        **existing[tag],
                        "tag": tag,
                        "mounted": True,
                        "already": True,
                    }
                )
                continue
            wanted = str(share.get("drive") or "").upper()[:1]
            drive = None
            if wanted and wanted not in used:
                drive = wanted
            elif free:
                drive = free.pop(0)
            if drive is None:
                results.append(
                    {
                        "tag": tag,
                        "mounted": False,
                        "error": "No free drive letter in the guest.",
                    }
                )
                continue
            code, out, err = _run(
                backend,
                domain,
                str(info["launchctl"]),
                [
                    "start",
                    "virtiofs",
                    _instance(tag),
                    tag,
                    f"\\\\.\\{drive}:",
                ],
                timeout=45,
            )
            mounted = False
            if code == 0:
                for _attempt in range(12):
                    if _exists(backend, domain, f"{drive}:\\"):
                        mounted = True
                        break
                    time.sleep(0.25)
            if not mounted:
                now = mount_status(backend, domain).get("mounts", {})
                if tag in now:
                    mounted = True
                    drive = str(now[tag].get("drive") or f"{drive}:").rstrip(":")
            results.append(
                {
                    "tag": tag,
                    "drive": f"{drive}:",
                    "mounted": mounted,
                    "exitcode": code,
                    "output": (out or err).strip(),
                }
            )
            used.add(drive)
    else:
        for share in shares:
            tag = str(share.get("tag") or "")
            if not tag:
                results.append(
                    {"tag": tag, "mounted": False, "error": "Missing guest tag."}
                )
                continue
            if tag in existing:
                results.append(
                    {
                        **existing[tag],
                        "tag": tag,
                        "mounted": True,
                        "already": True,
                    }
                )
                continue
            mountpoint = _linux_mountpoint(tag, share.get("mountpoint"))
            command = (
                f"mkdir -p {shlex.quote(mountpoint)} && "
                f"mount -t virtiofs {shlex.quote(tag)} "
                f"{shlex.quote(mountpoint)}"
            )
            code, out, err = _shell(
                backend, domain, command, timeout=45
            )
            mounted = code == 0
            if not mounted:
                now = mount_status(backend, domain).get("mounts", {})
                if tag in now:
                    mounted = True
                    mountpoint = str(now[tag].get("path") or mountpoint)
            results.append(
                {
                    "tag": tag,
                    "path": mountpoint,
                    "mounted": mounted,
                    "exitcode": code,
                    "output": (out or err).strip(),
                }
            )
    return {
        "domain": domain,
        "prerequisites": info,
        "mounts": results,
        "mounted": all(item.get("mounted") for item in results)
        if results
        else False,
    }


def unmount(
    backend: LibvirtBackend, domain: str, tag: str
) -> dict[str, Any]:
    info = detect(backend, domain)
    if not info["ready"]:
        raise Conflict(
            "VirtioFS prerequisites are missing: "
            + ", ".join(info["missing"])
        )
    mode = str(info.get("mode", "windows"))
    if mode == "linux":
        mounts = mount_status(backend, domain).get("mounts", {})
        target = (mounts.get(tag) or {}).get("path")
        if not target:
            return {
                "tag": tag,
                "unmounted": False,
                "output": "Not mounted.",
            }
        code, out, err = _shell(
            backend,
            domain,
            f"umount {shlex.quote(str(target))}",
            timeout=30,
        )
        return {
            "tag": tag,
            "unmounted": code == 0,
            "exitcode": code,
            "output": (out or err).strip(),
        }
    code, out, err = _run(
        backend,
        domain,
        str(info["launchctl"]),
        ["stop", "virtiofs", _instance(tag)],
        timeout=30,
    )
    return {
        "tag": tag,
        "unmounted": code == 0,
        "exitcode": code,
        "output": (out or err).strip(),
    }
