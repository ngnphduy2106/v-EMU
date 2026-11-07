from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from vemu.domain import Disk, DomainSpec, Network, random_mac
from vemu.errors import ValidationError


class DomainTests(unittest.TestCase):
    def test_random_mac_format(self) -> None:
        mac = random_mac()
        self.assertRegex(mac, r"^52:54:00:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$")

    def test_xml(self) -> None:
        spec = DomainSpec(
            name="sds-1",
            memory_mib=4096,
            vcpus=2,
            disks=[Disk(path="/tmp/disk-0001.qcow2")],
            networks=[
                Network(network="default", mac="52:54:00:aa:bb:cc")
            ],
            firmware="efi",
        )
        root = ET.fromstring(spec.to_xml())
        self.assertEqual(root.tag, "domain")
        self.assertEqual(root.get("type"), "kvm")
        self.assertEqual(root.findtext("name"), "sds-1")
        self.assertEqual(root.findtext("memory"), "4096")
        self.assertEqual(root.findtext("vcpu"), "2")
        os_element = root.find("os")
        self.assertIsNotNone(os_element)
        self.assertEqual(os_element.get("firmware"), "efi")
        self.assertEqual(root.find("os/type").get("arch"), "x86_64")

        disk = root.find("./devices/disk")
        self.assertIsNotNone(disk)
        self.assertEqual(disk.find("source").get("file"), "/tmp/disk-0001.qcow2")
        self.assertEqual(disk.find("target").get("bus"), "virtio")

        interface = root.find("./devices/interface")
        self.assertIsNotNone(interface)
        self.assertEqual(interface.find("mac").get("address"), "52:54:00:aa:bb:cc")

        graphics = root.find("./devices/graphics")
        self.assertIsNotNone(graphics)
        self.assertEqual(graphics.get("listen"), "127.0.0.1")
        self.assertEqual(graphics.get("websocket"), "-1")

        channel = root.find("./devices/channel")
        self.assertIsNotNone(channel)
        self.assertEqual(
            channel.find("target").get("name"), "org.qemu.guest_agent.0"
        )

        self.assertEqual(
            root.find("pm/suspend-to-mem").get("enabled"), "no"
        )
        self.assertEqual(
            root.find("pm/suspend-to-disk").get("enabled"), "yes"
        )

    def test_arm_has_no_pm_element(self) -> None:
        spec = DomainSpec(
            name="arm-1",
            arch="aarch64",
            machine="virt",
            cpu_mode="maximum",
            firmware="efi",
            disks=[Disk(path="/tmp/disk-arm.qcow2")],
        )
        root = ET.fromstring(spec.to_xml())
        self.assertIsNone(root.find("pm"))

    def test_x86_identity_is_fixed_and_unique(self) -> None:
        first = DomainSpec(
            name="vm1",
            uuid="11111111-2222-3333-4444-555555555555",
            disks=[Disk(path="/tmp/one.qcow2")],
        )
        second = DomainSpec(
            name="vm2",
            disks=[Disk(path="/tmp/two.qcow2")],
        )
        root = ET.fromstring(first.to_xml())
        other = ET.fromstring(second.to_xml())

        self.assertEqual(
            root.findtext("uuid"), "11111111-2222-3333-4444-555555555555"
        )
        self.assertNotEqual(root.findtext("uuid"), other.findtext("uuid"))
        self.assertEqual(root.find("os/smbios").get("mode"), "sysinfo")
        sysinfo = root.find("sysinfo")
        self.assertIsNotNone(sysinfo)
        self.assertEqual(sysinfo.get("type"), "smbios")
        self.assertEqual(
            sysinfo.findtext("./bios/entry[@name='vendor']"),
            "CORESYSTEM VN",
        )
        self.assertEqual(
            sysinfo.findtext("./system/entry[@name='manufacturer']"),
            "CORESYSTEM VN",
        )
        self.assertEqual(
            sysinfo.findtext("./system/entry[@name='product']"), "vEMU"
        )
        self.assertEqual(
            sysinfo.findtext("./system/entry[@name='version']"), "1.0"
        )
        self.assertEqual(
            sysinfo.findtext("./bios/entry[@name='version']"), "vEMU 1.0"
        )
        self.assertEqual(
            sysinfo.findtext("./system/entry[@name='serial']"),
            "VEMU-11111111",
        )
        self.assertEqual(
            sysinfo.findtext("./baseBoard/entry[@name='manufacturer']"),
            "CORESYSTEM VN",
        )
        self.assertEqual(
            sysinfo.findtext("./chassis/entry[@name='manufacturer']"),
            "CORESYSTEM VN",
        )
        oem = [
            item.text for item in sysinfo.findall("./oemStrings/entry")
        ]
        self.assertIn("ManagedBy=CORESYSTEM VN", oem)
        self.assertRegex(
            sysinfo.findtext("./bios/entry[@name='date']"),
            r"^\d{2}/\d{2}/\d{4}$",
        )

    def test_smbios_serial_uses_short_uppercase_uuid_prefix(self) -> None:
        from vemu.system_identity import serial

        self.assertEqual(
            serial("847665e5-f64d-4851-ad58-4795f9c44e4b"),
            "VEMU-847665E5",
        )

    def test_arm64_has_no_smbios_identity(self) -> None:
        spec = DomainSpec(
            name="arm1",
            arch="aarch64",
            machine="virt",
            cpu_mode="maximum",
            disks=[Disk(path="/tmp/x.qcow2")],
        )
        root = ET.fromstring(spec.to_xml())
        self.assertIsNone(root.find("sysinfo"))
        self.assertIsNone(root.find("os/smbios"))

    def test_windows_hyperv_and_video_memory(self) -> None:
        spec = DomainSpec(
            name="win1",
            disks=[Disk(path="/tmp/x.qcow2")],
            video="vga",
            video_memory_mib=64,
            hyperv=True,
            serial_console=True,
        )
        root = ET.fromstring(spec.to_xml())
        hyperv = root.find("./features/hyperv")
        self.assertIsNotNone(hyperv)
        self.assertEqual(hyperv.get("mode"), "custom")
        for feature in (
            "relaxed",
            "vapic",
            "vpindex",
            "runtime",
            "synic",
            "stimer",
            "frequencies",
            "tlbflush",
            "ipi",
            "spinlocks",
        ):
            self.assertIsNotNone(hyperv.find(feature))
        self.assertIsNotNone(root.find("./clock/timer[@name='hypervclock']"))
        self.assertEqual(
            root.find("./devices/video/model").get("vram"), "65536"
        )
        self.assertIsNotNone(root.find("./devices/serial"))
        self.assertIsNotNone(root.find("./devices/console"))

    def test_video_memory_requires_vga_or_qxl(self) -> None:
        with self.assertRaises(ValidationError):
            DomainSpec(
                name="vm1",
                disks=[Disk(path="/tmp/x.qcow2")],
                video="virtio",
                video_memory_mib=64,
            ).to_xml()
        qxl = DomainSpec(
            name="vm1",
            disks=[Disk(path="/tmp/x.qcow2")],
            video="qxl",
            video_memory_mib=128,
        )
        model = ET.fromstring(qxl.to_xml()).find("./devices/video/model")
        self.assertEqual(model.get("vgamem"), "131072")
        self.assertIsNone(model.get("vram"))

    def test_vemu_domain_has_management_metadata(self) -> None:
        spec = DomainSpec(
            name="vm1",
            disks=[Disk(path="/tmp/x.qcow2")],
        )
        root = ET.fromstring(spec.to_xml())
        metadata = root.find(
            ".//{https://vemu.local/management}management"
        )
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.get("managed_by"), "v-EMU (full)")
        self.assertEqual(metadata.get("os_type"), "generic")

    def test_empty_cdrom_has_no_source(self) -> None:
        spec = DomainSpec(
            name="vm1",
            disks=[
                Disk(path="/tmp/x.qcow2"),
                Disk(
                    path="",
                    bus="sata",
                    target="sdb",
                    device="cdrom",
                    format="raw",
                    discard="",
                ),
            ],
        )
        root = ET.fromstring(spec.to_xml())
        cdrom = root.find("./devices/disk[@device='cdrom']")
        self.assertIsNotNone(cdrom)
        self.assertIsNone(cdrom.find("source"))
        self.assertEqual(cdrom.find("target").get("dev"), "sdb")

    def test_arm64_adds_usb_keyboard(self) -> None:
        spec = DomainSpec(
            name="arm1",
            arch="aarch64",
            machine="virt",
            cpu_mode="maximum",
            disks=[Disk(path="/tmp/x.qcow2")],
        )
        root = ET.fromstring(spec.to_xml())
        keyboards = root.findall("./devices/input[@type='keyboard']")
        self.assertEqual(len(keyboards), 1)
        self.assertEqual(keyboards[0].get("bus"), "usb")

    def test_arm64_adds_tcg_cache_and_rng(self) -> None:
        spec = DomainSpec(
            name="arm1",
            arch="aarch64",
            machine="virt",
            cpu_mode="maximum",
            disks=[Disk(path="/tmp/x.qcow2")],
        )
        root = ET.fromstring(spec.to_xml())
        cache = root.find("./features/tcg/tb-cache")
        self.assertIsNotNone(cache)
        self.assertEqual(cache.get("unit"), "MiB")
        self.assertEqual(cache.text, "2048")
        self.assertEqual(root.find("./devices/rng").get("model"), "virtio")
        self.assertEqual(
            root.findtext("./devices/rng/backend"), "/dev/urandom"
        )

    def test_x86_has_no_tcg_tuning(self) -> None:
        spec = DomainSpec(
            name="x86",
            disks=[Disk(path="/tmp/x.qcow2")],
        )
        root = ET.fromstring(spec.to_xml())
        self.assertIsNone(root.find("./features/tcg"))
        self.assertIsNone(root.find("./devices/rng"))

    def test_generated_mac_is_used(self) -> None:
        network = Network(network="default")
        mac = network.resolved_mac()
        self.assertRegex(mac, r"^52:54:00:")
        self.assertEqual(network.resolved_mac(), mac)

    def test_invalid_name(self) -> None:
        with self.assertRaises(ValidationError):
            DomainSpec(name="-bad", disks=[Disk(path="/tmp/x.qcow2")]).to_xml()
        with self.assertRaises(ValidationError):
            DomainSpec(name="bad name", disks=[Disk(path="/tmp/x.qcow2")]).to_xml()

    def test_requires_disk(self) -> None:
        with self.assertRaises(ValidationError):
            DomainSpec(name="vm1").to_xml()

    def test_nvme_supported(self) -> None:
        spec = DomainSpec(
            name="vm1",
            machine="q35",
            disks=[Disk(path="/tmp/x.qcow2", bus="nvme", target="nvme0n1")],
        )
        root = ET.fromstring(spec.to_xml())
        target = root.find("./devices/disk/target")
        self.assertEqual(target.get("bus"), "nvme")
        self.assertEqual(target.get("dev"), "nvme0n1")
        controller = root.find("./devices/controller[@type='nvme']")
        self.assertIsNotNone(controller)
        self.assertEqual(controller.findtext("serial"), "VEMU-NVME-0")

    def test_nvme_requires_q35(self) -> None:
        with self.assertRaises(ValidationError):
            DomainSpec(
                name="vm1",
                machine="pc",
                disks=[Disk(path="/tmp/x.qcow2", bus="nvme", target="nvme0n1")],
            ).to_xml()

    def test_tpm_device(self) -> None:
        spec = DomainSpec(
            name="win11",
            machine="q35",
            firmware="efi",
            tpm=True,
            disks=[Disk(path="/tmp/x.qcow2")],
        )
        root = ET.fromstring(spec.to_xml())
        tpm = root.find("./devices/tpm")
        self.assertIsNotNone(tpm)
        self.assertEqual(tpm.get("model"), "tpm-crb")
        backend = tpm.find("backend")
        self.assertEqual(backend.get("type"), "emulator")
        self.assertEqual(backend.get("version"), "2.0")

    def test_invalid_mac(self) -> None:
        with self.assertRaises(ValidationError):
            DomainSpec(
                name="vm1",
                disks=[Disk(path="/tmp/x.qcow2")],
                networks=[Network(network="default", mac="not-a-mac")],
            ).to_xml()

    def test_low_memory(self) -> None:
        with self.assertRaises(ValidationError):
            DomainSpec(
                name="vm1",
                memory_mib=128,
                disks=[Disk(path="/tmp/x.qcow2")],
            ).to_xml()

    def test_multi_disk_has_one_boot_disk_and_scsi_controller(self) -> None:
        spec = DomainSpec(
            name="multi",
            disks=[
                Disk(path="/tmp/disk-1.qcow2", bus="scsi", target="sda"),
                Disk(path="/tmp/disk-2.qcow2", bus="scsi", target="sdb"),
                Disk(path="/tmp/disk-3.qcow2", bus="scsi", target="sdc"),
            ],
        )
        root = ET.fromstring(spec.to_xml())
        boots = root.findall("./devices/disk/boot")
        self.assertEqual(len(boots), 1)
        self.assertEqual(boots[0].get("order"), "1")
        controller = root.find("./devices/controller[@type='scsi']")
        self.assertIsNotNone(controller)
        self.assertEqual(controller.get("model"), "lsilogic")


if __name__ == "__main__":
    unittest.main()
