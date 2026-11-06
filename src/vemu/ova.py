"""Best-effort OVA/OVF inspection and safe extraction."""

from __future__ import annotations

import re
import tarfile
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .errors import NotFound, ValidationError

OVF_NS = "http://schemas.dmtf.org/ovf/envelope/1"
RASD_NS = (
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/"
    "CIM_ResourceAllocationSettingData"
)
VSSD_NS = (
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/"
    "CIM_VirtualSystemSettingData"
)
VMW_NS = "http://www.vmware.com/schema/ovf"
ALLOWED_SUFFIXES = {".ovf", ".vmdk", ".mf", ".cert"}
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


@dataclass
class OvfDisk:
    disk_id: str
    file: str
    capacity: int
    populated_size: int = 0
    file_size: int = 0
    disk_format: str = ""
    synthetic: bool = False


@dataclass
class OvfNic:
    index: int
    name: str
    network: str
    model: str
    mac: str | None = None


@dataclass
class OvfInfo:
    ovf_name: str
    name: str
    vcpus: int = 1
    memory_mib: int = 2048
    disks: list[OvfDisk] = field(default_factory=list)
    nics: list[OvfNic] = field(default_factory=list)
    firmware: str = "bios"
    firmware_explicit: bool = False
    serial: bool = False
    serial_explicit: bool = False
    os_type: str = ""
    profile: str = ""
    warnings: list[str] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element | None, name: str) -> str:
    if element is None:
        return ""
    child = element.find(f"{{{RASD_NS}}}{name}")
    if child is None:
        child = element.find(f"{{{OVF_NS}}}{name}")
    if child is None:
        child = element.find(name)
    return (child.text or "").strip() if child is not None else ""


def _capacity(value: str, units: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    text = (units or "").lower().replace(" ", "")
    if "2^30" in text:
        return number * (1024**3)
    if "2^20" in text:
        return number * (1024**2)
    if "2^10" in text:
        return number * 1024
    return number


def _memory_mib(value: str, units: str) -> int:
    text = (units or "").lower().replace(" ", "")
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 2048
    if "2^30" in text:
        return number * 1024
    if "2^10" in text:
        return max(1, number // 1024)
    if text.startswith("byte") and "2^20" not in text:
        return max(1, number // (1024**2))
    return max(1, number)


def _vmx_version(text: str) -> int:
    match = re.search(r"vmx-(\d+)", text or "")
    return int(match.group(1)) if match else 0


def parse_ovf(xml: str, *, member_sizes: dict[str, int] | None = None) -> OvfInfo:
    member_sizes = member_sizes or {}
    try:
        root = ET.fromstring(xml.lstrip("\ufeff"))
    except ET.ParseError as exc:
        raise ValidationError(f"Cannot parse OVF XML: {exc}") from exc
    systems = root.findall(f".//{{{OVF_NS}}}VirtualSystem")
    warnings: list[str] = []
    if not systems:
        raise ValidationError("OVF has no VirtualSystem section.")
    if len(systems) > 1:
        warnings.append(
            f"OVF contains {len(systems)} VirtualSystems; only the first is imported."
        )
    system = systems[0]
    name = (system.findtext(f"{{{OVF_NS}}}Name") or "imported-vm").strip()
    os_section = system.find(f"{{{OVF_NS}}}OperatingSystemSection")
    os_type = ""
    if os_section is not None:
        os_type = (
            os_section.get(f"{{{VMW_NS}}}osType")
            or os_section.findtext(f"{{{OVF_NS}}}Description")
            or ""
        ).strip()

    profile = ""
    deployments = root.find(f".//{{{OVF_NS}}}DeploymentOptionSection")
    if deployments is not None:
        configurations = deployments.findall(f"{{{OVF_NS}}}Configuration")
        chosen = next(
            (
                item
                for item in configurations
                if (item.get(f"{{{OVF_NS}}}default") or "").lower()
                in {"true", "1"}
            ),
            configurations[0] if configurations else None,
        )
        if chosen is not None:
            profile = chosen.get(f"{{{OVF_NS}}}id") or ""
            label = (
                chosen.findtext(f"{{{OVF_NS}}}Label") or profile
            ).strip()
            if len(configurations) > 1:
                warnings.append(
                    f"OVF offers {len(configurations)} deployment profiles; "
                    f"using '{profile}' ({label}). Override CPU/RAM if needed."
                )

    vcpus = 1
    memory_mib = 2048
    serial = False
    serial_explicit = False
    hardware = system.find(f"{{{OVF_NS}}}VirtualHardwareSection")
    items = hardware.findall(f"{{{OVF_NS}}}Item") if hardware is not None else []
    disk_items: list[tuple[str, str]] = []
    nics: list[OvfNic] = []
    for item in items:
        configured = (
            item.get("configuration")
            or item.get(f"{{{OVF_NS}}}configuration")
            or ""
        ).strip()
        if profile and configured and profile not in configured.split():
            continue
        resource_type = _text(item, "ResourceType")
        quantity = _text(item, "VirtualQuantity")
        units = _text(item, "AllocationUnits")
        if resource_type == "3" and quantity.isdigit():
            vcpus = max(1, int(quantity))
        elif resource_type == "4" and quantity.isdigit():
            memory_mib = _memory_mib(quantity, units)
        elif resource_type == "21":
            serial = True
            serial_explicit = True
        elif resource_type == "17":
            host = _text(item, "HostResource")
            disk_id = host.rsplit("/", 1)[-1] if host else ""
            disk_items.append((disk_id, _text(item, "ElementName")))
        elif resource_type == "10":
            connection = _text(item, "Connection") or _text(item, "ElementName")
            model = _text(item, "ResourceSubType") or "virtio"
            mac = _text(item, "Address") or None
            if mac and not MAC_RE.match(mac):
                mac = None
            nics.append(
                OvfNic(
                    index=len(nics),
                    name=_text(item, "ElementName") or f"Ethernet {len(nics) + 1}",
                    network=connection,
                    model=model,
                    mac=mac.lower() if mac else None,
                )
            )

    disk_section = root.find(f".//{{{OVF_NS}}}DiskSection")
    disk_defs: dict[str, OvfDisk] = {}
    if disk_section is not None:
        for disk in disk_section.findall(f"{{{OVF_NS}}}Disk"):
            disk_id = disk.get(f"{{{OVF_NS}}}diskId") or ""
            file_ref = disk.get(f"{{{OVF_NS}}}fileRef") or ""
            capacity = _capacity(
                disk.get(f"{{{OVF_NS}}}capacity") or "0",
                disk.get(f"{{{OVF_NS}}}capacityAllocationUnits") or "",
            )
            populated = _capacity(
                disk.get(f"{{{OVF_NS}}}populatedSize") or "0",
                "",
            )
            disk_defs[disk_id] = OvfDisk(
                disk_id=disk_id,
                file=file_ref,
                capacity=capacity,
                populated_size=populated,
                disk_format=disk.get(f"{{{OVF_NS}}}format") or "",
            )

    refs = root.find(f".//{{{OVF_NS}}}References")
    files: dict[str, tuple[str, int]] = {}
    if refs is not None:
        for file_element in refs.findall(f"{{{OVF_NS}}}File"):
            file_id = file_element.get(f"{{{OVF_NS}}}id") or ""
            files[file_id] = (
                file_element.get(f"{{{OVF_NS}}}href") or "",
                int(file_element.get(f"{{{OVF_NS}}}size") or 0),
            )

    disks: list[OvfDisk] = []
    attached_files: set[str] = set()
    placeholder_items: list[tuple[str, str, OvfDisk]] = []
    unknown_disks: set[str] = set()
    for disk_id, element_name in disk_items:
        if not disk_id:
            continue
        disk = disk_defs.get(disk_id)
        if disk is None:
            if disk_id not in unknown_disks:
                warnings.append(f"OVF references unknown disk '{disk_id}'.")
                unknown_disks.add(disk_id)
            continue
        file_name, file_size = files.get(disk.file, (disk.file, 0))
        if not file_name:
            if disk.capacity > 0:
                placeholder_items.append((disk_id, element_name, disk))
            continue
        if file_name in attached_files:
            continue
        disk.file = file_name
        disk.file_size = member_sizes.get(file_name, file_size)
        attached_files.add(file_name)
        disks.append(disk)

    skipped_unattached = 0
    for disk in disk_defs.values():
        if not disk.file:
            continue
        file_name, _size = files.get(disk.file, (disk.file, 0))
        if file_name and file_name not in attached_files:
            skipped_unattached += 1
    if skipped_unattached:
        warnings.append(
            f"Skipped {skipped_unattached} file-backed disk(s) that the OVF "
            "does not attach to the virtual hardware."
        )

    placeholders = placeholder_items
    empty_bytes = 0
    for disk_id, _name, source_disk in placeholders:
        disks.append(
            OvfDisk(
                disk_id=disk_id,
                file="",
                capacity=source_disk.capacity,
                disk_format=source_disk.disk_format,
                synthetic=True,
            )
        )
        empty_bytes += int(source_disk.capacity or 0)
    if placeholders:
        warnings.append(
            f"OVF declares {len(placeholders)} empty disk(s) "
            f"(thin, {empty_bytes // 1024**3} GiB total) that will be created."
        )
    if not disks:
        warnings.append("OVF declares no usable disk.")
    if not nics:
        warnings.append("OVF declares no network adapter; one will be suggested.")

    firmware = "bios"
    firmware_explicit = False
    for config in root.iter(f"{{{VMW_NS}}}Config"):
        key = config.get(f"{{{VMW_NS}}}key") or ""
        value = (config.get(f"{{{VMW_NS}}}value") or "").lower()
        if key == "firmware" and value in {"bios", "efi"}:
            firmware = value
            firmware_explicit = True
    return OvfInfo(
        ovf_name="",
        name=name,
        vcpus=vcpus,
        memory_mib=memory_mib,
        disks=disks,
        nics=nics,
        firmware=firmware,
        firmware_explicit=firmware_explicit,
        serial=serial,
        serial_explicit=serial_explicit,
        os_type=os_type,
        profile=profile,
        warnings=warnings,
    )


class _Archive:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.kind = "tar" if tarfile.is_tarfile(path) else "zip"
        if self.kind == "zip" and not zipfile.is_zipfile(path):
            raise ValidationError(f"Not an OVA/ZIP archive: {path}")

    def members(self) -> list[tuple[str, int]]:
        if self.kind == "zip":
            with zipfile.ZipFile(self.path) as archive:
                return [(info.filename, info.file_size) for info in archive.infolist()]
        with tarfile.open(self.path) as archive:
            return [(member.name, member.size) for member in archive.getmembers()]

    def read(self, name: str, limit: int = 2 * 1024 * 1024) -> bytes:
        if self.kind == "zip":
            with zipfile.ZipFile(self.path) as archive:
                info = archive.getinfo(name)
                if info.file_size > limit:
                    raise ValidationError(f"OVF file is too large: {name}")
                return archive.read(info)
        with tarfile.open(self.path) as archive:
            member = archive.getmember(name)
            if member.size > limit:
                raise ValidationError(f"OVF file is too large: {name}")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValidationError(f"Cannot read archive member: {name}")
            return handle.read()

    def extract(
        self,
        names: set[str],
        dest: Path,
        *,
        allowed: set[str] = ALLOWED_SUFFIXES,
    ) -> dict[str, Path]:
        dest.mkdir(parents=True, exist_ok=True)
        extracted: dict[str, Path] = {}
        total = 0
        budget = max(self.path.stat().st_size * 2, 1024**3)
        if self.kind == "zip":
            with zipfile.ZipFile(self.path) as archive:
                for info in archive.infolist():
                    if info.filename not in names:
                        continue
                    _safe_name(info.filename, allowed)
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        raise ValidationError(
                            f"Refusing symlink in archive: {info.filename}"
                        )
                    total += info.file_size
                    if total > budget:
                        raise ValidationError("OVA extraction exceeds the size limit.")
                    target = dest / info.filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(info))
                    extracted[info.filename] = target
            return extracted
        with tarfile.open(self.path) as archive:
            for member in archive.getmembers():
                if member.name not in names:
                    continue
                _safe_name(member.name, allowed)
                if member.issym() or member.islnk() or member.isdev():
                    raise ValidationError(
                        f"Refusing special file in archive: {member.name}"
                    )
                total += member.size
                if total > budget:
                    raise ValidationError("OVA extraction exceeds the size limit.")
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                target = dest / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(handle.read())
                extracted[member.name] = target
        return extracted


def _safe_name(name: str, allowed: set[str] = ALLOWED_SUFFIXES) -> str:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValidationError(f"Unsafe archive path: {name}")
    if path.suffix.lower() not in allowed:
        raise ValidationError(f"Unsupported file in archive: {name}")
    return name


def inspect_archive(path: str | Path) -> tuple[OvfInfo, list[str]]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise NotFound(f"OVA file was not found: {source}")
    archive = _Archive(source)
    members = archive.members()
    sizes = {name: size for name, size in members}
    ovfs = [name for name, _ in members if name.lower().endswith(".ovf")]
    if not ovfs:
        raise ValidationError("Archive contains no OVF descriptor.")
    choices: list[tuple[int, str, OvfInfo]] = []
    for name in ovfs:
        info = parse_ovf(
            archive.read(name).decode("utf-8", "replace"),
            member_sizes=sizes,
        )
        text = archive.read(name).decode("utf-8", "replace")
        version = _vmx_version(text)
        penalty = (
            20
            if re.search(r"(vapp|nsxt|ztna)", name, re.IGNORECASE)
            else 0
        )
        info.ovf_name = name
        choices.append((version * 100 - penalty, name, info))
    choices.sort(key=lambda item: item[0], reverse=True)
    return choices[0][2], [name for _, name, _ in choices]


def extract_archive(
    path: str | Path, dest: str | Path
) -> tuple[dict[str, Path], OvfInfo]:
    source = Path(path).expanduser().resolve()
    info, _ = inspect_archive(source)
    archive = _Archive(source)
    names = {
        name
        for name, _ in archive.members()
        if name == info.ovf_name
        or name.lower().endswith((".vmdk", ".mf", ".cert"))
    }
    extracted = archive.extract(names, Path(dest).expanduser().resolve())
    return extracted, info


DISK_SUFFIXES = {".qcow2", ".raw", ".img", ".vmdk"}


def inspect_disk_archive(path: str | Path) -> tuple[str, int, str]:
    """Return the single disk member name/size and its qemu-img format."""
    source = Path(path).expanduser().resolve()
    archive = _Archive(source)
    candidates = [
        (name, size)
        for name, size in archive.members()
        if Path(name).suffix.lower() in DISK_SUFFIXES
    ]
    if not candidates:
        raise ValidationError("Archive contains no disk image.")
    if len(candidates) > 1:
        raise ValidationError(
            "Archive contains multiple disk images; extract and import one "
            "disk at a time."
        )
    name, size = candidates[0]
    suffix = Path(name).suffix.lower()
    source_format = "raw" if suffix == ".img" else suffix.lstrip(".")
    return name, size, source_format


def extract_disk_archive(path: str | Path, dest: str | Path) -> tuple[Path, str]:
    source = Path(path).expanduser().resolve()
    name, _size, source_format = inspect_disk_archive(source)
    archive = _Archive(source)
    extracted = archive.extract(
        {name}, Path(dest).expanduser().resolve(), allowed=DISK_SUFFIXES
    )
    return extracted[name], source_format
