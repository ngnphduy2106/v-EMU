from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest import mock

from vemu import service
from vemu.errors import ValidationError
from vemu.vmconfig import apply_config, parse_config

XML = """<domain type='kvm'>
  <name>vm1</name>
  <uuid>uuid-1</uuid>
  <memory unit='KiB'>2097152</memory>
  <currentMemory unit='KiB'>2097152</currentMemory>
  <vcpu placement='static'>2</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type></os>
  <cpu mode='host-passthrough'/>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/tmp/a.qcow2'/>
      <target dev='vda' bus='virtio'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x04' function='0x0'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='/tmp/install.iso'/>
      <target dev='sda' bus='sata'/>
    </disk>
    <interface type='network'>
      <mac address='52:54:00:aa:bb:cc'/>
      <source network='default'/>
      <model type='virtio'/>
    </interface>
    <video><model type='virtio' vram='16384'/></video>
  </devices>
</domain>"""


class VmConfigTests(unittest.TestCase):
    def test_parse(self) -> None:
        config = parse_config(XML)
        self.assertEqual(config["name"], "vm1")
        self.assertEqual(config["memory_mib"], 2048)
        self.assertEqual(config["vcpus"], 2)
        self.assertEqual(config["cpu_mode"], "host-passthrough")
        self.assertEqual(config["machine"], "q35")
        self.assertEqual(config["firmware"], "bios")
        self.assertEqual(config["video"], "virtio")
        self.assertEqual(config["nics"][0]["model"], "virtio")
        self.assertEqual(config["nics"][0]["link"], "up")
        self.assertEqual(config["nics"][0]["type"], "network")
        self.assertEqual(len(config["disks"]), 2)

    def test_parse_nic_link_and_type(self) -> None:
        xml = XML.replace(
            "<interface type='network'>",
            "<interface type='network'><link state='down'/>",
        )
        nic = parse_config(xml)["nics"][0]
        self.assertEqual(nic["link"], "down")
        self.assertEqual(nic["type"], "network")
        self.assertEqual(nic["bridge"], "")

    def test_parse_ignores_usb_answer_media(self) -> None:
        xml = XML.replace(
            "</devices>",
            "  <disk type='file' device='disk'>\n"
            "    <source file='/tmp/unattend.img'/>\n"
            "    <target dev='sdz' bus='usb' removable='on'/>\n"
            "  </disk>\n"
            "</devices>",
        )
        self.assertEqual(
            [disk["source"] for disk in parse_config(xml)["disks"]],
            [disk["source"] for disk in parse_config(XML)["disks"]],
        )

    def test_parse_keeps_usb_cdrom_media(self) -> None:
        xml = XML.replace(
            "</devices>",
            "  <disk type='file' device='cdrom'>\n"
            "    <source file='/tmp/install.iso'/>\n"
            "    <target dev='sdb' bus='usb'/>\n"
            "    <boot order='1'/>\n"
            "  </disk>\n"
            "</devices>",
        )
        disks = parse_config(xml)["disks"]
        usb = [disk for disk in disks if disk["bus"] == "usb"]
        self.assertEqual(len(usb), 1)
        self.assertEqual(usb[0]["device"], "cdrom")
        self.assertEqual(usb[0]["source"], "/tmp/install.iso")


    def test_parse_video_memory(self) -> None:
        xml = XML.replace(
            "<video><model type='virtio' vram='16384'/></video>",
            "<video><model type='vga' vram='131072'/></video>",
        )
        self.assertEqual(parse_config(xml)["video_memory_mib"], 128)

    def test_apply_video_memory(self) -> None:
        updated = apply_config(
            XML, video="vga", video_memory_mib=256
        )
        model = ET.fromstring(updated).find("./devices/video/model")
        self.assertEqual(model.get("vram"), "262144")
        self.assertEqual(parse_config(updated)["video_memory_mib"], 256)
        updated = apply_config(updated, video="virtio")
        model = ET.fromstring(updated).find("./devices/video/model")
        self.assertIsNone(model.get("vram"))

    def test_parse_and_apply_qxl_video_memory(self) -> None:
        xml = XML.replace(
            "<video><model type='virtio' vram='16384'/></video>",
            "<video><model type='qxl' vgamem='131072' heads='1'/></video>",
        )
        self.assertEqual(parse_config(xml)["video"], "qxl")
        self.assertEqual(parse_config(xml)["video_memory_mib"], 128)
        updated = apply_config(xml, video="qxl", video_memory_mib=64)
        model = ET.fromstring(updated).find("./devices/video/model")
        self.assertEqual(model.get("vgamem"), "65536")
        self.assertIsNone(model.get("vram"))
        self.assertEqual(parse_config(updated)["video_memory_mib"], 64)
        switched = apply_config(updated, video="virtio")
        model = ET.fromstring(switched).find("./devices/video/model")
        self.assertIsNone(model.get("vram"))
        self.assertIsNone(model.get("vgamem"))

    def test_parse_serial_console(self) -> None:
        xml = XML.replace(
            "</devices>",
            "  <serial type='pty'><target port='0'/></serial>\n"
            "  <console type='pty'><target type='serial' port='0'/></console>\n"
            "</devices>",
        )
        self.assertTrue(parse_config(xml)["serial"])
        self.assertFalse(parse_config(XML)["serial"])

    def test_add_serial_console(self) -> None:
        from vemu.vmconfig import add_serial_console

        updated = add_serial_console(XML)
        root = ET.fromstring(updated)
        self.assertIsNotNone(root.find("./devices/serial"))
        self.assertIsNotNone(root.find("./devices/console"))
        self.assertTrue(parse_config(updated)["serial"])
        self.assertEqual(add_serial_console(updated), updated)

    def test_parse_custom_pflash_loader_is_uefi(self) -> None:
        xml = XML.replace(
            "<os><type arch='x86_64' machine='q35'>hvm</type></os>",
            "<os><loader readonly='yes' type='pflash' format='raw'>"
            "/usr/share/vemu/firmware/x64/OVMF_CODE.fd</loader>"
            "<type arch='x86_64' machine='q35'>hvm</type></os>",
        )
        config = parse_config(xml)
        self.assertEqual(config["firmware"], "efi")
        self.assertFalse(config["secure_boot"])

    def test_parse_secure_boot_loader(self) -> None:
        xml = XML.replace(
            "<os><type arch='x86_64' machine='q35'>hvm</type></os>",
            "<os><loader readonly='yes' secure='yes' type='pflash' "
            "format='raw'>/usr/share/vemu/firmware/x64/"
            "OVMF_CODE.secboot.fd</loader>"
            "<type arch='x86_64' machine='q35'>hvm</type></os>",
        ).replace(
            "</os>",
            "</os><features><smm state='on'/></features>",
        )
        config = parse_config(xml)
        self.assertEqual(config["firmware"], "efi")
        self.assertTrue(config["secure_boot"])

    def test_apply_core(self) -> None:
        updated = apply_config(
            XML,
            memory_mib=4096,
            vcpus=4,
            cpu_mode="host-model",
            video="qxl",
            nic_model="e1000e",
            disk_bus="sata",
        )
        root = ET.fromstring(updated)
        self.assertEqual(root.find("memory").text, "4096")
        self.assertEqual(root.find("memory").get("unit"), "MiB")
        self.assertEqual(root.findtext("vcpu"), "4")
        self.assertEqual(root.find("cpu").get("mode"), "host-model")
        self.assertEqual(root.find("./devices/video/model").get("type"), "qxl")
        self.assertIsNone(root.find("./devices/video/model").get("vram"))
        self.assertEqual(
            root.find("./devices/interface/model").get("type"), "e1000e"
        )
        data_disk = root.find("./devices/disk")
        self.assertEqual(data_disk.find("target").get("bus"), "sata")
        self.assertEqual(data_disk.find("target").get("dev"), "sdb")
        self.assertIsNone(data_disk.find("address"))
        self.assertIsNotNone(root.find("./devices/controller[@type='sata']"))

    def test_apply_usb_disk_bus(self) -> None:
        updated = apply_config(XML, disk_bus="usb")
        root = ET.fromstring(updated)
        data_disk = root.find("./devices/disk")
        self.assertEqual(data_disk.find("target").get("bus"), "usb")
        self.assertEqual(data_disk.find("target").get("dev"), "sdb")
        self.assertIsNotNone(root.find("./devices/controller[@type='usb']"))

    def test_retarget_keeps_usb_answer_media(self) -> None:
        xml = XML.replace(
            "</devices>",
            "  <disk type='file' device='disk' model='usb-storage'>\n"
            "    <driver name='qemu' type='raw'/>\n"
            "    <source file='/tmp/oobe.img'/>\n"
            "    <target dev='sdz' bus='usb' removable='on'/>\n"
            "    <boot order='99'/>\n"
            "  </disk>\n"
            "</devices>",
        )
        root = ET.fromstring(apply_config(xml, disk_bus="nvme"))
        data = next(
            disk
            for disk in root.findall("./devices/disk")
            if disk.find("source") is not None
            and disk.find("source").get("file") == "/tmp/a.qcow2"
        )
        answer = next(
            disk
            for disk in root.findall("./devices/disk")
            if disk.find("source") is not None
            and disk.find("source").get("file") == "/tmp/oobe.img"
        )
        self.assertEqual(data.find("target").get("bus"), "nvme")
        self.assertIsNone(data.find("target").get("removable"))
        self.assertEqual(answer.find("target").get("bus"), "usb")
        self.assertEqual(answer.find("target").get("dev"), "sdz")
        self.assertEqual(answer.find("target").get("removable"), "on")

    def test_ide_on_q35_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            apply_config(XML, disk_bus="ide")

    def test_service_update(self) -> None:
        class FakeBackend:
            def __init__(self) -> None:
                self.xml = XML
                self.updated = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, name):
                return SimpleNamespace(state="running")

            def config_xml(self, name):
                return self.xml

            def redefine_domain(self, name, xml):
                self.updated = xml
                self.xml = xml
                return SimpleNamespace(name=name)

        backend = FakeBackend()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.vm_config_update(
                "vm1", memory_mib=4096, video="qxl"
            )
        self.assertTrue(result["pending_restart"])
        self.assertEqual(result["config"]["memory_mib"], 4096)
        self.assertIn("memory_mib", " ".join(result["changes"]))
        self.assertIn("4096", backend.updated)
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            vcpu_result = service.vm_config_update("vm1", vcpus=3)
        self.assertTrue(vcpu_result["pending_restart"])
        self.assertTrue(
            any("vCPU" in warning for warning in vcpu_result["warnings"])
        )

    def test_memory_balloon_apply_and_service(self) -> None:
        from vemu.errors import VemuError

        updated = apply_config(XML, memory_mib=1024, memory_max_mib=2048)
        root = ET.fromstring(updated)
        self.assertEqual(root.findtext("memory"), "2048")
        self.assertEqual(root.findtext("currentMemory"), "1024")
        parsed = parse_config(updated)
        self.assertEqual(parsed["memory_mib"], 1024)
        self.assertEqual(parsed["memory_max_mib"], 2048)
        with self.assertRaises(ValidationError):
            apply_config(XML, memory_mib=4096, memory_max_mib=2048)

        calls: list[int] = []

        class FakeBackend:
            def __init__(self, fail: bool = False) -> None:
                self.xml = XML
                self.fail = fail

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, name):
                return SimpleNamespace(state="running")

            def config_xml(self, name):
                return self.xml

            def redefine_domain(self, name, xml):
                self.xml = xml
                return SimpleNamespace(name=name)

            def set_memory_live(self, name, memory_mib):
                if self.fail:
                    raise VemuError("balloon missing")
                calls.append(memory_mib)

        backend = FakeBackend()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.vm_config_update("vm1", memory_mib=1024)
        self.assertEqual(calls, [1024])
        self.assertTrue(result["live_memory"])
        self.assertFalse(result["pending_restart"])
        self.assertEqual(result["config"]["memory_mib"], 1024)
        self.assertEqual(result["config"]["memory_max_mib"], 2048)

        failing = FakeBackend(fail=True)
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=failing
        ):
            fallback = service.vm_config_update("vm1", memory_mib=1024)
        self.assertFalse(fallback["live_memory"])
        self.assertTrue(fallback["pending_restart"])
        self.assertTrue(fallback["warnings"])
        self.assertEqual(fallback["config"]["memory_mib"], 1024)
        self.assertEqual(fallback["config"]["memory_max_mib"], 2048)

    def test_vcpu_boot_maximum(self) -> None:
        header = XML.replace(
            "<vcpu placement='static'>2</vcpu>",
            "<vcpu placement='static' current='2'>4</vcpu>",
        )
        parsed = parse_config(header)
        self.assertEqual(parsed["vcpus"], 2)
        self.assertEqual(parsed["vcpus_max"], 4)

        stopped = apply_config(header, vcpus=2, vcpus_max=6)
        root = ET.fromstring(stopped)
        self.assertEqual(root.findtext("vcpu"), "6")
        self.assertEqual(root.find("vcpu").get("current"), "2")

        static_xml = apply_config(XML, vcpus=3)
        static_root = ET.fromstring(static_xml)
        self.assertEqual(static_root.findtext("vcpu"), "3")
        self.assertIsNone(static_root.find("vcpu").get("current"))

        with self.assertRaises(ValidationError):
            apply_config(XML, vcpus=4, vcpus_max=2)

    def test_vcpu_live_service(self) -> None:
        from vemu.errors import VemuError

        header = XML.replace(
            "<vcpu placement='static'>2</vcpu>",
            "<vcpu placement='static' current='2'>4</vcpu>",
        )
        calls: list[int] = []

        class FakeBackend:
            def __init__(self, fail: bool = False) -> None:
                self.xml = header
                self.fail = fail

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, name):
                return SimpleNamespace(state="running")

            def config_xml(self, name):
                return self.xml

            def redefine_domain(self, name, xml):
                self.xml = xml
                return SimpleNamespace(name=name)

            def set_vcpus_live(self, name, vcpus):
                if self.fail:
                    raise VemuError("no vcpu hotplug")
                calls.append(vcpus)

        backend = FakeBackend()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.vm_config_update("vm1", vcpus=4)
        self.assertEqual(calls, [4])
        self.assertTrue(result["live_vcpus"])
        self.assertFalse(result["pending_restart"])
        self.assertEqual(result["config"]["vcpus"], 4)
        self.assertEqual(result["config"]["vcpus_max"], 4)
        self.assertEqual(ET.fromstring(backend.xml).find("vcpu").get("current"), "4")

        failing = FakeBackend(fail=True)
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=failing
        ):
            fallback = service.vm_config_update("vm1", vcpus=4)
        self.assertFalse(fallback["live_vcpus"])
        self.assertTrue(fallback["pending_restart"])
        self.assertTrue(fallback["warnings"])

    def test_vcpu_above_boot_maximum(self) -> None:
        calls: list[int] = []

        class FakeBackend:
            def __init__(self) -> None:
                self.xml = XML

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, name):
                return SimpleNamespace(state="running")

            def config_xml(self, name):
                return self.xml

            def redefine_domain(self, name, xml):
                self.xml = xml
                return SimpleNamespace(name=name)

            def set_vcpus_live(self, name, vcpus):
                calls.append(vcpus)

        backend = FakeBackend()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.vm_config_update("vm1", vcpus=4)
        self.assertEqual(calls, [])
        self.assertFalse(result["live_vcpus"])
        self.assertTrue(result["pending_restart"])
        self.assertTrue(
            any("above the boot maximum" in item for item in result["warnings"])
        )
        self.assertEqual(result["config"]["vcpus_max"], 4)

    def test_service_autostart(self) -> None:
        from vemu.backend import DomainInfo

        calls: list[tuple[str, bool]] = []

        class FakeBackend:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def set_autostart(self, name, enabled):
                calls.append((name, enabled))

            def info(self, name):
                return DomainInfo(name, "uuid", "stopped", [], True, "bios", "v-EMU (full)")

        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeBackend()
        ):
            result = service.domain_set_autostart("vm1", True)
        self.assertEqual(calls, [("vm1", True)])
        self.assertTrue(result["autostart"])

    def test_service_delete_vm(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from vemu.backend import DomainInfo
        from vemu.errors import Conflict

        calls: list[tuple[str, bool]] = []

        class FakeBackend:
            def __init__(self, state: str, disks: list[str]) -> None:
                self.state = state
                self.disks = disks

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, name):
                return DomainInfo(name, "uuid", self.state, self.disks, False, "bios", "v-EMU (full)")

            def undefine(self, name, force=False):
                calls.append((name, force))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = root / "vm1"
            chain.mkdir()
            disk = chain / "disk-0001.qcow2"
            disk.write_bytes(b"disk")
            (chain / "chain.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "base": str(root / "base.qcow2"),
                        "layers": [
                            {
                                "id": "0001",
                                "path": str(disk),
                                "parent": "base",
                            }
                        ],
                        "active": "0001",
                    }
                ),
                encoding="utf-8",
            )
            unmanaged = root / "external.qcow2"
            unmanaged.write_bytes(b"disk")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=FakeBackend(
                    "running", [str(disk), str(unmanaged)]
                ),
            ):
                with self.assertRaises(Conflict):
                    service.domain_delete("vm1")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=FakeBackend(
                    "running", [str(disk), str(unmanaged)]
                ),
            ):
                result = service.domain_delete(
                    "vm1", delete_storage=True, force=True
                )
            self.assertFalse(chain.exists())
            self.assertTrue(unmanaged.is_file())
            self.assertEqual(result["storage_deleted"], [str(chain)])
            self.assertTrue(result["warnings"])
            self.assertEqual(calls, [("vm1", True)])

    def test_delete_vm_cleans_metadata_when_storage_is_deleted(self) -> None:
        import json
        import os
        import tempfile
        from pathlib import Path

        from vemu import drift
        from vemu.backend import DomainInfo
        from vemu.shares import ShareAssignment, ShareStore

        class FakeBackend:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, name):
                return DomainInfo(
                    name, "uuid", "stopped", [disk_path], False, "bios",
                    "v-EMU (full)"
                )

            def undefine(self, name, force=False):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = root / "labs" / "vm1"
            chain.mkdir(parents=True)
            disk_path = chain / "disk-0001.qcow2"
            disk_path.write_bytes(b"disk")
            (chain / "chain.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "base": str(root / "base.qcow2"),
                        "layers": [
                            {"id": "0001", "path": str(disk_path),
                             "parent": "base"}
                        ],
                        "active": "0001",
                    }
                ),
                encoding="utf-8",
            )
            backup_dir = root / "backups" / "vm1"
            backup_dir.mkdir(parents=True)
            (backup_dir / "20260101.xml").write_text("<domain/>")
            baseline_dir = root / "baselines"
            share_config = root / "shares.json"
            store = ShareStore(share_config)
            store.attach(
                ShareAssignment(domain="vm1", share="Data", tag="Data")
            )
            with mock.patch.dict(
                os.environ,
                {
                    "VEMU_BACKUP_DIR": str(root / "backups"),
                    "VEMU_BASELINE_DIR": str(baseline_dir),
                    "VEMU_SHARE_CONFIG": str(share_config),
                },
            ), mock.patch(
                "vemu.service.LibvirtBackend", return_value=FakeBackend()
            ):
                drift.store_baseline("vm1", "<domain/>")
                result = service.domain_delete(
                    "vm1", delete_storage=True
                )
                self.assertFalse(chain.exists())
                self.assertFalse(backup_dir.exists())
                self.assertFalse(drift.baseline_path("vm1").exists())
                self.assertEqual(store.assignments_for("vm1"), [])
            self.assertEqual(result["share_assignments_deleted"], 1)
            self.assertEqual(len(result["metadata_deleted"]), 2)
            self.assertFalse(result["warnings"])

    def test_virtiofs_share(self) -> None:
        from vemu.errors import Conflict
        from vemu.vmconfig import (
            add_virtiofs_share,
            parse_config,
            remove_virtiofs_share,
        )

        updated = add_virtiofs_share(
            XML, "work", "/home/user/share", read_only=True
        )
        root = ET.fromstring(updated)
        backing = root.find("memoryBacking")
        self.assertIsNotNone(backing)
        self.assertEqual(backing.find("source").get("type"), "memfd")
        self.assertEqual(backing.find("access").get("mode"), "shared")
        share = root.find("./devices/filesystem")
        self.assertIsNotNone(share)
        self.assertEqual(share.find("driver").get("type"), "virtiofs")
        self.assertEqual(share.find("source").get("dir"), "/home/user/share")
        self.assertEqual(share.find("target").get("dir"), "work")
        self.assertIsNotNone(share.find("readonly"))
        config = parse_config(updated)
        self.assertEqual(config["shares"][0]["tag"], "work")
        self.assertTrue(config["shares"][0]["read_only"])
        with self.assertRaises(Conflict):
            add_virtiofs_share(updated, "work", "/tmp")
        removed, info = remove_virtiofs_share(updated, "work")
        self.assertEqual(info["tag"], "work")
        self.assertFalse(parse_config(removed)["shares"])

    def test_tpm_and_usb(self) -> None:
        from vemu.vmconfig import (
            add_tpm,
            add_usb_controller,
            parse_config,
            remove_tpm,
        )
        with_tpm = add_tpm(XML)
        config = parse_config(with_tpm)
        self.assertTrue(config["tpm"])
        root = ET.fromstring(with_tpm)
        self.assertEqual(
            root.find("./devices/tpm").get("model"), "tpm-crb"
        )
        self.assertEqual(
            root.find("./devices/tpm/backend").get("version"), "2.0"
        )
        without = parse_config(remove_tpm(with_tpm))
        self.assertFalse(without["tpm"])
        with_usb = add_usb_controller(XML)
        self.assertTrue(parse_config(with_usb)["usb"])
        self.assertIsNotNone(
            ET.fromstring(with_usb).find("./devices/controller[@type='usb']")
        )

    def test_audio(self) -> None:
        from vemu.vmconfig import add_audio, parse_config, remove_audio

        with_audio = add_audio(XML)
        config = parse_config(with_audio)
        self.assertTrue(config["audio"])
        root = ET.fromstring(with_audio)
        self.assertEqual(
            root.find("./devices/sound").get("model"), "ich9"
        )
        self.assertEqual(
            root.find("./devices/audio").get("type"), "none"
        )
        without = parse_config(remove_audio(with_audio))
        self.assertFalse(without["audio"])

    def test_nvme_disk_and_bus_change(self) -> None:
        from vemu.vmconfig import add_disk

        updated, target = add_disk(XML, "/tmp/nvme.qcow2", "nvme")
        self.assertEqual(target, "nvme0n1")
        root = ET.fromstring(updated)
        disk = next(
            item
            for item in root.findall("./devices/disk")
            if item.find("source").get("file") == "/tmp/nvme.qcow2"
        )
        self.assertEqual(disk.find("target").get("bus"), "nvme")
        self.assertEqual(disk.find("target").get("dev"), "nvme0n1")
        controller = root.find("./devices/controller[@type='nvme']")
        self.assertIsNotNone(controller)
        self.assertEqual(controller.findtext("serial"), "VEMU-NVME-0")

        retargeted = apply_config(XML, disk_bus="nvme")
        root = ET.fromstring(retargeted)
        self.assertEqual(
            root.find("./devices/disk/target").get("dev"), "nvme0n1"
        )
        self.assertEqual(
            root.find("./devices/disk/target").get("bus"), "nvme"
        )
        self.assertIsNotNone(
            root.find("./devices/controller[@type='nvme']")
        )

    def test_nvme_requires_q35(self) -> None:
        from vemu.vmconfig import add_disk

        pc_xml = XML.replace("machine='q35'", "machine='pc'")
        with self.assertRaises(ValidationError):
            add_disk(pc_xml, "/tmp/nvme.qcow2", "nvme")
        with self.assertRaises(ValidationError):
            apply_config(pc_xml, disk_bus="nvme")

    def test_cdrom_change_and_eject(self) -> None:
        from vemu.errors import NotFound
        from vemu.vmconfig import (
            cdrom_info,
            find_cdrom,
            set_cdrom_media,
        )

        root = ET.fromstring(XML)
        disk = find_cdrom(root, "sda")
        self.assertEqual(cdrom_info(disk)["source"], "/tmp/install.iso")
        set_cdrom_media(disk, "/tmp/other.iso")
        self.assertEqual(cdrom_info(disk)["source"], "/tmp/other.iso")
        set_cdrom_media(disk, None)
        self.assertIsNone(cdrom_info(disk)["source"])
        with self.assertRaises(NotFound):
            find_cdrom(root, "sdz")

    def test_service_serial_add(self) -> None:
        from vemu.errors import Conflict

        state = {"value": "stopped"}
        redefined: list[str] = []

        class FakeBackend:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, _name):
                return SimpleNamespace(state=state["value"])

            def config_xml(self, _name):
                return XML

            def redefine_domain(self, _name, xml):
                redefined.append(xml)

        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeBackend()
        ):
            payload = service.vm_serial_add("vm1")
        self.assertTrue(payload["changed"])
        self.assertTrue(redefined)
        self.assertIn("<serial", redefined[0])
        self.assertIn("<console", redefined[0])

        state["value"] = "running"
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeBackend()
        ), self.assertRaises(Conflict):
            service.vm_serial_add("vm1")

    def test_service_media_change_eject(self) -> None:
        import tempfile
        from pathlib import Path

        from vemu.errors import NotFound

        calls: list[tuple] = []

        class FakeBackend:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, name):
                return SimpleNamespace(
                    state="running", managed_by="v-EMU (full)"
                )

            def config_xml(self, name):
                return XML

            def update_media(self, name, path, target=None, *, force=False):
                calls.append((name, path, target, force))
                return {
                    "target": target or "sda",
                    "bus": "sata",
                    "format": "raw",
                    "source": path,
                    "ejected": path is None,
                    "live": True,
                }

        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "new.iso"
            media.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=FakeBackend()
            ):
                changed = service.vm_media_change(
                    "vm1", str(media), target="sda"
                )
                forced_change = service.vm_media_change(
                    "vm1", str(media), target="sda", force=True
                )
                ejected = service.vm_media_eject("vm1", target="sda")
                forced = service.vm_media_eject(
                    "vm1", target="sda", force=True
                )
                listed = service.vm_media_list("vm1")
                with self.assertRaises(NotFound):
                    service.vm_media_change("vm1", str(Path(tmp) / "nope.iso"))
        self.assertEqual(changed["media"], str(media.resolve()))
        self.assertFalse(changed["pending_restart"])
        self.assertTrue(ejected["ejected"])
        self.assertTrue(forced_change["force"])
        self.assertTrue(forced["force"])
        self.assertEqual(calls[0][1], str(media.resolve()))
        self.assertEqual(calls[1][3], True)
        self.assertEqual(calls[2][3], False)
        self.assertEqual(calls[3][3], True)
        self.assertEqual(len(listed["cdroms"]), 1)
        self.assertEqual(listed["cdroms"][0]["target"], "sda")

    def test_service_disk_resize(self) -> None:
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        qemu_img = shutil.which("qemu-img")
        if not qemu_img:
            self.skipTest("qemu-img not installed")

        calls: list[int] = []

        class FakeBackend:
            def __init__(self, state: str) -> None:
                self.state = state

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, name):
                return SimpleNamespace(state=self.state)

            def config_xml(self, name):
                return XML.replace("/tmp/a.qcow2", str(disk))

            def block_resize(self, name, target, size_bytes):
                calls.append(size_bytes)

        with tempfile.TemporaryDirectory() as tmp:
            disk = Path(tmp) / "a.qcow2"
            subprocess.run(
                [qemu_img, "create", "-f", "qcow2", str(disk), "1G"],
                check=True,
                capture_output=True,
            )
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=FakeBackend("stopped"),
            ):
                offline = service.vm_disk_resize("vm1", str(disk), 2)
            self.assertTrue(offline["changed"])
            self.assertFalse(offline["live"])
            self.assertEqual(offline["old_size_gb"], 1)
            self.assertEqual(offline["new_size_gb"], 2)
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=FakeBackend("running"),
            ):
                online = service.vm_disk_resize("vm1", str(disk), 3)
            self.assertTrue(online["live"])
            self.assertEqual(calls, [3 * 1024**3])
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=FakeBackend("stopped"),
            ):
                with self.assertRaises(ValidationError):
                    service.vm_disk_resize("vm1", str(disk), 1)

    def test_parse_devices(self) -> None:
        from vemu.domain import Disk, DomainSpec
        from vemu.vmconfig import parse_config

        spec = DomainSpec(
            name="vm1",
            memory_mib=1024,
            vcpus=2,
            machine="q35",
            disks=[
                Disk(
                    path="/tmp/a.qcow2",
                    bus="sata",
                    target="sda",
                    boot=2,
                )
            ],
            guest_agent=True,
        )
        config = parse_config(spec.to_xml())
        controller_types = {
            item["type"] for item in config["controllers"]
        }
        self.assertIn("sata", controller_types)
        self.assertIn("usb", controller_types)
        self.assertEqual(config["disks"][0]["boot"], 2)
        device_types = {
            item["type"] for item in config["other_devices"]
        }
        for expected in ("channel", "input", "memballoon", "graphics"):
            self.assertIn(expected, device_types)
        channel = next(
            item
            for item in config["other_devices"]
            if item["type"] == "channel"
        )
        self.assertIn("org.qemu.guest_agent.0", channel["detail"])

    def test_add_remove_disk(self) -> None:
        from vemu.errors import Conflict
        from vemu.vmconfig import add_disk, remove_disk

        updated, target = add_disk(XML, "/tmp/b.qcow2", "virtio")
        self.assertEqual(target, "vdb")
        root = ET.fromstring(updated)
        sources = [
            disk.find("source").get("file")
            for disk in root.findall("./devices/disk")
        ]
        self.assertIn("/tmp/b.qcow2", sources)

        removed, info = remove_disk(updated, "/tmp/b.qcow2")
        self.assertEqual(info["target"], "vdb")
        self.assertNotIn(
            "/tmp/b.qcow2",
            [
                disk.find("source").get("file")
                for disk in ET.fromstring(removed).findall("./devices/disk")
            ],
        )
        with self.assertRaises(Conflict):
            remove_disk(removed, "/tmp/a.qcow2")

    def test_add_disk_reserves_chain_targets(self) -> None:
        from vemu.vmconfig import add_disk

        updated, target = add_disk(
            XML,
            "/tmp/c.qcow2",
            "sata",
            reserved_targets={"sda", "sdb"},
        )
        self.assertEqual(target, "sdc")
        self.assertIn(
            "/tmp/c.qcow2",
            [
                disk.find("source").get("file")
                for disk in ET.fromstring(updated).findall("./devices/disk")
            ],
        )

    def test_add_disk_ide_q35_rejected(self) -> None:
        from vemu.vmconfig import add_disk

        with self.assertRaises(ValidationError):
            add_disk(XML, "/tmp/b.qcow2", "ide")

    def test_service_disk_add_remove(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path

        class FakeBackend:
            def __init__(self) -> None:
                self.xml = XML

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, name):
                return SimpleNamespace(state="stopped")

            def config_xml(self, name):
                return self.xml

            def redefine_domain(self, name, xml):
                self.xml = xml
                return SimpleNamespace(name=name)

        backend = FakeBackend()
        if not shutil.which("qemu-img"):
            self.skipTest("qemu-img not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "extra.qcow2"
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ):
                added = service.vm_disk_add(
                    "vm1", str(path), size_gb=1, bus="virtio"
                )
                self.assertTrue(path.is_file())
                self.assertEqual(added["disk"]["target"], "vdb")
                removed = service.vm_disk_remove(
                    "vm1", "/tmp/a.qcow2", delete_file=False
                )
                self.assertEqual(removed["disk"]["source"], "/tmp/a.qcow2")
                self.assertTrue(path.is_file())

    def test_parse_capabilities(self) -> None:
        from vemu.vmconfig import parse_capabilities

        domcaps = """<domainCapabilities>
          <devices>
            <disk><enum name='bus'>
              <value>virtio</value><value>sata</value><value>usb</value>
            </enum></disk>
            <video><enum name='modelType'>
              <value>vga</value><value>qxl</value><value>none</value>
            </enum></video>
          </devices>
          <cpu>
            <mode name='host-passthrough' supported='yes'/>
            <mode name='host-model' supported='yes'/>
          </cpu>
        </domainCapabilities>"""
        caps = """<capabilities><guest>
          <arch name='x86_64'>
            <domain type='kvm'/>
            <machine>pc-q35-10.2</machine><machine>pc-i440fx-10.2</machine>
          </arch>
          <arch name='aarch64'>
            <domain type='qemu'/>
            <machine>virt-10.2</machine><machine>virt</machine>
          </arch>
        </guest></capabilities>"""
        parsed = parse_capabilities(domcaps, caps)
        self.assertEqual(parsed["disk_buses"], ["virtio", "sata", "usb"])
        self.assertEqual(parsed["video_models"], ["vga", "qxl"])
        self.assertEqual(
            parsed["cpu_modes"], ["host-passthrough", "host-model"]
        )
        self.assertIn("pc-q35-10.2", parsed["machines"])
        self.assertIn("e1000e", parsed["nic_models"])
        self.assertEqual(
            [item["name"] for item in parsed["arches"]],
            ["x86_64", "aarch64"],
        )
        aarch64 = parse_capabilities(domcaps, caps, arch="aarch64")
        self.assertEqual(aarch64["arch"], "aarch64")
        self.assertIn("virt", aarch64["machines"])
        self.assertEqual(aarch64["cpu_modes"], ["maximum"])

    def test_cpu_modes_are_arch_specific(self) -> None:
        with self.assertRaises(ValidationError):
            apply_config(XML, cpu_mode="maximum")
        with self.assertRaises(ValidationError):
            apply_config(XML, cpu_mode="custom")
        arm_xml = XML.replace(
            "<type arch='x86_64' machine='q35'>",
            "<type arch='aarch64' machine='virt'>",
        )
        with self.assertRaises(ValidationError):
            apply_config(arm_xml, cpu_mode="host-passthrough")
        updated = apply_config(arm_xml, cpu_mode="maximum")
        self.assertEqual(
            parse_config(updated)["cpu_mode"], "maximum"
        )

    def test_cli_parser(self) -> None:
        from vemu.cli import build_parser

        parser = build_parser()
        show = parser.parse_args(["config", "show", "--domain", "vm1"])
        self.assertEqual(show.func.__name__, "_cmd_config_show")
        update = parser.parse_args(
            [
                "config",
                "set",
                "--domain",
                "vm1",
                "--memory",
                "4096",
                "--video",
                "qxl",
                "--disk-bus",
                "sata",
            ]
        )
        self.assertEqual(update.func.__name__, "_cmd_config_set")
        self.assertEqual(update.memory, 4096)
        self.assertEqual(update.disk_bus, "sata")
        disk_add = parser.parse_args(
            [
                "config",
                "disk-add",
                "--domain",
                "vm1",
                "--path",
                "/tmp/extra.qcow2",
            ]
        )
        self.assertEqual(disk_add.func.__name__, "_cmd_config_disk_add")
        disk_remove = parser.parse_args(
            [
                "config",
                "disk-remove",
                "--domain",
                "vm1",
                "--source",
                "/tmp/extra.qcow2",
            ]
        )
        self.assertEqual(
            disk_remove.func.__name__, "_cmd_config_disk_remove"
        )
        caps = parser.parse_args(["config", "caps", "--machine", "q35"])
        self.assertEqual(caps.func.__name__, "_cmd_config_caps")
        media_list = parser.parse_args(
            ["media", "list", "--domain", "vm1"]
        )
        self.assertEqual(media_list.func.__name__, "_cmd_media_list")
        media_change = parser.parse_args(
            [
                "media",
                "change",
                "--domain",
                "vm1",
                "--path",
                "/tmp/new.iso",
                "--target",
                "sda",
            ]
        )
        self.assertEqual(media_change.func.__name__, "_cmd_media_change")
        media_eject = parser.parse_args(
            ["media", "eject", "--domain", "vm1", "--force"]
        )
        self.assertEqual(media_eject.func.__name__, "_cmd_media_eject")
        self.assertTrue(media_eject.force)
        resize = parser.parse_args(
            [
                "config",
                "disk-resize",
                "--domain",
                "vm1",
                "--source",
                "/tmp/extra.qcow2",
                "--size",
                "20",
            ]
        )
        self.assertEqual(resize.func.__name__, "_cmd_config_disk_resize")
        self.assertEqual(resize.size, 20)
        autostart = parser.parse_args(
            ["autostart", "--domain", "vm1", "--off"]
        )
        self.assertEqual(autostart.func.__name__, "_cmd_autostart")
        self.assertTrue(autostart.off)
        stats = parser.parse_args(["stats", "--domain", "vm1"])
        self.assertEqual(stats.func.__name__, "_cmd_stats")
        delete = parser.parse_args(
            ["delete", "--domain", "vm1", "--delete-storage", "--force"]
        )
        self.assertEqual(delete.func.__name__, "_cmd_delete_vm")
        self.assertTrue(delete.delete_storage)
        self.assertTrue(delete.force)
        tpm = parser.parse_args(["config", "tpm-add", "--domain", "vm1"])
        self.assertEqual(tpm.func.__name__, "_cmd_config_tpm_add")
        usb = parser.parse_args(["config", "usb-add", "--domain", "vm1"])
        self.assertEqual(usb.func.__name__, "_cmd_config_usb_add")
        audio = parser.parse_args(["config", "audio-add", "--domain", "vm1"])
        self.assertEqual(audio.func.__name__, "_cmd_config_audio_add")

    def test_doctor_accepts_rhel_qemu_kvm_path(self) -> None:
        from vemu import diagnose

        with mock.patch(
            "vemu.diagnose.shutil.which", return_value=None
        ), mock.patch(
            "vemu.diagnose.Path.is_file", return_value=True
        ):
            self.assertEqual(
                diagnose._qemu_system_x86(), "/usr/libexec/qemu-kvm"
            )

    def test_tool_lookup_includes_sbin(self) -> None:
        import tempfile
        from pathlib import Path

        from vemu import tools

        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "example-tool"
            tool.write_text("#!/bin/sh\n", encoding="utf-8")
            tool.chmod(0o755)
            with mock.patch(
                "vemu.tools.shutil.which", return_value=None
            ), mock.patch.object(tools, "_EXTRA_DIRS", (tmp,)):
                self.assertEqual(tools.which("example-tool"), str(tool))
                self.assertIsNone(tools.which("missing-tool"))

    def test_bootmenu_xml(self) -> None:
        from vemu.backend import LibvirtBackend

        backend = LibvirtBackend.__new__(LibvirtBackend)
        xml = (
            "<domain><os><type arch='x86_64' machine='q35'>hvm</type>"
            "</os><devices/></domain>"
        )
        enabled = backend._bootmenu_xml(xml, True, 5000)
        root = ET.fromstring(enabled)
        bootmenu = root.find("os/bootmenu")
        self.assertIsNotNone(bootmenu)
        self.assertEqual(bootmenu.get("enable"), "yes")
        self.assertEqual(bootmenu.get("timeout"), "5000")
        disabled = backend._bootmenu_xml(enabled, False, 5000)
        self.assertIsNone(ET.fromstring(disabled).find("os/bootmenu"))

    def test_usb_attached_set(self) -> None:
        from vemu.backend import LibvirtBackend

        xml = """<domain><devices>
          <hostdev mode='subsystem' type='usb' managed='yes'>
            <source><vendor id='0x046D'/><product id='0xC077'/></source>
          </hostdev>
        </devices></domain>"""
        self.assertEqual(
            LibvirtBackend._usb_attached_set(xml),
            {("0x046d", "0xc077")},
        )

    def test_ensure_usb_keyboard(self) -> None:
        from vemu.vmconfig import ensure_usb_keyboard

        xml = (
            "<domain><devices>"
            "<input type='tablet' bus='usb'/>"
            "</devices></domain>"
        )
        updated = ensure_usb_keyboard(xml)
        root = ET.fromstring(updated)
        keyboards = root.findall("./devices/input[@type='keyboard']")
        self.assertEqual(len(keyboards), 1)
        self.assertEqual(keyboards[0].get("bus"), "usb")
        self.assertIsNotNone(root.find("./devices/controller[@type='usb']"))
        self.assertEqual(ensure_usb_keyboard(updated), updated)
        ps2 = "<domain><devices><input type='keyboard' bus='ps2'/></devices></domain>"
        self.assertEqual(ensure_usb_keyboard(ps2), ps2)

    def test_ensure_tcg_tuning(self) -> None:
        from vemu.vmconfig import ensure_tcg_tuning

        xml = (
            "<domain><os><type arch='aarch64' machine='virt'/></os>"
            "<features><acpi/></features><devices/></domain>"
        )
        updated = ensure_tcg_tuning(xml)
        root = ET.fromstring(updated)
        cache = root.find("./features/tcg/tb-cache")
        self.assertIsNotNone(cache)
        self.assertEqual(cache.get("unit"), "MiB")
        self.assertEqual(cache.text, "2048")
        self.assertEqual(root.find("./devices/rng").get("model"), "virtio")
        self.assertEqual(ensure_tcg_tuning(updated), updated)
        x86 = (
            "<domain><os><type arch='x86_64' machine='q35'/></os>"
            "<devices/></domain>"
        )
        self.assertEqual(ensure_tcg_tuning(x86), x86)

    def test_ensure_suspend_disabled(self) -> None:
        from vemu.vmconfig import ensure_suspend_disabled

        xml = (
            "<domain><os><type arch='x86_64' machine='q35'/></os>"
            "<features><acpi/></features><devices/></domain>"
        )
        updated = ensure_suspend_disabled(xml)
        root = ET.fromstring(updated)
        self.assertEqual(root.find("pm/suspend-to-mem").get("enabled"), "no")
        self.assertEqual(root.find("pm/suspend-to-disk").get("enabled"), "yes")
        tags = [child.tag for child in root]
        self.assertLess(tags.index("pm"), tags.index("devices"))
        self.assertEqual(ensure_suspend_disabled(updated), updated)

        existing = (
            "<domain><os><type arch='x86_64' machine='q35'/></os>"
            "<pm><suspend-to-mem enabled='yes'/>"
            "<suspend-to-disk enabled='no'/></pm><devices/></domain>"
        )
        root = ET.fromstring(ensure_suspend_disabled(existing))
        self.assertEqual(root.find("pm/suspend-to-mem").get("enabled"), "no")
        self.assertEqual(root.find("pm/suspend-to-disk").get("enabled"), "no")

        arm = (
            "<domain><os><type arch='aarch64' machine='virt'/></os>"
            "<features><acpi/></features><devices/></domain>"
        )
        self.assertEqual(ensure_suspend_disabled(arm), arm)

    def test_promote_installed_disk(self) -> None:
        from vemu.vmconfig import promote_installed_disk

        xml = (
            "<domain><devices>"
            "<disk type='file' device='disk'><source file='/d.qcow2'/>"
            "<target dev='vda'/><boot order='2'/></disk>"
            "<disk type='file' device='cdrom'><source file='/i.iso'/>"
            "<target dev='sdb'/><boot order='1'/></disk>"
            "</devices></domain>"
        )
        updated = promote_installed_disk(xml)
        root = ET.fromstring(updated)
        disks = root.findall("./devices/disk")
        self.assertEqual(disks[0].find("boot").get("order"), "1")
        self.assertIsNone(disks[1].find("boot"))
        self.assertEqual(promote_installed_disk(updated), updated)

    def test_promote_disk_when_boot_order_already_duplicates(self) -> None:
        from vemu.vmconfig import promote_installed_disk

        xml = (
            "<domain><devices>"
            "<disk type='file' device='disk'><source file='/d.qcow2'/>"
            "<target dev='vda'/><boot order='1'/></disk>"
            "<disk type='file' device='cdrom'><source file='/i.iso'/>"
            "<target dev='sdb'/><boot order='1'/></disk>"
            "</devices></domain>"
        )
        root = ET.fromstring(promote_installed_disk(xml))
        disks = root.findall("./devices/disk")
        self.assertEqual(disks[0].find("boot").get("order"), "1")
        self.assertIsNone(disks[1].find("boot"))

    def test_installed_disk_accepts_esxi_footprint(self) -> None:
        from vemu.backend import LibvirtBackend

        backend = LibvirtBackend.__new__(LibvirtBackend)
        backend.config_xml = lambda _name: (
            "<domain><devices><disk device='disk'>"
            "<target dev='sda'/></disk></devices></domain>"
        )
        backend.block_info = lambda _name, _dev: {
            "capacity": 128 * 1024**3,
            "allocation": 200704,
            "physical": 200704,
        }
        self.assertEqual(
            backend.installed_disk_target("vm1"),
            None,
        )
        backend.block_info = lambda _name, _dev: {
            "capacity": 128 * 1024**3,
            "allocation": 717 * 1024**2,
            "physical": 717 * 1024**2,
        }
        self.assertEqual(backend.installed_disk_target("vm1"), "sda")

    def test_eject_cdrom_removes_source_and_boot(self) -> None:
        from vemu.vmconfig import set_cdrom_media

        disk = ET.fromstring(
            "<disk type='file' device='cdrom'>"
            "<source file='/i.iso'/><target dev='sdb' bus='sata'/>"
            "<boot order='1'/></disk>"
        )
        set_cdrom_media(disk, None)
        self.assertIsNone(disk.find("source"))
        self.assertIsNone(disk.find("boot"))

    def test_remove_virtiofs_shares(self) -> None:
        from vemu.vmconfig import remove_virtiofs_shares

        xml = XML.replace(
            "</devices>",
            "  <filesystem type='mount' accessmode='passthrough'>\n"
            "    <driver type='virtiofs'/>\n"
            "    <source dir='/tmp/share'/>\n"
            "    <target dir='Pictures'/>\n"
            "  </filesystem>\n"
            "</devices>",
        ).replace(
            "<vcpu placement='static'>2</vcpu>",
            "<vcpu placement='static'>2</vcpu>\n"
            "  <memoryBacking><source type='memfd'/><access mode='shared'/></memoryBacking>",
        )
        cleaned, tags = remove_virtiofs_shares(xml)
        self.assertEqual(tags, ["Pictures"])
        root = ET.fromstring(cleaned)
        self.assertIsNone(root.find("./devices/filesystem"))
        self.assertIsNone(root.find("memoryBacking"))

    def test_apply_video_ramfb_drops_pci_address(self) -> None:
        xml = XML.replace(
            "<video><model type='virtio' vram='16384'/></video>",
            "<video><model type='virtio' vram='16384'/>"
            "<address type='pci' domain='0x0000' bus='0x00' "
            "slot='0x06' function='0x0'/></video>",
        )
        updated = apply_config(xml, video="ramfb")
        video = ET.fromstring(updated).find("./devices/video")
        self.assertIsNone(video.find("address"))
        self.assertEqual(video.find("model").get("type"), "ramfb")
        self.assertIsNone(video.find("model").get("vram"))


    def test_nvme_moves_to_pcie_root_port(self) -> None:
        from vemu.vmconfig import ensure_nvme_on_root_port

        xml = """<domain>
  <os><type arch="aarch64" machine="virt"/></os>
  <devices>
    <controller type="pci" index="0" model="pcie-root"/>
    <controller type="pci" index="1" model="pcie-root-port">
      <address type="pci" bus="0x00" slot="0x01" function="0x0"/>
    </controller>
    <controller type="pci" index="2" model="pcie-root-port">
      <address type="pci" bus="0x00" slot="0x01" function="0x1"/>
    </controller>
    <controller type="pci" index="3" model="pcie-to-pci-bridge">
      <address type="pci" bus="0x01" slot="0x00" function="0x0"/>
    </controller>
    <controller type="pci" index="4" model="pcie-root-port">
      <address type="pci" bus="0x00" slot="0x01" function="0x2"/>
    </controller>
    <controller type="nvme" index="0">
      <serial>VEMU-NVME-0</serial>
      <address type="pci" bus="0x03" slot="0x01" function="0x0"/>
    </controller>
    <interface type="network">
      <address type="pci" bus="0x02" slot="0x00" function="0x0"/>
    </interface>
  </devices>
</domain>"""
        updated = ensure_nvme_on_root_port(xml)
        root = ET.fromstring(updated)
        nvme = root.find("./devices/controller[@type='nvme']")
        self.assertEqual(nvme.find("address").get("bus"), "0x04")
        self.assertIsNone(
            root.find("./devices/controller[@model='pcie-to-pci-bridge']")
        )
        self.assertEqual(ensure_nvme_on_root_port(xml.replace("aarch64", "x86_64", 1)), xml.replace("aarch64", "x86_64", 1))

    def test_cli_plan_commands(self) -> None:
        from vemu.cli import build_parser

        parser = build_parser()
        init = parser.parse_args(
            ["init", "/tmp/lab.yaml", "--name", "demo", "--force"]
        )
        self.assertEqual(init.func.__name__, "_cmd_init")
        self.assertTrue(init.force)
        ssh = parser.parse_args(
            ["ssh", "node1", "--user", "fedora", "--dry-run"]
        )
        self.assertEqual(ssh.func.__name__, "_cmd_ssh")
        self.assertEqual(ssh.user, "fedora")
        console = parser.parse_args(
            ["console", "node1", "--open", "--json"]
        )
        self.assertEqual(console.func.__name__, "_cmd_console")
        self.assertTrue(console.open)
        reset = parser.parse_args(
            ["reset", "--lab", "lab.yaml", "--vm", "node1", "--force"]
        )
        self.assertEqual(reset.func.__name__, "_cmd_lab_reset")
        export = parser.parse_args(
            [
                "export",
                "--ansible-inventory",
                "--dest",
                "/tmp/inventory.yml",
            ]
        )
        self.assertEqual(export.func.__name__, "_cmd_export")
        self.assertTrue(export.ansible_inventory)
        gc = parser.parse_args(["gc", "/tmp/labs", "--force"])
        self.assertEqual(gc.func.__name__, "_cmd_gc")
        self.assertTrue(gc.force)
        snapshot_create = parser.parse_args(
            [
                "snapshot",
                "create",
                "--domain",
                "node1",
                "--description",
                "before upgrade",
            ]
        )
        self.assertEqual(
            snapshot_create.func.__name__, "_cmd_snapshot_create"
        )
        self.assertEqual(snapshot_create.description, "before upgrade")
        self.assertFalse(hasattr(snapshot_create, "live"))
        snapshot_edit = parser.parse_args(
            [
                "snapshot",
                "edit",
                "--domain",
                "node1",
                "--layer",
                "0002",
                "--description",
                "updated notes",
            ]
        )
        self.assertEqual(
            snapshot_edit.func.__name__, "_cmd_snapshot_edit"
        )
        self.assertEqual(snapshot_edit.description, "updated notes")
        snapshot_list = parser.parse_args(
            ["snapshot", "list", "--domain", "node1"]
        )
        self.assertEqual(
            snapshot_list.func.__name__, "_cmd_snapshot_list"
        )
        legacy = parser.parse_args(
            ["snapshot", "--chain", "/tmp/chain"]
        )
        self.assertEqual(legacy.func.__name__, "_cmd_snapshot")
        template_edit = parser.parse_args(
            [
                "template",
                "edit",
                "--template",
                "/tmp/templates/rocky",
                "--os-family",
                "linux",
                "--no-sysprepped",
            ]
        )
        self.assertEqual(
            template_edit.func.__name__, "_cmd_template_update"
        )
        self.assertEqual(template_edit.os_family, "linux")
        self.assertFalse(template_edit.sysprepped)
        template_import = parser.parse_args(
            [
                "template",
                "import",
                "--from",
                "/tmp/golden.qcow2",
                "--dest",
                "/tmp/templates",
                "--name",
                "golden",
                "--os-family",
                "windows",
                "--sysprepped",
            ]
        )
        self.assertEqual(
            template_import.func.__name__, "_cmd_template_import"
        )
        self.assertEqual(template_import.os_family, "windows")
        self.assertTrue(template_import.sysprepped)
        template_list = parser.parse_args(
            ["template", "list", "--root", "/tmp/templates"]
        )
        self.assertEqual(template_list.func.__name__, "_cmd_templates")
        template_adopt = parser.parse_args(
            [
                "template",
                "adopt",
                "--from",
                "/tmp/base.qcow2",
                "--dest",
                "/tmp/templates",
                "--name",
                "base",
            ]
        )
        self.assertEqual(
            template_adopt.func.__name__, "_cmd_template_import"
        )
        template_build = parser.parse_args(
            [
                "template",
                "build",
                "--domain",
                "node1",
                "--dest",
                "/tmp/templates",
            ]
        )
        self.assertEqual(template_build.func.__name__, "_cmd_seal")
        network_status = parser.parse_args(["net", "status", "--json"])
        self.assertEqual(
            network_status.func.__name__, "_cmd_network_status"
        )
        doctor = parser.parse_args(["doctor", "--repair", "--dry-run"])
        self.assertEqual(doctor.func.__name__, "_cmd_doctor")
        self.assertTrue(doctor.repair)
        self.assertTrue(doctor.dry_run)
        storage_repair = parser.parse_args(
            ["storage", "repair", "--path", "/tmp/store", "--force-selinux"]
        )
        self.assertEqual(
            storage_repair.func.__name__, "_cmd_storage_repair"
        )
        self.assertTrue(storage_repair.force_selinux)


class NicServiceTests(unittest.TestCase):
    def test_nic_link(self) -> None:
        captured: dict = {}

        class FakeBackend:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, _name):
                return SimpleNamespace(managed_by="v-EMU (full)")

            def set_interface_link(self, domain, mac, *, up) -> None:
                captured.update(domain=domain, mac=mac, up=up)

        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeBackend()
        ):
            result = service.vm_nic_link(
                "vm1", "52:54:00:aa:bb:cc", False
            )
        self.assertEqual(
            captured,
            {
                "domain": "vm1",
                "mac": "52:54:00:aa:bb:cc",
                "up": False,
            },
        )
        self.assertEqual(result["link"], "down")

    def test_nic_network_resolves_profile(self) -> None:
        captured: dict = {}

        class FakeStore:
            def __init__(self, _config):
                pass

            def find_libvirt_name(self, _name):
                return "vemu-lab-nat"

        class FakeBackend:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, _name):
                return SimpleNamespace(managed_by="v-EMU (full)")

            def set_interface_network(self, domain, mac, network) -> None:
                captured.update(
                    domain=domain, mac=mac, network=network
                )

        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeBackend()
        ), mock.patch("vemu.service.NetworkStore", FakeStore):
            result = service.vm_nic_network(
                "vm1", "52:54:00:aa:bb:cc", "lab-nat"
            )
        self.assertEqual(captured["network"], "vemu-lab-nat")
        self.assertEqual(result["network"], "lab-nat")
        self.assertEqual(result["libvirt_network"], "vemu-lab-nat")


if __name__ == "__main__":
    unittest.main()
