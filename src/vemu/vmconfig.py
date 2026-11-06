"""Read and update libvirt domain hardware without hard-coding one layout."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from .domain import (
    TCG_TB_CACHE_MIB,
    cpu_modes_for_arch,
    disk_target_name,
)
from .errors import Conflict, MutationError, NotFound, ValidationError

VIDEO_MODELS = {"virtio", "vga", "qxl", "cirrus", "vmvga", "bochs", "ramfb"}
NIC_MODELS = {"virtio", "e1000e", "e1000", "rtl8139", "vmxnet3", "pcnet"}
DISK_BUSES = {"virtio", "sata", "scsi", "ide", "nvme", "usb"}
CPU_MODES = {"host-passthrough", "host-model", "maximum"}


def _memory_mib(element: ET.Element | None) -> int:
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
    return max(1, value // 1024)


def _firmware(root: ET.Element) -> str:
    os_element = root.find("os")
    if os_element is not None and os_element.get("firmware") == "efi":
        return "efi"
    loader = root.find("os/loader")
    if loader is not None and loader.get("type") == "pflash":
        return "efi"
    return "bios"


def _secure_boot(root: ET.Element) -> bool:
    """Secure Boot is enabled only when libvirt marks the loader secure."""
    loader = root.find("os/loader")
    return loader is not None and loader.get("secure") == "yes"


def _controller_entry(element: ET.Element) -> dict[str, str]:
    controller_type = element.get("type", "")
    return {
        "type": controller_type,
        "index": element.get("index", ""),
        "model": element.get("model", ""),
        "serial": element.findtext("serial", ""),
    }


def _other_device_entry(
    tag: str, element: ET.Element
) -> dict[str, str]:
    if tag == "channel":
        target = element.find("target")
        target_type = target.get("type", "") if target is not None else ""
        name = target.get("name", "") if target is not None else ""
        detail = f"{element.get('type', '')} {target_type}:{name}".strip()
    elif tag == "input":
        detail = (
            f"{element.get('type', '')} ({element.get('bus', '')})"
        ).strip()
    elif tag == "graphics":
        detail = element.get("type", "")
        listen = element.get("listen")
        if listen:
            detail += f" {listen}"
        port = element.get("port")
        if port and port not in {"-1", "0"}:
            detail += f":{port}"
        elif element.get("autoport") == "yes":
            detail += " autoport"
    elif tag == "tpm":
        backend = element.find("backend")
        detail = element.get("model", "")
        if backend is not None:
            detail += (
                f" backend={backend.get('type', '')}"
                f" {backend.get('version', '')}"
            ).rstrip()
    elif tag == "watchdog":
        detail = (
            f"{element.get('model', '')} action={element.get('action', '')}"
        ).strip()
    elif tag in {"sound", "memballoon", "panic", "rng"}:
        detail = element.get("model", "") or element.get("type", "")
        if tag == "rng":
            backend = element.find("backend")
            if backend is not None and backend.get("model"):
                detail += f" backend={backend.get('model')}"
    elif tag in {"serial", "console", "parallel"}:
        target = element.find("target")
        detail = target.get("type", "") if target is not None else ""
        port = target.get("port") if target is not None else None
        if port:
            detail += f" port={port}"
    elif tag == "redirdev":
        detail = f"{element.get('bus', '')} {element.get('type', '')}".strip()
    elif tag == "shmem":
        detail = element.findtext("name", "")
    elif tag == "vsock":
        detail = element.get("model", "")
    else:
        detail = element.get("type", "") or element.get("model", "")
    return {"type": tag, "detail": detail.strip() or tag}


def parse_config(xml: str) -> dict[str, Any]:
    root = ET.fromstring(xml)
    os_element = root.find("os/type")
    management = root.find(
        ".//{https://vemu.local/management}management"
    )
    guest_os = (
        management.get("os_type") if management is not None else None
    ) or "generic"
    cpu = root.find("cpu")
    disks: list[dict[str, Any]] = []
    for disk in root.findall("./devices/disk"):
        source = disk.find("source")
        target = disk.find("target")
        driver = disk.find("driver")
        boot = disk.find("boot")
        if (
            target is not None
            and target.get("bus") == "usb"
            and disk.get("device") == "disk"
        ):
            # Removable answer media is not user data. USB CD-ROMs (used as
            # installer media on TCG guests) are real media and stay visible.
            continue
        disks.append(
            {
                "device": disk.get("device", "disk"),
                "bus": target.get("bus", "") if target is not None else "",
                "target": target.get("dev", "") if target is not None else "",
                "format": driver.get("type", "") if driver is not None else "",
                "source": source.get("file", "") if source is not None else "",
                "boot": int(boot.get("order", 0)) if boot is not None else 0,
            }
        )
    nics: list[dict[str, Any]] = []
    for interface in root.findall("./devices/interface"):
        mac = interface.find("mac")
        source = interface.find("source")
        model = interface.find("model")
        link = interface.find("link")
        nics.append(
            {
                "mac": mac.get("address", "") if mac is not None else "",
                "type": interface.get("type", ""),
                "network": source.get("network", "") if source is not None else "",
                "bridge": source.get("bridge", "") if source is not None else "",
                "model": model.get("type", "") if model is not None else "",
                "link": (
                    link.get("state", "up") if link is not None else "up"
                ),
            }
        )
    memory_element = root.find("memory")
    current_element = root.find("currentMemory")
    memory_max_mib = _memory_mib(memory_element)
    memory_mib = (
        _memory_mib(current_element)
        if current_element is not None
        else memory_max_mib
    )
    video = root.find("./devices/video/model")
    video_memory_mib: int | None = None
    if video is not None:
        raw_memory = video.get("vgamem") or video.get("vram")
        if raw_memory:
            try:
                video_memory_mib = int(raw_memory) // 1024
            except ValueError:
                video_memory_mib = None
    shares: list[dict[str, Any]] = []
    for filesystem in root.findall("./devices/filesystem"):
        driver = filesystem.find("driver")
        source = filesystem.find("source")
        target = filesystem.find("target")
        shares.append(
            {
                "tag": target.get("dir", "") if target is not None else "",
                "source": source.get("dir", "") if source is not None else "",
                "driver": driver.get("type", "") if driver is not None else "",
                "read_only": filesystem.find("readonly") is not None,
            }
        )
    controllers = [
        _controller_entry(controller)
        for controller in root.findall("./devices/controller")
    ]
    other_devices = []
    for tag in (
        "channel",
        "input",
        "memballoon",
        "sound",
        "tpm",
        "rng",
        "watchdog",
        "panic",
        "graphics",
        "serial",
        "console",
        "parallel",
        "redirdev",
        "shmem",
        "vsock",
        "hostdev",
    ):
        for element in root.findall(f"./devices/{tag}"):
            other_devices.append(_other_device_entry(tag, element))
    vcpu_element = root.find("vcpu")
    vcpus_max = int(
        (vcpu_element.text if vcpu_element is not None else "1") or 1
    )
    vcpus = int(
        vcpu_element.get("current") or vcpus_max
        if vcpu_element is not None
        else 1
    )
    return {
        "name": root.findtext("name", ""),
        "uuid": root.findtext("uuid", ""),
        "memory_mib": memory_mib,
        "memory_max_mib": memory_max_mib,
        "vcpus": vcpus,
        "vcpus_max": vcpus_max,
        "cpu_mode": cpu.get("mode", "") if cpu is not None else "",
        "machine": os_element.get("machine", "") if os_element is not None else "",
        "arch": os_element.get("arch", "") if os_element is not None else "",
        "os_type": guest_os,
        "firmware": _firmware(root),
        "secure_boot": _secure_boot(root),
        "video": video.get("type", "") if video is not None else "",
        "video_memory_mib": video_memory_mib,
        "serial": (
            root.find("./devices/serial") is not None
            or root.find("./devices/console") is not None
        ),
        "tpm": root.find("./devices/tpm") is not None,
        "usb": root.find("./devices/controller[@type='usb']") is not None,
        "audio": root.find("./devices/sound") is not None,
        "disks": disks,
        "nics": nics,
        "shares": shares,
        "controllers": controllers,
        "other_devices": other_devices,
    }


def _retarget_disks(root: ET.Element, bus: str) -> None:
    devices = root.find("devices")
    if devices is None:
        raise MutationError("Domain XML has no <devices>.")
    all_disks = devices.findall("disk")
    used = set()
    for disk in all_disks:
        target = disk.find("target")
        if target is not None and target.get("dev"):
            used.add(target.get("dev"))
    counter = 0
    for disk in all_disks:
        if disk.get("device") != "disk":
            continue
        target = disk.find("target")
        if (
            target is not None
            and target.get("bus") == "usb"
        ):
            # Windows OOBE/autounattend USB storage is removable answer
            # media, not a data disk. Retargeting it to NVMe/SATA also left
            # removable='on' on a non-USB target, which libvirt rejects.
            continue
        if target is None:
            target = ET.SubElement(disk, "target")
        while True:
            candidate = disk_target_name(bus, counter)
            counter += 1
            if candidate not in used:
                used.add(candidate)
                break
        target.set("bus", bus)
        target.set("dev", candidate)
        address = disk.find("address")
        if address is not None:
            disk.remove(address)
        if bus == "usb":
            target.set("removable", "on")
        else:
            target.attrib.pop("removable", None)
    _ensure_controller(devices, bus)


def _ensure_controller(devices: ET.Element, bus: str) -> None:
    if bus == "virtio":
        return
    if bus == "sata":
        if devices.find("controller[@type='sata']") is None:
            ET.SubElement(devices, "controller", type="sata", index="0")
    elif bus == "scsi":
        if devices.find("controller[@type='scsi']") is None:
            ET.SubElement(
                devices,
                "controller",
                type="scsi",
                index="0",
                model="virtio-scsi",
            )
    elif bus == "ide":
        if devices.find("controller[@type='ide']") is None:
            ET.SubElement(devices, "controller", type="ide", index="0")
    elif bus == "usb":
        if devices.find("controller[@type='usb']") is None:
            ET.SubElement(
                devices, "controller", type="usb", index="0", model="qemu-xhci"
            )
    elif bus == "nvme":
        if devices.find("controller[@type='nvme']") is None:
            controller = ET.SubElement(
                devices, "controller", type="nvme", index="0"
            )
            ET.SubElement(controller, "serial").text = "VEMU-NVME-0"


def apply_config(
    xml: str,
    *,
    memory_mib: int | None = None,
    memory_max_mib: int | None = None,
    vcpus: int | None = None,
    vcpus_max: int | None = None,
    cpu_mode: str | None = None,
    machine: str | None = None,
    video: str | None = None,
    video_memory_mib: int | None = None,
    disk_bus: str | None = None,
    nic_model: str | None = None,
) -> str:
    root = ET.fromstring(xml)
    if cpu_mode is not None:
        os_type = root.find("os/type")
        arch = os_type.get("arch", "x86_64") if os_type is not None else "x86_64"
        allowed = cpu_modes_for_arch(arch)
        if cpu_mode not in allowed:
            raise ValidationError(
                f"CPU mode '{cpu_mode}' is not valid for {arch}; use "
                + " or ".join(allowed) + "."
            )
    if memory_mib is not None:
        if memory_mib < 256:
            raise ValidationError("Memory must be at least 256 MiB.")
        maximum = memory_max_mib if memory_max_mib is not None else memory_mib
        if maximum < memory_mib:
            raise ValidationError(
                "Current memory must not exceed the maximum memory."
            )
        memory = root.find("memory")
        if memory is None:
            memory = ET.Element("memory")
            root.insert(0, memory)
        memory.set("unit", "MiB")
        memory.text = str(maximum)
        current = root.find("currentMemory")
        if current is None:
            current = ET.Element("currentMemory", unit="MiB")
            root.insert(list(root).index(memory) + 1, current)
        current.set("unit", "MiB")
        current.text = str(memory_mib)
    if vcpus is not None:
        if vcpus < 1:
            raise ValidationError("At least one vCPU is required.")
        if vcpus_max is not None and vcpus_max < vcpus:
            raise ValidationError(
                "The vCPU boot maximum must not be lower than the current "
                "count."
            )
        vcpu = root.find("vcpu")
        if vcpu is None:
            vcpu = ET.SubElement(root, "vcpu")
        existing_max = int(vcpu.text or vcpus)
        maximum = vcpus_max if vcpus_max is not None else existing_max
        maximum = max(maximum, vcpus)
        vcpu.text = str(maximum)
        if (
            vcpus_max is not None
            or vcpu.get("current") is not None
            or maximum > vcpus
        ):
            vcpu.set("current", str(vcpus))
        else:
            vcpu.attrib.pop("current", None)
    if cpu_mode is not None:
        if cpu_mode not in CPU_MODES:
            raise ValidationError(f"Unsupported CPU mode: {cpu_mode}")
        cpu = root.find("cpu")
        if cpu is None:
            cpu = ET.SubElement(root, "cpu")
        cpu.set("mode", cpu_mode)
        if cpu_mode != "custom":
            model = cpu.find("model")
            if model is not None:
                cpu.remove(model)
    if machine is not None:
        os_type = root.find("os/type")
        if os_type is None:
            raise MutationError("Domain XML has no <os><type>.")
        os_type.set("machine", machine)
    if video is not None or video_memory_mib is not None:
        devices = root.find("devices")
        if devices is None:
            raise MutationError("Domain XML has no <devices>.")
        video_element = devices.find("video")
        if video_element is None:
            video_element = ET.SubElement(devices, "video")
        model = video_element.find("model")
        if model is None:
            model = ET.SubElement(video_element, "model")
        target_video = video or model.get("type", "")
        if target_video not in VIDEO_MODELS:
            raise ValidationError(
                f"Unsupported video model: {target_video}"
            )
        old_vram = model.get("vram")
        old_vgamem = model.get("vgamem")
        for attribute in list(model.attrib):
            if attribute not in {"type", "heads", "primary"}:
                del model.attrib[attribute]
        model.set("type", target_video)
        model.set("heads", "1")
        model.set("primary", "yes")
        if target_video in {"vga", "qxl"}:
            attribute = "vgamem" if target_video == "qxl" else "vram"
            previous = old_vgamem if target_video == "qxl" else old_vram
            if video_memory_mib is not None:
                if video_memory_mib not in {64, 128, 256}:
                    raise ValidationError(
                        "Video memory must be 64, 128 or 256 MiB."
                    )
                model.set(attribute, str(video_memory_mib * 1024))
            elif previous:
                model.set(attribute, previous)
        if target_video == "ramfb":
            # ramfb is a sysbus device; libvirt rejects a PCI address (and
            # the stale alias) left over from virtio/vga.
            for child in list(video_element):
                if child.tag in {"address", "alias"}:
                    video_element.remove(child)
    if nic_model is not None:
        if nic_model not in NIC_MODELS:
            raise ValidationError(f"Unsupported NIC model: {nic_model}")
        for interface in root.findall("./devices/interface"):
            model = interface.find("model")
            if model is None:
                model = ET.SubElement(interface, "model")
            model.set("type", nic_model)
    if disk_bus is not None:
        if disk_bus not in DISK_BUSES:
            raise ValidationError(f"Unsupported disk bus: {disk_bus}")
        os_type = root.find("os/type")
        current_machine = (
            os_type.get("machine", "") if os_type is not None else ""
        )
        current_arch = (
            os_type.get("arch", "x86_64") if os_type is not None else "x86_64"
        )
        if disk_bus == "ide" and "q35" in current_machine:
            raise ValidationError(
                "The q35 machine has no IDE controller; use sata or scsi."
            )
        if (
            disk_bus == "nvme"
            and current_arch == "x86_64"
            and "q35" not in current_machine
        ):
            raise ValidationError(
                "NVMe disks need the q35 machine (PCIe); use sata, scsi or "
                "virtio with pc/i440fx."
            )
        _retarget_disks(root, disk_bus)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def find_cdrom(root: ET.Element, target: str | None = None) -> ET.Element:
    cdroms = [
        disk
        for disk in root.findall("./devices/disk")
        if disk.get("device") == "cdrom"
    ]
    if not cdroms:
        raise NotFound("This VM has no CD/DVD drive.")
    if target:
        for disk in cdroms:
            element = disk.find("target")
            if element is not None and element.get("dev") == target:
                return disk
        raise NotFound(f"CD/DVD drive {target} was not found.")
    return cdroms[0]


def set_cdrom_media(disk: ET.Element, path: str | None) -> None:
    """Change or eject the medium of one CD/DVD disk element."""
    backing = disk.find("backingStore")
    if backing is not None:
        disk.remove(backing)
    source = disk.find("source")
    if path is None:
        if source is not None:
            disk.remove(source)
        # An ejected installer must not retain a boot order. Otherwise a
        # running domain can already have the OS disk at order 1 and libvirt
        # rejects the CD device update with "boot order 1 is already used".
        boot = disk.find("boot")
        if boot is not None:
            disk.remove(boot)
        return
    if source is None:
        source = ET.Element("source", file=path)
        insert_at = 1 if disk.find("driver") is not None else 0
        disk.insert(insert_at, source)
    else:
        source.set("file", path)


def cdrom_info(disk: ET.Element) -> dict[str, str | None]:
    source = disk.find("source")
    target = disk.find("target")
    driver = disk.find("driver")
    return {
        "target": target.get("dev") if target is not None else "",
        "bus": target.get("bus") if target is not None else "",
        "format": driver.get("type") if driver is not None else "",
        "source": source.get("file") if source is not None else None,
    }


def add_disk(
    xml: str,
    path: str,
    bus: str,
    *,
    format: str = "qcow2",
    reserved_targets: set[str] | None = None,
) -> tuple[str, str]:
    if bus not in DISK_BUSES:
        raise ValidationError(f"Unsupported disk bus: {bus}")
    if format not in {"qcow2", "raw"}:
        raise ValidationError("Disk format must be qcow2 or raw.")
    root = ET.fromstring(xml)
    os_type = root.find("os/type")
    machine = os_type.get("machine", "") if os_type is not None else ""
    arch = os_type.get("arch", "x86_64") if os_type is not None else "x86_64"
    if bus == "ide" and "q35" in machine:
        raise ValidationError(
            "The q35 machine has no IDE controller; use sata or scsi."
        )
    if bus == "nvme" and arch == "x86_64" and "q35" not in machine:
        raise ValidationError(
            "NVMe disks need the q35 machine (PCIe); use sata, scsi or "
            "virtio with pc/i440fx."
        )
    devices = root.find("devices")
    if devices is None:
        raise MutationError("Domain XML has no <devices>.")
    existing_sources = {
        (disk.find("source").get("file") if disk.find("source") is not None else "")
        for disk in devices.findall("disk")
    }
    if path in existing_sources:
        raise Conflict(f"Disk is already attached: {path}")
    used = set(reserved_targets or set())
    for disk in devices.findall("disk"):
        target = disk.find("target")
        if target is not None and target.get("dev"):
            used.add(target.get("dev"))
    target = None
    max_slots = 256 if bus == "nvme" else 26
    for index in range(max_slots):
        candidate = disk_target_name(bus, index)
        if candidate not in used:
            target = candidate
            break
    if target is None:
        raise Conflict(f"No free target name for disk bus {bus}.")
    disk_element = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk_element, "driver", name="qemu", type=format)
    ET.SubElement(disk_element, "source", file=path)
    ET.SubElement(disk_element, "target", dev=target, bus=bus)
    _ensure_controller(devices, bus)
    ET.indent(root, space="  ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(root, encoding="unicode"),
        target,
    )


def remove_disk(xml: str, source: str) -> tuple[str, dict[str, Any]]:
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        raise MutationError("Domain XML has no <devices>.")
    data_disks = [
        disk for disk in devices.findall("disk")
        if disk.get("device") == "disk"
    ]
    if len(data_disks) <= 1:
        raise Conflict("A VM must keep at least one data disk.")
    found = None
    for disk in data_disks:
        element = disk.find("source")
        if element is not None and element.get("file") == source:
            found = disk
            break
    if found is None:
        raise NotFound(f"Data disk was not found: {source}")
    target = found.find("target")
    info = {
        "source": source,
        "bus": target.get("bus", "") if target is not None else "",
        "target": target.get("dev", "") if target is not None else "",
    }
    devices.remove(found)
    ET.indent(root, space="  ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(root, encoding="unicode"),
        info,
    )


def add_tpm(xml: str, version: str = "2.0") -> str:
    if version not in {"1.2", "2.0"}:
        raise ValidationError("TPM version must be 1.2 or 2.0.")
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        raise MutationError("Domain XML has no <devices>.")
    if devices.find("tpm") is not None:
        raise Conflict("A TPM device is already present.")
    os_type = root.find("os/type")
    machine = os_type.get("machine", "") if os_type is not None else ""
    model = "tpm-crb" if "q35" in machine else "tpm-tis"
    tpm = ET.SubElement(devices, "tpm", model=model)
    ET.SubElement(tpm, "backend", type="emulator", version=version)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def remove_tpm(xml: str) -> str:
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        raise MutationError("Domain XML has no <devices>.")
    tpm = devices.find("tpm")
    if tpm is None:
        raise NotFound("No TPM device is present.")
    devices.remove(tpm)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def ensure_usb_keyboard(xml: str) -> str:
    """Add a USB keyboard when the domain has no keyboard at all."""
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        return xml
    if devices.find("input[@type='keyboard']") is not None:
        return xml
    ET.SubElement(devices, "input", type="keyboard", bus="usb")
    if devices.find("controller[@type='usb']") is None:
        ET.SubElement(
            devices, "controller", type="usb", index="0", model="qemu-xhci"
        )
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def _root_arch(root: ET.Element) -> str:
    os_element = root.find("os")
    if os_element is None:
        return ""
    type_element = os_element.find("type")
    return (type_element.get("arch", "") if type_element is not None else "")


def _serialize(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def ensure_suspend_disabled(xml: str) -> str:
    """Disable ACPI S3 sleep on an x86_64 domain.

    A guest that enters S3 can leave the QEMU display scanout inactive after
    wake; keyboard and pointer input then do nothing until the guest resumes.
    Deep sleep has no practical value in a lab VM, so v-EMU keeps S3 disabled.
    Hibernate (S4) is left alone: it powers the domain off and resumes with a
    normal boot.

    libvirt rejects the ``pm`` element on ARM/RISC-V ("setting ACPI S3/S4 not
    supported"), so non-x86 domains are returned unchanged.
    """
    root = ET.fromstring(xml)
    if _root_arch(root) != "x86_64":
        return xml
    pm = root.find("pm")
    if pm is None:
        pm = ET.Element("pm")
        devices = root.find("devices")
        index = list(root).index(devices) if devices is not None else len(root)
        root.insert(index, pm)
    suspend = pm.find("suspend-to-mem")
    if suspend is None:
        suspend = ET.SubElement(pm, "suspend-to-mem")
    changed = suspend.get("enabled") != "no"
    suspend.set("enabled", "no")
    # libvirt disables S4 by default when <pm> is present. Make the intent
    # explicit so hibernate keeps working for callers that do not set it.
    disk = pm.find("suspend-to-disk")
    if disk is None:
        disk = ET.SubElement(pm, "suspend-to-disk")
        disk.set("enabled", "yes")
        changed = True
    if not changed:
        return xml
    return _serialize(root)


def ensure_tcg_tuning(xml: str) -> str:
    """Add the TCG translation cache and a virtio RNG to emulated guests.

    libvirt exposes the TCG cache as ``<features><tcg><tb-cache>`` which maps
    to QEMU's ``-accel tcg,tb-size=...``. Without it Windows/WinPE boots make
    the default cache thrash. Older libvirt versions reject the element, so
    callers retry without it on failure.
    """
    root = ET.fromstring(xml)
    arch = _root_arch(root)
    if not arch or arch == "x86_64":
        return xml
    features = root.find("features")
    if features is None:
        features = ET.Element("features")
        os_element = root.find("os")
        index = list(root).index(os_element) + 1 if os_element is not None else 0
        root.insert(index, features)
    tcg = features.find("tcg")
    if tcg is None:
        tcg = ET.SubElement(features, "tcg")
    cache = tcg.find("tb-cache")
    if cache is None:
        cache = ET.SubElement(tcg, "tb-cache")
    cache.set("unit", "MiB")
    cache.text = str(TCG_TB_CACHE_MIB)

    devices = root.find("devices")
    if devices is not None and devices.find("rng") is None:
        rng = ET.SubElement(devices, "rng", model="virtio")
        ET.SubElement(rng, "backend", model="random").text = "/dev/urandom"
    return _serialize(root)


def promote_installed_disk(xml: str) -> str:
    """Make the installed hard disk the only boot candidate.

    Mirrors Cockpit's boot order editor: the hard disk gets an explicit boot
    order while CD-ROM entries lose theirs. The installer media stays
    attached (and can be re-enabled), but the firmware boots the OS on the
    disk after an installation.
    """
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        return xml
    disks: list[ET.Element] = []
    cdroms_with_boot: list[ET.Element] = []
    for disk in devices.findall("disk"):
        if disk.get("device") == "cdrom":
            if disk.find("boot") is not None:
                cdroms_with_boot.append(disk)
            continue
        if disk.get("device", "disk") == "disk":
            disks.append(disk)
    if not disks or not cdroms_with_boot:
        return xml

    def order_key(disk: ET.Element) -> int:
        boot = disk.find("boot")
        try:
            return int(boot.get("order", "999")) if boot is not None else 999
        except ValueError:
            return 999

    bootable = [disk for disk in disks if disk.find("boot") is not None]
    ordered = sorted(bootable or disks[:1], key=order_key)
    for index, disk in enumerate(ordered, start=1):
        boot = disk.find("boot")
        if boot is None:
            boot = ET.SubElement(disk, "boot")
        boot.set("order", str(index))
    for cdrom in cdroms_with_boot:
        boot = cdrom.find("boot")
        if boot is not None:
            cdrom.remove(boot)
    return _serialize(root)


def ensure_nvme_on_root_port(xml: str) -> str:
    """Move NVMe controllers off the PCIe-to-PCI bridge (ARM/RISC-V).

    libvirt may place ``nvme`` behind ``pcie-pci-bridge`` on the ``virt``
    machine. Windows' stornvme driver expects a PCIe device and does not bind
    to it, so WinPE reports no disk. Pin the controller to a free PCIe root
    port and drop the now-unused legacy bridge.
    """
    root = ET.fromstring(xml)
    if _root_arch(root) not in {"aarch64", "riscv64"}:
        return xml
    devices = root.find("devices")
    if devices is None:
        return xml
    controllers = devices.findall("controller")
    nvme = [item for item in controllers if item.get("type") == "nvme"]
    bridges = [
        item
        for item in controllers
        if item.get("model") == "pcie-to-pci-bridge"
    ]
    root_ports = [
        item
        for item in controllers
        if item.get("model") == "pcie-root-port"
    ]
    if not nvme or not bridges or not root_ports:
        return xml

    def pci_bus(element: ET.Element) -> int | None:
        address = element.find("address")
        if address is None or address.get("type") != "pci":
            return None
        try:
            return int(address.get("bus", "0"), 16)
        except ValueError:
            return None

    root_indices = set()
    for port in root_ports:
        try:
            root_indices.add(int(port.get("index", "0")))
        except ValueError:
            continue
    occupied = set()
    for element in list(devices):
        if element in nvme or element in bridges:
            continue
        bus = pci_bus(element)
        if bus is not None and bus != 0:
            occupied.add(bus)
    bridge_buses = {
        bus for bus in (pci_bus(item) for item in bridges) if bus is not None
    }
    occupied |= bridge_buses
    free = [
        index
        for index in sorted(root_indices)
        if index != 0 and index not in occupied
    ]
    if not free:
        return xml
    target_bus = free[0]

    # Only remove the bridge when every device behind it is an NVMe (libvirt
    # puts the NVMe controller there alone on the virt machine).
    stray = {
        bus
        for bus in (pci_bus(item) for item in list(devices))
        if bus is not None and bus not in root_indices and bus != 0
    }
    nvme_buses = {bus for bus in (pci_bus(item) for item in nvme) if bus}
    if not stray <= nvme_buses:
        return xml

    for item in nvme:
        address = item.find("address")
        if address is None:
            address = ET.SubElement(item, "address")
        address.set("type", "pci")
        address.set("domain", "0x0000")
        address.set("bus", f"0x{target_bus:02x}")
        address.set("slot", "0x00")
        address.set("function", "0x0")
    for bridge in bridges:
        devices.remove(bridge)
    return _serialize(root)


def add_usb_controller(xml: str) -> str:
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        raise MutationError("Domain XML has no <devices>.")
    if devices.find("controller[@type='usb']") is not None:
        raise Conflict("A USB controller is already present.")
    ET.SubElement(
        devices, "controller", type="usb", index="0", model="qemu-xhci"
    )
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def add_audio(xml: str) -> str:
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        raise MutationError("Domain XML has no <devices>.")
    if devices.find("sound") is not None:
        raise Conflict("An audio device is already present.")
    os_type = root.find("os/type")
    machine = os_type.get("machine", "") if os_type is not None else ""
    model = "ich9" if "q35" in machine else "ac97"
    ET.SubElement(devices, "sound", model=model)
    if devices.find("audio") is None:
        ET.SubElement(devices, "audio", id="1", type="none")
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def remove_audio(xml: str) -> str:
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        raise MutationError("Domain XML has no <devices>.")
    sound = devices.find("sound")
    if sound is None:
        raise NotFound("No audio device is present.")
    devices.remove(sound)
    for audio in devices.findall("audio"):
        devices.remove(audio)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def add_virtiofs_share(
    xml: str,
    tag: str,
    source: str,
    *,
    read_only: bool = False,
) -> str:
    if not tag or not source:
        raise ValidationError("Share tag and source directory are required.")
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        raise MutationError("Domain XML has no <devices>.")
    for filesystem in devices.findall("filesystem"):
        target = filesystem.find("target")
        if target is not None and target.get("dir") == tag:
            raise Conflict(f"A VirtioFS share named '{tag}' already exists.")
    _ensure_virtiofs_memory_backing(root)
    filesystem = ET.SubElement(
        devices, "filesystem", type="mount", accessmode="passthrough"
    )
    ET.SubElement(filesystem, "driver", type="virtiofs")
    ET.SubElement(filesystem, "source", dir=str(source))
    ET.SubElement(filesystem, "target", dir=tag)
    if read_only:
        ET.SubElement(filesystem, "readonly")
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def remove_virtiofs_share(xml: str, tag: str) -> tuple[str, dict[str, Any]]:
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        raise MutationError("Domain XML has no <devices>.")
    found = None
    for filesystem in devices.findall("filesystem"):
        target = filesystem.find("target")
        if target is not None and target.get("dir") == tag:
            found = filesystem
            break
    if found is None:
        raise NotFound(f"VirtioFS share '{tag}' was not found.")
    devices.remove(found)
    ET.indent(root, space="  ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(root, encoding="unicode"),
        {"tag": tag},
    )


def _ensure_virtiofs_memory_backing(root: ET.Element) -> None:
    existing = root.find("memoryBacking")
    if existing is not None:
        source = existing.find("source")
        access = existing.find("access")
        if (
            source is not None
            and source.get("type") == "memfd"
            and access is not None
            and access.get("mode") == "shared"
        ):
            return
        raise Conflict(
            "VirtioFS needs <memoryBacking> with memfd/shared; this VM "
            "already uses a different memory backing configuration."
        )
    backing = ET.Element("memoryBacking")
    ET.SubElement(backing, "source", type="memfd")
    ET.SubElement(backing, "access", mode="shared")
    index = 0
    for position, child in enumerate(list(root)):
        if child.tag in {"memory", "currentMemory"}:
            index = position + 1
    root.insert(index, backing)


def parse_capabilities(
    domain_capabilities_xml: str,
    capabilities_xml: str | None = None,
    *,
    arch: str = "x86_64",
) -> dict[str, Any]:
    root = ET.fromstring(domain_capabilities_xml)
    disk_buses = _enum_values(root.find("devices/disk"), "bus")
    video_models = _enum_values(root.find("devices/video"), "modelType")
    cpu_modes = [
        mode.get("name", "")
        for mode in root.findall("cpu/mode")
        if mode.get("name") and mode.get("supported") != "no"
    ]
    machines: list[str] = []
    arches: list[dict[str, Any]] = []
    if capabilities_xml:
        caps = ET.fromstring(capabilities_xml)
        for arch_element in caps.findall("./guest/arch"):
            name = arch_element.get("name", "")
            if not name:
                continue
            arch_machines = [
                machine.text.strip()
                for machine in arch_element.findall("machine")
                if machine.text and machine.text.strip()
            ]
            arches.append(
                {
                    "name": name,
                    "domain_types": [
                        item.get("type", "")
                        for item in arch_element.findall("domain")
                    ],
                    "machines": arch_machines,
                }
            )
            if name == arch:
                machines = arch_machines
    disk_buses = [item for item in disk_buses if item in DISK_BUSES]
    video_models = [item for item in video_models if item in VIDEO_MODELS]
    allowed_cpu = set(cpu_modes_for_arch(arch))
    cpu_modes = [
        item for item in cpu_modes if item in CPU_MODES and item in allowed_cpu
    ]
    return {
        "arch": arch,
        "disk_buses": disk_buses or sorted(DISK_BUSES),
        "video_models": video_models or sorted(VIDEO_MODELS),
        "cpu_modes": cpu_modes or list(cpu_modes_for_arch(arch)),
        "machines": machines,
        "arches": arches,
        "nic_models": sorted(NIC_MODELS),
    }


def _enum_values(parent: ET.Element | None, name: str) -> list[str]:
    if parent is None:
        return []
    enum = parent.find(f"enum[@name='{name}']")
    if enum is None:
        return []
    return [
        (value.text or "").strip()
        for value in enum.findall("value")
        if value.text and value.text.strip()
    ]


def diff_summary(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    for key in (
        "memory_mib",
        "memory_max_mib",
        "vcpus",
        "vcpus_max",
        "cpu_mode",
        "machine",
        "video",
    ):
        if before.get(key) != after.get(key):
            changes.append(f"{key}: {before.get(key)} → {after.get(key)}")
    before_disk_buses = {item.get("bus") for item in before.get("disks", [])}
    after_disk_buses = {item.get("bus") for item in after.get("disks", [])}
    if before_disk_buses != after_disk_buses:
        changes.append(
            "disk bus: "
            f"{','.join(sorted(before_disk_buses))} → "
            f"{','.join(sorted(after_disk_buses))}"
        )
    before_nics = {item.get("model") for item in before.get("nics", [])}
    after_nics = {item.get("model") for item in after.get("nics", [])}
    if before_nics != after_nics:
        changes.append(
            "nic model: "
            f"{','.join(sorted(before_nics))} → "
            f"{','.join(sorted(after_nics))}"
        )
    return changes


def add_serial_console(xml: str) -> str:
    """Add a pty serial+console pair to a domain XML definition."""
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        raise MutationError("Domain XML has no <devices>.")
    if devices.find("serial") is not None or devices.find("console") is not None:
        return xml
    os_type = root.find("os/type")
    arch = os_type.get("arch", "x86_64") if os_type is not None else "x86_64"
    serial = ET.SubElement(devices, "serial", type="pty")
    target = {"port": "0"}
    if arch == "x86_64":
        target["type"] = "isa-serial"
    ET.SubElement(serial, "target", **target)
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


MANAGEMENT_NS = "https://vemu.local/management"
ET.register_namespace("vemu", MANAGEMENT_NS)


def set_management_metadata(
    xml: str,
    *,
    mode: str,
    managed_by: str,
    origin: str,
    backup: str | None = None,
    os_type: str | None = None,
) -> str:
    """Set/replace the v-EMU management marker without touching other metadata."""
    root = ET.fromstring(xml)
    metadata = root.find("metadata")
    if metadata is None:
        metadata = ET.SubElement(root, "metadata")
    previous = metadata.find(f"{{{MANAGEMENT_NS}}}management")
    old_baseline = previous.get("baseline") if previous is not None else None
    old_os_type = previous.get("os_type") if previous is not None else None
    for old in list(metadata.findall(f"{{{MANAGEMENT_NS}}}management")):
        metadata.remove(old)
    attrs = {
        "managed_by": managed_by,
        "mode": mode,
        "origin": origin,
    }
    if backup:
        attrs["backup"] = backup
    if old_baseline:
        attrs["baseline"] = old_baseline
    if os_type in {"windows", "linux", "hypervisor", "generic"}:
        attrs["os_type"] = os_type
    elif old_os_type:
        attrs["os_type"] = old_os_type
    ET.SubElement(metadata, f"{{{MANAGEMENT_NS}}}management", attrs)
    ET.indent(root, space="  ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(root, encoding="unicode")
    )


def remove_virtiofs_shares(xml: str) -> tuple[str, list[str]]:
    """Remove all VirtioFS filesystems and their shared-memory backing."""
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is None:
        raise MutationError("Domain XML has no <devices>.")
    tags: list[str] = []
    changed = False
    for filesystem in list(devices.findall("filesystem")):
        driver = filesystem.find("driver")
        if driver is None or driver.get("type") != "virtiofs":
            continue
        target = filesystem.find("target")
        tags.append(target.get("dir", "") if target is not None else "")
        devices.remove(filesystem)
        changed = True
    if changed:
        backing = root.find("memoryBacking")
        if backing is not None:
            source = backing.find("source")
            access = backing.find("access")
            if (
                source is not None
                and source.get("type") == "memfd"
                and access is not None
                and access.get("mode") == "shared"
            ):
                root.remove(backing)
        ET.indent(root, space="  ")
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(root, encoding="unicode")
        )
    return xml, [tag for tag in tags if tag]
