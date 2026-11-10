"""Service functions shared by the CLI and the daemon.

Keeping the logic here means the daemon and the direct CLI path cannot drift.
The daemon is the production entry point; the CLI can call these functions
directly for development or when no daemon is running.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import drift, hostbridge, ova
from .backend import LibvirtBackend
from .chain import (
    CHAIN_FILE,
    ChainManager,
    MultiDiskChain,
    find_commit_journals,
)
from .cloudinit import CloudInitConfig, build_cloud_init_iso
from .virtcheck import domain_type_for
from .domain import NAME_RE, Disk, DomainSpec, Network, disk_target_name
from .firmware import firmware_for
from .guest_virtiofs import mount as guest_virtiofs_mount
from .guest_virtiofs import mount_status as guest_virtiofs_mount_status
from .guest_virtiofs import unmount as guest_virtiofs_unmount
from .shares import ShareAssignment, ShareDefinition, ShareStore
from .guest_clipboard import (
    ensure_guest_clipboard_agent,
    get_guest_clipboard,
    set_guest_clipboard,
)
from .errors import (
    Conflict,
    ExternalCommandError,
    NotFound,
    ValidationError,
    VemuError,
)
from .identity import reset_linux
from . import performance
from . import runtime_profile
from .lowprio import qemu_convert_command
from .naming import render_names
from .pathpolicy import PathPolicy
from .tools import which as find_which
from .network import (
    HostReservation,
    NetworkProfile,
    NetworkStore,
    PortForward,
    apply_libvirt_forward_rules,
    apply_nft,
    nft_ruleset,
)
from .provision import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_WINDOWS_EDITION,
    edition_info,
    write_windows_floppy,
    write_windows_oobe_floppy,
    write_windows_unattend,
)
from .storage import (
    StorageStore,
    apply_fix,
    build_storage_entries,
    inspect_location,
    iter_location_files,
    qcow2_backing_file,
    scan_chains,
)
from .template import (
    TEMPLATE_FILE,
    TEMPLATE_OS_FAMILIES,
    Template,
    list_templates,
    normalize_os_family,
    resolve_template,
    seal_domain,
    validate_name,
)
from .vmconfig import (
    MANAGEMENT_NS,
    add_audio,
    add_disk,
    add_serial_console,
    add_tpm,
    add_usb_controller,
    add_virtiofs_share,
    apply_config,
    diff_summary,
    parse_config,
    remove_audio,
    remove_disk,
    remove_tpm,
    remove_virtiofs_share,
    remove_virtiofs_shares,
    set_cdrom_media,
    set_management_metadata,
)


def _clone_hostname(name: str) -> str:
    hostname = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-")[:63]
    return hostname or "vm"


def _relabel(path: str | Path) -> None:
    """Apply the storage location's SELinux context to new files."""
    restorecon = find_which("restorecon")
    if os.geteuid() != 0 or restorecon is None:
        return
    subprocess.run([restorecon, "-R", str(path)], capture_output=True, check=False)


def _apply_custom_firmware(spec: DomainSpec) -> bool:
    """Use the v-EMU firmware for new EFI domains when it is installed.

    Returns True when the Secure Boot firmware pair was used. If Secure Boot
    was requested but is unavailable, falls back to the normal firmware and
    clears ``spec.secure_boot``.
    """
    if spec.firmware != "efi":
        spec.secure_boot = False
        return False
    if spec.secure_boot and spec.arch != "x86_64":
        spec.secure_boot = False
    files = firmware_for(spec.arch, secure_boot=spec.secure_boot)
    secure = bool(spec.secure_boot and files is not None)
    if files is None and spec.secure_boot:
        spec.secure_boot = False
        files = firmware_for(spec.arch)
    if files is None:
        return False
    spec.loader_path = str(files.loader)
    spec.loader_format = files.loader_format
    spec.nvram_template = str(files.nvram_template)
    spec.nvram_format = files.nvram_format
    spec.loader_secure = secure
    return secure


def _domain_dict(domain) -> dict[str, Any]:
    return {
        "name": domain.name,
        "uuid": domain.uuid,
        "state": domain.state,
        "disks": domain.disks,
        "autostart": domain.autostart,
        "firmware": domain.firmware,
        "managed_by": domain.managed_by,
        "os_type": domain.os_type,
    }


def _require_writable(
    backend: LibvirtBackend,
    domain: str,
    *,
    full: bool = False,
):
    info = backend.info(domain)
    managed = getattr(info, "managed_by", "v-EMU (full)")
    if managed == "External":
        raise Conflict(
            f"Domain '{domain}' is external/read-only. Adopt it into v-EMU "
            "before changing it."
        )
    if full and managed != "v-EMU (full)":
        if managed == "v-EMU (partial)":
            raise Conflict(
                f"Domain '{domain}' is Partial; migrate it to Full before "
                "using disk/chain operations."
            )
        raise Conflict(
            f"Domain '{domain}' does not have a v-EMU-managed disk chain."
        )
    return info


def adopt_domain(
    domain: str,
    mode: str = "partial",
    *,
    backup_dir: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Adopt an External VM without moving its disks (Partial/Read-Only)."""
    if mode not in {"read-only", "partial"}:
        raise ValidationError("Adopt mode must be read-only or partial.")
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        if mode == "read-only":
            return {
                "domain": domain,
                "mode": "read-only",
                "managed_by": info.managed_by,
                "changed": False,
            }
        if info.managed_by != "External":
            raise Conflict(
                f"Domain '{domain}' is already managed by v-EMU."
            )
        if info.state != "stopped":
            raise Conflict(
                "Shut down the VM before Partial adoption."
            )
        xml = backend.config_xml(domain)
        backups = Path(
            backup_dir
            or os.environ.get("VEMU_BACKUP_DIR")
            or "/var/lib/vemu/backups"
        ).expanduser() / domain
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backups / f"{stamp}-adopt.xml"
        backup_path.write_text(xml, encoding="utf-8")
        updated = set_management_metadata(
            xml,
            mode="partial",
            managed_by="v-EMU (partial)",
            origin="external",
            backup=str(backup_path),
        )
        backend.redefine_domain(domain, updated)
    return {
        "domain": domain,
        "mode": "partial",
        "managed_by": "v-EMU (partial)",
        "backup": str(backup_path),
        "changed": True,
    }


def domain_rescue_start(
    domain: str,
    *,
    backup_dir: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Start an External VM for manual rescue without adopting it."""
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        if info.managed_by != "External":
            raise Conflict(
                "Rescue start is only for External/read-only VMs."
            )
        if info.state != "stopped":
            raise Conflict(f"Domain '{domain}' is not stopped.")
        xml = backend.config_xml(domain)
        backups = Path(
            backup_dir
            or os.environ.get("VEMU_BACKUP_DIR")
            or "/var/lib/vemu/backups"
        ).expanduser() / domain
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backups / f"{stamp}-rescue.xml"
        backup_path.write_text(xml, encoding="utf-8")
        backend.start(domain)
    return {
        "domain": domain,
        "started": True,
        "backup": str(backup_path),
        "managed_by": "External",
    }


def domain_rescue_shutdown(
    domain: str,
    uri: str = "qemu:///system",
    *,
    force: bool = False,
    wait: bool = True,
) -> dict[str, Any]:
    """Shut down a running External VM without adopting it."""
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        if info.managed_by != "External":
            raise Conflict(
                "Rescue shutdown is only for External/read-only VMs."
            )
        if info.state != "running":
            raise Conflict(f"Domain '{domain}' is not running.")
        backend.shutdown(domain, force=force, wait=wait)
    return {
        "domain": domain,
        "shutdown": True,
        "force": force,
        "managed_by": "External",
    }


@contextmanager
def _migration_lock():
    """Serialize host-heavy Full migrations across daemon/CLI processes."""
    run_dir = os.environ.get("VEMU_RUN_DIR")
    candidates = [
        Path(run_dir or "/run/vemu") / "migrate.lock",
        Path(tempfile.gettempdir()) / f"vemu-migrate-{os.geteuid()}.lock",
    ]
    handle = None
    last_error: OSError | None = None
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+")
            break
        except OSError as exc:
            last_error = exc
    if handle is None:
        raise Conflict(f"Cannot create a migration lock: {last_error}")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Conflict(
                "Another Full migration is already running. Wait for it to "
                "finish before starting another."
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def domains_list(uri: str = "qemu:///system") -> list[dict[str, Any]]:
    with LibvirtBackend(uri) as backend:
        domains = backend.list_domains()
    return [_domain_dict(domain) for domain in domains]


def domain_start(domain: str, uri: str = "qemu:///system") -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        removed_usb = backend.start(domain)
        _apply_runtime_profile(backend, domain)
        payload = _domain_dict(backend.info(domain))
        if removed_usb:
            payload["usb_removed"] = removed_usb
        return payload


def runtime_profile_get() -> dict[str, Any]:
    return runtime_profile.profile_payload()


def runtime_profile_set(
    profile: str, uri: str = "qemu:///system"
) -> dict[str, Any]:
    """Persist a best-effort profile and apply it to managed domains."""
    key = runtime_profile.save_profile(profile)
    payload = runtime_profile.profile_payload(key)
    applied: list[str] = []
    warnings: list[str] = []
    with LibvirtBackend(uri) as backend:
        for info in backend.list_domains():
            if info.managed_by == "External":
                continue
            try:
                result = backend.apply_resource_profile(info.name, key)
            except (VemuError, AttributeError) as exc:
                warnings.append(f"{info.name}: {exc}")
                continue
            applied.append(info.name)
            warnings.extend(
                f"{info.name}: {item}"
                for item in result.get("warnings", [])
            )
    return {**payload, "applied": applied, "warnings": warnings}


def _apply_runtime_profile(backend, domain: str) -> list[str]:
    """Best-effort: never fail a VM operation because weights are unsupported."""
    apply = getattr(backend, "apply_resource_profile", None)
    if apply is None:
        return []
    try:
        result = apply(domain)
    except (VemuError, AttributeError):
        return []
    return list(result.get("warnings", []))


def domain_stats(domain: str, uri: str = "qemu:///system") -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        return backend.domain_stats(domain)

def domain_drift(domain: str, uri: str = "qemu:///system") -> dict[str, Any]:
    """Return baseline/current XML hash and drift status for one domain."""
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        xml = backend.config_xml(domain)
    details = drift.details(xml)
    changes: list[str] = []
    if details.get("status") == "drifted":
        baseline_xml = drift.load_baseline(domain)
        if baseline_xml:
            changes = drift.diff(baseline_xml, xml)
    return {
        "domain": domain,
        "managed_by": info.managed_by,
        **details,
        "changes": changes,
    }


def domain_baseline_refresh(
    domain: str | None = None, uri: str = "qemu:///system"
) -> dict[str, Any]:
    """Pin the current XML of managed domains as the drift baseline."""
    refreshed: list[str] = []
    with LibvirtBackend(uri) as backend:
        names = (
            [domain]
            if domain
            else [
                item.name
                for item in backend.list_domains()
                if item.managed_by != "External"
            ]
        )
        for name in names:
            _require_writable(backend, name)
            xml = backend.config_xml(name)
            backend.redefine_domain(name, drift.set_baseline(xml))
            refreshed.append(name)
    return {"refreshed": refreshed}




def domain_set_autostart(
    domain: str, enabled: bool, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        backend.set_autostart(domain, enabled)
        return _domain_dict(backend.info(domain))


def _delete_backup_root() -> Path:
    configured = os.environ.get("VEMU_BACKUP_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.geteuid() == 0:
        return Path("/var/lib/vemu/backups")
    return Path.home() / ".local" / "state" / "vemu" / "backups"


def domain_delete(
    domain: str,
    *,
    delete_storage: bool = False,
    force: bool = False,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Undefine a VM, optionally deleting v-EMU-managed chain directories."""
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain)
        if info.state != "stopped" and not force:
            raise Conflict(
                "Shut the VM down before deleting it, or use force."
            )
        disks = list(info.disks)
        backend.undefine(domain, force=force)
    deleted: list[str] = []
    warnings: list[str] = []
    metadata_deleted: list[str] = []
    share_assignments = 0
    if delete_storage:
        for path in disks:
            disk_path = Path(path).expanduser().resolve()
            directory = disk_path.parent
            if str(directory) in deleted:
                continue
            if not (directory / CHAIN_FILE).is_file():
                warnings.append(f"Kept unmanaged disk: {disk_path}")
                continue
            try:
                ChainManager(directory).load()
            except VemuError as exc:
                warnings.append(f"Kept {directory}: {exc}")
                continue
            shutil.rmtree(directory)
            deleted.append(str(directory))
        backup_dir = _delete_backup_root() / domain
        if backup_dir.exists():
            try:
                shutil.rmtree(backup_dir)
                metadata_deleted.append(str(backup_dir))
            except OSError as exc:
                warnings.append(f"Kept XML backups {backup_dir}: {exc}")
        baseline = drift.baseline_path(domain)
        if baseline.is_file():
            try:
                baseline.unlink()
                metadata_deleted.append(str(baseline))
            except OSError as exc:
                warnings.append(f"Kept drift baseline {baseline}: {exc}")
        try:
            share_assignments = ShareStore().purge_domain(domain)
        except VemuError as exc:
            warnings.append(f"Could not remove share assignments: {exc}")
    return {
        "domain": domain,
        "deleted": True,
        "storage_deleted": deleted,
        "metadata_deleted": metadata_deleted,
        "share_assignments_deleted": share_assignments,
        "warnings": warnings,
    }


def domain_shutdown(
    domain: str,
    uri: str = "qemu:///system",
    *,
    force: bool = False,
    wait: bool = True,
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        backend.shutdown(domain, force=force, wait=wait)
        return _domain_dict(backend.info(domain))


def domain_resume(domain: str, uri: str = "qemu:///system") -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        backend.resume(domain)
        return _domain_dict(backend.info(domain))


def domain_force_off(domain: str, uri: str = "qemu:///system") -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        backend.destroy(domain)
        return _domain_dict(backend.info(domain))


def domain_restart(domain: str, uri: str = "qemu:///system") -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        backend.shutdown(domain)
        backend.start(domain)
        return _domain_dict(backend.info(domain))


def _chain_dir_for_domain(backend: LibvirtBackend, domain: str) -> Path:
    info = backend.info(domain)
    if not getattr(info, "disks", None):
        raise NotFound(f"Domain '{domain}' has no disk.")
    chain_dir = Path(info.disks[0]).parent
    if not (chain_dir / "chain.json").is_file():
        raise NotFound(
            f"Domain '{domain}' is not managed by v-EMU: no chain.json next to "
            f"{info.disks[0]}."
        )
    return chain_dir


def require_chain_writable(
    chain: str | Path, uri: str = "qemu:///system"
) -> None:
    """Reject chain mutations when a linked domain is not v-EMU full."""
    chain_dir = Path(chain).expanduser().resolve()
    with LibvirtBackend(uri) as backend:
        for domain in backend.list_domains():
            if any(
                Path(disk).expanduser().resolve().parent == chain_dir
                for disk in domain.disks
            ):
                if domain.managed_by != "v-EMU (full)":
                    raise Conflict(
                        f"Chain '{chain_dir}' belongs to a domain that is not "
                        "v-EMU (full); adopt/migrate it first."
                    )
                return
    # An unreferenced chain can be maintained as a legacy/admin object.


def _eject_all_cdroms(
    backend: LibvirtBackend, domain: str
) -> list[str]:
    """Eject every attached CD/DVD medium from a stopped domain.

    Snapshot chains only cover data disks, but the live XML can still point at
    an ISO that is later deleted. Cockpit snapshots restore the XML and fail
    when the path is gone; vEMU avoids the problem by leaving an empty tray
    before creating or reverting a snapshot.
    """
    xml = backend.config_xml(domain)
    root = ET.fromstring(xml)
    ejected: list[str] = []
    changed = False
    for disk in root.findall("./devices/disk"):
        if disk.get("device") != "cdrom":
            continue
        source = disk.find("source")
        if source is None or not source.get("file"):
            continue
        target = disk.find("target")
        device = target.get("dev") if target is not None else None
        set_cdrom_media(disk, None)
        ejected.append(device or "cdrom")
        changed = True
    if changed:
        backend.redefine_domain(
            domain, ET.tostring(root, encoding="unicode")
        )
    return ejected


def _chain_source_changes(
    backend: LibvirtBackend, domain: str, chain: Any
) -> dict[str, str]:
    """Map the domain's data disks to the chain's active disks.

    A v1 chain belongs to exactly one domain disk. It is identified by its
    layer paths, not by XML order: a VM may also have an unmanaged data disk,
    and the old ``disks[0]`` assumption could replace the wrong disk. A V2
    chain maps by target and rejects a second disk that points at a chain
    layer under a different target.
    """
    root = ET.fromstring(backend.config_xml(domain))

    def resolved(value: str | Path) -> str:
        return str(Path(value).expanduser().resolve())

    changes: dict[str, str] = {}
    if isinstance(chain, MultiDiskChain):
        wanted = {
            target: disk.active_disk()
            for target, disk in chain.disks.items()
        }
        chain_paths = {
            resolved(path)
            for disk in chain.disks.values()
            for path in [disk.base, *(layer.path for layer in disk.layers)]
        }
        matched: set[str] = set()
        for disk in root.findall("./devices/disk"):
            if disk.get("device") != "disk":
                continue
            target = disk.find("target")
            source = disk.find("source")
            if target is None or source is None:
                continue
            dev = target.get("dev")
            current = source.get("file")
            if not dev or not current:
                continue
            if dev in wanted:
                matched.add(dev)
                if resolved(current) != resolved(wanted[dev]):
                    changes[current] = wanted[dev]
            elif resolved(current) in chain_paths:
                raise Conflict(
                    f"Disk '{dev}' points at a layer of this chain but its "
                    "target is not part of the chain. Detach the duplicate "
                    "disk or convert the VM to chain v2."
                )
        missing = set(wanted) - matched
        if missing:
            raise NotFound(
                "Chain disk targets were not found in domain XML: "
                + ", ".join(sorted(missing))
            )
        return changes

    chain_paths = {
        resolved(path)
        for path in [chain.base, *(layer.path for layer in chain.layers)]
    }
    matches: list[str] = []
    for disk in root.findall("./devices/disk"):
        if disk.get("device") != "disk":
            continue
        source = disk.find("source")
        if source is None:
            continue
        current = source.get("file")
        if current and resolved(current) in chain_paths:
            matches.append(current)
    if not matches:
        raise NotFound(
            f"None of the data disks of '{domain}' belongs to this chain."
        )
    if len(matches) > 1:
        raise Conflict(
            f"Multiple data disks of '{domain}' point at layers of the same "
            "chain. Detach the duplicate disk before creating a snapshot."
        )
    current = matches[0]
    active = chain.active_disk()
    if resolved(current) != resolved(active):
        changes[current] = active
    return changes



def snapshots_info(domain: str, uri: str = "qemu:///system") -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        try:
            chain_dir = _chain_dir_for_domain(backend, domain)
        except NotFound as exc:
            try:
                snapshots = backend.snapshot_details(domain)
            except VemuError:
                snapshots = []
            return {
                "managed": False,
                "domain": domain,
                "running": info.state == "running",
                "reason": str(exc),
                "snapshots": snapshots,
                "external_snapshots": any(
                    item.get("kind") == "external"
                    for item in snapshots
                ),
            }
        chain = ChainManager(chain_dir).info()
        return {
            "managed": True,
            "domain": domain,
            "running": info.state == "running",
            "chain": str(chain_dir),
            **chain,
        }


def _require_stopped(backend: LibvirtBackend, domain: str) -> Path:
    info = _require_writable(backend, domain, full=True)
    if info.state != "stopped":
        raise Conflict(f"Shut down '{domain}' before managing snapshots.")
    return _chain_dir_for_domain(backend, domain)


SNAPSHOT_DESCRIPTION_MAX = 4096


def _snapshot_description(value: object) -> str:
    text = (
        str(value or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )
    if len(text) > SNAPSHOT_DESCRIPTION_MAX:
        raise ValidationError(
            "Snapshot description must be at most "
            f"{SNAPSHOT_DESCRIPTION_MAX} characters."
        )
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ValidationError(
            "Snapshot description contains unsupported control characters."
        )
    return text


def snapshots_create(
    domain: str,
    label: str = "",
    uri: str = "qemu:///system",
    *,
    description: str = "",
) -> dict[str, Any]:
    description = _snapshot_description(description)
    with LibvirtBackend(uri) as backend:
        chain_dir = _require_stopped(backend, domain)
        manager = ChainManager(chain_dir)
        before = backend.config_xml(domain)
        # Validate disk mapping before creating a layer. Duplicate chain
        # layers or a missing chain disk must not leave an orphan overlay.
        _chain_source_changes(backend, domain, manager.load())
        ejected = _eject_all_cdroms(backend, domain)
        try:
            snapshot = manager.snapshot(label, description)
            _relabel(chain_dir)
            changes = _chain_source_changes(
                backend, domain, manager.load()
            )
            if changes:
                backend.update_disk_sources(domain, changes)
        except Exception:
            # Do not leave the VM with ejected media when no snapshot was
            # created. Best-effort rollback; the original error still wins.
            try:
                backend.redefine_domain(domain, before)
            except VemuError:
                pass
            raise
        return {
            "layer": snapshot.to_dict(),
            "ejected": ejected,
            **manager.info(),
        }


def snapshots_update(
    domain: str,
    layer: str,
    *,
    label: str | None = None,
    description: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Update snapshot name/description without changing the disk chain."""
    if label is None and description is None:
        raise ValidationError("No snapshot metadata was provided.")
    if label is not None:
        label = str(label).strip()
    if description is not None:
        description = _snapshot_description(description)
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain, full=True)
        chain_dir = _chain_dir_for_domain(backend, domain)
        manager = ChainManager(chain_dir)
        snapshot = manager.update_snapshot(
            layer, label=label, description=description
        )
        _relabel(chain_dir)
        return {"layer": snapshot.to_dict(), **manager.info()}


def snapshots_revert(
    domain: str, layer: str, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        chain_dir = _require_stopped(backend, domain)
        manager = ChainManager(chain_dir)
        before = backend.config_xml(domain)
        _chain_source_changes(backend, domain, manager.load())
        ejected = _eject_all_cdroms(backend, domain)
        try:
            manager.revert(layer)
            _relabel(chain_dir)
            changes = _chain_source_changes(
                backend, domain, manager.load()
            )
            if changes:
                backend.update_disk_sources(domain, changes)
        except Exception:
            try:
                backend.redefine_domain(domain, before)
            except VemuError:
                pass
            raise
        return {**manager.info(), "ejected": ejected}



def snapshots_delete(
    domain: str,
    layer: str,
    uri: str = "qemu:///system",
    *,
    force: bool = False,
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        chain_dir = _require_stopped(backend, domain)
        manager = ChainManager(chain_dir)
        manager.delete(layer, force=force)
        _relabel(chain_dir)
        changes = _chain_source_changes(backend, domain, manager.load())
        if changes:
            backend.update_disk_sources(domain, changes)
        return manager.info()


def console_info(domain: str, uri: str = "qemu:///system") -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        return {"uri": uri, **backend.console_info(domain)}


def serial_info(
    domain: str, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        return {"uri": uri, **backend.serial_info(domain)}


def vm_serial_add(
    domain: str,
    *,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Add a serial/console pty to a stopped domain."""
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain)
        if info.state != "stopped":
            raise Conflict(
                f"Domain '{domain}' must be stopped to add a serial console."
            )
        before = backend.config_xml(domain)
        after = add_serial_console(before)
        if after != before:
            backend.redefine_domain(domain, after)
    return {"domain": domain, "serial": True, "changed": after != before}


def _human_bytes(value: int | float) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


IMPORT_PRESETS: dict[str, dict[str, Any]] = {
    "generic": {
        "label": "Generic VM",
        "vcpus": 2,
        "memory_mib": 2048,
        "firmware": "bios",
        "machine": "q35",
        "disk_bus": "sata",
        "nic_model": "e1000e",
        "nic_count": 1,
        "video": "vga",
        "serial_console": False,
        "data_disk_gb": 0,
    },
    "appliance": {
        "label": "Network appliance",
        "vcpus": 1,
        "memory_mib": 4096,
        "firmware": "bios",
        "machine": "q35",
        "disk_bus": "virtio",
        "nic_model": "virtio",
        "nic_count": 2,
        "video": "vga",
        "serial_console": True,
        "data_disk_gb": 0,
    },
    "vcsa": {
        "label": "VMware VCSA (Tiny)",
        "vcpus": 2,
        "memory_mib": 14336,
        "firmware": "efi",
        "machine": "q35",
        "disk_bus": "sata",
        "nic_model": "e1000e",
        "nic_count": 1,
        "video": "virtio",
        "serial_console": False,
        "data_disk_gb": 0,
    },
}


def _import_specs(
    preset: str,
    *,
    vcpus: int | None = None,
    memory_mib: int | None = None,
    firmware: str | None = None,
    machine: str | None = None,
    disk_bus: str | None = None,
    nic_model: str | None = None,
    nic_count: int | None = None,
    serial_console: bool | None = None,
    data_disk_gb: int | None = None,
    video: str | None = None,
) -> dict[str, Any]:
    if preset not in IMPORT_PRESETS:
        raise ValidationError(f"Unknown import preset: {preset}")
    specs = dict(IMPORT_PRESETS[preset])
    specs.pop("label", None)
    overrides = {
        "vcpus": vcpus,
        "memory_mib": memory_mib,
        "firmware": firmware,
        "machine": machine,
        "disk_bus": disk_bus,
        "nic_model": nic_model,
        "nic_count": nic_count,
        "serial_console": serial_console,
        "data_disk_gb": data_disk_gb,
        "video": video,
    }
    for key, value in overrides.items():
        if value is not None:
            specs[key] = value
    if specs["firmware"] not in {"bios", "efi"}:
        raise ValidationError("Firmware must be bios or efi.")
    if specs["disk_bus"] not in {
        "virtio", "sata", "scsi", "ide", "nvme"
    }:
        raise ValidationError(f"Unsupported disk bus: {specs['disk_bus']}")
    if specs["video"] not in {"virtio", "vga", "qxl", "cirrus", "vmvga", "bochs"}:
        raise ValidationError("Unsupported video model.")
    if specs["nic_model"] not in {
        "virtio", "e1000e", "e1000", "rtl8139", "pcnet"
    }:
        raise ValidationError(
            f"Unsupported NIC model: {specs['nic_model']}"
        )
    if specs["vcpus"] < 1:
        raise ValidationError("At least one vCPU is required.")
    if specs["memory_mib"] < 256:
        raise ValidationError("Memory must be at least 256 MiB.")
    if int(specs["nic_count"]) < 1:
        raise ValidationError("At least one NIC is required.")
    if int(specs["data_disk_gb"]) < 0:
        raise ValidationError("Data disk size must not be negative.")
    return specs


def _qemu_img_info(qemu_img: str, source: str) -> dict[str, Any]:
    result = subprocess.run(
        [qemu_img, "info", "--output=json", "-U", source],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ExternalCommandError(
            f"qemu-img info {source}", result.returncode, result.stderr
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExternalCommandError(
            f"qemu-img info {source}", 0, result.stdout
        ) from exc
    return data if isinstance(data, dict) else {}


def _migration_data_disks(config_xml: str) -> list[dict[str, Any]]:
    return [
        disk
        for disk in parse_config(config_xml).get("disks", [])
        if disk.get("device") == "disk" and disk.get("source")
    ]


def _qemu_measure(
    qemu_img: str,
    source: str,
    *,
    input_format: str | None = None,
    snapshot: str | None = None,
) -> dict[str, Any]:
    command = [qemu_img, "measure", "--output=json", "-O", "qcow2"]
    if input_format:
        command += ["-f", input_format]
    if snapshot:
        command += ["-l", snapshot]
    command.append(source)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ExternalCommandError(
            " ".join(command),
            result.returncode,
            result.stderr or result.stdout,
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExternalCommandError(
            " ".join(command), 0, result.stdout
        ) from exc
    return payload if isinstance(payload, dict) else {}


def _management_fields(root: ET.Element) -> dict[str, str]:
    metadata = root.find(f".//{{{MANAGEMENT_NS}}}management")
    if metadata is None:
        return {}
    return {
        key: metadata.get(key) or ""
        for key in ("mode", "managed_by", "origin", "backup")
    }


def _rewrite_chain_path(path: str, old_root: Path, new_root: Path) -> str:
    try:
        relative = Path(path).relative_to(old_root)
    except ValueError:
        return path
    return str(new_root / relative)


def _apply_new_firmware(
    root: ET.Element,
    *,
    files: Any,
    nvram_path: Path,
    secure: bool,
) -> None:
    """Point an EFI domain at the v-EMU firmware and a fresh NVRAM."""
    os_element = root.find("os")
    if os_element is None:
        raise ValidationError("VM XML has no <os> element.")
    # Explicit custom paths are incompatible with libvirt's firmware
    # auto-selection; drop the selector and its feature block.
    os_element.attrib.pop("firmware", None)
    selector = os_element.find("firmware")
    if selector is not None:
        os_element.remove(selector)
    loader = os_element.find("loader")
    if loader is None:
        loader = ET.SubElement(os_element, "loader")
    loader.set("readonly", "yes")
    loader.set("type", "pflash")
    loader.set("format", str(files.loader_format))
    if secure:
        loader.set("secure", "yes")
    else:
        loader.attrib.pop("secure", None)
    loader.text = str(files.loader)
    nvram = os_element.find("nvram")
    if nvram is None:
        nvram = ET.SubElement(os_element, "nvram")
    nvram.set("template", str(files.nvram_template))
    nvram.set("templateFormat", str(files.nvram_format))
    nvram.set("format", str(files.nvram_format))
    nvram.text = str(nvram_path)
    if secure:
        features = root.find("features")
        if features is None:
            features = ET.SubElement(root, "features")
        smm = features.find("smm")
        if smm is None:
            smm = ET.SubElement(features, "smm")
        smm.set("state", "on")


def _firmware_plan(
    root: ET.Element, *, domain: str, new_firmware: bool
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return firmware reset details and blocking reasons."""
    if not new_firmware:
        return None, []
    os_type = root.find("os/type")
    loader = root.find("os/loader")
    if os_type is None or loader is None:
        return None, [
            "The VM does not use an EFI loader; --new-firmware has no effect."
        ]
    arch = str(os_type.get("arch", "x86_64"))
    secure = loader.get("secure") == "yes"
    files = firmware_for(arch, secure_boot=secure)
    if files is None:
        return None, [
            f"v-EMU firmware is not installed for {arch} "
            f"(secure boot={secure})."
        ]
    nvram_path = (
        Path("/var/lib/libvirt/qemu/nvram")
        / f"{domain}_VARS.{files.nvram_format}"
    )
    return {
        "arch": arch,
        "secure_boot": secure,
        "loader": str(files.loader),
        "loader_format": files.loader_format,
        "nvram_template": str(files.nvram_template),
        "nvram_format": files.nvram_format,
        "nvram_path": str(nvram_path),
    }, []


def vm_rename_plan(
    domain: str,
    new_name: str,
    *,
    rename_disks: bool = True,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Validate a VM rename without changing the domain or disk files."""
    new_name = new_name.strip()
    if not NAME_RE.match(new_name):
        raise ValidationError(
            "VM name must start with a letter or digit, contain only letters, "
            "digits, '.', '_', '-', '[', ']', and be at most 64 characters."
        )
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        config_xml = backend.config_xml(domain)
        existing = {item.name for item in backend.list_domains()}
        snapshots: list[str] = []
        snapshot_details: list[dict[str, Any]] = []
        if hasattr(backend, "snapshot_details"):
            try:
                snapshot_details = backend.snapshot_details(domain)
            except VemuError:
                snapshot_details = []
        if snapshot_details:
            snapshots = [
                str(item.get("name", "")) for item in snapshot_details
            ]
        elif hasattr(backend, "snapshot_names"):
            try:
                snapshots = backend.snapshot_names(domain)
            except VemuError:
                snapshots = []
    root = ET.fromstring(config_xml)
    metadata = _management_fields(root)
    managed_by = metadata.get("managed_by") or info.managed_by
    mode = metadata.get("mode", "")
    full = mode == "full" or managed_by == "v-EMU (full)"
    reasons: list[str] = []
    warnings: list[str] = []
    if new_name == domain:
        reasons.append("The new name is the same as the current name.")
    if new_name in existing:
        reasons.append(f"Domain '{new_name}' already exists.")
    if info.state != "stopped":
        reasons.append("The VM must be shut down before renaming.")
    if managed_by == "External":
        reasons.append(
            "External/read-only domains cannot be renamed; adopt the VM first."
        )
    data_disks = [
        disk
        for disk in root.findall("./devices/disk")
        if disk.get("device") == "disk"
    ]
    chain_dir: Path | None = None
    new_chain_dir: Path | None = None
    disks: list[dict[str, str]] = []
    if rename_disks:
        if not full:
            reasons.append(
                "Disk rename is only supported for v-EMU (full) chains; "
                "use domain-only rename for this VM."
            )
        elif not data_disks:
            reasons.append("The VM has no data disk to rename.")
        else:
            first_source = data_disks[0].find("source")
            first_path = (
                first_source.get("file") if first_source is not None else None
            )
            if not first_path:
                reasons.append("The first data disk has no file source.")
            else:
                chain_dir = Path(first_path).parent.resolve()
                new_chain_dir = chain_dir.parent / new_name
                if not (chain_dir / CHAIN_FILE).is_file():
                    reasons.append(
                        f"No v-EMU chain was found next to {first_path}."
                    )
                elif new_chain_dir.exists():
                    reasons.append(
                        f"Chain destination already exists: {new_chain_dir}"
                    )
                else:
                    try:
                        manager = ChainManager(chain_dir)
                        chain = manager.load()
                        if isinstance(chain, MultiDiskChain):
                            wanted = {
                                target: disk.active_disk()
                                for target, disk in chain.disks.items()
                            }
                        else:
                            target = (
                                data_disks[0].find("target").get("dev")
                                if data_disks[0].find("target") is not None
                                else ""
                            )
                            wanted = {str(target): chain.active_disk()}
                        for disk in data_disks:
                            target = disk.find("target")
                            source = disk.find("source")
                            if target is None or source is None:
                                continue
                            dev = str(target.get("dev", ""))
                            path = source.get("file")
                            if not path:
                                continue
                            if dev not in wanted:
                                reasons.append(
                                    f"Data disk '{dev}' is not part of the "
                                    "v-EMU chain."
                                )
                                continue
                            if Path(path).parent.resolve() != chain_dir:
                                reasons.append(
                                    f"Data disk '{dev}' is outside the chain "
                                    f"directory: {path}"
                                )
                                continue
                            disks.append(
                                {
                                    "target": dev,
                                    "old": str(wanted[dev]),
                                    "new": _rewrite_chain_path(
                                        str(wanted[dev]),
                                        chain_dir,
                                        new_chain_dir,
                                    ),
                                }
                            )
                    except VemuError as exc:
                        reasons.append(str(exc))
    else:
        warnings.append(
            "Only the libvirt domain name will change; disk paths stay as-is."
        )
    if snapshots and rename_disks:
        warnings.append(
            "libvirt snapshot metadata is not renamed with the disk chain: "
            + ", ".join(snapshots)
        )
    return {
        "domain": domain,
        "new_name": new_name,
        "state": info.state,
        "managed_by": managed_by,
        "mode": mode,
        "full": full,
        "rename_disks": bool(rename_disks and full and chain_dir),
        "chain_dir": str(chain_dir) if chain_dir else None,
        "new_chain_dir": str(new_chain_dir) if new_chain_dir else None,
        "disks": disks,
        "snapshots": snapshots,
        "warnings": warnings,
        "reasons": reasons,
        "supported": not reasons,
    }


def vm_rename(
    domain: str,
    new_name: str,
    *,
    rename_disks: bool = True,
    backup_dir: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Rename a stopped VM and, for Full chains, its disk directory."""
    plan = vm_rename_plan(
        domain, new_name, rename_disks=rename_disks, uri=uri
    )
    if not plan["supported"]:
        raise Conflict("; ".join(plan["reasons"]))
    old_chain = Path(plan["chain_dir"]) if plan["chain_dir"] else None
    new_chain = (
        Path(plan["new_chain_dir"]) if plan["new_chain_dir"] else None
    )
    backups = Path(
        backup_dir
        or os.environ.get("VEMU_BACKUP_DIR")
        or "/var/lib/vemu/backups"
    ).expanduser() / domain
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backups / f"{stamp}-rename.xml"
    with LibvirtBackend(uri) as backend:
        if backend.info(domain).state != "stopped":
            raise Conflict(f"Shut down '{domain}' before renaming.")
        config_xml = backend.config_xml(domain)
        backup_path.write_text(config_xml, encoding="utf-8")
        renamed = False
        chain_manager: ChainManager | None = None
        new_manager: ChainManager | None = None
        shares_changed = 0
        try:
            if plan["rename_disks"] and old_chain and new_chain:
                chain_manager = ChainManager(old_chain)
                new_manager = chain_manager.rename(new_chain)
            backend.rename_domain(domain, new_name)
            renamed = True
            root = ET.fromstring(backend.config_xml(new_name))
            changed = False
            if old_chain and new_chain:
                for disk in root.findall("./devices/disk"):
                    if disk.get("device") != "disk":
                        continue
                    source = disk.find("source")
                    if source is None:
                        continue
                    current = source.get("file")
                    if current:
                        rewritten = _rewrite_chain_path(
                            current, old_chain, new_chain
                        )
                        if rewritten != current:
                            source.set("file", rewritten)
                            changed = True
            metadata = _management_fields(root)
            updated = ET.tostring(root, encoding="unicode")
            if metadata:
                updated = set_management_metadata(
                    updated,
                    mode=metadata.get("mode", "full"),
                    managed_by=metadata.get("managed_by", "v-EMU (full)"),
                    origin=metadata.get("origin", "vemu"),
                    backup=metadata.get("backup") or str(backup_path),
                )
                changed = True
            if changed:
                backend.redefine_domain(new_name, updated)
            shares_changed = ShareStore().rename_domain(domain, new_name)
        except Exception:
            if shares_changed:
                try:
                    ShareStore().rename_domain(new_name, domain)
                except Exception:
                    pass
            if renamed:
                try:
                    backend.rename_domain(new_name, domain)
                    backend.redefine_domain(domain, config_xml)
                except Exception:
                    pass
            if new_manager is not None and new_chain and new_chain.exists():
                try:
                    new_manager.rename(old_chain)
                except Exception:
                    pass
            raise
    if new_chain:
        _relabel(new_chain)
    return {
        **plan,
        "domain": new_name,
        "old_name": domain,
        "backup": str(backup_path),
        "shares_updated": shares_changed,
    }


def _import_destination(
    name: str, storage_config: str | None
) -> tuple[Path | None, Path | None]:
    locations = StorageStore(storage_config).load()
    labs = next(
        (item["path"] for item in locations if item["role"] == "labs"),
        None,
    )
    if not labs:
        return None, None
    root = Path(labs).expanduser()
    return root, root / name


def _import_disk_target(bus: str, index: int) -> str:
    return disk_target_name(bus, index)


def _import_nic_model(model: str, fallback: str) -> tuple[str, str | None]:
    value = (model or "").lower()
    if value in {"vmxnet3", "vmxnet2", "vmxnet"}:
        return fallback, f"OVF NIC model '{model}' is not KVM-native; using {fallback}."
    if value in {"e1000", "e1000e", "rtl8139", "pcnet", "virtio"}:
        return value, None
    return fallback, f"Unknown OVF NIC model '{model}'; using {fallback}."


def vm_import_plan(
    kind: str,
    source: str,
    name: str,
    *,
    preset: str = "generic",
    network: str | None = None,
    network_config: str | None = None,
    storage_config: str | None = None,
    vcpus: int | None = None,
    memory_mib: int | None = None,
    firmware: str | None = None,
    machine: str | None = None,
    disk_bus: str | None = None,
    nic_model: str | None = None,
    nic_count: int | None = None,
    serial_console: bool | None = None,
    data_disk_gb: int | None = None,
    video: str | None = None,
    nics: list[dict[str, Any]] | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Inspect a qcow2/OVA and build an import plan without changing anything."""
    kind = kind.lower().strip()
    if kind not in {"disk", "ova"}:
        raise ValidationError("Import kind must be 'disk' or 'ova'.")
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise NotFound(f"Import source was not found: {source_path}")
    if not NAME_RE.match(name):
        raise ValidationError(
            "VM name must start with a letter or digit, contain only letters, "
            "digits, '.', '_', '-', '[', ']', and be at most 64 characters."
        )
    specs = _import_specs(
        preset,
        vcpus=vcpus,
        memory_mib=memory_mib,
        firmware=firmware,
        machine=machine,
        disk_bus=disk_bus,
        nic_model=nic_model,
        nic_count=nic_count,
        serial_console=serial_console,
        data_disk_gb=data_disk_gb,
        video=video,
    )
    reasons: list[str] = []
    warnings: list[str] = []
    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        reasons.append("qemu-img was not found.")
    labs, destination = _import_destination(name, storage_config)
    if labs is None:
        reasons.append("No storage location with role 'labs' is registered.")
    elif not labs.is_dir():
        reasons.append(f"Storage location does not exist: {labs}")
    if destination is not None and destination.exists():
        reasons.append(f"Destination already exists: {destination}")

    disks: list[dict[str, Any]] = []
    imported_nics: list[dict[str, Any]] = []
    ovf_info: ova.OvfInfo | None = None
    ovf_choices: list[str] = []
    estimated = 0

    if kind == "disk":
        info: dict[str, Any] = {}
        source_format = ""
        archive_member: str | None = None
        probe = source_path
        probe_dir: Path | None = None
        if source_path.suffix.lower() == ".zip":
            try:
                archive_member, _, archive_format = ova.inspect_disk_archive(
                    source_path
                )
                probe_dir = Path(
                    tempfile.mkdtemp(prefix=".vemu-disk-")
                )
                probe, source_format = ova.extract_disk_archive(
                    source_path, probe_dir
                )
                source_format = source_format or archive_format
            except VemuError as exc:
                reasons.append(str(exc))
                probe = None
        try:
            if qemu_img and probe is not None:
                try:
                    info = _qemu_img_info(qemu_img, str(probe))
                    source_format = source_format or str(
                        info.get("format") or ""
                    )
                except VemuError as exc:
                    reasons.append(str(exc))
            if source_format not in {"qcow2", "raw", "vmdk"}:
                reasons.append(
                    f"Unsupported disk format '{source_format or 'unknown'}'; "
                    "use qcow2, raw or vmdk."
                )
            if (
                qemu_img
                and probe is not None
                and source_format in {"qcow2", "raw", "vmdk"}
            ):
                try:
                    measure = _qemu_measure(
                        qemu_img, str(probe), input_format=source_format
                    )
                    required = int(
                        measure.get("required")
                        or info.get("actual-size")
                        or probe.stat().st_size
                    )
                except VemuError as exc:
                    reasons.append(str(exc))
                    required = 0
            else:
                required = 0
        finally:
            if probe_dir is not None:
                shutil.rmtree(probe_dir, ignore_errors=True)
        estimated += required
        disks.append(
            {
                "target": _import_disk_target(str(specs["disk_bus"]), 0),
                "bus": str(specs["disk_bus"]),
                "source": str(source_path),
                "archive_member": archive_member,
                "archive": bool(archive_member),
                "source_format": source_format or "qcow2",
                "size_bytes": int(info.get("virtual-size") or 0),
                "size_human": _human_bytes(
                    int(info.get("virtual-size") or 0)
                ),
                "estimate_bytes": required,
                "estimate_human": _human_bytes(required),
            }
        )
    else:
        try:
            ovf_info, ovf_choices = ova.inspect_archive(source_path)
        except VemuError as exc:
            reasons.append(str(exc))
            ovf_info = None
        if ovf_info is not None:
            # OVF is authoritative for hardware when it provides a value,
            # but explicit caller overrides still win.
            specs = _import_specs(
                preset,
                vcpus=vcpus if vcpus is not None else ovf_info.vcpus,
                memory_mib=(
                    memory_mib
                    if memory_mib is not None
                    else ovf_info.memory_mib
                ),
                firmware=(
                    firmware
                    if firmware is not None
                    else (
                        ovf_info.firmware
                        if ovf_info.firmware_explicit
                        else None
                    )
                ),
                machine=machine,
                disk_bus=disk_bus,
                nic_model=nic_model,
                nic_count=nic_count,
                serial_console=(
                    serial_console
                    if serial_console is not None
                    else (
                        ovf_info.serial
                        if ovf_info.serial_explicit
                        else None
                    )
                ),
                data_disk_gb=data_disk_gb,
                video=video,
            )
            warnings.extend(ovf_info.warnings)
            warnings.append(
                "Space estimate is derived from OVF/archive metadata; the "
                "actual import re-checks the converted disk size."
            )
            if len(ovf_choices) > 1:
                warnings.append(
                    "Archive contains multiple OVF files; using "
                    f"{ovf_info.ovf_name}."
                )
            for index, disk in enumerate(ovf_info.disks):
                target = _import_disk_target(
                    str(specs["disk_bus"]), index
                )
                capacity = int(disk.capacity or 0)
                if disk.synthetic or not disk.file:
                    if capacity <= 0:
                        continue
                    estimate = 1024**2
                    estimated += estimate
                    disks.append(
                        {
                            "target": target,
                            "bus": str(specs["disk_bus"]),
                            "source": "",
                            "source_format": "qcow2",
                            "size_bytes": capacity,
                            "size_human": _human_bytes(capacity),
                            "estimate_bytes": estimate,
                            "estimate_human": _human_bytes(estimate),
                            "synthetic": True,
                        }
                    )
                    continue
                estimate = max(
                    int(disk.populated_size or 0),
                    int(disk.file_size or 0),
                )
                if not estimate:
                    estimate = capacity
                estimated += estimate
                disks.append(
                    {
                        "target": target,
                        "bus": str(specs["disk_bus"]),
                        "source": disk.file,
                        "source_format": "vmdk",
                        "size_bytes": capacity or estimate,
                        "size_human": _human_bytes(capacity or estimate),
                        "estimate_bytes": estimate,
                        "estimate_human": _human_bytes(estimate),
                    }
                )
            if not ovf_info.disks:
                reasons.append("OVF contains no usable disk.")
            primary_network = network or "NAT"
            if nics is not None:
                for index, item in enumerate(nics):
                    selected = dict(item)
                    selected.setdefault(
                        "target", f"nic{index + 1}"
                    )
                    selected.setdefault("network", primary_network)
                    selected.setdefault("model", specs["nic_model"])
                    selected.setdefault("mac", None)
                    imported_nics.append(selected)
            else:
                count = max(len(ovf_info.nics), int(specs["nic_count"]))
                for index in range(count):
                    source_nic = (
                        ovf_info.nics[index]
                        if index < len(ovf_info.nics)
                        else None
                    )
                    fallback = str(specs["nic_model"])
                    model = fallback
                    if source_nic is not None:
                        model, warning = _import_nic_model(
                            source_nic.model, fallback
                        )
                        if warning:
                            warnings.append(warning)
                    imported_nics.append(
                        {
                            "target": f"nic{index + 1}",
                            "name": (
                                source_nic.name
                                if source_nic is not None
                                else f"Ethernet {index + 1}"
                            ),
                            "network": primary_network,
                            "model": model,
                            "mac": source_nic.mac if source_nic else None,
                        }
                    )

    if kind == "disk" and nics is not None:
        for index, item in enumerate(nics):
            selected = dict(item)
            selected.setdefault("target", f"nic{index + 1}")
            selected.setdefault("network", network or "NAT")
            selected.setdefault("model", specs["nic_model"])
            selected.setdefault("mac", None)
            imported_nics.append(selected)
    if kind == "disk" and nics is None:
        for index in range(max(1, int(specs["nic_count"]))):
            imported_nics.append(
                {
                    "target": f"nic{index + 1}",
                    "name": f"Ethernet {index + 1}",
                    "network": network or "NAT",
                    "model": str(specs["nic_model"]),
                    "mac": None,
                }
            )
    if specs["data_disk_gb"]:
        next_index = len(disks)
        disks.append(
            {
                "target": _import_disk_target(
                    str(specs["disk_bus"]), next_index
                ),
                "bus": str(specs["disk_bus"]),
                "source": "",
                "source_format": "qcow2",
                "size_bytes": int(specs["data_disk_gb"]) * 1024**3,
                "size_human": _human_bytes(
                    int(specs["data_disk_gb"]) * 1024**3
                ),
                "estimate_bytes": 1024**3,
                "estimate_human": _human_bytes(1024**3),
                "synthetic": True,
                "size_gb": int(specs["data_disk_gb"]),
            }
        )
    if not imported_nics:
        imported_nics.append(
            {
                "target": "nic1",
                "name": "Ethernet 1",
                "network": network or "NAT",
                "model": str(specs["nic_model"]),
                "mac": None,
            }
        )

    available = (
        shutil.disk_usage(labs).free if labs and labs.is_dir() else 0
    )
    headroom = int(estimated * 0.15)
    required_bytes = estimated + headroom
    sufficient = bool(available >= required_bytes) if required_bytes else False
    if required_bytes and not sufficient:
        reasons.append(
            f"Insufficient space: need {_human_bytes(required_bytes)}, "
            f"available {_human_bytes(available)}."
        )
    return {
        "kind": kind,
        "source": str(source_path),
        "name": name,
        "preset": preset,
        "state": "new",
        "mode": "import",
        "destination": str(destination) if destination else None,
        "specs": specs,
        "disks": disks,
        "nics": imported_nics,
        "estimated_bytes": estimated,
        "headroom_bytes": headroom,
        "required_bytes": required_bytes,
        "available_bytes": available,
        "estimated_human": _human_bytes(estimated),
        "required_human": _human_bytes(required_bytes),
        "available_human": _human_bytes(available),
        "sufficient": sufficient,
        "supported": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "warnings": list(dict.fromkeys(warnings)),
        "ovf": (
            {
                "name": ovf_info.name,
                "ovf_name": ovf_info.ovf_name,
                "os_type": ovf_info.os_type,
                "firmware": ovf_info.firmware,
                "serial": ovf_info.serial,
                "choices": ovf_choices,
            }
            if ovf_info is not None
            else None
        ),
    }


def vm_import(
    kind: str,
    source: str,
    name: str,
    *,
    preset: str = "generic",
    network: str | None = None,
    network_config: str | None = None,
    storage_config: str | None = None,
    vcpus: int | None = None,
    memory_mib: int | None = None,
    firmware: str | None = None,
    machine: str | None = None,
    disk_bus: str | None = None,
    nic_model: str | None = None,
    nic_count: int | None = None,
    serial_console: bool | None = None,
    data_disk_gb: int | None = None,
    video: str | None = None,
    nics: list[dict[str, Any]] | None = None,
    start: bool = False,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    plan = vm_import_plan(
        kind,
        source,
        name,
        preset=preset,
        network=network,
        network_config=network_config,
        storage_config=storage_config,
        vcpus=vcpus,
        memory_mib=memory_mib,
        firmware=firmware,
        machine=machine,
        disk_bus=disk_bus,
        nic_model=nic_model,
        nic_count=nic_count,
        serial_console=serial_console,
        data_disk_gb=data_disk_gb,
        video=video,
        nics=nics,
        uri=uri,
    )
    if not plan["supported"]:
        raise Conflict("; ".join(plan["reasons"]))
    if not plan["sufficient"]:
        raise Conflict(
            f"Not enough space: need {plan['required_human']}, "
            f"available {plan['available_human']}."
        )
    with _migration_lock():
        return _vm_import_impl(
            plan,
            source=source,
            name=name,
            preset=preset,
            network=network,
            network_config=network_config,
            storage_config=storage_config,
            start=start,
            uri=uri,
        )


def _vm_import_impl(
    plan: dict[str, Any],
    *,
    source: str,
    name: str,
    preset: str,
    network: str | None,
    network_config: str | None,
    storage_config: str | None,
    start: bool,
    uri: str,
) -> dict[str, Any]:
    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        raise NotFound("qemu-img was not found.")
    destination = Path(str(plan["destination"]))
    destination_created = False
    temp_dir: Path | None = None
    defined = False
    base_paths: dict[str, str] = {}
    specs = plan["specs"]
    try:
        destination.mkdir(parents=True, exist_ok=False)
        destination_created = True
        extracted: dict[str, Path] = {}
        if plan["kind"] == "ova":
            temp_dir = Path(
                tempfile.mkdtemp(
                    prefix=".vemu-ova-", dir=str(destination.parent)
                )
            )
            extracted, _ = ova.extract_archive(source, temp_dir)
        elif any(disk.get("archive") for disk in plan["disks"]):
            temp_dir = Path(
                tempfile.mkdtemp(
                    prefix=".vemu-disk-", dir=str(destination.parent)
                )
            )
        chain_specs: list[dict[str, Any]] = []
        for index, disk in enumerate(plan["disks"]):
            target = str(disk["target"])
            base_name = (
                "base.qcow2"
                if len(plan["disks"]) == 1
                else f"base-{target}.qcow2"
            )
            base = destination / base_name
            if disk.get("synthetic"):
                size = int(disk.get("size_bytes") or 0)
                size_arg = str(size) if size else f"{int(disk.get('size_gb') or 0)}G"
                command = [
                    qemu_img,
                    "create",
                    "-f",
                    "qcow2",
                    str(base),
                    size_arg,
                ]
                result = subprocess.run(
                    command, capture_output=True, text=True, check=False
                )
                if result.returncode != 0:
                    raise ExternalCommandError(
                        " ".join(command),
                        result.returncode,
                        result.stderr or result.stdout,
                    )
            else:
                if plan["kind"] == "ova":
                    source_file = extracted.get(str(disk["source"]))
                elif disk.get("archive"):
                    source_file, _ = ova.extract_disk_archive(
                        str(disk["source"]), temp_dir
                    )
                else:
                    source_file = Path(str(disk["source"]))
                if source_file is None or not Path(source_file).is_file():
                    raise NotFound(
                        f"Imported disk file was not found: {disk['source']}"
                    )
                input_format = str(
                    disk.get("source_format") or "qcow2"
                )
                try:
                    measure = _qemu_measure(
                        qemu_img,
                        str(source_file),
                        input_format=input_format,
                    )
                    needed = int(measure.get("required") or 0)
                except VemuError:
                    needed = 0
                if needed:
                    free = shutil.disk_usage(destination).free
                    if needed + int(needed * 0.15) > free:
                        raise Conflict(
                            f"Not enough space for {disk.get('source')}: "
                            f"need {_human_bytes(needed)}, "
                            f"available {_human_bytes(free)}."
                        )
                command = qemu_convert_command(
                    qemu_img,
                    str(source_file),
                    str(base),
                    input_format=input_format,
                )
                result = subprocess.run(
                    command, capture_output=True, text=True, check=False
                )
                if result.returncode != 0:
                    raise ExternalCommandError(
                        " ".join(command),
                        result.returncode,
                        result.stderr or result.stdout,
                    )
            check = subprocess.run(
                [qemu_img, "check", str(base)],
                capture_output=True,
                text=True,
                check=False,
            )
            if check.returncode != 0:
                raise ExternalCommandError(
                    " ".join([qemu_img, "check", str(base)]),
                    check.returncode,
                    check.stderr or check.stdout,
                )
            try:
                os.chmod(base, 0o444)
            except OSError:
                pass
            base_paths[target] = str(base)
            chain_specs.append(
                {
                    "target": target,
                    "bus": str(disk["bus"]),
                    "base": str(base),
                    "label": "imported",
                }
            )
        manager = ChainManager.create_multi(destination, chain_specs)
        chain = manager.load()
        active = {
            target: disk_.active_disk()
            for target, disk_ in chain.disks.items()
        }
        disk_specs = []
        for index, disk in enumerate(plan["disks"]):
            disk_specs.append(
                Disk(
                    path=active[str(disk["target"])],
                    bus=str(disk["bus"]),
                    target=str(disk["target"]),
                    format="qcow2",
                    boot=1 if index == 0 else 0,
                )
            )
        network_entries = []
        network_store = NetworkStore(network_config)
        with LibvirtBackend(uri) as backend:
            for nic in plan["nics"]:
                selected_network = (
                    network_store.find_libvirt_name(str(nic["network"]))
                    or str(nic["network"])
                )
                mac = nic.get("mac")
                network_entries.append(
                    Network(
                        network=selected_network,
                        model=str(nic.get("model") or specs["nic_model"]),
                        mac=str(mac) if mac else backend.unique_mac(),
                    )
                )
            spec = DomainSpec(
                name=name,
                memory_mib=int(specs["memory_mib"]),
                vcpus=int(specs["vcpus"]),
                disks=disk_specs,
                networks=network_entries,
                arch="x86_64",
                domain_type=domain_type_for("x86_64"),
                machine=str(specs["machine"]),
                firmware=str(specs["firmware"]),
                video=str(specs["video"]),
                serial_console=bool(specs["serial_console"]),
                cpu_mode=(
                    "host-passthrough"
                    if domain_type_for("x86_64") == "kvm"
                    else "maximum"
                ),
                guest_agent=True,
                os_type=(
                    str(specs.get("os_type", "generic"))
                    if str(specs.get("os_type", "generic"))
                    in {"windows", "linux", "hypervisor", "generic"}
                    else "generic"
                ),
            )
            _apply_custom_firmware(spec)
            backend.define(spec.to_xml())
            _apply_runtime_profile(backend, name)
            defined = True
            if start:
                backend.start(name)
        _relabel(destination)
        return {
            **plan,
            "disks": [
                {
                    "target": disk["target"],
                    "bus": disk["bus"],
                    "active_disk": active[str(disk["target"])],
                    "base": base_paths.get(str(disk["target"])),
                }
                for disk in plan["disks"]
            ],
            "chain": str(destination / CHAIN_FILE),
            "defined": True,
            "started": start,
        }
    except Exception:
        if destination_created and not defined:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)



def _qemu_backing_chain(qemu_img: str, source: str) -> list[dict[str, Any]]:
    """Return qcow2 files from top overlay down to the base image."""
    command = [
        qemu_img,
        "info",
        "--backing-chain",
        "--output=json",
        source,
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ExternalCommandError(
            " ".join(command), result.returncode, result.stderr
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExternalCommandError(
            " ".join(command), 0, result.stdout
        ) from exc
    if not isinstance(payload, list):
        raise VemuError("qemu-img returned an invalid backing chain.")
    source_dir = Path(source).expanduser().resolve().parent
    entries: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        if not filename:
            continue
        path = Path(str(filename)).expanduser()
        if not path.is_absolute():
            path = source_dir / path
        entries.append(
            {
                "path": str(path.resolve()),
                "format": str(item.get("format", "")),
                "actual_size": int(item.get("actual-size") or 0),
            }
        )
    if not entries:
        raise VemuError("qemu-img returned an empty backing chain.")
    return entries


def _qemu_image_info(qemu_img: str, source: str) -> dict[str, Any]:
    command = [qemu_img, "info", "--output=json", source]
    result = subprocess.run(
        command, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ExternalCommandError(
            " ".join(command), result.returncode, result.stderr
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExternalCommandError(
            " ".join(command), 0, result.stdout
        ) from exc
    return payload if isinstance(payload, dict) else {}


def _snapshot_tree_order(
    snapshots: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Return snapshots keyed by name, in parent-before-child order."""
    by_name = {
        str(item.get("name", "")): item for item in snapshots
    }
    depth: dict[str, int] = {}

    def visit(name: str, stack: set[str]) -> int:
        if name in depth:
            return depth[name]
        if name in stack:
            raise ValidationError(
                f"Snapshot parent cycle at '{name}'."
            )
        snapshot = by_name.get(name)
        if snapshot is None:
            raise ValidationError(f"Snapshot '{name}' was not found.")
        parent_name = str(snapshot.get("parent", ""))
        level = 0
        if parent_name:
            if parent_name not in by_name:
                raise ValidationError(
                    f"Snapshot '{name}' references missing parent "
                    f"'{parent_name}'."
                )
            level = visit(parent_name, stack | {name}) + 1
        depth[name] = level
        return level

    for name in by_name:
        visit(name, set())
    order = sorted(
        by_name,
        key=lambda name: (
            depth[name],
            str(by_name[name].get("created_at", "")),
            name,
        ),
    )
    return by_name, order


def _snapshot_import_plan(
    disks: list[dict[str, Any]],
    qemu_img: str,
    snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Map libvirt snapshots (external or internal) onto a v2 chain."""
    names = [str(item.get("name", "")) for item in snapshots]
    empty = {
        "supported": False,
        "reason": "No libvirt snapshots to import.",
        "kind": "",
        "snapshots": names,
        "layer_count": 0,
        "bytes": 0,
        "disks": [],
    }
    if not snapshots:
        return empty
    reasons: list[str] = []
    modes: set[str] = set()
    for snapshot in snapshots:
        name = str(snapshot.get("name", ""))
        if snapshot.get("kind") not in {"external", "internal"}:
            reasons.append(f"{name}: unsupported snapshot kind")
        memory = str(snapshot.get("memory", "no"))
        if memory not in ("", "no", "none"):
            reasons.append(f"{name}: memory snapshots cannot be imported")
        disk_modes = {
            str(disk.get("snapshot", ""))
            for disk in snapshot.get("disks", [])
        }
        if not disk_modes:
            disk_modes = {str(snapshot.get("kind", ""))}
        if len(disk_modes) != 1:
            reasons.append(
                f"{name}: every disk must use the same snapshot mode"
            )
            continue
        mode = next(iter(disk_modes), "")
        if mode not in {"external", "internal"}:
            reasons.append(f"{name}: unsupported disk snapshot mode")
            continue
        modes.add(mode)
        if not snapshot.get("state_sources"):
            reasons.append(f"{name}: no per-disk snapshot state found")
    if len(modes) != 1:
        reasons.append("mixed external and internal snapshots are unsupported")
    if reasons:
        return {**empty, "reason": "; ".join(reasons)}
    mode = next(iter(modes))
    try:
        by_name, order = _snapshot_tree_order(snapshots)
    except ValidationError as exc:
        return {**empty, "reason": str(exc)}
    if mode == "external":
        return _external_snapshot_plan(
            disks, qemu_img, snapshots, by_name, order, names
        )
    return _internal_snapshot_plan(
        disks, qemu_img, snapshots, by_name, order, names
    )


def _external_snapshot_plan(
    disks: list[dict[str, Any]],
    qemu_img: str,
    snapshots: list[dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
    order: list[str],
    names: list[str],
) -> dict[str, Any]:
    """Import an external snapshot tree, including inactive branches."""
    empty = {
        "supported": False,
        "reason": "No libvirt snapshots to import.",
        "kind": "external",
        "snapshots": names,
        "layer_count": 0,
        "bytes": 0,
        "disks": [],
    }
    reasons: list[str] = []
    targets = [str(item.get("target", "")) for item in disks]
    disk_specs: list[dict[str, Any]] = []
    signature: list[tuple[str, str, bool]] | None = None
    seen_paths: set[str] = set()
    total_bytes = 0
    for disk in disks:
        target = str(disk.get("target", ""))
        current_source = str(
            Path(str(disk.get("source", ""))).expanduser().resolve()
        )
        disk_bytes = 0
        try:
            current_chain = _qemu_backing_chain(qemu_img, current_source)
        except VemuError as exc:
            reasons.append(f"{target}: {exc}")
            continue
        base = current_chain[-1]["path"]
        state_path: dict[str, str] = {}
        chains: dict[str, list[dict[str, Any]] | None] = {}
        for name in order:
            snapshot = by_name[name]
            state = str(snapshot.get("state_sources", {}).get(target, ""))
            if not state:
                reasons.append(f"{name}: missing disk target {target}")
                continue
            resolved = str(Path(state).expanduser().resolve())
            state_path[name] = resolved
            if resolved == base:
                chains[name] = None
                continue
            try:
                chain = _qemu_backing_chain(qemu_img, resolved)
            except VemuError as exc:
                reasons.append(f"{name}: {exc}")
                continue
            if chain[-1]["path"] != base:
                reasons.append(
                    f"{name}: disk target {target} uses a different base"
                )
                continue
            chains[name] = chain
        # Parent links must match the real backing files, including branches.
        for name in order:
            snapshot = by_name[name]
            parent_name = str(snapshot.get("parent", ""))
            state = state_path.get(name)
            if not state or state == base:
                if parent_name:
                    parent_state = state_path.get(parent_name, "")
                    if parent_state and parent_state != base:
                        reasons.append(
                            f"{name}: base snapshot cannot have parent "
                            f"{parent_name}"
                        )
                continue
            chain = chains.get(name)
            if not chain:
                continue
            immediate = (
                chain[1]["path"] if len(chain) > 1 else base
            )
            expected = base
            if parent_name:
                expected = state_path.get(parent_name, "")
            if not expected:
                reasons.append(
                    f"{name}: missing parent state for {target}"
                )
                continue
            if immediate != expected:
                reasons.append(
                    f"{name}: backing does not match parent "
                    f"{parent_name or 'base'} on {target}"
                )

        layers: list[dict[str, Any]] = []
        layer_ids: dict[str, str] = {}
        for index, name in enumerate(order, start=1):
            snapshot = by_name[name]
            parent_name = str(snapshot.get("parent", ""))
            parent_id = "base"
            if parent_name:
                parent_id = layer_ids.get(parent_name, "")
                if not parent_id:
                    reasons.append(
                        f"{name}: parent {parent_name} was not imported"
                    )
                    continue
            state = state_path.get(name, "")
            layer_ids[name] = f"{index:04d}"
            layers.append(
                {
                    "source": None if state == base else state,
                    "source_kind": "external",
                    "label": name,
                    "created_at": str(snapshot.get("created_at", "")),
                    "parent": parent_id,
                    "active": False,
                }
            )
        if not layers:
            reasons.append(f"{target}: no importable layers found")
            continue
        active_name = next(
            (
                name
                for name, state in state_path.items()
                if state == current_source
            ),
            "",
        )
        if not active_name or active_name not in layer_ids:
            parent_path = (
                current_chain[1]["path"]
                if len(current_chain) > 1
                else base
            )
            parent_id = "base"
            if parent_path != base:
                parent_name = next(
                    (
                        name
                        for name, state in state_path.items()
                        if state == parent_path
                    ),
                    "",
                )
                if not parent_name:
                    reasons.append(
                        f"{target}: current state is not on a snapshot branch"
                    )
                    continue
                parent_id = layer_ids.get(parent_name, "")
                if not parent_id:
                    reasons.append(
                        f"{target}: current branch parent was not imported"
                    )
                    continue
            layers.append(
                {
                    "source": current_source,
                    "source_kind": "external",
                    "label": "current",
                    "created_at": "",
                    "parent": parent_id,
                    "active": False,
                }
            )
        for layer in layers:
            layer["active"] = False
        found = False
        for layer in layers:
            if layer["label"] == active_name or (
                active_name == "" and layer["label"] == "current"
            ):
                layer["active"] = True
                found = True
        if not found:
            layers[-1]["active"] = True
        current_signature = [
            (str(layer["label"]), str(layer["parent"]), bool(layer["active"]))
            for layer in layers
        ]
        if signature is None:
            signature = current_signature
        elif current_signature != signature:
            reasons.append(
                f"{target}: snapshot tree does not align across disks"
            )
        for path in [base, current_source] + list(state_path.values()):
            resolved = str(Path(path).expanduser().resolve())
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                disk_bytes += Path(resolved).stat().st_size
            except OSError:
                pass
        total_bytes += disk_bytes
        disk_specs.append(
            {
                "target": target,
                "bus": str(disk.get("bus", "")),
                "base": base,
                "layers": layers,
                "bytes": disk_bytes,
            }
        )
    if reasons:
        return {**empty, "disks": disk_specs, "reason": "; ".join(reasons)}
    if len(disk_specs) != len(targets):
        return {
            **empty,
            "disks": disk_specs,
            "reason": "Not every data disk has an importable tree.",
        }
    return {
        "supported": True,
        "reason": "",
        "kind": "external",
        "snapshots": names,
        "layer_count": len(disk_specs[0]["layers"]),
        "bytes": total_bytes,
        "disks": disk_specs,
    }


def _internal_snapshot_plan(
    disks: list[dict[str, Any]],
    qemu_img: str,
    snapshots: list[dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
    order: list[str],
    names: list[str],
) -> dict[str, Any]:
    """Import internal qcow2 snapshots as standalone state layers."""
    empty = {
        "supported": False,
        "reason": "No libvirt snapshots to import.",
        "kind": "internal",
        "snapshots": names,
        "layer_count": 0,
        "bytes": 0,
        "disks": [],
    }
    reasons: list[str] = []
    targets = [str(item.get("target", "")) for item in disks]
    disk_specs: list[dict[str, Any]] = []
    signature: list[tuple[str, str, bool]] | None = None
    total_bytes = 0
    for disk in disks:
        target = str(disk.get("target", ""))
        current_source = str(
            Path(str(disk.get("source", ""))).expanduser().resolve()
        )
        disk_bytes = 0
        try:
            info = _qemu_image_info(qemu_img, current_source)
            virtual_size = int(info.get("virtual-size") or 0)
        except (VemuError, ValueError) as exc:
            reasons.append(f"{target}: {exc}")
            continue
        if virtual_size <= 0:
            reasons.append(f"{target}: cannot determine virtual size")
            continue
        state_sources: dict[str, str] = {}
        for name in order:
            snapshot = by_name[name]
            state = str(
                snapshot.get("state_sources", {}).get(target, "")
            )
            if not state:
                reasons.append(f"{name}: missing disk target {target}")
                continue
            resolved = str(Path(state).expanduser().resolve())
            if not Path(resolved).is_file():
                reasons.append(f"{name}: snapshot image was not found")
                continue
            state_sources[name] = resolved
            try:
                measure = _qemu_measure(
                    qemu_img, resolved, snapshot=name
                )
            except VemuError as exc:
                reasons.append(f"{name}: {exc}")
                continue
            disk_bytes += int(measure.get("required") or 0)
        try:
            current_measure = _qemu_measure(qemu_img, current_source)
            disk_bytes += int(current_measure.get("required") or 0)
        except VemuError as exc:
            reasons.append(f"{target}: {exc}")
            continue
        layers: list[dict[str, Any]] = []
        layer_ids: dict[str, str] = {}
        for index, name in enumerate(order, start=1):
            snapshot = by_name[name]
            parent_name = str(snapshot.get("parent", ""))
            parent_id = "base"
            if parent_name:
                parent_id = layer_ids.get(parent_name, "")
                if not parent_id:
                    reasons.append(
                        f"{name}: parent {parent_name} was not imported"
                    )
                    continue
            layer_ids[name] = f"{index:04d}"
            layers.append(
                {
                    "source": state_sources.get(name, ""),
                    "source_kind": "internal",
                    "snapshot": name,
                    "label": name,
                    "created_at": str(snapshot.get("created_at", "")),
                    "parent": parent_id,
                    "active": False,
                }
            )
        current_parent = next(
            (
                name
                for name in order
                if str(by_name[name].get("current", ""))
                in {"yes", "1", "true", "True"}
            ),
            order[-1] if order else "",
        )
        parent_id = layer_ids.get(current_parent, "base") if current_parent else "base"
        active_id = f"{len(layers) + 1:04d}"
        layers.append(
            {
                "source": current_source,
                "source_kind": "current",
                "label": "current",
                "created_at": "",
                "parent": parent_id,
                "active": True,
            }
        )
        current_signature = [
            (str(layer["label"]), str(layer["parent"]), bool(layer["active"]))
            for layer in layers
        ]
        if signature is None:
            signature = current_signature
        elif current_signature != signature:
            reasons.append(
                f"{target}: snapshot tree does not align across disks"
            )
        total_bytes += disk_bytes
        disk_specs.append(
            {
                "target": target,
                "bus": str(disk.get("bus", "")),
                "base": None,
                "virtual_size": virtual_size,
                "layers": layers,
                "bytes": disk_bytes,
            }
        )
    if reasons:
        return {**empty, "disks": disk_specs, "reason": "; ".join(reasons)}
    if len(disk_specs) != len(targets):
        return {
            **empty,
            "disks": disk_specs,
            "reason": "Not every data disk has an importable tree.",
        }
    return {
        "supported": True,
        "reason": "",
        "kind": "internal",
        "snapshots": names,
        "layer_count": len(disk_specs[0]["layers"]),
        "bytes": total_bytes,
        "disks": disk_specs,
    }


def vm_migration_plan(
    domain: str,
    *,
    new_firmware: bool = False,
    storage_config: str | None = None,
    import_snapshots: bool = False,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Read-only Full-migration preflight and size estimate."""
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        config_xml = backend.config_xml(domain)
        root = ET.fromstring(config_xml)
        disks = _migration_data_disks(config_xml)
        snapshots: list[str] = []
        snapshot_details: list[dict[str, Any]] = []
        if hasattr(backend, "snapshot_details"):
            try:
                snapshot_details = backend.snapshot_details(domain)
            except VemuError:
                snapshot_details = []
        if snapshot_details:
            snapshots = [
                str(item.get("name", "")) for item in snapshot_details
            ]
        elif hasattr(backend, "snapshot_names"):
            try:
                snapshots = backend.snapshot_names(domain)
            except VemuError:
                snapshots = []
    locations = StorageStore(storage_config).load()
    labs = next(
        (item["path"] for item in locations if item["role"] == "labs"),
        None,
    )
    destination = str(Path(labs).expanduser() / domain) if labs else None
    qemu_img = shutil.which("qemu-img")
    reasons: list[str] = []
    firmware_plan, firmware_reasons = _firmware_plan(
        root, domain=domain, new_firmware=new_firmware
    )
    reasons.extend(firmware_reasons)
    disk_plans: list[dict[str, Any]] = []
    required = 0
    if not labs:
        reasons.append("No storage location with role 'labs' is registered.")
    elif not Path(labs).is_dir():
        reasons.append(f"Storage location does not exist: {labs}")
    if not qemu_img:
        reasons.append("qemu-img was not found.")
    if info.state != "stopped":
        reasons.append("The VM must be shut down.")
    if not disks:
        reasons.append("The VM has no data disk to migrate.")
    if destination and Path(destination).exists():
        reasons.append(f"Destination already exists: {destination}")
    if qemu_img:
        for disk in disks:
            source = str(disk.get("source") or "")
            try:
                measure = _qemu_measure(qemu_img, source)
                required += int(measure.get("required") or 0)
                disk_plans.append(
                    {
                        "target": disk.get("target", ""),
                        "bus": disk.get("bus", ""),
                        "source": source,
                        "required": int(measure.get("required") or 0),
                        "required_human": _human_bytes(
                            int(measure.get("required") or 0)
                        ),
                    }
                )
            except VemuError as exc:
                reasons.append(f"{source}: {exc}")
    import_plan = (
        _snapshot_import_plan(disks, qemu_img, snapshot_details)
        if qemu_img and snapshot_details
        else {
            "supported": False,
            "reason": "No libvirt snapshots to import.",
            "snapshots": snapshots,
            "layer_count": 0,
            "bytes": 0,
            "disks": [],
        }
    )
    if import_snapshots and snapshots and info.managed_by != "External":
        reasons.append(
            "Snapshot import is only available for External VMs before "
            "migration; Partial/Full XML already belongs to v-EMU."
        )
    if import_snapshots and snapshots:
        if not import_plan["supported"]:
            reasons.append(
                "Cannot import snapshots: "
                + str(import_plan.get("reason", "unsupported snapshot layout"))
            )
        else:
            required = int(import_plan.get("bytes") or required)
            by_target = {
                str(item.get("target", "")): item
                for item in import_plan.get("disks", [])
            }
            for disk_plan in disk_plans:
                item = by_target.get(str(disk_plan.get("target", "")))
                if item is None:
                    continue
                disk_plan["required"] = int(item.get("bytes") or 0)
                disk_plan["required_human"] = _human_bytes(
                    disk_plan["required"]
                )
            disk_plan_map = {
                str(item.get("target", "")): item for item in disk_plans
            }
            missing = set(by_target) - set(disk_plan_map)
            if missing:
                reasons.append(
                    "Snapshot import is missing disk(s): "
                    + ", ".join(sorted(missing))
                )
    snapshot_loss_required = bool(snapshots) and not (
        import_snapshots and import_plan["supported"]
    )
    available = (
        shutil.disk_usage(labs).free if labs and Path(labs).is_dir() else 0
    )
    headroom = int(required * 0.15)
    total_required = required + headroom
    sufficient = bool(available >= total_required) if total_required else False
    if total_required and not sufficient:
        reasons.append(
            f"Insufficient space: need {_human_bytes(total_required)}, "
            f"available {_human_bytes(available)}."
        )
    return {
        "domain": domain,
        "state": info.state,
        "managed_by": info.managed_by,
        "mode": "full",
        "new_firmware": bool(new_firmware),
        "firmware": firmware_plan,
        "supported": not reasons,
        "reasons": reasons,
        "snapshots": snapshots,
        "snapshot_import": import_plan,
        "snapshot_loss_required": snapshot_loss_required,
        "data_disk_count": len(disks),
        "multi_disk": len(disks) > 1,
        "disks": disk_plans,
        "estimated_bytes": required,
        "headroom_bytes": headroom,
        "required_bytes": total_required,
        "available_bytes": available,
        "sufficient": sufficient,
        "estimated_human": _human_bytes(required),
        "required_human": _human_bytes(total_required),
        "available_human": _human_bytes(available),
        "destination": destination,
    }


def vm_migrate_full(
    domain: str,
    *,
    accept_snapshot_loss: bool = False,
    import_snapshots: bool = False,
    start: bool = False,
    new_firmware: bool = False,
    storage_config: str | None = None,
    backup_dir: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Flatten disks or import external snapshots into a v2 chain."""
    with _migration_lock():
        return _vm_migrate_full_impl(
            domain,
            accept_snapshot_loss=accept_snapshot_loss,
            import_snapshots=import_snapshots,
            start=start,
            new_firmware=new_firmware,
            storage_config=storage_config,
            backup_dir=backup_dir,
            uri=uri,
        )


def _vm_migrate_full_impl(
    domain: str,
    *,
    accept_snapshot_loss: bool = False,
    import_snapshots: bool = False,
    start: bool = False,
    new_firmware: bool = False,
    storage_config: str | None = None,
    backup_dir: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    plan = vm_migration_plan(
        domain,
        new_firmware=new_firmware,
        storage_config=storage_config,
        import_snapshots=import_snapshots,
        uri=uri,
    )
    if not plan["supported"]:
        raise Conflict("; ".join(plan["reasons"]))
    if (
        import_snapshots
        and plan["snapshots"]
        and not plan["snapshot_import"]["supported"]
    ):
        raise ValidationError(
            "Cannot import snapshots: "
            + str(plan["snapshot_import"].get("reason", "unsupported layout"))
        )
    if plan["snapshot_loss_required"] and not accept_snapshot_loss:
        raise ValidationError(
            "This VM has external snapshots. Full migration flattens the "
            "current state and does not import those snapshots. Re-run with "
            "import_snapshots to keep them, or with accept_snapshot_loss "
            "after keeping a backup."
        )
    if not plan["sufficient"]:
        raise Conflict(
            f"Not enough space: need {plan['required_human']}, "
            f"available {plan['available_human']}."
        )
    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        raise NotFound("qemu-img was not found.")
    storage = StorageStore(storage_config).load()
    labs = next(
        (item["path"] for item in storage if item["role"] == "labs"),
        None,
    )
    if not labs:
        raise NotFound("No labs storage location is registered.")
    destination = Path(labs).expanduser() / domain
    if destination.exists():
        raise Conflict(f"Destination already exists: {destination}")
    backups = Path(
        backup_dir
        or os.environ.get("VEMU_BACKUP_DIR")
        or "/var/lib/vemu/backups"
    ).expanduser() / domain
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backups / f"{stamp}.xml"
    defined = False
    destination_created = False
    base_paths: dict[str, str] = {}
    active_paths: dict[str, str] = {}
    try:
        with LibvirtBackend(uri) as backend:
            if backend.info(domain).state != "stopped":
                raise Conflict(
                    f"Shut down '{domain}' before Full migration."
                )
            config_xml = backend.config_xml(domain)
            backup_path.write_text(config_xml, encoding="utf-8")
            destination.mkdir(parents=True, exist_ok=False)
            destination_created = True
            if import_snapshots and plan["snapshots"]:
                manager = ChainManager.import_multi(
                    destination,
                    plan["snapshot_import"]["disks"],
                    qemu_img=qemu_img,
                )
                chain = manager.load()
                base_paths = {
                    target: disk.base
                    for target, disk in chain.disks.items()
                }
            else:
                specs: list[dict[str, str]] = []
                for item in plan["disks"]:
                    target = str(item["target"])
                    source = str(item["source"])
                    base = destination / (
                        "base.qcow2"
                        if len(plan["disks"]) == 1
                        else f"base-{target}.qcow2"
                    )
                    command = qemu_convert_command(
                        qemu_img, source, str(base)
                    )
                    result = subprocess.run(
                        command, capture_output=True, text=True, check=False
                    )
                    if result.returncode != 0:
                        raise ExternalCommandError(
                            " ".join(command),
                            result.returncode,
                            result.stderr,
                        )
                    check = subprocess.run(
                        [qemu_img, "check", str(base)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if check.returncode != 0:
                        raise ExternalCommandError(
                            " ".join([qemu_img, "check", str(base)]),
                            check.returncode,
                            check.stderr or check.stdout,
                        )
                    try:
                        os.chmod(base, 0o444)
                    except OSError:
                        pass
                    base_paths[target] = str(base)
                    specs.append(
                        {
                            "target": target,
                            "bus": str(item.get("bus", "")),
                            "base": str(base),
                            "label": "migrated",
                        }
                    )
                manager = ChainManager.create_multi(destination, specs)
                chain = manager.load()
            active_paths = {
                target: disk.active_disk()
                for target, disk in chain.disks.items()
            }
            root = ET.fromstring(config_xml)
            changed: set[str] = set()
            for disk in root.findall("./devices/disk"):
                if disk.get("device") != "disk":
                    continue
                target_element = disk.find("target")
                source_element = disk.find("source")
                if target_element is None or source_element is None:
                    continue
                target = target_element.get("dev")
                new_path = active_paths.get(str(target))
                if new_path is None:
                    continue
                source_element.set("file", new_path)
                changed.add(str(target))
            missing = set(active_paths) - changed
            if missing:
                raise NotFound(
                    "Could not find data disk(s) in XML: "
                    + ", ".join(sorted(missing))
                )
            firmware_applied = False
            if new_firmware:
                firmware = plan.get("firmware")
                if not firmware:
                    raise NotFound("v-EMU firmware is not available.")
                files = firmware_for(
                    str(firmware["arch"]),
                    secure_boot=bool(firmware["secure_boot"]),
                )
                if files is None:
                    raise NotFound(
                        "v-EMU firmware is not installed for "
                        f"{firmware['arch']}."
                    )
                nvram_path = Path(str(firmware["nvram_path"]))
                try:
                    nvram_exists = nvram_path.exists()
                except OSError:
                    # A direct non-root CLI may not be able to stat the
                    # libvirt NVRAM directory. The daemon runs as root and
                    # handles the backup there; do not fail the plan only
                    # because this process cannot inspect it.
                    nvram_exists = False
                if nvram_exists:
                    shutil.move(
                        str(nvram_path),
                        str(backups / f"{stamp}-{nvram_path.name}"),
                    )
                _apply_new_firmware(
                    root,
                    files=files,
                    nvram_path=nvram_path,
                    secure=bool(firmware["secure_boot"]),
                )
                firmware_applied = True
            else:
                firmware_applied = False
            updated = set_management_metadata(
                ET.tostring(root, encoding="unicode"),
                mode="full",
                managed_by="v-EMU (full)",
                origin="external",
                backup=str(backup_path),
            )
            backend.redefine_domain(domain, updated)
            defined = True
    except Exception:
        if destination_created and not defined:
            # Leave no half-built chain behind so the migration can be retried.
            shutil.rmtree(destination, ignore_errors=True)
        raise
    _relabel(destination)
    if start:
        with LibvirtBackend(uri) as backend:
            backend.start(domain)
    disk_results = [
        {
            "target": target,
            "base": base_paths.get(target),
            "active_disk": active_paths.get(target),
        }
        for target in active_paths
    ]
    first_target = next(iter(active_paths))
    return {
        **plan,
        "backup": str(backup_path),
        "chain": str(destination / CHAIN_FILE),
        "disks": disk_results,
        "base": base_paths.get(first_target),
        "active_disk": active_paths.get(first_target),
        "firmware_applied": firmware_applied,
        "imported_snapshots": (
            list(plan["snapshots"])
            if import_snapshots and plan["snapshots"]
            else []
        ),
        "started": start,
    }


def vm_xml(domain: str, uri: str = "qemu:///system") -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        return {"domain": domain, "xml": backend.config_xml(domain)}


def domain_screenshot(
    domain: str, uri: str = "qemu:///system"
) -> dict[str, Any]:
    virsh = shutil.which("virsh")
    if not virsh:
        raise NotFound("virsh was not found.")
    handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    path = Path(handle.name)
    handle.close()
    try:
        result = subprocess.run(
            [virsh, "-c", uri, "screenshot", domain, str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if result.returncode != 0 or not path.is_file():
            raise VemuError(
                (result.stderr or result.stdout or "screenshot failed").strip()
            )
        data = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)
    if not data:
        raise VemuError("libvirt returned an empty screenshot.")
    return {
        "domain": domain,
        "format": "png",
        "size": len(data),
        "data": base64.b64encode(data).decode("ascii"),
    }


def vm_capabilities(
    machine: str | None = None,
    uri: str = "qemu:///system",
    *,
    arch: str = "x86_64",
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        return backend.capabilities(machine=machine, arch=arch)


def _qemu_img_size_bytes(path: Path) -> int | None:
    qemu_img = shutil.which("qemu-img")
    if not qemu_img or not path.is_file():
        return None
    result = subprocess.run(
        [qemu_img, "info", "--output=json", "-U", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    size = data.get("virtual-size")
    return int(size) if isinstance(size, int) and size > 0 else None


def vm_config_info(
    domain: str, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        xml = backend.config_xml(domain)
        config = parse_config(xml)
        has_v2_chain = False
        try:
            chain_dir = _chain_dir_for_domain(backend, domain)
        except NotFound:
            chain_dir = None
        if chain_dir is not None:
            try:
                has_v2_chain = isinstance(
                    ChainManager(chain_dir).load(), MultiDiskChain
                )
            except VemuError:
                has_v2_chain = False
    for disk in config.get("disks", []):
        disk["size_gb"] = None
        if disk.get("device") != "disk" or not disk.get("source"):
            continue
        size = _qemu_img_size_bytes(Path(disk["source"]))
        if size is not None:
            disk["size_gb"] = round(size / (1024**3), 2)
    return {
        "config": config,
        "state": info.state,
        "managed_by": info.managed_by,
        "has_v2_chain": has_v2_chain,
        "drift": drift.details(xml),
        "pending_restart": info.state == "running",
    }


def vm_media_list(
    domain: str, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        config = parse_config(backend.config_xml(domain))
    return {
        "domain": domain,
        "cdroms": [
            disk for disk in config.get("disks", [])
            if disk.get("device") == "cdrom"
        ],
    }


def vm_media_change(
    domain: str,
    path: str,
    *,
    target: str | None = None,
    force: bool = False,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    media = Path(path).expanduser().resolve()
    if not media.is_file():
        raise NotFound(f"Media file was not found: {media}")
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        result = backend.update_media(
            domain, str(media), target, force=force
        )
    return {
        "domain": domain,
        "media": str(media),
        **result,
        "force": force,
        "pending_restart": not bool(result.get("live")),
    }


def vm_media_eject(
    domain: str,
    *,
    target: str | None = None,
    force: bool = False,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        result = backend.update_media(domain, None, target, force=force)
    return {
        "domain": domain,
        "media": None,
        **result,
        "force": force,
        "pending_restart": not bool(result.get("live")),
    }


def vm_eject_installer_media(
    domain: str, *, uri: str = "qemu:///system"
) -> dict[str, Any]:
    """Eject CD media once the guest disk contains an installed OS.

    Called on the libvirt reboot event. Installers reboot into the installed
    system; leaving the ISO attached can make OVMF pick its stale USB boot
    entry again (the firmware boot order is persisted in NVRAM even after the
    libvirt boot flag is removed). An empty CD tray makes the firmware fall
    through to the disk.
    """
    ejected: list[str] = []
    with LibvirtBackend(uri) as backend:
        if backend.info(domain).managed_by == "External":
            return {"domain": domain, "ejected": ejected, "skipped": "read-only"}
        if backend.installed_disk_target(domain) is None:
            return {"domain": domain, "ejected": ejected}
        # Promote the installed disk before touching the CD device. The
        # running live XML may still carry the installer's boot order; the
        # next start uses the persistent definition.
        try:
            backend.promote_boot_order(domain)
        except VemuError:
            pass
        try:
            xml = backend.xml(domain)
        except VemuError:
            return {"domain": domain, "ejected": ejected}
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return {"domain": domain, "ejected": ejected}
        for disk in root.findall("./devices/disk"):
            if disk.get("device") != "cdrom":
                continue
            source = disk.find("source")
            if source is None or not source.get("file"):
                continue
            target = disk.find("target")
            device = target.get("dev") if target is not None else None
            try:
                backend.update_media(domain, None, device, force=True)
                ejected.append(device or "cdrom")
            except VemuError:
                continue
    return {"domain": domain, "ejected": ejected}


def vm_config_update(
    domain: str,
    *,
    memory_mib: int | None = None,
    vcpus: int | None = None,
    vcpus_max: int | None = None,
    cpu_mode: str | None = None,
    machine: str | None = None,
    video: str | None = None,
    video_memory_mib: int | None = None,
    disk_bus: str | None = None,
    nic_model: str | None = None,
    os_type: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    if os_type is not None and os_type not in {
        "windows", "linux", "hypervisor", "generic"
    }:
        raise ValidationError(
            "Guest OS type must be windows, linux, hypervisor or generic."
        )
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain)
        before_xml = backend.config_xml(domain)
        before = parse_config(before_xml)
        if disk_bus is not None:
            try:
                chain_dir = _chain_dir_for_domain(backend, domain)
            except NotFound:
                chain_dir = None
            if chain_dir is not None:
                loaded = ChainManager(chain_dir).load()
                if isinstance(loaded, MultiDiskChain):
                    raise Conflict(
                        "Changing the data disk bus is not supported for a "
                        "v2 chain; the bus and target are part of the chain "
                        "metadata. Add a new disk with the desired bus, or "
                        "recreate the VM."
                    )
        before_max = int(
            before.get("memory_max_mib") or before.get("memory_mib") or 0
        )
        before_current = int(before.get("memory_mib") or before_max)
        live_memory = False
        live_vcpus = False
        warnings: list[str] = []
        maximum: int | None = None
        before_vcpu_max = int(
            before.get("vcpus_max") or before.get("vcpus") or 1
        )
        if vcpus is None and vcpus_max is not None:
            vcpus = int(before.get("vcpus") or 1)
        vcpu_ceiling: int | None = None
        if vcpus is not None:
            if vcpus_max is not None:
                vcpu_ceiling = max(vcpus_max, vcpus)
            elif info.state == "running":
                vcpu_ceiling = max(before_vcpu_max, vcpus)
            else:
                vcpu_ceiling = vcpus
            if vcpus_max is not None and vcpus_max < vcpus:
                raise ValidationError(
                    "The vCPU boot maximum must not be lower than the "
                    "current count."
                )
            if (
                info.state == "running"
                and vcpus != before.get("vcpus")
            ):
                if vcpus <= before_vcpu_max:
                    try:
                        backend.set_vcpus_live(domain, vcpus)
                        live_vcpus = True
                    except VemuError as exc:
                        warnings.append(
                            f"{exc} The new value is saved for the next "
                            "start."
                        )
                else:
                    warnings.append(
                        "Live vCPU increase above the boot maximum "
                        f"({before_vcpu_max}) needs a restart. The new value "
                        "is saved for the next start."
                    )
        if memory_mib is not None:
            if info.state == "running" and memory_mib != before_current:
                if memory_mib <= before_max:
                    try:
                        backend.set_memory_live(domain, memory_mib)
                        live_memory = True
                    except VemuError as exc:
                        warnings.append(
                            f"{exc} The new value is saved for the next start."
                        )
                else:
                    warnings.append(
                        "Live memory increase above the current maximum "
                        f"({before_max} MiB) needs a restart. The new value "
                        "is saved for the next start."
                    )
            if live_memory or (
                info.state == "running" and memory_mib <= before_max
            ):
                maximum = before_max
            else:
                maximum = memory_mib
        updated_xml = apply_config(
            before_xml,
            memory_mib=memory_mib,
            memory_max_mib=maximum,
            vcpus=vcpus,
            vcpus_max=vcpu_ceiling,
            cpu_mode=cpu_mode,
            machine=machine,
            video=video,
            video_memory_mib=video_memory_mib,
            disk_bus=disk_bus,
            nic_model=nic_model,
        )
        changes: list[str] = []
        if os_type is not None:
            root = ET.fromstring(updated_xml)
            meta = _management_fields(root)
            updated_xml = set_management_metadata(
                updated_xml,
                mode=meta.get("mode") or "full",
                managed_by=meta.get("managed_by") or info.managed_by,
                origin=meta.get("origin") or "vemu",
                backup=meta.get("backup") or None,
                os_type=os_type,
            )
        after = parse_config(updated_xml)
        changes = diff_summary(before, after)
        if os_type is not None and os_type != before.get("os_type"):
            changes.append(f"os_type: {before.get('os_type')} → {os_type}")
        backend.redefine_domain(domain, updated_xml)
        pending_restart = info.state == "running" and bool(changes)
        if info.state == "running" and changes:
            pending_restart = not all(
                (
                    live_memory
                    and change.startswith(
                        ("memory_mib:", "memory_max_mib:")
                    )
                )
                or (live_vcpus and change.startswith("vcpus"))
                for change in changes
            )
        return {
            "config": after,
            "changes": changes,
            "state": info.state,
            "pending_restart": pending_restart,
            "live_memory": live_memory,
            "live_vcpus": live_vcpus,
            "warnings": warnings,
        }


def vm_disk_add(
    domain: str,
    path: str,
    *,
    size_gb: int = 10,
    bus: str = "virtio",
    format: str = "qcow2",
    create_file: bool = True,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    if bus not in {"virtio", "sata", "scsi", "ide", "nvme"}:
        raise ValidationError(f"Unsupported disk bus: {bus}")
    if format not in {"qcow2", "raw"}:
        raise ValidationError("Disk format must be qcow2 or raw.")
    disk_path = Path(path).expanduser().resolve()
    if create_file and format == "raw" and size_gb < 1:
        raise ValidationError("Disk size must be at least 1 GB.")
    created_file = False
    if create_file:
        if disk_path.exists():
            raise Conflict(f"Disk file already exists: {disk_path}")
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        qemu_img = shutil.which("qemu-img")
        if not qemu_img:
            raise NotFound("qemu-img was not found.")
        result = subprocess.run(
            [
                qemu_img,
                "create",
                "-f",
                format,
                str(disk_path),
                f"{size_gb}G",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ExternalCommandError(
                " ".join([qemu_img, "create", "-f", format, str(disk_path)]),
                result.returncode,
                result.stderr or result.stdout,
            )
        created_file = True
    try:
        with LibvirtBackend(uri) as backend:
            info = _require_writable(backend, domain, full=True)
            xml = backend.config_xml(domain)
            chain_dir = None
            try:
                chain_dir = _chain_dir_for_domain(backend, domain)
            except NotFound:
                pass
            manager = None
            chain = None
            if chain_dir is not None:
                manager = ChainManager(chain_dir)
                chain = manager.load()
                if not isinstance(chain, MultiDiskChain):
                    raise Conflict(
                        "Adding a disk to a legacy v1 chain is not supported; "
                        "recreate the VM as v2 first."
                    )
                if info.state != "stopped":
                    raise Conflict(
                        "Shut down the VM before adding a disk to a v2 chain."
                    )
                if format != "qcow2":
                    raise ValidationError(
                        "Only qcow2 data disks can be added to a v2 chain."
                    )
            chain_targets = (
                set(chain.disks)
                if isinstance(chain, MultiDiskChain)
                else set()
            )
            updated, target = add_disk(
                xml,
                str(disk_path),
                bus,
                format=format,
                reserved_targets=chain_targets,
            )
            new_chain = None
            active_path = str(disk_path)
            if manager is not None:
                new_chain = manager.add_disk(
                    target, disk_path, bus=bus, label="added"
                )
                active_path = new_chain.active_disk()
                root = ET.fromstring(updated)
                for disk in root.findall("./devices/disk"):
                    target_element = disk.find("target")
                    source = disk.find("source")
                    if (
                        target_element is not None
                        and target_element.get("dev") == target
                        and source is not None
                    ):
                        source.set("file", active_path)
                        break
                updated = ET.tostring(root, encoding="unicode")
            try:
                backend.redefine_domain(domain, updated)
            except Exception:
                if manager is not None and new_chain is not None:
                    try:
                        manager.remove_disk(target)
                    except VemuError:
                        pass
                    for layer in new_chain.layers:
                        Path(layer.path).unlink(missing_ok=True)
                raise
            if created_file and manager is not None:
                try:
                    os.chmod(disk_path, 0o444)
                except OSError:
                    pass
            return {
                "disk": {
                    "source": active_path,
                    "base": str(disk_path) if manager is not None else None,
                    "target": target,
                    "bus": bus,
                    "format": format,
                    "size_gb": size_gb,
                },
                "config": parse_config(updated),
                "state": info.state,
                "pending_restart": info.state == "running",
            }
    except Exception:
        if created_file:
            disk_path.unlink(missing_ok=True)
        raise


def vm_disk_remove(
    domain: str,
    source: str,
    *,
    delete_file: bool = False,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    disk_path = Path(source).expanduser().resolve()
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain, full=True)
        xml = backend.config_xml(domain)
        chain_dir = None
        try:
            chain_dir = _chain_dir_for_domain(backend, domain)
        except NotFound:
            pass
        manager = None
        chain = None
        target = None
        active_path = str(disk_path)
        if chain_dir is not None:
            manager = ChainManager(chain_dir)
            chain = manager.load()
            if isinstance(chain, MultiDiskChain):
                try:
                    target = next(
                        key
                        for key, disk in chain.disks.items()
                        if disk_path
                        in {
                            Path(disk.base).resolve(),
                            *(Path(layer.path).resolve() for layer in disk.layers),
                        }
                    )
                except StopIteration:
                    target = None
                if target is not None:
                    if info.state != "stopped":
                        raise Conflict(
                            "Shut down the VM before removing a disk from a "
                            "v2 chain."
                        )
                    if len(chain.disks) < 2:
                        raise Conflict(
                            "Cannot remove the only data disk from a chain."
                        )
                    active_path = chain.disks[target].active_disk()
            else:
                chain_paths = {
                    Path(chain.base).resolve(),
                    *(Path(layer.path).resolve() for layer in chain.layers),
                }
                if disk_path in chain_paths:
                    raise Conflict(
                        "Removing a disk from a legacy v1 chain is not "
                        "supported; recreate the VM as v2 first."
                    )
        old_chain_json = (
            manager.chain_file.read_text(encoding="utf-8")
            if manager is not None and target is not None
            else None
        )
        removed_chain = None
        if manager is not None and target is not None:
            removed_chain = manager.remove_disk(target)
        try:
            updated, removed = remove_disk(xml, active_path)
            backend.redefine_domain(domain, updated)
        except Exception:
            if old_chain_json is not None and manager is not None:
                manager.chain_file.write_text(old_chain_json, encoding="utf-8")
            raise
        deleted_files: list[str] = []
        if delete_file:
            paths: list[Path] = [disk_path]
            if removed_chain is not None:
                paths = [Path(layer.path) for layer in removed_chain.layers]
                base_path = Path(removed_chain.base)
                try:
                    base_path.relative_to(manager.chain_dir)
                except ValueError:
                    pass
                else:
                    paths.append(base_path)
            for path in paths:
                path.unlink(missing_ok=True)
                deleted_files.append(str(path))
        return {
            "disk": removed,
            "config": parse_config(updated),
            "file_deleted": delete_file,
            "deleted_files": deleted_files,
            "state": info.state,
            "pending_restart": info.state == "running",
        }


def vm_disk_resize(
    domain: str,
    source: str,
    size_gb: int,
    *,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    if size_gb < 1:
        raise ValidationError("Disk size must be at least 1 GB.")
    disk_path = Path(source).expanduser().resolve()
    if not disk_path.is_file():
        raise NotFound(f"Disk file was not found: {disk_path}")
    current_bytes = _qemu_img_size_bytes(disk_path)
    if current_bytes is None:
        raise VemuError(f"Cannot read the current disk size: {disk_path}")
    new_bytes = size_gb * 1024**3
    if new_bytes < current_bytes:
        raise ValidationError(
            "Expanding only: shrinking a disk is not supported. Prepare and "
            "copy to a smaller disk offline instead."
        )
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain, full=True)
        config = parse_config(backend.config_xml(domain))
        chain_dir = None
        try:
            chain_dir = _chain_dir_for_domain(backend, domain)
        except NotFound:
            pass
        if chain_dir is not None:
            chain = ChainManager(chain_dir).load()
            disks = (
                list(chain.disks.values())
                if isinstance(chain, MultiDiskChain)
                else [chain]
            )
            active = {Path(disk.active_disk()).resolve() for disk in disks}
            if disk_path not in active:
                raise Conflict(
                    "Only the active chain layer can be resized; revert to "
                    "the layer first or resize a non-chain disk."
                )
        disk = next(
            (
                item
                for item in config.get("disks", [])
                if item.get("device") == "disk"
                and item.get("source")
                and Path(item["source"]).resolve() == disk_path
            ),
            None,
        )
        if disk is None:
            raise NotFound(f"Data disk was not found: {disk_path}")
        if new_bytes == current_bytes:
            return {
                "domain": domain,
                "disk": disk,
                "old_size_gb": round(current_bytes / (1024**3), 2),
                "new_size_gb": round(current_bytes / (1024**3), 2),
                "live": info.state == "running",
                "pending_restart": False,
                "changed": False,
            }
        if info.state == "running":
            backend.block_resize(domain, disk["target"], new_bytes)
            live = True
        else:
            qemu_img = shutil.which("qemu-img")
            if not qemu_img:
                raise NotFound("qemu-img was not found.")
            result = subprocess.run(
                [qemu_img, "resize", str(disk_path), f"{size_gb}G"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise ExternalCommandError(
                    " ".join([qemu_img, "resize", str(disk_path)]),
                    result.returncode,
                    result.stderr or result.stdout,
                )
            live = False
    return {
        "domain": domain,
        "disk": disk,
        "old_size_gb": round(current_bytes / (1024**3), 2),
        "new_size_gb": size_gb,
        "live": live,
        "pending_restart": False,
        "changed": True,
    }


def vm_tpm_add(
    domain: str,
    *,
    version: str = "2.0",
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain)
        updated = add_tpm(backend.config_xml(domain), version)
        backend.redefine_domain(domain, updated)
        return {
            "config": parse_config(updated),
            "state": info.state,
            "pending_restart": info.state == "running",
        }


def vm_tpm_remove(
    domain: str, *, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain)
        updated = remove_tpm(backend.config_xml(domain))
        backend.redefine_domain(domain, updated)
        return {
            "config": parse_config(updated),
            "state": info.state,
            "pending_restart": info.state == "running",
        }


def vm_usb_add(
    domain: str, *, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain)
        updated = add_usb_controller(backend.config_xml(domain))
        backend.redefine_domain(domain, updated)
        return {
            "config": parse_config(updated),
            "state": info.state,
            "pending_restart": info.state == "running",
        }


def vm_usb_list(
    domain: str | None = None, *, uri: str = "qemu:///system"
) -> list[dict[str, Any]]:
    with LibvirtBackend(uri) as backend:
        return backend.usb_devices(domain)


def vm_usb_attach(
    domain: str,
    vendor: str,
    product: str,
    *,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        return backend.attach_usb(domain, vendor, product)


def vm_usb_detach(
    domain: str,
    vendor: str,
    product: str,
    *,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        return backend.detach_usb(domain, vendor, product)


def vm_boot_menu(
    domain: str,
    *,
    timeout_ms: int = 5000,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        return backend.boot_menu(domain, timeout_ms=timeout_ms)


def vm_boot_firmware(
    domain: str, *, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        return backend.boot_firmware(domain)


def vm_promote_boot(
    domain: str | None = None, *, uri: str = "qemu:///system"
) -> dict[str, Any]:
    """Give an installed disk priority over attached installer media."""
    with LibvirtBackend(uri) as backend:
        if domain:
            _require_writable(backend, domain)
        return backend.vm_promote_boot(domain)


def vm_audio_add(
    domain: str, *, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain)
        updated = add_audio(backend.config_xml(domain))
        backend.redefine_domain(domain, updated)
        return {
            "config": parse_config(updated),
            "state": info.state,
            "pending_restart": info.state == "running",
        }


def vm_audio_remove(
    domain: str, *, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain)
        updated = remove_audio(backend.config_xml(domain))
        backend.redefine_domain(domain, updated)
        return {
            "config": parse_config(updated),
            "state": info.state,
            "pending_restart": info.state == "running",
        }


def _share_rows(
    backend: LibvirtBackend,
    domain: str,
    state: str,
    store: ShareStore | None = None,
) -> list[dict[str, Any]]:
    config = parse_config(backend.config_xml(domain))
    live_tags: set[str] = set()
    if state == "running":
        try:
            live = parse_config(backend.live_xml(domain))
            live_tags = {
                str(item.get("tag", ""))
                for item in live.get("shares", [])
            }
        except VemuError:
            live_tags = set()
    assignments = {
        item.share: item
        for item in (store or ShareStore()).assignments_for(domain)
    }
    rows: list[dict[str, Any]] = []
    for share in config.get("shares", []):
        tag = str(share.get("tag", ""))
        assignment = assignments.get(tag)
        rows.append(
            {
                **share,
                "auto_mount": bool(assignment.auto_mount) if assignment else False,
                "drive": assignment.drive if assignment else None,
                "mountpoint": assignment.mountpoint if assignment else None,
                "share": assignment.share if assignment else None,
                "live": (state != "running") or tag in live_tags,
                "pending_restart": state == "running" and tag not in live_tags,
            }
        )
    return rows


def vm_shares(
    domain: str, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        shares = _share_rows(backend, domain, info.state)
    return {
        "domain": domain,
        "state": info.state,
        "shares": shares,
        "pending_restart": any(
            item.get("pending_restart") for item in shares
        ),
    }


def shares_list() -> list[dict[str, Any]]:
    return ShareStore().list()


def share_catalog_add(
    name: str,
    path: str,
    tag: str,
    *,
    read_only: bool = False,
    description: str = "",
) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_dir():
        raise NotFound(f"Share directory was not found: {source}")
    item = ShareDefinition(
        name=name.strip(),
        path=str(source),
        tag=tag.strip(),
        read_only=read_only,
        description=description.strip(),
    )
    return ShareStore().add(item)


def share_catalog_update(
    name: str,
    path: str,
    tag: str,
    *,
    read_only: bool = False,
    description: str = "",
) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_dir():
        raise NotFound(f"Share directory was not found: {source}")
    item = ShareDefinition(
        name=name.strip(),
        path=str(source),
        tag=tag.strip(),
        read_only=read_only,
        description=description.strip(),
    )
    return ShareStore().update(item)


def share_catalog_remove(name: str) -> dict[str, Any]:
    return ShareStore().remove(name)


def vm_share_attach(
    domain: str,
    share_name: str,
    *,
    tag: str | None = None,
    read_only: bool | None = None,
    auto_mount: bool = False,
    drive: str | None = None,
    mountpoint: str | None = None,
    config: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    store = ShareStore(config)
    item = store.get(share_name)
    selected_tag = (tag or item.tag).strip()
    selected_read_only = item.read_only if read_only is None else read_only
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain)
        config_xml = backend.config_xml(domain)
        existing = next(
            (
                share
                for share in parse_config(config_xml).get("shares", [])
                if str(share.get("tag", "")) == selected_tag
            ),
            None,
        )
        if existing is not None:
            if str(Path(str(existing.get("source", ""))).resolve()) != item.path:
                raise Conflict(
                    f"Tag '{selected_tag}' is already attached to a different "
                    "host directory. Detach it first."
                )
            if bool(existing.get("read_only")) != selected_read_only:
                raise Conflict(
                    f"Tag '{selected_tag}' is already attached with a "
                    "different read-only mode. Detach it first."
                )
            updated = config_xml
        else:
            updated = add_virtiofs_share(
                config_xml,
                selected_tag,
                item.path,
                read_only=selected_read_only,
            )
            backend.redefine_domain(domain, updated)
        rows = _share_rows(backend, domain, info.state, store)
    assignment = ShareAssignment(
        domain=domain,
        share=item.name,
        tag=selected_tag,
        read_only=selected_read_only,
        auto_mount=bool(auto_mount),
        drive=drive.upper() if drive else None,
        mountpoint=mountpoint,
    )
    store.attach(assignment)
    return {
        "domain": domain,
        "share": item.to_dict(),
        "assignment": assignment.to_dict(),
        "state": info.state,
        "pending_restart": info.state == "running",
        "shares": rows,
    }


def vm_share_detach(
    domain: str,
    share_name: str,
    *,
    config: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    store = ShareStore(config)
    assignment = next(
        (
            item
            for item in store.assignments_for(domain)
            if item.share == share_name
        ),
        None,
    )
    if assignment is None:
        raise NotFound(
            f"Share '{share_name}' is not attached to '{domain}'."
        )
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain)
        updated, removed = remove_virtiofs_share(
            backend.config_xml(domain), assignment.tag
        )
        backend.redefine_domain(domain, updated)
    store.detach(domain, share_name)
    return {
        "domain": domain,
        "share": share_name,
        "removed": removed,
        "state": info.state,
        "pending_restart": info.state == "running",
    }


def vm_share_options(
    domain: str,
    share_name: str,
    *,
    auto_mount: bool | None = None,
    drive: str | None = None,
    mountpoint: str | None = None,
    config: str | None = None,
) -> dict[str, Any]:
    store = ShareStore(config)
    assignment = next(
        (
            item
            for item in store.assignments_for(domain)
            if item.share == share_name
        ),
        None,
    )
    if assignment is None:
        raise NotFound(
            f"Share '{share_name}' is not attached to '{domain}'."
        )
    if auto_mount is not None:
        assignment.auto_mount = bool(auto_mount)
    if drive is not None:
        assignment.drive = drive.upper() if drive else None
    if mountpoint is not None:
        assignment.mountpoint = mountpoint.strip() or None
    return store.update_assignment(assignment)


def vm_share_status(
    domain: str, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        shares = _share_rows(backend, domain, info.state)
        mounts: dict[str, dict[str, Any]] = {}
        guest_mode = "unknown"
        if info.state == "running" and shares:
            try:
                status = guest_virtiofs_mount_status(backend, domain)
                mounts = status.get("mounts", {})
                guest_mode = str(status.get("mode", "unknown"))
            except VemuError:
                mounts = {}
        for share in shares:
            tag = str(share.get("tag", ""))
            mount = mounts.get(tag) or {}
            share["mounted"] = bool(mount)
            share["guest_mount"] = mount.get("drive") or mount.get("path")
    return {
        "domain": domain,
        "state": info.state,
        "guest_mode": guest_mode,
        "shares": shares,
        "pending_restart": any(
            item.get("pending_restart") for item in shares
        ),
    }


def vm_share_mount(
    domain: str,
    *,
    tag: str | None = None,
    config: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        if info.state != "running":
            raise Conflict(
                f"Domain '{domain}' must be running to mount shares."
            )
        store = ShareStore(config)
        shares = _share_rows(backend, domain, info.state, store)
        if tag:
            shares = [
                item for item in shares
                if str(item.get("tag", "")) == tag
            ]
        if not shares:
            raise Conflict(
                f"Domain '{domain}' has no matching VirtioFS shares."
            )
        pending = [item for item in shares if item.get("pending_restart")]
        if pending:
            raise Conflict(
                "VirtioFS shares were added after the VM started. Shut down "
                "and start the VM again (not a Windows reboot), then retry."
            )
        assignments = {
            item.tag: item
            for item in store.assignments_for(domain)
        }
        payload = []
        for item in shares:
            tag = str(item.get("tag", ""))
            assignment = assignments.get(tag)
            payload.append(
                {
                    "tag": tag,
                    "drive": assignment.drive if assignment else None,
                    "mountpoint": (
                        assignment.mountpoint if assignment else None
                    ),
                }
            )
        result = guest_virtiofs_mount(backend, domain, payload)
    return {
        "domain": domain,
        "state": info.state,
        **result,
    }


def vm_share_unmount(
    domain: str, tag: str, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        if info.state != "running":
            raise Conflict(
                f"Domain '{domain}' must be running to unmount shares."
            )
        return {
            "domain": domain,
            **guest_virtiofs_unmount(backend, domain, tag),
        }


def vm_share_auto_mount(
    domain: str,
    *,
    config: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    store = ShareStore(config)
    auto = {
        item.tag
        for item in store.assignments_for(domain)
        if item.auto_mount
    }
    if not auto:
        return {"domain": domain, "mounts": [], "skipped": True}
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        if info.state != "running":
            return {"domain": domain, "mounts": [], "skipped": True}
        shares = [
            item
            for item in _share_rows(backend, domain, info.state, store)
            if item.get("tag") in auto and not item.get("pending_restart")
        ]
        if not shares:
            return {"domain": domain, "mounts": [], "skipped": True}
        payload = [
            {
                "tag": str(item.get("tag", "")),
                "drive": item.get("drive"),
                "mountpoint": item.get("mountpoint"),
            }
            for item in shares
        ]
        result = guest_virtiofs_mount(backend, domain, payload)
    return {"domain": domain, **result}


def vm_share_add(
    domain: str,
    tag: str,
    source: str,
    *,
    read_only: bool = False,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    path = Path(source).expanduser().resolve()
    if not path.is_dir():
        raise NotFound(f"Share directory was not found: {path}")
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain)
        updated = add_virtiofs_share(
            backend.config_xml(domain),
            tag,
            str(path),
            read_only=read_only,
        )
        backend.redefine_domain(domain, updated)
        config = parse_config(updated)
    return {
        "domain": domain,
        "config": config,
        "shares": config.get("shares", []),
        "state": info.state,
        "pending_restart": info.state == "running",
    }


def vm_share_remove(
    domain: str, tag: str, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        info = _require_writable(backend, domain)
        updated, removed = remove_virtiofs_share(
            backend.config_xml(domain), tag
        )
        backend.redefine_domain(domain, updated)
        config = parse_config(updated)
    return {
        "domain": domain,
        "removed": removed,
        "config": config,
        "shares": config.get("shares", []),
        "state": info.state,
        "pending_restart": info.state == "running",
    }


def clone_create(
    template_path: str,
    dest: str,
    *,
    count: int = 1,
    name: str = "vm-{{n}}",
    start_at: int = 1,
    define: bool = False,
    start: bool = False,
    memory: int | None = None,
    vcpus: int | None = None,
    network: str = "default",
    network_config: str | None = None,
    firmware: str | None = None,
    uri: str = "qemu:///system",
    reset_identity: bool = False,
    cloud_init: bool = False,
    cloud_init_user: str = "lab",
    cloud_init_password: str | None = None,
    cloud_init_ssh_key_file: str | None = None,
    cloud_init_packages: list[str] | None = None,
    cloud_init_run: list[str] | None = None,
    cloud_init_ip: str | None = None,
    cloud_init_gateway: str | None = None,
    cloud_init_dns: list[str] | None = None,
    windows_oobe: bool = False,
    windows_admin_password: str | None = None,
) -> dict[str, Any]:
    if start and not define:
        raise ValidationError("--start requires --define.")
    template = resolve_template(template_path)
    os_family, inferred_os_family = template.effective_os_family()
    if cloud_init and os_family != "linux":
        raise ValidationError(
            "cloud-init is for Linux templates; this template is marked "
            f"as {os_family}."
        )
    if windows_oobe and (
        os_family != "windows" or not template.sysprepped
    ):
        raise ValidationError(
            "The Windows OOBE answer is only available for a sysprepped "
            "Windows template."
        )
    if cloud_init:
        # cloud-init owns instance-id/hostname; do not run a second reset.
        reset_identity = False
    if (cloud_init_ip or cloud_init_gateway) and not cloud_init:
        raise ValidationError(
            "--ci-ip/--ci-gateway require --cloud-init."
        )
    ssh_keys: list[str] = []
    if cloud_init_ssh_key_file:
        key_path = Path(cloud_init_ssh_key_file).expanduser().resolve()
        if not key_path.is_file():
            raise NotFound(f"SSH public key was not found: {key_path}")
        key = key_path.read_text(encoding="utf-8").strip()
        if not key:
            raise ValidationError(f"SSH public key is empty: {key_path}")
        ssh_keys.append(key)
    if cloud_init and not ssh_keys and not cloud_init_password:
        raise ValidationError(
            "cloud-init needs --ci-ssh-key or --ci-password."
        )
    dest_path = Path(dest).expanduser().resolve()
    dest_path.mkdir(parents=True, exist_ok=True)
    existing = {entry.name for entry in dest_path.iterdir() if entry.is_dir()}
    names = render_names(name, count, start=start_at, existing=existing)
    macs: dict[str, str] = {}
    if cloud_init and (cloud_init_ip or cloud_init_gateway):
        if not define:
            raise ValidationError(
                "Static cloud-init networking requires --define."
            )
        with LibvirtBackend(uri) as backend:
            used: set[str] = set()
            for clone_name in names:
                for _ in range(50):
                    mac = backend.unique_mac()
                    if mac not in used:
                        used.add(mac)
                        macs[clone_name] = mac
                        break

    created: list[dict[str, Any]] = []
    identity_skips: list[str] = []
    template_disks = template.disk_specs()
    first_target = str(template_disks[0]["target"])
    for clone_name in names:
        manager = ChainManager.create_multi(
            dest_path / clone_name,
            template_disks,
        )
        chain = manager.load()
        active = {
            target: disk.active_disk()
            for target, disk in chain.disks.items()
        }
        seed_iso = None
        oobe_floppy = None
        if cloud_init:
            seed = CloudInitConfig(
                hostname=clone_name,
                user=cloud_init_user,
                password=cloud_init_password,
                ssh_keys=ssh_keys,
                packages=cloud_init_packages or [],
                runcmd=cloud_init_run or [],
                mac=macs.get(clone_name),
                static_ip=cloud_init_ip,
                gateway=cloud_init_gateway,
                dns=cloud_init_dns or [],
            )
            seed_iso = build_cloud_init_iso(manager.chain_dir, seed)
        elif os_family == "windows" and template.sysprepped and windows_oobe:
            if not windows_admin_password:
                raise ValidationError(
                    "The Windows OOBE answer needs an administrator password "
                    "of at least 8 characters."
                )
            oobe_floppy = write_windows_oobe_floppy(
                manager.chain_dir,
                computer_name=clone_name,
                admin_password=windows_admin_password,
            )
        created.append(
            {
                "name": clone_name,
                "chain": str(manager.chain_file),
                "disk": active[first_target],
                "disks": [
                    {
                        "target": target,
                        "bus": chain.disks[target].bus,
                        "source": active[target],
                    }
                    for target in active
                ],
                "cloud_init_iso": str(seed_iso) if seed_iso else None,
                "windows_oobe_floppy": (
                    str(oobe_floppy) if oobe_floppy else None
                ),
                "mac": macs.get(clone_name),
                "defined": False,
                "started": False,
                "identity_reset": False,
                "identity_reset_skipped": False,
            }
        )
        _relabel(manager.chain_dir)

    if reset_identity and os_family == "linux":
        try:
            for item in created:
                hostname = _clone_hostname(item["name"])
                identity = reset_linux(
                    item["disk"], hostname, allow_unsupported=True
                )
                item["identity_reset"] = identity.executed
                if identity.skipped:
                    item["identity_reset_skipped"] = True
                    if (
                        identity.reason
                        and identity.reason not in identity_skips
                    ):
                        identity_skips.append(identity.reason)
                _relabel(Path(item["chain"]).parent)
        except Exception:
            # The domain has not been defined yet. Remove the freshly created
            # chains so a failed identity reset does not leave orphan VMs.
            for item in created:
                shutil.rmtree(Path(item["chain"]).parent, ignore_errors=True)
            raise

    warnings: list[str] = list(identity_skips)
    if inferred_os_family:
        warnings.append(
            "Template has no os_family; assuming "
            f"{os_family} from its legacy sysprep flag. "
            "Set the OS family in Templates to make this explicit."
        )
    if os_family == "linux" and template.sysprepped:
        warnings.append(
            "Template is marked Linux but still has the legacy sysprepped "
            "flag; the flag is ignored for this Linux clone."
        )
    secure_fallback = False
    if define:
        memory_mib = memory if memory is not None else template.memory_mib
        clone_vcpus = vcpus if vcpus is not None else template.vcpus
        clone_firmware = firmware or template.firmware
        libvirt_network = (
            NetworkStore(network_config).find_libvirt_name(network) or network
        )
        with LibvirtBackend(uri) as backend:
            for item in created:
                data_disks = [
                    Disk(
                        path=disk["source"],
                        bus=disk["bus"] or template.disk_bus,
                        target=disk["target"],
                    )
                    for disk in item["disks"]
                ]
                if item.get("cloud_init_iso"):
                    data_disks.append(
                        Disk(
                            path=item["cloud_init_iso"],
                            bus="sata",
                            target="sdb",
                            device="cdrom",
                            format="raw",
                            discard="",
                        )
                    )
                if item.get("windows_oobe_floppy"):
                    data_disks.append(
                        Disk(
                            path=item["windows_oobe_floppy"],
                            bus="usb",
                            target="sdz",
                            device="disk",
                            format="raw",
                            discard="",
                            boot=99,
                        )
                    )
                if not item.get("cloud_init_iso"):
                    # Keep a removable CD/DVD tray available even when the
                    # template was sealed without one. The clone can then use
                    # Change/Eject media from the hardware editor.
                    data_disks.append(
                        Disk(
                            path="",
                            bus="sata",
                            target="sdb",
                            device="cdrom",
                            format="raw",
                            discard="",
                        )
                    )
                template_os = template.effective_os_family()[0]
                spec = DomainSpec(
                    name=item["name"],
                    memory_mib=memory_mib,
                    vcpus=clone_vcpus,
                    disks=data_disks,
                    networks=[
                        Network(
                            network=libvirt_network,
                            mac=item.get("mac") or backend.unique_mac(),
                            model=template.nic_model,
                        )
                    ],
                    arch=template.arch,
                    domain_type=domain_type_for(template.arch),
                    machine=template.machine,
                    firmware=clone_firmware,
                    video=template.video,
                    video_memory_mib=template.video_memory_mib,
                    cpu_mode=(
                        "host-passthrough"
                        if domain_type_for(template.arch) == "kvm"
                        else "maximum"
                    ),
                    secure_boot=template.secure_boot,
                    tpm=template.tpm,
                    os_type=(
                        "windows" if template_os == "windows" else "linux"
                    ),
                )
                secure_requested = spec.secure_boot
                secure_applied = _apply_custom_firmware(spec)
                if secure_requested and not secure_applied:
                    secure_fallback = True
                backend.define(spec.to_xml())
                _apply_runtime_profile(backend, item["name"])
                item["defined"] = True
                if start:
                    backend.start(item["name"])
                    item["started"] = True

    if secure_fallback:
        warnings.append(
            "Secure Boot firmware is not installed; clones use the standard "
            "v-EMU firmware. Run ./setup.sh --standard to install it."
        )
    if template.tpm:
        warnings.append(
            "Clones receive a new TPM state. BitLocker or LUKS-TPM volumes "
            "may enter recovery; keep the recovery key or disable/suspend "
            "encryption before sealing."
        )
    if template.secure_boot:
        warnings.append(
            "Secure Boot is inherited from the template. The clone gets its "
            "own NVRAM and does not copy the source VM's UEFI variables."
        )
    return {"created": created, "warnings": warnings}


def vm_create_from_iso(
    name: str,
    iso: str,
    dest: str,
    *,
    disk_size_gb: int = 40,
    memory_mib: int = 4096,
    vcpus: int = 2,
    network: str = "default",
    network_config: str | None = None,
    os_type: str = "windows",
    os_variant: str = "",
    arch: str = "x86_64",
    windows_unattend: bool | None = None,
    windows_edition: str = DEFAULT_WINDOWS_EDITION,
    admin_password: str = DEFAULT_ADMIN_PASSWORD,
    computer_name: str | None = None,
    virtio_iso: str | None = None,
    firmware: str = "bios",
    machine: str = "q35",
    disk_bus: str | None = None,
    iso_bus: str | None = None,
    nic_model: str | None = None,
    video: str | None = None,
    video_memory_mib: int | None = None,
    hyperv: bool | None = None,
    cpu_mode: str = "host-passthrough",
    tpm: bool = False,
    secure_boot: bool | None = None,
    start: bool = False,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Create a VM that boots from an ISO (optionally with Windows unattend)."""
    if os_type not in {"windows", "linux", "hypervisor", "other"}:
        raise ValidationError(
            "OS type must be windows, linux, hypervisor or other."
        )
    if arch not in {"x86_64", "aarch64", "riscv64"}:
        raise ValidationError(f"Unsupported architecture: {arch}")
    if firmware not in {"bios", "efi"}:
        raise ValidationError("Firmware must be bios or efi.")
    if not machine:
        raise ValidationError("Machine type must not be empty.")
    # non_x86: the guest uses the ARM/RISC-V "virt" platform.
    # emulated: the guest arch differs from the host, so it runs under TCG.
    domain_type = domain_type_for(arch)
    emulated = domain_type == "qemu"
    non_x86 = arch != "x86_64"
    if non_x86:
        if machine in {"q35", "pc"} or "virt" not in machine:
            machine = "virt"
        firmware = "efi"
        disk_bus = disk_bus or "virtio"
        nic_model = nic_model or "virtio"
    if emulated and cpu_mode in {"host-passthrough", "host-model"}:
        cpu_mode = "maximum"
    if windows_unattend is None:
        unattend = (
            not non_x86
            and os_type == "windows"
            and os_variant in {"", "windows-server-2025"}
        )
    else:
        unattend = bool(windows_unattend)
    if unattend and os_type != "windows":
        raise ValidationError(
            "Unattended install is only available when the install ISO is Windows."
        )
    if unattend and os_variant not in {"", "windows-server-2025"}:
        raise ValidationError(
            "The unattended answer file targets Windows Server 2025. "
            "Use a manual install for this Windows version."
        )
    if unattend and non_x86:
        raise ValidationError(
            "Unattended Windows installs require x86_64; use the interactive "
            "installer for other architectures."
        )
    # Treat non-Linux guests as generic hardware: virtio drivers may not be
    # present in the installer, so SATA + e1000e + VGA are the safe defaults.
    generic_hardware = os_type in {"windows", "hypervisor", "other"}
    disk_bus = disk_bus or ("sata" if generic_hardware else "virtio")
    nic_model = nic_model or ("e1000e" if generic_hardware else "virtio")
    if video is None:
        if os_type == "linux" and arch == "x86_64":
            video = "vga"
        else:
            video = "vga" if generic_hardware else "virtio"
    if (
        video_memory_mib is None
        and os_type == "windows"
        and arch == "x86_64"
        and video in {"vga", "qxl"}
    ):
        video_memory_mib = 64
    if video_memory_mib is not None and video not in {"vga", "qxl"}:
        raise ValidationError(
            "Video memory is only configurable with vga or qxl."
        )
    if hyperv is None:
        hyperv = os_type == "windows" and arch == "x86_64"
    if non_x86:
        # virt-platform guests keep virtio defaults unless the caller chose a device.
        nic_model = nic_model or "virtio"
        if disk_bus == "ide":
            disk_bus = "virtio"
        if video == "vga" and generic_hardware:
            # WinPE and installed Windows have no virtio-gpu driver; ramfb
            # keeps the firmware framebuffer visible.
            video = "ramfb"
        if video not in {"virtio", "vga", "ramfb"}:
            video = "ramfb" if generic_hardware else "virtio"
    if disk_bus not in {"virtio", "sata", "scsi", "ide", "nvme", "usb"}:
        raise ValidationError(f"Unsupported disk bus: {disk_bus}")
    if iso_bus is not None and iso_bus not in {"sata", "scsi", "ide", "usb"}:
        raise ValidationError(f"Unsupported ISO bus: {iso_bus}")
    if nic_model not in {"virtio", "e1000e", "e1000", "rtl8139", "vmxnet3", "pcnet"}:
        raise ValidationError(f"Unsupported NIC model: {nic_model}")
    if video not in {"virtio", "vga", "qxl", "cirrus", "vmvga", "bochs", "ramfb"}:
        raise ValidationError(f"Unsupported video model: {video}")
    if cpu_mode not in {
        "host-passthrough",
        "host-model",
        "custom",
        "maximum",
    }:
        raise ValidationError(f"Unsupported CPU mode: {cpu_mode}")
    if disk_bus == "ide" and "q35" in machine:
        raise ValidationError(
            "The q35 machine has no IDE controller; use sata, scsi or virtio."
        )
    if disk_bus == "nvme" and not non_x86 and "q35" not in machine:
        raise ValidationError(
            "NVMe disks need the q35 machine (PCIe); use sata, scsi or "
            "virtio with pc/i440fx."
        )
    warnings: list[str] = []
    if emulated:
        warnings.append(
            f"{arch} runs under TCG emulation on this host and is much slower "
            "than native KVM. Use an architecture-matched ISO and UEFI firmware."
        )
    if non_x86 and os_type == "windows":
        warnings.append(
            "Windows on non-x86 architectures needs an architecture-"
            "specific ISO; the Windows Server 2025 unattended template is "
            "disabled."
        )
    if os_type == "windows" and (
        disk_bus != "sata" or nic_model != "e1000e" or video != "vga"
    ) and not non_x86:
        warnings.append(
            "Windows setup may not see virtio disk/NIC without drivers. "
            "SATA + e1000e + VGA are the safe defaults."
        )
    if os_type == "windows" and not unattend:
        warnings.append(
            "Windows will use the interactive installer; no answer file is "
            "generated."
        )
    if os_type == "other" and (
        disk_bus == "virtio" or nic_model == "virtio" or video == "virtio"
    ):
        warnings.append(
            "Virtio devices need guest drivers. Consider SATA + e1000e + VGA "
            "for BSD or unknown guest OSes."
        )
    if os_type == "hypervisor" and (
        disk_bus != "sata" or nic_model != "e1000e"
    ):
        warnings.append(
            "The ESXi installer commonly expects SATA + e1000e; verify that "
            "the selected devices are visible before installing."
        )
    if firmware == "efi" and not non_x86 and "q35" not in machine:
        warnings.append(
            "UEFI is normally used with q35; verify the guest firmware."
        )
    if tpm and non_x86:
        if arch == "aarch64":
            warnings.append(
                "TPM 2.0 is validated with the v-EMU aarch64 firmware; the "
                "guest OS still needs its own TPM driver."
            )
        elif arch == "riscv64":
            warnings.append(
                "TPM 2.0 is initialized by the v-EMU riscv64 firmware; verify the "
                "guest sees it (/sys/class/tpm or Device Manager)."
            )
        else:
            warnings.append(
                "TPM emulation on non-x86 architectures may not be supported "
                "by the guest firmware; verify the VM boots."
            )
    if tpm and not firmware == "efi":
        warnings.append(
            "TPM 2.0 is usually paired with UEFI; enable UEFI for Windows 11."
        )
    iso_path = Path(iso).expanduser().resolve()
    if not iso_path.is_file():
        raise NotFound(f"Install ISO was not found: {iso_path}")
    minimum_disk = 32 if os_type in {"windows", "hypervisor"} else 8
    if disk_size_gb < minimum_disk:
        raise ValidationError(
            f"Disk must be at least {minimum_disk} GB for {os_type}."
        )
    minimum_memory = (
        4096 if os_type == "hypervisor"
        else 2048 if os_type == "windows"
        else 512
    )
    if memory_mib < minimum_memory:
        raise ValidationError(
            f"Memory must be at least {minimum_memory} MiB for {os_type}."
        )
    edition_label = None
    index = 0
    if unattend:
        index, edition_label = edition_info(windows_edition)
        if len(admin_password) < 8:
            raise ValidationError(
                "Windows administrator password must be at least 8 characters."
            )
        if disk_size_gb < 32:
            raise ValidationError(
                "Windows Server 2025 needs a disk of at least 32 GB."
            )

    virtio_path = None
    if virtio_iso:
        virtio_path = Path(virtio_iso).expanduser().resolve()
        if not virtio_path.is_file():
            raise NotFound(f"Virtio driver ISO was not found: {virtio_path}")

    dest_path = Path(dest).expanduser().resolve()
    dest_path.mkdir(parents=True, exist_ok=True)
    chain_dir = dest_path / name
    if chain_dir.exists():
        raise Conflict(f"VM directory already exists: {chain_dir}")
    chain_dir.mkdir(parents=True)
    base = chain_dir / "base.qcow2"
    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        raise NotFound("qemu-img was not found.")
    result = subprocess.run(
        [qemu_img, "create", "-f", "qcow2", str(base), f"{disk_size_gb}G"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ExternalCommandError(
            " ".join([qemu_img, "create", "-f", "qcow2", str(base)]),
            result.returncode,
            result.stderr or result.stdout,
        )
    defined = False
    try:
        manager = ChainManager.create(
            chain_dir,
            base,
            label="install",
            target=disk_target_name(disk_bus),
            bus=disk_bus,
        )
        chain = manager.load()
        disk = chain.active_disk()
        unattend_iso = None
        unattend_floppy = None
        if unattend:
            unattend_iso, unattend_xml = write_windows_unattend(
                chain_dir,
                computer_name=computer_name or name,
                admin_password=admin_password,
                edition=windows_edition,
                virtio_iso=bool(virtio_path),
                firmware=firmware,
            )
            unattend_floppy = write_windows_floppy(
                chain_dir, unattend_xml
            )

        cd_bus = iso_bus or ("ide" if disk_bus == "ide" else "sata")
        cd_prefix = "hd" if cd_bus == "ide" else "sd"
        disks = [
            Disk(
                path=disk,
                bus=disk_bus,
                target=disk_target_name(disk_bus),
                format="qcow2",
                boot=2,
            ),
            Disk(
                path=str(iso_path),
                bus=cd_bus,
                target=f"{cd_prefix}b",
                device="cdrom",
                format="raw",
                discard="",
                boot=1,
            ),
        ]
        if unattend_floppy is not None:
            disks.append(
                Disk(
                    path=str(unattend_floppy),
                    bus="usb",
                    target="sdz",
                    device="disk",
                    format="raw",
                    discard="",
                    boot=99,
                )
            )
        if virtio_path is not None:
            disks.append(
                Disk(
                    path=str(virtio_path),
                    bus=cd_bus,
                    target=f"{cd_prefix}c",
                    device="cdrom",
                    format="raw",
                    discard="",
                )
            )

        libvirt_network = (
            NetworkStore(network_config).find_libvirt_name(network) or network
        )
        with LibvirtBackend(uri) as backend:
            spec = DomainSpec(
                name=name,
                memory_mib=memory_mib,
                vcpus=vcpus,
                disks=disks,
                networks=[
                    Network(
                        network=libvirt_network,
                        mac=backend.unique_mac(),
                        model=nic_model,
                    )
                ],
                arch=arch,
                domain_type=domain_type,
                machine=machine,
                firmware=firmware,
                video=video,
                video_memory_mib=video_memory_mib,
                hyperv=bool(hyperv),
                serial_console=arch == "x86_64",
                cpu_mode=cpu_mode,
                guest_agent=True,
                tpm=tpm,
                secure_boot=(
                    bool(secure_boot)
                    if secure_boot is not None
                    else (firmware == "efi" and arch == "x86_64")
                ),
                os_type=(
                    os_type
                    if os_type in {"windows", "linux", "hypervisor", "generic"}
                    else "generic"
                ),
            )
            secure_requested = spec.secure_boot
            secure_applied = _apply_custom_firmware(spec)
            if (
                secure_requested
                and not secure_applied
                and firmware == "efi"
                and arch == "x86_64"
            ):
                warnings.append(
                    "Secure Boot firmware is not installed; using standard "
                    "v-EMU firmware for this VM. Run ./setup.sh --standard "
                    "to install it."
                )
            backend.define(spec.to_xml())
            _apply_runtime_profile(backend, name)
            defined = True
            started = False
            if start:
                backend.start(name)
                started = True
        _relabel(chain_dir)
        return {
            "name": name,
            "chain": str(chain_dir),
            "disk": disk,
            "install_iso": str(iso_path),
            "unattend_iso": str(unattend_iso) if unattend_iso else None,
            "unattend_floppy": (
                str(unattend_floppy) if unattend_floppy else None
            ),
            "virtio_iso": str(virtio_path) if virtio_path else None,
            "os_type": os_type,
            "os_variant": os_variant or os_type,
            "arch": arch,
            "domain_type": domain_type,
            "windows_unattend": unattend,
            "firmware": firmware,
            "secure_boot": bool(spec.secure_boot),
            "machine": machine,
            "disk_bus": disk_bus,
            "iso_bus": cd_bus,
            "nic_model": nic_model,
            "video": video,
            "video_memory_mib": video_memory_mib,
            "hyperv": bool(hyperv),
            "serial_console": arch == "x86_64",
            "cpu_mode": cpu_mode,
            "tpm": tpm,
            "warnings": warnings,
            "windows_edition": windows_edition if unattend else None,
            "windows_image_index": index if unattend else None,
            "windows_edition_label": edition_label,
            "computer_name": computer_name or name,
            "admin_password": admin_password if unattend else None,
            "defined": True,
            "started": started,
        }
    except Exception:
        # The VM was created by this call; do not leave a half-built directory.
        if defined:
            try:
                with LibvirtBackend(uri) as backend:
                    backend.undefine(name, force=True)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        shutil.rmtree(chain_dir, ignore_errors=True)
        raise


def vm_clipboard_agent_start(
    domain: str,
    *,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Start the guest clipboard helper when the guest needs one."""
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        if info.state != "running":
            raise Conflict(
                f"Domain '{domain}' must be running for guest clipboard."
            )
        ensure_guest_clipboard_agent(backend, domain)
    return {"domain": domain, "started": True}


def vm_clipboard_get(
    domain: str,
    *,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Read the active guest clipboard through QEMU guest agent."""
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        if info.state != "running":
            raise Conflict(
                f"Domain '{domain}' must be running for guest clipboard."
            )
        text = get_guest_clipboard(backend, domain)
    return {"domain": domain, "text": text}


def vm_clipboard_set(
    domain: str,
    text: str,
    *,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Write host text to the active guest clipboard."""
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        if info.state != "running":
            raise Conflict(
                f"Domain '{domain}' must be running for guest clipboard."
            )
        set_guest_clipboard(backend, domain, text)
    return {"domain": domain, "copied": True}


def seal_template(
    domain: str,
    dest: str,
    *,
    name: str | None = None,
    description: str = "",
    os_family: str = "",
    sysprepped: bool = False,
    shutdown: bool = False,
    in_place: bool = False,
    accept_bitlocker: bool = False,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    from .template import slugify

    os_family = normalize_os_family(os_family)
    if not os_family:
        os_family = "windows" if sysprepped else "linux"
    if os_family == "linux" and sysprepped:
        raise ValidationError(
            "A Linux template cannot be marked as sysprepped. "
            "Choose the Windows guest OS for a sysprepped image."
        )
    if os_family == "windows" and not accept_bitlocker:
        raise ValidationError(
            "Sealing a Windows base image requires confirming that BitLocker "
            "or FDE is off/suspended. v-EMU does not copy TPM state; clones "
            "receive a new TPM and encrypted volumes may enter recovery. "
            "Pass --accept-bitlocker after checking the guest."
        )
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain, full=True)
    template_name = name or f"{slugify(domain)}-base"
    removed_shares: list[str] = []
    with LibvirtBackend(uri) as backend:
        config = backend.config_xml(domain)
        cleaned, removed_shares = remove_virtiofs_shares(config)
        if removed_shares:
            backend.redefine_domain(domain, cleaned)
        template = seal_domain(
            backend,
            domain,
            dest,
            template_name,
            description,
            sysprepped=sysprepped,
            os_family=os_family,
            shutdown=shutdown,
            in_place=in_place,
        )
    _relabel(Path(template.base).parent)
    data = template.to_dict()
    data["removed_shares"] = removed_shares
    return data


def chain_journals(root: str) -> list[str]:
    return [str(path) for path in find_commit_journals(root)]


def chain_recover(chain_dir: str) -> dict[str, Any]:
    manager = ChainManager(chain_dir)
    result = manager.recover_commit()
    if result is None:
        return {
            "chain": str(Path(chain_dir).expanduser().resolve()),
            "recovered": False,
            "pending": False,
        }
    _relabel(Path(chain_dir))
    return {**result, "pending": False}


def identity_reset(
    chain_path: str,
    hostname: str,
    *,
    machine_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    manager = ChainManager(chain_path)
    chain = manager.load()
    if isinstance(chain, MultiDiskChain):
        raise Conflict(
            "Identity reset does not support multi-disk chains yet."
        )
    if chain.active is None:
        raise Conflict("Chain has no active layer.")
    result = reset_linux(
        chain.active_disk(), hostname, machine_id, dry_run=dry_run
    )
    return {
        "disk": result.disk,
        "hostname": result.hostname,
        "machine_id": result.machine_id,
        "executed": result.executed,
        "ssh_host_key": result.ssh_host_key,
        "command": result.command,
    }


def _performance_probe(backend, domain: str) -> tuple[object, object]:
    info = backend.info(domain)
    if info.state != "stopped":
        raise Conflict(
            f"Domain '{domain}' must be stopped before performance tuning."
        )
    if info.managed_by == "External":
        raise Conflict(
            f"Domain '{domain}' is external/read-only. Adopt it into v-EMU "
            "before tuning it."
        )
    if getattr(info, "os_type", "") == "windows":
        raise Conflict("Performance tuning is only available for Linux guests.")
    if not info.disks:
        raise Conflict(f"Domain '{domain}' has no disk to tune.")
    xml = backend.config_xml(domain)
    probe = performance.probe(domain, info.disks[0], xml)
    if len(info.disks) > 1:
        probe.warnings.append(
            "Only the first disk was inspected; keep the guest root disk first."
        )
    return info, probe


def performance_probe(
    domain: str, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        _info, probe = _performance_probe(backend, domain)
        return probe.as_dict()


def performance_apply(
    domain: str, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        info, probe = _performance_probe(backend, domain)
        if not probe.supported:
            raise ValidationError(probe.reason or "Guest is not supported.")
        result = performance.apply(domain, info.disks[0], backend.config_xml(domain))
        if result.get("xml_changed"):
            backend.redefine_domain(domain, str(result["xml"]))
        result.pop("xml", None)
        return result


def storage_inspect(path: str, role: str = "labs") -> dict[str, Any]:
    return inspect_location(path, role).to_dict()


def storage_add(
    path: str,
    role: str = "labs",
    *,
    config: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    location = inspect_location(path, role)
    location, actions = apply_fix(location, dry_run=dry_run)
    if not dry_run:
        StorageStore(config).add(location.path, location.role)
    return {
        "location": location.to_dict(),
        "actions": [action.to_dict() for action in actions],
    }


def storage_repair(
    path: str,
    role: str = "labs",
    *,
    dry_run: bool = False,
    force_selinux: bool = False,
) -> dict[str, Any]:
    location = inspect_location(path, role)
    if force_selinux:
        if location.selinux_mode not in {"Enforcing", "Permissive"}:
            raise ValidationError(
                "SELinux is not enabled. Enable it in the host configuration, "
                "reboot, then run the repair again."
            )
        missing = [
            tool
            for tool in ("semanage", "restorecon")
            if find_which(tool) is None
        ]
        if missing:
            raise ValidationError(
                "SELinux repair needs: "
                + ", ".join(missing)
                + ". Install policycoreutils and "
                "policycoreutils-python-utils (or the distro equivalents)."
            )
    location, actions = apply_fix(
        location, dry_run=dry_run, force_selinux=force_selinux
    )
    return {
        "location": location.to_dict(),
        "actions": [action.to_dict() for action in actions],
        "force_selinux": force_selinux,
    }


def storage_registry(config: str | None = None) -> list[dict[str, str]]:
    """Return registered storage paths/roles without filesystem inspection."""
    return StorageStore(config).load()


def storage_list(config: str | None = None) -> list[dict[str, Any]]:
    return [location.to_dict() for location in StorageStore(config).inspect_all()]


def storage_remove(path: str, config: str | None = None) -> dict[str, str]:
    return StorageStore(config).remove(path)


def _registered_storage(
    path: str, config: str | None
) -> tuple[Any, Path]:
    resolved = Path(path).expanduser().resolve()
    for item in StorageStore(config).load():
        root = Path(item["path"]).expanduser().resolve()
        if resolved == root or root in resolved.parents:
            return inspect_location(root, item["role"]), resolved
    raise NotFound(f"Path is not inside a registered storage location: {path}")


def _domain_file_sources(
    backend: LibvirtBackend,
) -> dict[Path, list[str]]:
    sources: dict[Path, list[str]] = {}
    for domain in backend.list_domains():
        try:
            xml = backend.config_xml(domain.name)
        except VemuError:
            continue
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            continue
        for element in root.iter():
            if element.tag not in {"source", "backingStore"}:
                continue
            name = element.get("file")
            if not name:
                continue
            path = Path(name).expanduser().resolve()
            sources.setdefault(path, []).append(domain.name)
    return sources


def _storage_index(
    locations: list[dict[str, str]],
    backend: LibvirtBackend,
) -> tuple[
    list[Path],
    dict[Path, list[str]],
    dict[Path, list[Path]],
    list[dict[str, Any]],
]:
    files: list[Path] = []
    for item in locations:
        files.extend(iter_location_files(item["path"]))
    sources = _domain_file_sources(backend)
    backing: dict[Path, list[Path]] = {}
    for path in files:
        if path.suffix.lower() != ".qcow2":
            continue
        backing_file = qcow2_backing_file(path)
        if backing_file is not None:
            backing.setdefault(backing_file, []).append(path)
    chains: list[dict[str, Any]] = []
    for item in locations:
        chains.extend(scan_chains(item["path"]))
    return files, sources, backing, chains


def storage_files(
    path: str,
    *,
    config: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """List ISO/images and chain directories inside a registered location."""
    location, _resolved = _registered_storage(path, config)
    locations = StorageStore(config).load()
    with LibvirtBackend(uri) as backend:
        files, sources, backing, chains = _storage_index(locations, backend)
    entries = build_storage_entries(
        location.path,
        location.role,
        files,
        sources,
        backing,
        chains,
    )
    return {"location": location.to_dict(), **entries}


def storage_delete_file(
    path: str,
    *,
    config: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Delete an ISO/image, refusing files still referenced by a VM or chain.

    Chain directories are deleted as a unit and only when the whole chain is
    orphaned.
    """
    location, resolved = _registered_storage(path, config)
    if location.role == "templates":
        raise Conflict(
            "Template images are deleted from the Templates page, "
            "not Storage."
        )
    data = storage_files(location.path, config=config, uri=uri)
    entries = list(data["files"]) + list(data["chains"])
    match = next(
        (
            item
            for item in entries
            if Path(item["path"]).resolve() == resolved
        ),
        None,
    )
    if match is None:
        raise NotFound(f"No managed media found at {resolved}.")
    if not match["deletable"]:
        raise Conflict(f"Cannot delete {resolved.name}: {match['status']}")
    if resolved.is_symlink():
        raise Conflict("Refusing to delete a symbolic link.")
    if match["kind"] == "chain":
        if not (resolved / CHAIN_FILE).is_file():
            raise Conflict("Chain directory has no chain.json.")
        shutil.rmtree(resolved)
    else:
        resolved.unlink()
    return {
        "path": str(resolved),
        "kind": match["kind"],
        "deleted": True,
    }


def storage_import_file(
    source: str,
    location_path: str,
    *,
    config: str | None = None,
) -> dict[str, Any]:
    """Copy an ISO/image into a registered storage location."""
    from .storage import FILE_SUFFIXES

    location, root = _registered_storage(location_path, config)
    if location.role == "templates":
        raise Conflict("Import templates from the Templates page.")
    origin = Path(source).expanduser().resolve()
    if origin.is_symlink() or not origin.is_file():
        raise ValidationError(f"Source is not a regular file: {source}")
    if origin.suffix.lower() not in FILE_SUFFIXES:
        raise ValidationError(
            "Only ISO, qcow2, raw or img files can be imported."
        )
    destination = root / origin.name
    if destination.exists():
        raise Conflict(f"File already exists: {destination}")
    size = origin.stat().st_size
    free = shutil.disk_usage(root).free
    if free < size + 128 * 1024 * 1024:
        raise Conflict(
            f"Not enough free space in {root} ({free} bytes free, "
            f"{size} needed plus 128 MiB headroom)."
        )
    shutil.copy2(origin, destination)
    _relabel(destination)
    return {
        "path": str(destination),
        "size": size,
        "location": location.path,
    }



def templates_list(root: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for template in list_templates(root):
        data = template.to_dict()
        base = template.base_path()
        data["base_size"] = base.stat().st_size if base.is_file() else 0
        items.append(data)
    return items


def template_import(
    source: str,
    dest_root: str,
    name: str,
    *,
    description: str = "",
    arch: str = "x86_64",
    machine: str = "q35",
    firmware: str = "bios",
    disk_bus: str = "virtio",
    video: str = "virtio",
    video_memory_mib: int | None = None,
    nic_model: str = "virtio",
    secure_boot: bool = False,
    tpm: bool = False,
    os_family: str = "",
    sysprepped: bool = False,
) -> dict[str, Any]:
    """Import a Packer/builder qcow2 artifact as an immutable template."""
    validate_name(name)
    os_family = normalize_os_family(os_family)
    if not os_family:
        os_family = "windows" if sysprepped else "linux"
    if os_family == "linux" and sysprepped:
        raise ValidationError(
            "A Linux template cannot be marked sysprepped."
        )
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise NotFound(f"Golden image was not found: {source_path}")
    dest = Path(dest_root).expanduser().resolve() / name
    if dest.exists():
        raise Conflict(f"Template directory already exists: {dest}")
    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        raise NotFound("qemu-img was not found.")
    dest.mkdir(parents=True)
    base = dest / "base.qcow2"
    command = qemu_convert_command(
        qemu_img, str(source_path), str(base)
    )
    result = subprocess.run(
        command, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise ExternalCommandError(
            " ".join(command), result.returncode, result.stderr or result.stdout
        )
    os.chmod(base, 0o444)
    template = Template(
        name=name,
        base=str(base),
        description=description,
        source_domain=f"import:{source_path.name}",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        arch=arch,
        machine=machine,
        firmware=firmware,
        memory_mib=2048,
        vcpus=2,
        disk_bus=disk_bus,
        video=video,
        video_memory_mib=video_memory_mib,
        nic_model=nic_model,
        secure_boot=secure_boot and firmware == "efi" and arch == "x86_64",
        tpm=tpm,
        os_family=os_family,
        sysprepped=sysprepped,
        never_booted=True,
        in_place=False,
        disks=[
            {
                "target": disk_target_name(disk_bus),
                "base": str(base),
                "bus": disk_bus,
            }
        ],
    )
    template.save(dest)
    _relabel(dest)
    data = template.to_dict()
    data["base_size"] = base.stat().st_size
    return data


def template_update(
    template_path: str,
    *,
    os_family: str | None = None,
    sysprepped: bool | None = None,
) -> dict[str, Any]:
    """Update template metadata without touching its base images."""
    if os_family is None and sysprepped is None:
        raise ValidationError("No template metadata was provided.")
    path = Path(template_path).expanduser().resolve()
    if path.is_dir():
        directory = path
        metadata = path / TEMPLATE_FILE
    elif path.is_file() and path.name == TEMPLATE_FILE:
        directory = path.parent
        metadata = path
    else:
        raise ValidationError(
            "Template update needs a template directory or template.json."
        )
    template = resolve_template(metadata)
    if os_family is not None:
        os_family = normalize_os_family(os_family)
        if not os_family:
            raise ValidationError("Choose a Linux or Windows template.")
    else:
        os_family = template.os_family
    if os_family == "linux":
        # Linux never uses Windows sysprep; keep the metadata consistent.
        sysprepped = False
    elif sysprepped is None:
        sysprepped = template.sysprepped
    if os_family == "linux" and sysprepped:
        raise ValidationError(
            "A Linux template cannot be marked sysprepped."
        )
    template.os_family = os_family
    template.sysprepped = bool(sysprepped)
    template.save(directory)
    return {**template.to_dict(), "path": str(directory), "updated": True}


def template_delete(
    template_path: str,
    *,
    config: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Delete a template directory only when no VM or clone references it."""
    root = Path(template_path).expanduser().resolve()
    if not root.is_dir() or not (root / "template.json").is_file():
        raise ValidationError(
            f"Not a template directory: {root}"
        )
    PathPolicy.from_config(config).resolve(
        root, roles=("templates",), must_exist=True
    )
    template = resolve_template(root)
    bases = [Path(item["base"]).resolve() for item in template.disk_specs()]
    for base in bases:
        if root not in base.parents and base != root / base.name:
            raise Conflict(
                "Template base is outside its template directory; "
                "refusing to delete only part of it."
            )
    locations = StorageStore(config).load()
    with LibvirtBackend(uri) as backend:
        _files, sources, backing, _chains = _storage_index(
            locations, backend
        )
    descendants: set[Path] = set()
    queue = list(bases)
    while queue:
        parent = queue.pop()
        for child in backing.get(parent, []):
            if child not in descendants:
                descendants.add(child)
                queue.append(child)
    references: list[str] = []
    for path in [*bases, *descendants]:
        references.extend(sources.get(path, []))
    if descendants:
        references.extend(
            f"chain:{path}" for path in sorted(descendants)
        )
    if references:
        raise Conflict(
            "Template is in use by: " + ", ".join(sorted(set(references)))
        )
    shutil.rmtree(root)
    return {
        "template": template.name,
        "path": str(root),
        "deleted": True,
    }


def template_export(
    template_path: str, destination: str, *, format: str = "qcow2"
) -> dict[str, Any]:
    if format not in {"qcow2", "raw"}:
        raise ValidationError("Export format must be qcow2 or raw.")
    template = resolve_template(template_path)
    if len(template.disk_specs()) > 1:
        raise ValidationError(
            "Exporting a multi-disk template to one image is not supported; "
            "export each disk base separately."
        )
    source = template.base_path()
    if not source.is_file():
        raise NotFound(f"Template base was not found: {source}")
    dest = Path(destination).expanduser().resolve()
    if dest.exists():
        raise Conflict(f"Export destination already exists: {dest}")
    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        raise NotFound("qemu-img was not found.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = qemu_convert_command(
        qemu_img,
        str(source),
        str(dest),
        output_format=format,
        direct_target=format == "qcow2",
    )
    result = subprocess.run(
        command, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ExternalCommandError(
            " ".join(command), result.returncode, result.stderr or result.stdout
        )
    return {
        "template": template.name,
        "path": str(dest),
        "format": format,
        "size": dest.stat().st_size,
    }


# ---------------------------------------------------------------------- networks


def _network_dict(
    profile: NetworkProfile,
    runtime: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    data = profile.to_dict()
    actual = runtime or {}
    data["runtime"] = {
        "defined": bool(actual),
        "active": bool(actual.get("active", False)),
        "autostart": bool(actual.get("autostart", False)),
        "bridge": actual.get("bridge") or data["bridge"],
        "subnet": actual.get("subnet"),
    }
    if error:
        data["runtime"]["error"] = error
    return data


def _network_runtime(
    uri: str,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    try:
        with LibvirtBackend(uri) as backend:
            return {
                str(network["name"]): network
                for network in backend.list_networks()
            }, None
    except VemuError as exc:
        return {}, str(exc)


def _apply_nft_safe(profiles: list[NetworkProfile]) -> dict[str, Any]:
    try:
        return apply_nft(profiles)
    except VemuError as exc:
        return {"applied": False, "reason": str(exc)}


def _apply_rules_safe(profiles: list[NetworkProfile]) -> dict[str, Any]:
    nft = _apply_nft_safe(profiles)
    try:
        libvirt = apply_libvirt_forward_rules(profiles)
    except VemuError as exc:
        libvirt = {"applied": False, "reason": str(exc)}
    return {**nft, "libvirt": libvirt}


def networks_list(
    config: str | None = None, uri: str = "qemu:///system"
) -> list[dict[str, Any]]:
    profiles = NetworkStore(config).load()
    runtime, error = _network_runtime(uri)
    return [
        _network_dict(profile, runtime.get(profile.libvirt_name), error)
        for profile in profiles
    ]


def network_add(
    name: str,
    mode: str = "nat",
    subnet: str = "auto",
    dhcp: bool = True,
    dns_domain: str = "vemu.local",
    description: str = "",
    bridge: str = "",
    *,
    config: str | None = None,
) -> dict[str, Any]:
    profile = NetworkProfile(
        name=name,
        mode=mode,
        subnet=subnet,
        dhcp=dhcp,
        dns_domain=dns_domain,
        description=description,
        bridge_interface=bridge,
    )
    return NetworkStore(config).add(profile)


def network_bridge_inventory() -> dict[str, Any]:
    """Return host bridges and unused Ethernet uplinks for the wizard."""

    return hostbridge.collect_inventory()


def network_bridge_create(
    name: str,
    bridge: str,
    uplink: str,
    addressing: str = "dhcp",
    address: str = "",
    gateway: str = "",
    dns: str = "",
    description: str = "",
    *,
    config: str | None = None,
    backup_dir: str | None = None,
    run=None,
    sleep=None,
) -> dict[str, Any]:
    """Create a host bridge, then register the matching v-EMU profile.

    Detection and execution are separated so a bad request fails before any
    host change. The NetworkManager profile backup is written by
    :mod:`vemu.hostbridge` before the first mutation.
    """

    store = NetworkStore(config)
    if any(profile.name == name for profile in store.load()):
        raise Conflict(f"Network '{name}' already exists.")
    profile = NetworkProfile(
        name=name,
        mode="bridge",
        subnet="auto",
        dhcp=False,
        dns_domain="",
        description=description,
        bridge_interface=bridge,
    )
    profile.validate()
    kwargs: dict[str, Any] = {}
    if run is not None:
        kwargs["run"] = run
    plan = hostbridge.plan_bridge(
        bridge,
        uplink,
        addressing,
        address,
        gateway,
        dns,
        **kwargs,
    )
    create_kwargs: dict[str, Any] = {}
    if run is not None:
        create_kwargs["run"] = run
    if backup_dir is not None:
        create_kwargs["backup_dir"] = backup_dir
    if sleep is not None:
        create_kwargs["sleep"] = sleep
    result = hostbridge.create_bridge(plan, **create_kwargs)
    added = store.add(profile)
    return {**result, "profile": added}


def network_update(
    name: str,
    mode: str,
    subnet: str,
    dhcp: bool,
    dns_domain: str,
    description: str = "",
    *,
    config: str | None = None,
) -> dict[str, Any]:
    store = NetworkStore(config)
    profile = store.get(name)
    profile.mode = mode
    profile.subnet = subnet
    profile.dhcp = dhcp
    profile.dns_domain = dns_domain
    profile.description = description
    return store.update(profile)


def network_remove(
    name: str,
    *,
    config: str | None = None,
    uri: str = "qemu:///system",
    purge: bool = False,
) -> dict[str, Any]:
    store = NetworkStore(config)
    profile = store.get(name)
    if purge:
        try:
            with LibvirtBackend(uri) as backend:
                running = _network_running_vms(backend, profile)
                if running:
                    raise Conflict(
                        f"Network '{name}' is used by running VM(s): "
                        + ", ".join(running)
                        + ". Shut them down before purging it."
                    )
                backend.stop_network(profile.libvirt_name, undefine=True)
        except NotFound:
            pass
    entry = store.remove(name)
    nft = _apply_rules_safe(store.load())
    return {"removed": entry, "purged": purge, "nft": nft}


def network_plan(
    config: str | None = None, uri: str = "qemu:///system"
) -> dict[str, Any]:
    profiles = NetworkStore(config).load()
    runtime, error = _network_runtime(uri)
    actions = []
    for profile in profiles:
        actual = runtime.get(profile.libvirt_name)
        if actual is None:
            action = "create"
        elif not actual.get("active"):
            action = "start"
        elif (
            actual.get("subnet")
            and actual.get("subnet") != profile.to_dict()["subnet"]
        ):
            action = "update"
        else:
            action = "ok"
        actions.append(
            {
                "name": profile.name,
                "libvirt_name": profile.libvirt_name,
                "action": action,
                "active": bool(actual and actual.get("active")),
            }
        )
    return {
        "actions": actions,
        "nft": nft_ruleset(profiles),
        "error": error,
    }


def network_apply(
    config: str | None = None,
    uri: str = "qemu:///system",
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    store = NetworkStore(config)
    profiles = store.load()
    if dry_run:
        return {
            "dry_run": True,
            "networks": [],
            "plan": network_plan(config, uri)["actions"],
            "nft": {
                **apply_nft(profiles, dry_run=True),
                "libvirt": apply_libvirt_forward_rules(
                    profiles, dry_run=True
                ),
            },
        }
    applied: list[dict[str, Any]] = []
    with LibvirtBackend(uri) as backend:
        existing = {
            str(network["name"]) for network in backend.list_networks()
        }
        for profile in profiles:
            was_defined = profile.libvirt_name in existing
            backend.define_network(profile.libvirt_xml())
            info = backend.start_network(profile.libvirt_name)
            applied.append(
                {
                    "name": profile.name,
                    "action": "updated" if was_defined else "created",
                    **info,
                }
            )
    return {
        "networks": applied,
        "nft": _apply_rules_safe(profiles),
    }


def network_reservation_add(
    name: str,
    mac: str,
    ip: str,
    hostname: str = "",
    *,
    config: str | None = None,
) -> dict[str, Any]:
    store = NetworkStore(config)
    profile = store.get(name)
    profile.host_reservations.append(
        HostReservation(mac=mac, ip=ip, hostname=hostname)
    )
    profile.validate()
    store.update(profile)
    return {"profile": profile.to_dict(), "restart_required": True}


def network_reservation_remove(
    name: str,
    mac: str,
    *,
    config: str | None = None,
) -> dict[str, Any]:
    store = NetworkStore(config)
    profile = store.get(name)
    mac = mac.lower()
    remaining = [
        item for item in profile.host_reservations if item.mac.lower() != mac
    ]
    if len(remaining) == len(profile.host_reservations):
        raise NotFound(
            f"DHCP reservation for {mac} was not found on '{name}'."
        )
    profile.host_reservations = remaining
    store.update(profile)
    return {"profile": profile.to_dict(), "restart_required": True}


def _network_running_vms(
    backend: LibvirtBackend, profile: NetworkProfile
) -> list[str]:
    names: list[str] = []
    for item in backend.list_domains():
        if item.state != "running":
            continue
        try:
            root = ET.fromstring(backend.live_xml(item.name))
        except (VemuError, ET.ParseError):
            continue
        for interface in root.findall("./devices/interface"):
            source = interface.find("source")
            if source is None:
                continue
            if (
                source.get("network") == profile.libvirt_name
                or source.get("bridge") == profile.bridge_name
            ):
                names.append(item.name)
                break
    return sorted(names)


def network_restart(
    name: str,
    *,
    config: str | None = None,
    uri: str = "qemu:///system",
    force: bool = False,
) -> dict[str, Any]:
    store = NetworkStore(config)
    profile = store.get(name)
    with LibvirtBackend(uri) as backend:
        running = _network_running_vms(backend, profile)
        if running and not force:
            raise Conflict(
                f"Network '{name}' is used by running VM(s): "
                + ", ".join(running)
                + ". Shut them down before restarting the network, or use "
                "force (attached VMs will lose connectivity)."
            )
        backend.define_network(profile.libvirt_xml())
        backend.stop_network(profile.libvirt_name)
        info = backend.start_network(profile.libvirt_name)
    return {
        "name": name,
        **info,
        "force": force,
        "nft": _apply_rules_safe(store.load()),
    }


def network_port_forward_add(
    name: str,
    protocol: str,
    host_port: int,
    guest_ip: str,
    guest_port: int,
    description: str = "",
    *,
    config: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    store = NetworkStore(config)
    profile = store.get(name)
    profile.port_forwards.append(
        PortForward(
            protocol=protocol,
            host_port=host_port,
            guest_ip=guest_ip,
            guest_port=guest_port,
            description=description,
        )
    )
    profile.validate()
    store.update(profile)
    leases = _dhcp_leases(profile, uri)
    return {
        "profile": profile.to_dict(),
        "nft": _apply_rules_safe(store.load()),
        "warning": _port_forward_warning(profile, guest_ip, leases),
    }


def _dhcp_leases(
    profile: NetworkProfile, uri: str
) -> list[dict[str, Any]]:
    try:
        with LibvirtBackend(uri) as backend:
            return backend.dhcp_leases(profile.libvirt_name)
    except VemuError:
        return []


def _port_forward_warning(
    profile: NetworkProfile,
    guest_ip: str,
    leases: list[dict[str, Any]],
) -> str | None:
    reserved = {
        item.ip
        for item in profile.host_reservations
    }
    active = {str(item.get("ip", "")) for item in leases}
    if guest_ip in reserved or guest_ip in active:
        return None
    return (
        f"No DHCP reservation or active lease was found for {guest_ip}. "
        "The forward still works if the guest uses a static IP, but it will "
        "not follow a changing DHCP address."
    )


def network_leases(
    name: str,
    *,
    config: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    profile = NetworkStore(config).get(name)
    leases = _dhcp_leases(profile, uri)
    return {
        "name": name,
        "reservations": [
            item.to_dict() for item in profile.host_reservations
        ],
        "leases": leases,
    }


def _guest_ip_map(backend: LibvirtBackend, domain: str) -> dict[str, list[str]]:
    try:
        payload = backend.qemu_agent_command(
            domain,
            {"execute": "guest-network-get-interfaces"},
            timeout=4,
        )
    except VemuError:
        return {}
    result: dict[str, list[str]] = {}
    interfaces = payload if isinstance(payload, list) else []
    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        mac = str(interface.get("hardware-address", "")).lower()
        for address in interface.get("ip-addresses") or []:
            if not isinstance(address, dict):
                continue
            if address.get("ip-address-type") != "ipv4":
                continue
            value = str(address.get("ip-address", ""))
            if value and not value.startswith("127."):
                result.setdefault(mac, []).append(value)
    return result


def network_topology(
    *,
    config: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    profiles = networks_list(config, uri)
    by_libvirt = {
        str(profile.get("libvirt_name", "")): profile
        for profile in profiles
    }
    leases: dict[str, str] = {}
    for profile in NetworkStore(config).load():
        for lease in _dhcp_leases(profile, uri):
            mac = str(lease.get("mac", "")).lower()
            ip = str(lease.get("ip", ""))
            if mac and ip:
                leases[mac] = ip
    reservations: dict[str, str] = {}
    for profile in profiles:
        for reservation in profile.get("host_reservations", []):
            mac = str(reservation.get("mac", "")).lower()
            ip = str(reservation.get("ip", ""))
            if mac and ip:
                reservations[mac] = ip

    vms: list[dict[str, Any]] = []
    warnings: list[str] = []
    external_networks: set[str] = set()
    with LibvirtBackend(uri) as backend:
        domains = backend.list_domains()
        libvirt_networks = {
            str(network["name"]): network
            for network in backend.list_networks()
        }
        for domain in domains:
            interfaces = backend.interfaces(domain.name)
            guest_ips: dict[str, list[str]] = {}
            if domain.state == "running":
                guest_ips = _guest_ip_map(backend, domain.name)
                if not guest_ips and interfaces:
                    warnings.append(
                        f"{domain.name}: qemu-guest-agent did not return IP data"
                    )
            rendered = []
            for interface in interfaces:
                mac = str(interface.get("mac", "")).lower()
                network_name = str(
                    interface.get("network") or interface.get("bridge") or ""
                )
                profile = by_libvirt.get(network_name)
                display_network = (
                    str(profile.get("name")) if profile else network_name
                )
                if network_name and profile is None:
                    external_networks.add(network_name)
                ips = guest_ips.get(mac, [])
                source = "guest-agent" if ips else ""
                if not ips:
                    lease = leases.get(mac)
                    if lease:
                        ips = [lease]
                        source = "dhcp-lease"
                if not ips:
                    reserved = reservations.get(mac)
                    if reserved:
                        ips = [reserved]
                        source = "reservation"
                rendered.append(
                    {
                        "network": display_network,
                        "libvirt_network": network_name,
                        "mac": mac,
                        "model": interface.get("model"),
                        "ips": ips,
                        "ip_source": source or "unknown",
                    }
                )
            vms.append(
                {
                    "name": domain.name,
                    "state": domain.state,
                    "interfaces": rendered,
                }
            )
    for name in sorted(external_networks):
        actual = libvirt_networks.get(name) or {}
        profiles.append(
            {
                "name": name,
                "libvirt_name": name,
                "mode": str(actual.get("forward") or "external"),
                "subnet": actual.get("subnet") or "auto",
                "dhcp": bool(actual.get("dhcp", False)),
                "dns_domain": actual.get("dns_domain") or "",
                "description": "External libvirt network",
                "bridge": actual.get("bridge") or "",
                "runtime": {
                    "defined": bool(actual),
                    "active": bool(actual.get("active", False)),
                    "autostart": bool(actual.get("autostart", False)),
                    "bridge": actual.get("bridge") or "",
                    "subnet": actual.get("subnet"),
                },
                "port_forwards": [],
                "host_reservations": [],
                "external": True,
            }
        )
    return {
        "host": {"name": socket.gethostname()},
        "networks": profiles,
        "vms": sorted(vms, key=lambda item: item["name"].lower()),
        "warnings": warnings,
    }


def network_port_forward_remove(
    name: str,
    protocol: str,
    host_port: int,
    *,
    config: str | None = None,
) -> dict[str, Any]:
    store = NetworkStore(config)
    profile = store.get(name)
    remaining = [
        item
        for item in profile.port_forwards
        if not (item.protocol == protocol and item.host_port == host_port)
    ]
    if len(remaining) == len(profile.port_forwards):
        raise NotFound(
            f"Port forward {protocol}/{host_port} was not found on '{name}'."
        )
    profile.port_forwards = remaining
    store.update(profile)
    return {
        "profile": profile.to_dict(),
        "nft": _apply_rules_safe(store.load()),
    }


def domain_interfaces(
    domain: str, uri: str = "qemu:///system"
) -> list[dict[str, Any]]:
    with LibvirtBackend(uri) as backend:
        return backend.interfaces(domain)


def network_attach(
    domain: str,
    network: str,
    *,
    model: str = "virtio",
    config: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    libvirt_name = NetworkStore(config).find_libvirt_name(network) or network
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        mac = backend.unique_mac()
        backend.attach_interface(domain, libvirt_name, mac, model=model)
    return {
        "domain": domain,
        "network": network,
        "libvirt_network": libvirt_name,
        "mac": mac,
    }


def network_detach(
    domain: str, mac: str, uri: str = "qemu:///system"
) -> dict[str, str]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        backend.detach_interface(domain, mac)
    return {"domain": domain, "mac": mac}


def vm_nic_link(
    domain: str, mac: str, up: bool, uri: str = "qemu:///system"
) -> dict[str, Any]:
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        backend.set_interface_link(domain, mac, up=up)
    return {
        "domain": domain,
        "mac": mac,
        "link": "up" if up else "down",
    }


def vm_nic_network(
    domain: str,
    mac: str,
    network: str,
    *,
    config: str | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    libvirt_name = NetworkStore(config).find_libvirt_name(network) or network
    with LibvirtBackend(uri) as backend:
        _require_writable(backend, domain)
        backend.set_interface_network(domain, mac, libvirt_name)
    return {
        "domain": domain,
        "mac": mac,
        "network": network,
        "libvirt_network": libvirt_name,
    }

