"""Storage locations and SELinux/permission repair.

A storage location is a directory where v-EMU keeps templates, lab disks or
ISOs. Adding one is more than ``mkdir``: the ``qemu`` user must be able to
traverse it, SELinux must label images as ``virt_image_t`` (or
``virt_content_t`` for ISOs), and btrfs directories should be nodatacow.

All repair actions are planned first and can be dry-run. Applying them requires
root; the production daemon runs as root, the CLI can be used with sudo.
"""

from __future__ import annotations

import json
import os
import pwd
import grp
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import Conflict, NotFound, ValidationError
from .qemuuser import qemu_identity
from .tools import which as find_which

ROLES = {"labs", "templates", "isos"}
DEFAULT_CONFIG = "/etc/vemu/storage.json"
MIN_FREE_BYTES = 20 * 1024 * 1024 * 1024
FILE_SUFFIXES = {".iso", ".qcow2", ".raw", ".img"}


def iter_location_files(path: str | Path) -> list[Path]:
    """Return regular media/image files below a location (no symlinks)."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        return []
    files: list[Path] = []
    for candidate in root.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.suffix.lower() not in FILE_SUFFIXES:
            continue
        files.append(candidate.resolve())
    return sorted(files)


def build_storage_entries(
    root: str | Path,
    role: str,
    files: list[Path],
    sources: dict[Path, list[str]],
    backing: dict[Path, list[Path]],
    chains: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Classify files and chain directories for the storage manager UI."""
    base = Path(root).expanduser().resolve()

    def inside(path: Path, directory: Path) -> bool:
        return path == directory or directory in path.parents

    file_entries: list[dict[str, Any]] = []
    for path in files:
        if not inside(path, base):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        reasons: list[str] = []
        in_use = sources.get(path)
        if in_use:
            reasons.append(
                "in use by " + ", ".join(sorted(set(in_use)))
            )
        parents = [item for item in backing.get(path, []) if item != path]
        if parents:
            reasons.append(
                "backing file of " + ", ".join(sorted(p.name for p in parents))
            )
        chain = next(
            (
                item
                for item in chains
                if inside(path, item["dir"])
            ),
            None,
        )
        if chain is not None:
            reasons.append(f"inside chain {chain['dir'].name}")
        if role == "templates":
            reasons.append("template image")
        kind = "iso" if path.suffix.lower() == ".iso" else "image"
        file_entries.append(
            {
                "path": str(path),
                "name": str(path.relative_to(base)),
                "kind": kind,
                "size": size,
                "deletable": not reasons,
                "status": "; ".join(reasons) or "available",
                "in_use": sorted(set(in_use or [])),
            }
        )

    chain_entries: list[dict[str, Any]] = []
    for chain in chains:
        directory: Path = chain["dir"]
        if not inside(directory, base):
            continue
        managed = [item for item in chain["files"] if inside(item, directory)]
        in_use: set[str] = set()
        for item in managed:
            in_use.update(sources.get(item, []))
        external = [
            parent
            for item in managed
            for parent in backing.get(item, [])
            if not inside(parent, directory)
        ]
        reasons: list[str] = []
        if in_use:
            reasons.append("in use by " + ", ".join(sorted(in_use)))
        if external:
            reasons.append(
                "backing files used by "
                + ", ".join(sorted({p.name for p in external}))
            )
        if role == "templates":
            reasons.append("template chain")
        deletable = not reasons
        size = 0
        try:
            size = sum(
                item.stat().st_size
                for item in directory.rglob("*")
                if item.is_file() and not item.is_symlink()
            )
        except OSError:
            pass
        chain_entries.append(
            {
                "path": str(directory),
                "name": directory.name + "/",
                "kind": "chain",
                "size": size,
                "deletable": deletable,
                "status": "; ".join(reasons)
                if reasons
                else "orphan chain — safe to delete",
                "in_use": sorted(in_use),
            }
        )
    return {"files": file_entries, "chains": chain_entries}


def qcow2_backing_file(path: str | Path) -> Path | None:
    """Read the backing file of a qcow2 image without opening it for write."""
    qemu_img = find_which("qemu-img")
    if qemu_img is None:
        return None
    image = Path(path).expanduser().resolve()
    try:
        result = subprocess.run(
            [
                qemu_img,
                "info",
                "--output=json",
                "--force-share",
                str(image),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        data = json.loads(result.stdout)
    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ):
        return None
    name = data.get("full-backing-filename") or data.get("backing-filename")
    if not name:
        return None
    backing = Path(str(name))
    if not backing.is_absolute():
        backing = image.parent / backing
    return backing.expanduser().resolve()


def scan_chains(path: str | Path) -> list[dict[str, Any]]:
    """Return chain.json directories below a location and their files."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        return []
    chains: list[dict[str, Any]] = []
    for chain_file in sorted(root.rglob("chain.json")):
        try:
            data = json.loads(chain_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        directory = chain_file.parent.resolve()
        paths: set[Path] = set()
        base = data.get("base")
        if base:
            paths.add(Path(str(base)).expanduser().resolve())
        for layer in data.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            layer_path = layer.get("path")
            if layer_path:
                paths.add(Path(str(layer_path)).expanduser().resolve())
        for candidate in directory.glob("disk-*.qcow2"):
            if not candidate.is_symlink():
                paths.add(candidate.resolve())
        chains.append(
            {
                "dir": directory,
                "chain_file": chain_file.resolve(),
                "files": sorted(paths),
            }
        )
    return chains



def validate_role(role: str) -> str:
    if role not in ROLES:
        raise ValidationError(
            f"Invalid role '{role}'. Use one of: {', '.join(sorted(ROLES))}."
        )
    return role


def expected_label(role: str) -> str:
    return "virt_content_t" if role == "isos" else "virt_image_t"


@dataclass
class Action:
    description: str
    command: list[str]
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "command": self.command,
            "applied": self.applied,
        }


@dataclass
class Location:
    path: str
    role: str
    exists: bool
    filesystem: str
    total_bytes: int
    free_bytes: int
    selinux_mode: str
    selinux_label: str | None
    expected_label: str
    owner: str
    mode: str
    nodatacow: bool | None
    qemu_traverse: bool | None
    qemu_read: bool | None
    qemu_write: bool | None
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    healthy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "exists": self.exists,
            "filesystem": self.filesystem,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "selinux": {
                "mode": self.selinux_mode,
                "label": self.selinux_label,
                "expected": self.expected_label,
            },
            "owner": self.owner,
            "mode": self.mode,
            "nodatacow": self.nodatacow,
            "qemu_access": {
                "traverse": self.qemu_traverse,
                "read": self.qemu_read,
                "write": self.qemu_write,
            },
            "issues": self.issues,
            "warnings": self.warnings,
            "healthy": self.healthy,
        }


def _run(command: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            command,
            127,
            "",
            f"{command[0]}: command not found ({exc})",
        )


def _filesystem(path: Path) -> str:
    if shutil.which("findmnt"):
        result = _run(["findmnt", "-no", "FSTYPE", "-T", str(path)])
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
    if shutil.which("stat"):
        result = _run(["stat", "-f", "-c", "%T", str(path)])
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return "unknown"


def _selinux_tool(name: str) -> str:
    return find_which(name) or name


def _selinux_mode() -> str:
    tool = find_which("getenforce")
    if tool:
        result = _run([tool])
        value = result.stdout.strip()
        if value:
            return value
    enforce = Path("/sys/fs/selinux/enforce")
    if enforce.is_file():
        try:
            return (
                "Enforcing"
                if enforce.read_text(encoding="utf-8").strip() == "1"
                else "Permissive"
            )
        except OSError:
            pass
    return "Disabled"


def _selinux_label(path: Path) -> str | None:
    if not shutil.which("ls"):
        return None
    result = _run(["ls", "-Zd", str(path)])
    if result.returncode != 0 or not result.stdout.strip():
        return None
    context = result.stdout.split()[0]
    parts = context.split(":")
    return parts[2] if len(parts) >= 3 else None


def _owner_mode(path: Path) -> tuple[str, str]:
    stat = path.stat()
    try:
        user = pwd.getpwuid(stat.st_uid).pw_name
    except KeyError:
        user = str(stat.st_uid)
    try:
        group = grp.getgrgid(stat.st_gid).gr_name
    except KeyError:
        group = str(stat.st_gid)
    return f"{user}:{group}", oct(stat.st_mode & 0o7777)[2:]


def _has_nodatacow(path: Path, filesystem: str) -> bool | None:
    if filesystem != "btrfs":
        return False
    if not shutil.which("lsattr"):
        return None
    result = _run(["lsattr", "-d", str(path)])
    if result.returncode != 0:
        return None
    fields = result.stdout.split()
    return bool(fields) and "C" in fields[0]


def _qemu_identity():
    try:
        return qemu_identity()
    except ValidationError:
        return None


def _qemu_user() -> str | None:
    identity = _qemu_identity()
    return identity.user if identity is not None else None


def _qemu_access(path: Path, flag: str) -> bool | None:
    if os.geteuid() != 0:
        return None
    user = _qemu_user()
    if not user or not shutil.which("runuser"):
        return None
    result = _run(["runuser", "-u", user, "--", "test", flag, str(path)])
    return result.returncode == 0


def _has_qemu_acl(path: Path, user: str) -> bool | None:
    if not shutil.which("getfacl"):
        return None
    result = _run(["getfacl", "-p", str(path)])
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith(f"user:{user}:"):
            permissions = line.rsplit(":", 1)[-1]
            return "x" in permissions
    return False


def inspect_location(path: str | Path, role: str = "labs") -> Location:
    validate_role(role)
    resolved = Path(path).expanduser().resolve()
    exists = resolved.is_dir()
    filesystem = _filesystem(resolved) if exists else "unknown"
    total = free = 0
    if exists:
        try:
            statvfs = os.statvfs(resolved)
            total = statvfs.f_blocks * statvfs.f_frsize
            free = statvfs.f_bavail * statvfs.f_frsize
        except OSError:
            pass

    selinux_mode = _selinux_mode()
    label = _selinux_label(resolved) if exists else None
    expected = expected_label(role)
    owner, mode = ("unknown", "unknown")
    nodatacow: bool | None = None
    if exists:
        owner, mode = _owner_mode(resolved)
        nodatacow = _has_nodatacow(resolved, filesystem)

    qemu_traverse = _qemu_access(resolved, "-x") if exists else None
    qemu_read = _qemu_access(resolved, "-r") if exists else None
    qemu_write = _qemu_access(resolved, "-w") if exists else None

    issues: list[str] = []
    warnings: list[str] = []
    if not exists:
        issues.append("Directory does not exist.")
    else:
        if selinux_mode == "Enforcing" and label != expected:
            issues.append(
                f"SELinux label is {label or 'unknown'}; expected {expected}."
            )
        if qemu_traverse is False:
            issues.append("The qemu user cannot traverse this path.")
        if qemu_read is False:
            issues.append("The qemu user cannot read this path.")
        if qemu_write is False:
            issues.append("The qemu user cannot write to this path.")
        if filesystem == "btrfs" and nodatacow is False:
            warnings.append(
                "btrfs directory is not nodatacow; expect fragmentation."
            )
        if free and free < MIN_FREE_BYTES:
            warnings.append(
                f"Only {free / (1024 ** 3):.1f} GB free; "
                "at least 20 GB recommended."
            )

    return Location(
        path=str(resolved),
        role=role,
        exists=exists,
        filesystem=filesystem,
        total_bytes=total,
        free_bytes=free,
        selinux_mode=selinux_mode,
        selinux_label=label,
        expected_label=expected,
        owner=owner,
        mode=mode,
        nodatacow=nodatacow,
        qemu_traverse=qemu_traverse,
        qemu_read=qemu_read,
        qemu_write=qemu_write,
        issues=issues,
        warnings=warnings,
        healthy=not issues,
    )


def plan_fix(location: Location, *, force_selinux: bool = False) -> list[Action]:
    """Return the commands needed to make a location usable.

    ``force_selinux`` also queues a recursive relabel when the top-level
    directory already has the expected label. That is required after SELinux
    was disabled while images were created: the directory may be labelled but
    files below it are not.
    """
    path = Path(location.path)
    actions: list[Action] = []

    if not location.exists:
        actions.append(
            Action("Create directory", ["mkdir", "-p", str(path)])
        )

    filesystem = location.filesystem
    if filesystem == "unknown":
        parent = path.parent if path.parent.exists() else Path("/")
        filesystem = _filesystem(parent)

    if filesystem == "btrfs" and (
        not location.exists or location.nodatacow is False
    ):
        try:
            empty = not any(path.iterdir()) if path.is_dir() else True
        except OSError:
            empty = False
        if empty:
            actions.append(
                Action(
                    "Disable copy-on-write for btrfs",
                    ["chattr", "+C", str(path)],
                )
            )

    identity = qemu_identity()
    if location.owner != identity.owner:
        actions.append(
            Action("Set ownership", ["chown", identity.owner, str(path)])
        )
    if location.mode != ("755" if location.role == "isos" else "750"):
        mode = "0755" if location.role == "isos" else "0750"
        actions.append(
            Action("Set directory mode", ["chmod", mode, str(path)])
        )

    qemu_user = identity.user
    if qemu_user:
        for parent in reversed(path.parents):
            if parent == Path("/"):
                continue
            if _has_qemu_acl(parent, qemu_user) is True:
                continue
            try:
                mode = parent.stat().st_mode
            except OSError:
                continue
            if mode & 0o001:
                continue
            actions.append(
                Action(
                    f"Allow qemu to traverse {parent}",
                    ["setfacl", "-m", f"u:{qemu_user}:x", str(parent)],
                )
            )

    selinux_active = location.selinux_mode == "Enforcing" or (
        force_selinux and location.selinux_mode == "Permissive"
    )
    if selinux_active and (
        force_selinux
        or location.selinux_label != location.expected_label
    ):
        actions.append(
            Action(
                "Add SELinux file context",
                [
                    _selinux_tool("semanage"),
                    "fcontext",
                    "-a",
                    "-t",
                    location.expected_label,
                    f"{path}(/.*)?",
                ],
            )
        )
        actions.append(
            Action(
                "Apply SELinux labels",
                [_selinux_tool("restorecon"), "-R", str(path)],
            )
        )

    return actions


def apply_fix(
    location: Location,
    *,
    dry_run: bool = False,
    force_selinux: bool = False,
) -> tuple[Location, list[Action]]:
    """Apply the repair plan. Requires root unless dry_run is true."""
    actions = plan_fix(location, force_selinux=force_selinux)
    if dry_run:
        return location, actions
    if os.geteuid() != 0:
        raise ValidationError(
            "Storage repair requires root. Re-run with sudo or use the daemon."
        )
    if not actions:
        return location, actions

    for action in actions:
        result = _run(action.command)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            # semanage returns non-zero when the context already exists.
            if (
                action.command[:1] == [_selinux_tool("semanage")]
                and "already" in stderr.lower()
            ):
                action.applied = True
                continue
            raise ValidationError(
                f"{' '.join(action.command)} failed: {stderr or result.stdout.strip()}"
            )
        action.applied = True

    return inspect_location(location.path, location.role), actions


class StorageStore:
    """Persistent list of storage locations (JSON)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(
            path or os.environ.get("VEMU_CONFIG", DEFAULT_CONFIG)
        ).expanduser()

    def load(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [
            {"path": str(item["path"]), "role": str(item["role"])}
            for item in data.get("locations", [])
        ]

    def save(self, items: list[dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"version": 1, "locations": items}, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def add(self, path: str | Path, role: str) -> dict[str, str]:
        validate_role(role)
        resolved = str(Path(path).expanduser().resolve())
        items = self.load()
        for item in items:
            if item["path"] == resolved:
                raise Conflict(f"Storage location already registered: {resolved}")
        entry = {"path": resolved, "role": role}
        items.append(entry)
        self.save(items)
        return entry

    def remove(self, path: str | Path) -> dict[str, str]:
        resolved = str(Path(path).expanduser().resolve())
        items = self.load()
        for item in items:
            if item["path"] == resolved:
                items.remove(item)
                self.save(items)
                return item
        raise NotFound(f"Storage location is not registered: {resolved}")

    def inspect_all(self) -> list[Location]:
        return [inspect_location(item["path"], item["role"]) for item in self.load()]
