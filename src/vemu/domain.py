"""Libvirt domain XML generation for clones.

The generated domain is intentionally minimal and predictable: q35, virtio
disk/NIC/video, VNC bound to loopback, and a guest-agent channel. Storage and
network policies are added by the daemon in later milestones.
"""

from __future__ import annotations

import random
import re
import uuid as uuidlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import system_identity
from .errors import ValidationError

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}$")
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
DISK_TARGET_PREFIX = {
    "virtio": "vd",
    "sata": "sd",
    "scsi": "sd",
    "ide": "hd",
    "usb": "sd",
}

# Large enough for a Windows/WinPE boot under TCG; the QEMU default is much
# smaller and causes constant translation-cache flushes.
TCG_TB_CACHE_MIB = 2048
MANAGEMENT_NS = "https://vemu.local/management"
ET.register_namespace("vemu", MANAGEMENT_NS)


CPU_MODES_KVM = ("host-passthrough", "host-model")
CPU_MODES_TCG = ("maximum",)


def cpu_modes_for_arch(arch: str) -> tuple[str, ...]:
    """Return the CPU modes that make sense for a KVM or TCG guest."""
    return CPU_MODES_KVM if arch == "x86_64" else CPU_MODES_TCG


def disk_target_name(bus: str, ordinal: int = 0) -> str:
    """Return the libvirt target name for a disk bus and 0-based slot."""
    if bus == "nvme":
        return f"nvme0n{ordinal + 1}"
    try:
        prefix = DISK_TARGET_PREFIX[bus]
    except KeyError as exc:
        raise ValidationError(f"Unsupported disk bus: {bus}") from exc
    return f"{prefix}{chr(ord('a') + ordinal)}"


def random_mac() -> str:
    """Return a QEMU-prefixed, locally unique MAC address."""
    suffix = [random.randrange(0, 256) for _ in range(3)]
    return "52:54:00:%02x:%02x:%02x" % tuple(suffix)


@dataclass
class Disk:
    path: str
    bus: str = "virtio"
    target: str = "vda"
    device: str = "disk"
    format: str = "qcow2"
    discard: str = "unmap"
    boot: int = 0

    def validate(self) -> None:
        if not self.path and self.device not in {"cdrom", "floppy"}:
            raise ValidationError("Disk path must not be empty.")
        if self.bus not in {
            "virtio",
            "sata",
            "scsi",
            "ide",
            "nvme",
            "fdc",
            "usb",
        }:
            raise ValidationError(
                "Disk bus must be virtio, sata, scsi, ide, nvme, fdc or usb."
            )
        if self.device not in {"disk", "cdrom", "floppy"}:
            raise ValidationError(
                "Disk device must be 'disk', 'cdrom' or 'floppy'."
            )
        if self.format not in {"qcow2", "raw"}:
            raise ValidationError("Disk format must be qcow2 or raw.")
        if self.device in {"cdrom", "floppy"} and self.discard:
            self.discard = ""
        if self.device == "floppy" and self.bus != "fdc":
            raise ValidationError("Floppy devices must use the fdc bus.")
        if self.boot < 0:
            raise ValidationError("Disk boot order must be positive.")


@dataclass
class Network:
    network: str = "default"
    model: str = "virtio"
    mac: str | None = None

    def validate(self) -> None:
        if not self.network:
            raise ValidationError("Network name must not be empty.")
        if self.mac is not None and not MAC_RE.match(self.mac.lower()):
            raise ValidationError(f"Invalid MAC address: {self.mac}")

    def resolved_mac(self) -> str:
        if self.mac is None:
            self.mac = random_mac()
        return self.mac.lower()


@dataclass
class DomainSpec:
    name: str
    memory_mib: int = 2048
    vcpus: int = 2
    disks: list[Disk] = field(default_factory=list)
    networks: list[Network] = field(default_factory=list)
    arch: str = "x86_64"
    machine: str = "q35"
    domain_type: str = "kvm"
    firmware: str = "bios"
    cpu_mode: str = "host-passthrough"
    graphics: str = "vnc"
    guest_agent: bool = True
    video: str = "virtio"
    video_memory_mib: int | None = None
    os_type: str = "generic"
    hyperv: bool = False
    serial_console: bool = False
    tpm: bool = False
    secure_boot: bool = False
    loader_secure: bool = False
    loader_path: str | None = None
    loader_format: str = "raw"
    nvram_template: str | None = None
    nvram_format: str = "raw"
    uuid: str | None = None

    def validate(self) -> None:
        if not NAME_RE.match(self.name):
            raise ValidationError(
                "Domain name must start with a letter or digit, contain only "
                "letters, digits, '.', '_', '-', '[', ']', and be at most 64 characters."
            )
        if self.memory_mib < 256:
            raise ValidationError("Memory must be at least 256 MiB.")
        if self.vcpus < 1:
            raise ValidationError("At least one vCPU is required.")
        if not self.disks:
            raise ValidationError("A clone needs at least one disk.")
        if self.uuid is not None:
            try:
                uuidlib.UUID(self.uuid)
            except (ValueError, AttributeError) as exc:
                raise ValidationError(
                    f"Invalid domain UUID: {self.uuid}"
                ) from exc
        if self.firmware not in {"bios", "efi"}:
            raise ValidationError("Firmware must be 'bios' or 'efi'.")
        allowed_modes = cpu_modes_for_arch(self.arch)
        if self.cpu_mode not in allowed_modes:
            raise ValidationError(
                f"CPU mode '{self.cpu_mode}' is not valid for {self.arch}; "
                "use " + " or ".join(allowed_modes) + "."
            )
        if self.graphics not in {"vnc", "none"}:
            raise ValidationError("Graphics must be 'vnc' or 'none'.")
        if self.video not in {
            "virtio",
            "vga",
            "qxl",
            "cirrus",
            "vmvga",
            "bochs",
            "ramfb",
        }:
            raise ValidationError(
                "Video model must be virtio, vga, qxl, cirrus, vmvga, bochs "
                "or ramfb."
            )
        if self.video_memory_mib is not None:
            if self.video not in {"vga", "qxl"}:
                raise ValidationError(
                    "Video memory is only configurable for vga or qxl."
                )
            if self.video_memory_mib not in {64, 128, 256}:
                raise ValidationError(
                    "Video memory must be 64, 128 or 256 MiB."
                )
        for disk in self.disks:
            disk.validate()
        has_nvme = any(disk.bus == "nvme" for disk in self.disks)
        if has_nvme and self.arch == "x86_64" and "q35" not in self.machine:
            raise ValidationError(
                "NVMe disks need the q35 machine (PCIe); use sata, scsi or "
                "virtio with pc/i440fx."
            )
        for network in self.networks:
            network.validate()

    def to_xml(self) -> str:
        self.validate()
        if self.uuid is None:
            self.uuid = str(uuidlib.uuid4())
        domain = ET.Element("domain", type=self.domain_type)

        ET.SubElement(domain, "name").text = self.name
        ET.SubElement(domain, "uuid").text = self.uuid
        metadata = ET.SubElement(domain, "metadata")
        management_attrs = {
            "managed_by": "v-EMU (full)",
            "mode": "full",
            "origin": "vemu",
        }
        if self.os_type in {"windows", "linux", "hypervisor", "generic"}:
            management_attrs["os_type"] = self.os_type
        ET.SubElement(
            metadata,
            f"{{{MANAGEMENT_NS}}}management",
            **management_attrs,
        )
        memory = ET.SubElement(domain, "memory", unit="MiB")
        memory.text = str(self.memory_mib)
        current_memory = ET.SubElement(domain, "currentMemory", unit="MiB")
        current_memory.text = str(self.memory_mib)
        vcpu = ET.SubElement(domain, "vcpu", placement="static")
        vcpu.text = str(self.vcpus)

        if self.arch == "x86_64":
            # The identity is product-owned and intentionally not exposed as
            # a user setting.  Only new x86_64 domains reach this path.
            sysinfo = ET.SubElement(domain, "sysinfo", type="smbios")
            for group, entries in system_identity.smbios_groups(
                system_identity.PLATFORM_VERSION, self.uuid
            ).items():
                group_element = ET.SubElement(sysinfo, group)
                for name, value in entries:
                    ET.SubElement(
                        group_element, "entry", name=name
                    ).text = value
            oem = ET.SubElement(sysinfo, "oemStrings")
            for value in system_identity.oem_strings(
                system_identity.PLATFORM_VERSION
            ):
                ET.SubElement(oem, "entry").text = value

        os_element = ET.SubElement(domain, "os")
        if self.firmware == "efi":
            if self.loader_path and self.nvram_template:
                loader_attrs = {
                    "readonly": "yes",
                    "type": "pflash",
                    "format": self.loader_format,
                }
                if self.loader_secure:
                    loader_attrs["secure"] = "yes"
                ET.SubElement(
                    os_element,
                    "loader",
                    **loader_attrs,
                ).text = self.loader_path
                ET.SubElement(
                    os_element,
                    "nvram",
                    template=self.nvram_template,
                    templateFormat=self.nvram_format,
                    format=self.nvram_format,
                )
            else:
                os_element.set("firmware", "efi")
        ET.SubElement(
            os_element,
            "type",
            arch=self.arch,
            machine=self.machine,
        ).text = "hvm"
        if self.arch == "x86_64":
            ET.SubElement(os_element, "smbios", mode="sysinfo")

        features = ET.SubElement(domain, "features")
        ET.SubElement(features, "acpi")
        ET.SubElement(features, "apic")
        if self.hyperv:
            hyperv = ET.SubElement(features, "hyperv", mode="custom")
            for name in (
                "relaxed",
                "vapic",
                "vpindex",
                "runtime",
                "synic",
                "stimer",
                "frequencies",
                "tlbflush",
                "ipi",
            ):
                ET.SubElement(hyperv, name, state="on")
            ET.SubElement(
                hyperv, "spinlocks", state="on", retries="8191"
            )
        if self.loader_secure:
            # libvirt requires SMM for a pflash loader with secure='yes';
            # OVMF's SMM build then enforces the PK/KEK/db key store.
            ET.SubElement(features, "smm", state="on")
        if self.arch != "x86_64":
            # TCG needs a much larger translation cache for Windows/Linux
            # boots; the default (~32 MiB) thrashes and turns a five minute
            # boot into tens of minutes.
            tcg = ET.SubElement(features, "tcg")
            ET.SubElement(tcg, "tb-cache", unit="MiB").text = str(
                TCG_TB_CACHE_MIB
            )

        ET.SubElement(
            domain,
            "cpu",
            mode=self.cpu_mode,
            check="none" if self.cpu_mode == "host-passthrough" else "partial",
        )

        clock = ET.SubElement(domain, "clock", offset="utc")
        ET.SubElement(clock, "timer", name="rtc", tickpolicy="catchup")
        ET.SubElement(clock, "timer", name="pit", tickpolicy="delay")
        ET.SubElement(clock, "timer", name="hpet", present="no")
        if self.hyperv:
            ET.SubElement(
                clock, "timer", name="hypervclock", present="yes"
            )

        ET.SubElement(domain, "on_poweroff").text = "destroy"
        ET.SubElement(domain, "on_reboot").text = "restart"
        ET.SubElement(domain, "on_crash").text = "restart"

        if self.arch == "x86_64":
            # Deep sleep (ACPI S3) can strand the virtual display after wake,
            # so x86 guests do not offer it. Hibernate (S4) still works; ARM
            # and RISC-V reject the pm element entirely.
            pm = ET.SubElement(domain, "pm")
            ET.SubElement(pm, "suspend-to-mem", enabled="no")
            ET.SubElement(pm, "suspend-to-disk", enabled="yes")

        devices = ET.SubElement(domain, "devices")

        sata_disks = [disk for disk in self.disks if disk.bus == "sata"]
        for index in range((len(sata_disks) + 5) // 6):
            ET.SubElement(
                devices, "controller", type="sata", index=str(index)
            )
        if any(disk.bus == "scsi" for disk in self.disks):
            ET.SubElement(
                devices,
                "controller",
                type="scsi",
                index="0",
                model="lsilogic",
            )
        if any(disk.bus == "fdc" for disk in self.disks):
            ET.SubElement(devices, "controller", type="fdc", index="0")
        if any(disk.bus == "nvme" for disk in self.disks):
            controller = ET.SubElement(
                devices, "controller", type="nvme", index="0"
            )
            ET.SubElement(controller, "serial").text = "VEMU-NVME-0"

        boot_disks = [disk for disk in self.disks if disk.boot]
        for index, disk in enumerate(self.disks):
            disk_element = ET.SubElement(
                devices, "disk", type="file", device=disk.device
            )
            driver_attrs = {"name": "qemu", "type": disk.format}
            if disk.discard and disk.device == "disk":
                driver_attrs["discard"] = disk.discard
            ET.SubElement(disk_element, "driver", **driver_attrs)
            if disk.path:
                ET.SubElement(disk_element, "source", file=disk.path)
            target_attrs = {"dev": disk.target, "bus": disk.bus}
            if disk.bus == "usb":
                target_attrs["removable"] = "on"
            ET.SubElement(disk_element, "target", **target_attrs)
            if disk.boot:
                ET.SubElement(disk_element, "boot", order=str(disk.boot))
            elif (
                not boot_disks
                and index == 0
                and disk.device == "disk"
            ):
                ET.SubElement(disk_element, "boot", order="1")

        for network in self.networks:
            interface = ET.SubElement(devices, "interface", type="network")
            ET.SubElement(interface, "source", network=network.network)
            ET.SubElement(interface, "mac", address=network.resolved_mac())
            ET.SubElement(interface, "model", type=network.model)

        if self.graphics == "vnc":
            ET.SubElement(
                devices,
                "graphics",
                type="vnc",
                port="-1",
                autoport="yes",
                listen="127.0.0.1",
                websocket="-1",
            )

        if self.serial_console:
            serial = ET.SubElement(devices, "serial", type="pty")
            serial_target = {"port": "0"}
            if self.arch == "x86_64":
                serial_target["type"] = "isa-serial"
            ET.SubElement(serial, "target", **serial_target)
            console = ET.SubElement(devices, "console", type="pty")
            ET.SubElement(
                console,
                "target",
                type="serial",
                port="0",
            )

        video = ET.SubElement(devices, "video")
        video_attrs = {
            "type": self.video,
            "heads": "1",
            "primary": "yes",
        }
        if self.video_memory_mib is not None:
            if self.video == "qxl":
                video_attrs["vgamem"] = str(self.video_memory_mib * 1024)
            else:
                video_attrs["vram"] = str(self.video_memory_mib * 1024)
        ET.SubElement(video, "model", **video_attrs)

        if self.arch != "x86_64":
            # Matches the validated ARM64 rig; also keeps TCG boots from
            # stalling while the guest waits for entropy.
            rng = ET.SubElement(devices, "rng", model="virtio")
            ET.SubElement(rng, "backend", model="random").text = "/dev/urandom"

        ET.SubElement(devices, "input", type="tablet", bus="usb")
        if self.arch != "x86_64":
            # ARM/RISC-V virt machines have no PS/2 controller; without a
            # USB keyboard the noVNC console can only move the pointer.
            ET.SubElement(devices, "input", type="keyboard", bus="usb")
        ET.SubElement(
            devices, "controller", type="usb", index="0", model="qemu-xhci"
        )
        ET.SubElement(devices, "memballoon", model="virtio")

        if self.guest_agent:
            channel = ET.SubElement(devices, "channel", type="unix")
            ET.SubElement(
                channel,
                "target",
                type="virtio",
                name="org.qemu.guest_agent.0",
            )

        if self.tpm:
            model = "tpm-crb" if "q35" in self.machine else "tpm-tis"
            tpm = ET.SubElement(devices, "tpm", model=model)
            ET.SubElement(tpm, "backend", type="emulator", version="2.0")

        ET.indent(domain, space="  ")
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
            domain, encoding="unicode"
        )
