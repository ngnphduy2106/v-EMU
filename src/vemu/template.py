"""Template metadata and sealing.

Sealing copies the current disk of a shut-off VM into an immutable base image
and writes ``template.json`` next to it. Cloning then only creates small qcow2
overlays, so a lab never duplicates multi-gigabyte disks.

M0 copies the disk (non-destructive). In-place sealing is a later optimization.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .tools import which as find_which
from typing import Any, Protocol

from .chain import ChainManager
from .errors import Conflict, ExternalCommandError, NotFound, ValidationError
from .lowprio import qemu_convert_command

TEMPLATE_FILE = "template.json"
TEMPLATE_VERSION = 3
TEMPLATE_OS_FAMILIES = ("linux", "windows")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _relabel(path: Path) -> None:
    restorecon = find_which("restorecon")
    if os.geteuid() != 0 or restorecon is None:
        return
    subprocess.run(
        [restorecon, "-R", str(path)], capture_output=True, check=False
    )


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-") or "template"


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ValidationError(
            "Template name must start with a letter or digit and contain only "
            "letters, digits, '.', '_' and '-', up to 64 characters."
        )
    return name


class DomainSource(Protocol):
    def info(self, name: str): ...

    def xml(self, name: str) -> str: ...

    def shutdown(self, name: str, timeout: int = 90, force: bool = False) -> None: ...

    def update_disk_source(
        self, name: str, new_path: str, old_path: str | None = None
    ): ...


def normalize_os_family(value: object) -> str:
    text = str(value or "").strip().lower()
    if text not in {"", *TEMPLATE_OS_FAMILIES}:
        raise ValidationError(
            "Template os_family must be 'linux', 'windows' or empty."
        )
    return text


@dataclass
class Template:
    name: str
    base: str
    description: str = ""
    source_domain: str = ""
    created_at: str = ""
    arch: str = "x86_64"
    os_family: str = ""
    machine: str = "q35"
    firmware: str = "bios"
    memory_mib: int = 2048
    vcpus: int = 2
    disk_bus: str = "virtio"
    video: str = "virtio"
    video_memory_mib: int | None = None
    nic_model: str = "virtio"
    secure_boot: bool = False
    tpm: bool = False
    sysprepped: bool = False
    never_booted: bool = True
    in_place: bool = False
    disks: list[dict[str, Any]] = field(default_factory=list)
    version: int = TEMPLATE_VERSION

    def effective_os_family(self) -> tuple[str, bool]:
        """Return (family, inferred) for legacy templates without metadata."""
        if self.os_family in TEMPLATE_OS_FAMILIES:
            return self.os_family, False
        return ("windows" if self.sysprepped else "linux"), True

    def disk_specs(self) -> list[dict[str, Any]]:
        if self.disks:
            return [dict(item) for item in self.disks]
        return [
            {
                "target": "vda",
                "base": self.base,
                "bus": self.disk_bus,
            }
        ]

    def base_path(self) -> Path:
        specs = self.disk_specs()
        return Path(str(specs[0]["base"])).expanduser()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "base": self.base,
            "disks": self.disk_specs(),
            "description": self.description,
            "source_domain": self.source_domain,
            "created_at": self.created_at,
            "arch": self.arch,
            "os_family": self.os_family,
            "machine": self.machine,
            "firmware": self.firmware,
            "memory_mib": self.memory_mib,
            "vcpus": self.vcpus,
            "disk_bus": self.disk_bus,
            "video": self.video,
            "video_memory_mib": self.video_memory_mib,
            "nic_model": self.nic_model,
            "secure_boot": self.secure_boot,
            "tpm": self.tpm,
            "sysprepped": self.sysprepped,
            "never_booted": self.never_booted,
            "in_place": self.in_place,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Template":
        version = int(data.get("version", 0))
        if version not in {1, 2, TEMPLATE_VERSION}:
            raise ValidationError(
                f"Unsupported template version {version}; expected 1, 2 or "
                f"{TEMPLATE_VERSION}."
            )
        raw_disks = data.get("disks")
        disks: list[dict[str, Any]] = []
        if isinstance(raw_disks, list) and raw_disks:
            for item in raw_disks:
                if not isinstance(item, dict) or not item.get("base"):
                    raise ValidationError("Template disk entry is invalid.")
                disks.append(
                    {
                        "target": str(item.get("target", "vda")),
                        "base": str(item["base"]),
                        "bus": str(item.get("bus", "virtio")),
                    }
                )
        base = str(data.get("base") or (disks[0]["base"] if disks else ""))
        if not disks and base:
            disks = [
                {
                    "target": "vda",
                    "base": base,
                    "bus": str(data.get("disk_bus", "virtio")),
                }
            ]
        return cls(
            name=str(data["name"]),
            base=base,
            description=str(data.get("description", "")),
            source_domain=str(data.get("source_domain", "")),
            created_at=str(data.get("created_at", "")),
            arch=str(data.get("arch", "x86_64")),
            os_family=normalize_os_family(data.get("os_family", "")),
            machine=str(data.get("machine", "q35")),
            firmware=str(data.get("firmware", "bios")),
            memory_mib=int(data.get("memory_mib", 2048)),
            vcpus=int(data.get("vcpus", 2)),
            disk_bus=str(data.get("disk_bus", "virtio")),
            video=str(data.get("video", "virtio")),
            video_memory_mib=(
                int(data["video_memory_mib"])
                if data.get("video_memory_mib") is not None
                else None
            ),
            nic_model=str(data.get("nic_model", "virtio")),
            secure_boot=bool(data.get("secure_boot", False)),
            tpm=bool(data.get("tpm", False)),
            sysprepped=bool(data.get("sysprepped", False)),
            never_booted=bool(data.get("never_booted", True)),
            in_place=bool(data.get("in_place", False)),
            disks=disks,
            version=version,
        )

    def save(self, directory: str | Path) -> Path:
        directory = Path(directory).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / TEMPLATE_FILE
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return path


def load_template(path: str | Path) -> Template:
    path = Path(path).expanduser().resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    template = Template.from_dict(data)
    for item in template.disk_specs():
        disk_base = Path(str(item["base"]))
        if not disk_base.is_absolute():
            disk_base = (path.parent / disk_base).resolve()
            item["base"] = str(disk_base)
    if template.disks:
        template.disks = template.disk_specs()
        template.base = str(template.disks[0]["base"])
    else:
        base = Path(template.base)
        if not base.is_absolute():
            base = (path.parent / base).resolve()
            template.base = str(base)
    return template


def resolve_template(path: str | Path) -> Template:
    """Resolve a template directory, template.json, or a bare qcow2 file."""
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        metadata = path / TEMPLATE_FILE
        if not metadata.is_file():
            raise NotFound(f"No {TEMPLATE_FILE} in {path}.")
        return load_template(metadata)
    if path.is_file():
        if path.suffix == ".json" or path.name == TEMPLATE_FILE:
            return load_template(path)
        return Template(
            name=path.stem,
            base=str(path),
            description="Ad-hoc template from a qcow2 file.",
            disks=[{"target": "vda", "base": str(path), "bus": "virtio"}],
        )
    raise NotFound(f"Template not found: {path}")


def _memory_mib(root: ET.Element) -> int:
    element = root.find("memory")
    if element is None or not element.text:
        return 2048
    value = int(element.text)
    unit = (element.get("unit") or "KiB").lower()
    if unit in {"kib", "k"}:
        return max(1, value // 1024)
    if unit in {"mib", "m"}:
        return value
    if unit in {"gib", "g"}:
        return value * 1024
    if unit in {"bytes", "b"}:
        return max(1, value // (1024 * 1024))
    return max(1, value // 1024)


def parse_domain_xml(xml: str) -> dict[str, Any]:
    root = ET.fromstring(xml)
    os_element = root.find("os")
    os_type = root.find("os/type")
    loader = root.find("os/loader")
    firmware = "bios"
    if os_element is not None and os_element.get("firmware") == "efi":
        firmware = "efi"
    elif loader is not None and loader.get("type") == "pflash":
        firmware = "efi"

    disks: list[str] = []
    disk_targets: list[str] = []
    disk_buses: list[str] = []
    disk_bus = "virtio"
    for disk in root.findall("./devices/disk"):
        if disk.get("device") != "disk":
            continue
        target = disk.find("target")
        if target is not None and target.get("bus") == "usb":
            # Removable answer media (autounattend/OOBE USB) is not a data
            # disk and must not count for seal/clone/config.
            continue
        source = disk.find("source")
        if source is not None:
            path = source.get("file") or source.get("dev")
            if path:
                disks.append(path)
                disk_targets.append(
                    target.get("dev", "vda") if target is not None else "vda"
                )
                disk_buses.append(
                    target.get("bus", "virtio")
                    if target is not None
                    else "virtio"
                )
        if target is not None and target.get("bus"):
            disk_bus = target.get("bus")

    vcpu_text = root.findtext("vcpu") or "2"
    video = root.find("./devices/video/model")
    interface = root.find("./devices/interface/model")
    video_memory_mib: int | None = None
    if video is not None and video.get("type") in {"vga", "qxl"}:
        raw_memory = video.get("vgamem") or video.get("vram")
        if raw_memory:
            try:
                memory = int(raw_memory) // 1024
            except ValueError:
                memory = 0
            if memory in {64, 128, 256}:
                video_memory_mib = memory
    secure_boot = bool(
        loader is not None
        and str(loader.get("secure", "")).lower() in {"yes", "on", "true", "1"}
    )
    return {
        "arch": os_type.get("arch", "x86_64") if os_type is not None else "x86_64",
        "machine": os_type.get("machine", "q35") if os_type is not None else "q35",
        "firmware": firmware,
        "memory_mib": _memory_mib(root),
        "vcpus": int(vcpu_text),
        "disks": disks,
        "disk_targets": disk_targets,
        "disk_buses": disk_buses,
        "disk_bus": disk_bus,
        "video": video.get("type", "virtio") if video is not None else "virtio",
        "video_memory_mib": video_memory_mib,
        "nic_model": (
            interface.get("type", "virtio")
            if interface is not None
            else "virtio"
        ),
        "secure_boot": secure_boot,
        "tpm": root.find("./devices/tpm") is not None,
    }


def seal_domain(
    backend: DomainSource,
    domain_name: str,
    dest_root: str | Path,
    name: str,
    description: str = "",
    *,
    sysprepped: bool = False,
    os_family: str = "",
    shutdown: bool = False,
    in_place: bool = False,
    qemu_img: str = "qemu-img",
) -> Template:
    """Seal a shut-off domain as a template.

    With ``in_place=False`` the disk is copied into ``dest/name/base.qcow2``.
    With ``in_place=True`` the source disk becomes the immutable base, a small
    overlay is created for the source VM, and the domain is repointed to it.
    """
    validate_name(name)
    os_family = normalize_os_family(os_family)
    if not os_family:
        os_family = "windows" if sysprepped else "linux"
    if os_family == "linux" and sysprepped:
        raise ValidationError(
            "A Linux template cannot be marked sysprepped. "
            "Use os_family='windows' for sysprepped Windows images."
        )
    info = backend.info(domain_name)
    if getattr(info, "state", "stopped") == "running":
        if not shutdown:
            raise Conflict(
                f"Domain '{domain_name}' is running. Shut it down or use --shutdown."
            )
        backend.shutdown(domain_name)

    parsed = parse_domain_xml(backend.xml(domain_name))
    sources = [Path(item) for item in parsed["disks"]]
    targets = list(parsed.get("disk_targets") or [])
    buses = list(parsed.get("disk_buses") or [])
    if not sources:
        raise NotFound(f"Domain '{domain_name}' has no data disk.")
    if len(targets) != len(sources):
        targets = [f"vd{chr(97 + index)}" for index in range(len(sources))]
    if len(buses) != len(sources):
        buses = [parsed["disk_bus"]] * len(sources)
    for source in sources:
        if not source.is_file():
            raise NotFound(f"Source disk was not found: {source}")
    if in_place and len(sources) > 1:
        raise ValidationError(
            "In-place sealing supports one disk; use copy mode for multi-disk."
        )

    dest = Path(dest_root).expanduser().resolve() / name
    if dest.exists():
        raise Conflict(f"Template directory already exists: {dest}")

    if in_place:
        source = sources[0]
        check = subprocess.run(
            [qemu_img, "check", str(source)],
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode != 0:
            raise ExternalCommandError(
                " ".join([qemu_img, "check", str(source)]),
                check.returncode,
                check.stderr or check.stdout,
            )
        if (source.parent / "chain.json").is_file():
            manager = ChainManager(source.parent)
            previous = manager.load().active_disk()
            layer = manager.snapshot("sealed")
            backend.update_disk_source(
                domain_name, layer.path, old_path=str(source)
            )
            _relabel(source.parent)
            base = Path(previous)
        else:
            chain_dir = source.parent / f"{slugify(domain_name)}-chain"
            if chain_dir.exists():
                raise Conflict(f"Chain directory already exists: {chain_dir}")
            manager = ChainManager.create(
                chain_dir,
                source,
                label="post-seal",
                qemu_img=qemu_img,
                target=targets[0],
                bus=buses[0],
            )
            overlay = manager.load().active_disk()
            backend.update_disk_source(
                domain_name, overlay, old_path=str(source)
            )
            _relabel(chain_dir)
            base = source
        os.chmod(base, 0o444)
        dest.mkdir(parents=True)
        template_disks = [
            {"target": targets[0], "base": str(base), "bus": buses[0]}
        ]
        in_place_flag = True
    else:
        dest.mkdir(parents=True)
        template_disks = []
        for source, target, bus in zip(sources, targets, buses):
            base = dest / (
                "base.qcow2"
                if len(sources) == 1
                else f"base-{target}.qcow2"
            )
            command = qemu_convert_command(
                qemu_img, str(source), str(base)
            )
            result = subprocess.run(
                command, capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                shutil.rmtree(dest, ignore_errors=True)
                raise ExternalCommandError(
                    " ".join(command), result.returncode, result.stderr
                )

            check = subprocess.run(
                [qemu_img, "check", str(base)],
                capture_output=True,
                text=True,
                check=False,
            )
            if check.returncode != 0:
                shutil.rmtree(dest, ignore_errors=True)
                raise ExternalCommandError(
                    " ".join([qemu_img, "check", str(base)]),
                    check.returncode,
                    check.stderr or check.stdout,
                )
            os.chmod(base, 0o444)
            template_disks.append(
                {"target": target, "base": str(base), "bus": bus}
            )
        base = Path(template_disks[0]["base"])
        in_place_flag = False

    template = Template(
        name=name,
        base=str(base),
        description=description,
        source_domain=domain_name,
        created_at=_now(),
        arch=str(parsed["arch"]),
        machine=str(parsed["machine"]),
        firmware=str(parsed["firmware"]),
        memory_mib=int(parsed["memory_mib"]),
        vcpus=int(parsed["vcpus"]),
        disk_bus=str(parsed["disk_bus"]),
        video=str(parsed["video"]),
        video_memory_mib=parsed["video_memory_mib"],
        nic_model=str(parsed["nic_model"]),
        secure_boot=bool(parsed["secure_boot"]),
        tpm=bool(parsed["tpm"]),
        os_family=os_family,
        sysprepped=sysprepped,
        never_booted=True,
        in_place=in_place_flag,
        disks=template_disks,
    )
    template.save(dest)
    _relabel(dest)
    return template


def list_templates(root: str | Path) -> list[Template]:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotFound(f"Template root not found: {root}")
    templates: list[Template] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        metadata = entry / TEMPLATE_FILE
        if metadata.is_file():
            templates.append(load_template(metadata))
    return templates
