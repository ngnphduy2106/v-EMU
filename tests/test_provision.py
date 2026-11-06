from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from vemu import service
from vemu.domain import Disk, DomainSpec, Network
from vemu.errors import ValidationError
from vemu.provision import (
    WINDOWS_EDITIONS,
    build_unattend_iso,
    detect_iso_os,
    edition_info,
    render_windows_unattend,
    windows_computer_name,
    write_windows_floppy,
    write_windows_unattend,
)

QEMU_IMG = shutil.which("qemu-img")


class OsDetectionTests(unittest.TestCase):
    def test_windows_server_2025(self) -> None:
        detected = detect_iso_os(
            "/home/ISO/en-us_windows_server_2025_x64_dvd.iso"
        )
        self.assertEqual(detected["family"], "windows")
        self.assertEqual(detected["variant"], "windows-server-2025")
        self.assertTrue(detected["unattend"])

    def test_windows_client_is_manual(self) -> None:
        detected = detect_iso_os("/home/ISO/Win11_24H2_English_x64.iso")
        self.assertEqual(detected["family"], "windows")
        self.assertEqual(detected["variant"], "windows-11")
        self.assertFalse(detected["unattend"])
        detected10 = detect_iso_os("/home/ISO/Win10_22H2_English_x64.iso")
        self.assertEqual(detected10["variant"], "windows-10")

    def test_linux_distributions(self) -> None:
        for name, variant in (
            ("Fedora-Workstation-44.iso", "fedora"),
            ("ubuntu-24.04.2-desktop-amd64.iso", "ubuntu"),
            ("Rocky-9.5-x86_64-dvd.iso", "rocky"),
            ("debian-12.9.0-amd64-netinst.iso", "debian"),
        ):
            detected = detect_iso_os(name)
            self.assertEqual(detected["family"], "linux")
            self.assertEqual(detected["variant"], variant)
            self.assertFalse(detected["unattend"])

    def test_esxi_detection(self) -> None:
        detected = detect_iso_os(
            "VMware-VMvisor-Installer-9.1.1.0.25714478.x86_64.iso"
        )
        self.assertEqual(detected["family"], "hypervisor")
        self.assertEqual(detected["variant"], "esxi-8-9")
        self.assertEqual(detected["label"], "VMware ESXi")
        self.assertFalse(detected["unattend"])

    def test_bsd_and_unknown(self) -> None:
        freebsd = detect_iso_os("FreeBSD-14.2-RELEASE-amd64-dvd1.iso")
        self.assertEqual(freebsd["family"], "other")
        self.assertEqual(freebsd["variant"], "freebsd")
        unknown = detect_iso_os("install.iso")
        self.assertEqual(unknown["family"], "other")
        self.assertEqual(unknown["variant"], "other")
        self.assertFalse(unknown["unattend"])


class WindowsUnattendTests(unittest.TestCase):
    def test_image_indexes(self) -> None:
        self.assertEqual(edition_info("standard")[0], 2)
        self.assertEqual(edition_info("datacenter")[0], 4)
        self.assertEqual(
            edition_info("standard")[1],
            "Windows Server 2025 Standard (Desktop Experience)",
        )
        with self.assertRaises(ValidationError):
            edition_info("nope")
        self.assertEqual(set(WINDOWS_EDITIONS), {
            "standard-core",
            "standard",
            "datacenter-core",
            "datacenter",
        })

    def test_computer_name(self) -> None:
        self.assertEqual(windows_computer_name("win server 1"), "WIN-SERVER-1")
        self.assertEqual(
            windows_computer_name("a-very-long-virtual-machine-name"),
            "A-VERY-LONG-VIR",
        )

    def test_render_unattend(self) -> None:
        xml = render_windows_unattend(
            computer_name="win-lab-1",
            admin_password="Vemu!Lab2026",
            edition="standard",
            virtio_iso=True,
        )
        root = ET.fromstring(xml)
        self.assertEqual(root.tag, "{urn:schemas-microsoft-com:unattend}unattend")
        self.assertIn("/IMAGE/NAME", xml)
        self.assertIn(
            "Windows Server 2025 Standard (Desktop Experience)", xml
        )
        self.assertIn("TVRH6-WHNXV-R9WG3-9XRFY-MY832", xml)
        self.assertIn("<WillShowUI>Never</WillShowUI>", xml)
        self.assertIn("<Active>true</Active>", xml)
        self.assertIn("<InstallToAvailablePartition>false", xml)
        self.assertIn("WIN-LAB-1", xml)
        self.assertIn("Vemu!Lab2026", xml)
        self.assertIn("qemu-ga-x86_64.msi", xml)
        self.assertIn("vioserial", xml)
        self.assertIn("FirstLogonCommands", xml)
        self.assertIn("qemu-ga-install.log", xml)
        self.assertIn("DoNotOpenServerManagerAtLogon", xml)
        self.assertIn("fDenyTSConnections", xml)
        self.assertNotIn("\\\\guest-agent", xml)
        self.assertIn("8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c", xml)
        self.assertIn("powercfg /setactive", xml)
        self.assertIn("monitor-timeout-ac 0", xml)

    def test_short_password_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            render_windows_unattend(
                computer_name="win1", admin_password="short"
            )

    def test_build_unattend_iso(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "unattend"
            source.mkdir()
            (source / "autounattend.xml").write_text(
                render_windows_unattend(computer_name="win1"),
                encoding="utf-8",
            )
            output = build_unattend_iso(source, root / "unattend.iso")
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)

    def test_write_unattend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            iso, xml = write_windows_unattend(
                tmp,
                computer_name="win1",
                admin_password="Vemu!Lab2026",
                edition="standard",
                virtio_iso=False,
            )
            self.assertTrue(iso.is_file())
            self.assertTrue(xml.is_file())
            floppy = write_windows_floppy(tmp, xml)
            self.assertTrue(floppy.is_file())
            self.assertEqual(floppy.stat().st_size, 64 * 1024 * 1024)

    def test_render_oobe(self) -> None:
        from vemu.provision import (
            render_windows_oobe_unattend,
            write_windows_oobe_floppy,
        )

        xml = render_windows_oobe_unattend(
            computer_name="win-clone-7",
            admin_password="Vemu!Lab2026",
        )
        self.assertIn("WIN-CLONE-7", xml)
        self.assertIn("Vemu!Lab2026", xml)
        self.assertIn("<HideEULAPage>true</HideEULAPage>", xml)
        self.assertIn("8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c", xml)
        self.assertIn("monitor-timeout-ac 0", xml)
        self.assertIn("standby-timeout-ac 0", xml)
        with tempfile.TemporaryDirectory() as tmp:
            image = write_windows_oobe_floppy(
                tmp,
                computer_name="win-clone-7",
                admin_password="Vemu!Lab2026",
            )
            self.assertTrue(image.is_file())
            self.assertEqual(image.stat().st_size, 64 * 1024 * 1024)

    def test_high_performance_power_plan_can_be_disabled(self) -> None:
        with mock.patch(
            "vemu.provision.HIGH_PERFORMANCE_POWER_PLAN", False
        ):
            xml = render_windows_unattend(
                computer_name="win1",
                admin_password="Vemu!Lab2026",
                virtio_iso=True,
            )
        self.assertNotIn("8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c", xml)
        self.assertIn("monitor-timeout-ac 0", xml)

    def test_efi_unattend_layout(self) -> None:
        xml = render_windows_unattend(
            computer_name="win-efi",
            admin_password="Vemu!Lab2026",
            firmware="efi",
        )
        self.assertIn("<Type>EFI</Type>", xml)
        self.assertIn("<Type>MSR</Type>", xml)
        self.assertIn("<PartitionID>3</PartitionID>", xml)
        self.assertIn("<Format>FAT32</Format>", xml)

    def test_windows_clone_oobe(self) -> None:
        from vemu.template import Template

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.qcow2"
            subprocess.run(
                [QEMU_IMG, "create", "-f", "qcow2", str(base), "8M"],
                check=True,
                capture_output=True,
            )
            template_dir = root / "templates" / "win-base"
            template_dir.mkdir(parents=True)
            Template(
                name="win-base",
                base=str(base),
                disk_bus="sata",
                video="vga",
                nic_model="e1000e",
                sysprepped=True,
            ).save(template_dir)
            result = service.clone_create(
                str(template_dir),
                str(root / "labs"),
                count=1,
                name="win-clone-{{n}}",
                define=False,
                windows_oobe=True,
                windows_admin_password="Vemu!Lab2026",
            )
            item = result["created"][0]
            self.assertTrue(
                Path(item["windows_oobe_floppy"]).is_file()
            )
            self.assertIsNone(item["cloud_init_iso"])

            result = service.clone_create(
                str(template_dir),
                str(root / "labs-no-oobe"),
                count=1,
                name="win-clone-{{n}}",
                define=False,
            )
            self.assertIsNone(
                result["created"][0]["windows_oobe_floppy"]
            )


class DomainLayoutTests(unittest.TestCase):
    def test_windows_install_layout(self) -> None:
        spec = DomainSpec(
            name="win-1",
            memory_mib=4096,
            vcpus=2,
            disks=[
                Disk(
                    path="/tmp/disk.qcow2",
                    bus="sata",
                    target="sda",
                    boot=2,
                ),
                Disk(
                    path="/tmp/install.iso",
                    bus="sata",
                    target="sdb",
                    device="cdrom",
                    format="raw",
                    discard="",
                    boot=1,
                ),
            ],
            networks=[
                Network(
                    network="default",
                    mac="52:54:00:aa:bb:cc",
                    model="e1000e",
                )
            ],
            video="vga",
        )
        root = ET.fromstring(spec.to_xml())
        self.assertIsNotNone(root.find("./devices/controller[@type='sata']"))
        disks = root.findall("./devices/disk")
        self.assertEqual(len(disks), 2)
        self.assertEqual(disks[0].find("target").get("bus"), "sata")
        self.assertEqual(disks[1].get("device"), "cdrom")
        self.assertEqual(disks[1].find("driver").get("type"), "raw")
        self.assertEqual(disks[1].find("boot").get("order"), "1")
        self.assertEqual(disks[0].find("boot").get("order"), "2")
        self.assertEqual(
            root.find("./devices/interface/model").get("type"), "e1000e"
        )
        self.assertEqual(root.find("./devices/video/model").get("type"), "vga")


class PackerInteropTests(unittest.TestCase):
    @unittest.skipUnless(QEMU_IMG, "qemu-img is required")
    def test_import_export(self) -> None:
        from vemu.errors import Conflict
        from vemu.template import load_template

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "packer.qcow2"
            subprocess.run(
                [QEMU_IMG, "create", "-f", "qcow2", str(source), "8M"],
                check=True,
                capture_output=True,
            )
            imported = service.template_import(
                str(source),
                str(root / "templates"),
                "win-golden",
                description="Packer build",
                disk_bus="sata",
                video="vga",
                nic_model="e1000e",
                sysprepped=True,
            )
            self.assertEqual(imported["name"], "win-golden")
            template = load_template(
                root / "templates" / "win-golden" / "template.json"
            )
            self.assertEqual(template.disk_bus, "sata")
            self.assertEqual(template.video, "vga")
            self.assertEqual(template.nic_model, "e1000e")
            self.assertTrue(template.sysprepped)
            self.assertTrue(template.base_path().is_file())

            with self.assertRaises(Conflict):
                service.template_import(
                    str(source),
                    str(root / "templates"),
                    "win-golden",
                )

            exported = service.template_export(
                str(root / "templates" / "win-golden"),
                str(root / "exported.qcow2"),
            )
            self.assertTrue(Path(exported["path"]).is_file())
            self.assertGreater(exported["size"], 0)

    def test_cli_parser(self) -> None:
        from vemu.cli import build_parser

        parser = build_parser()
        imported = parser.parse_args(
            [
                "template",
                "import",
                "--from",
                "/tmp/golden.qcow2",
                "--dest",
                "/tmp/templates",
                "--name",
                "golden",
                "--disk-bus",
                "sata",
                "--video",
                "vga",
                "--nic-model",
                "e1000e",
            ]
        )
        self.assertEqual(imported.func.__name__, "_cmd_template_import")
        self.assertEqual(imported.disk_bus, "sata")
        exported = parser.parse_args(
            [
                "template",
                "export",
                "--template",
                "/tmp/templates/golden",
                "--dest",
                "/tmp/golden-out.qcow2",
            ]
        )
        self.assertEqual(exported.func.__name__, "_cmd_template_export")


class CreateVmTests(unittest.TestCase):
    def test_hypervisor_create_defaults(self) -> None:
        backend = mock.MagicMock()
        backend.__enter__.return_value = backend
        backend.unique_mac.return_value = "52:54:00:aa:bb:cc"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "VMware-VMvisor-Installer-9.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ):
                result = service.vm_create_from_iso(
                    "esxi-9",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=32,
                    memory_mib=4096,
                    os_type="hypervisor",
                    os_variant="esxi-8-9",
                    firmware="efi",
                    machine="q35",
                    disk_bus="sata",
                    nic_model="e1000e",
                    video="virtio",
                    tpm=True,
                    network="default",
                    network_config=str(root / "networks.json"),
                )
        self.assertTrue(result["defined"])
        self.assertEqual(result["os_type"], "hypervisor")
        self.assertEqual(result["os_variant"], "esxi-8-9")
        self.assertEqual(result["disk_bus"], "sata")
        self.assertEqual(result["nic_model"], "e1000e")
        self.assertEqual(result["video"], "virtio")
        self.assertTrue(result["tpm"])
        xml = backend.define.call_args[0][0]
        self.assertIn('<model type="e1000e"', xml)
        self.assertIn('<model type="virtio"', xml)
        self.assertIn("<tpm", xml)

    def setUp(self) -> None:
        # Keep these tests independent of firmware installed on the host.
        self._firmware_dir = tempfile.TemporaryDirectory()
        self._firmware_env = mock.patch.dict(
            os.environ, {"VEMU_FIRMWARE_DIR": self._firmware_dir.name}
        )
        self._firmware_env.start()

    def tearDown(self) -> None:
        self._firmware_env.stop()
        self._firmware_dir.cleanup()

    def _install_x64_firmware(self, *, standard: bool, secure: bool) -> None:
        directory = Path(self._firmware_dir.name) / "x64"
        directory.mkdir(parents=True, exist_ok=True)
        if standard:
            (directory / "OVMF_CODE.fd").write_bytes(b"std")
            (directory / "OVMF_VARS.fd").write_bytes(b"std-vars")
        if secure:
            (directory / "OVMF_CODE.secboot.fd").write_bytes(b"sec")
            (directory / "OVMF_VARS.secboot.fd").write_bytes(b"sec-vars")

    def test_cli_parser(self) -> None:
        from vemu.cli import build_parser

        args = build_parser().parse_args(
            [
                "create",
                "--name",
                "win-lab",
                "--iso",
                "/home/ISO/server.iso",
                "--dest",
                "/home/VM/vemu",
                "--edition",
                "datacenter",
                "--start",
            ]
        )
        self.assertEqual(args.func.__name__, "_cmd_create")
        manual = build_parser().parse_args(
            [
                "create",
                "--name",
                "bsd-lab",
                "--iso",
                "/home/ISO/FreeBSD.iso",
                "--dest",
                "/home/VM/vemu",
                "--os",
                "other",
                "--os-variant",
                "freebsd",
                "--no-unattend",
            ]
        )
        self.assertEqual(manual.os, "other")
        self.assertEqual(manual.os_variant, "freebsd")
        self.assertTrue(manual.no_unattend)
        arm = build_parser().parse_args(
            [
                "create",
                "--name",
                "arm-lab",
                "--iso",
                "/home/ISO/debian-arm64.iso",
                "--dest",
                "/home/VM/vemu",
                "--arch",
                "aarch64",
                "--os",
                "linux",
            ]
        )
        self.assertEqual(arm.arch, "aarch64")
        win11 = build_parser().parse_args(
            [
                "create",
                "--name",
                "win11-lab",
                "--iso",
                "/home/ISO/Win11.iso",
                "--dest",
                "/home/VM/vemu",
                "--firmware",
                "efi",
                "--tpm",
            ]
        )
        self.assertTrue(win11.tpm)
        self.assertIsNone(win11.secure_boot)
        no_secure = build_parser().parse_args(
            [
                "create",
                "--name",
                "linux-lab",
                "--iso",
                "/home/ISO/archlinux.iso",
                "--dest",
                "/home/VM/vemu",
                "--firmware",
                "efi",
                "--no-secure-boot",
            ]
        )
        self.assertFalse(no_secure.secure_boot)
        self.assertEqual(args.edition, "datacenter")
        self.assertTrue(args.start)
        self.assertEqual(args.disk_size, 40)

        seal = build_parser().parse_args(
            [
                "seal",
                "--domain",
                "win-lab",
                "--dest",
                "/home/VM/templates",
                "--sysprepped",
            ]
        )
        self.assertTrue(seal.sysprepped)

    def test_vm_create_from_iso(self) -> None:
        from vemu import service

        captured = {}

        class FakeBackend:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def unique_mac(self) -> str:
                return "52:54:00:aa:bb:cc"

            def define(self, xml: str) -> None:
                captured["xml"] = xml

            def start(self, name: str) -> None:
                captured["started"] = name

            def undefine(self, name: str, force: bool = False) -> None:
                captured["undefined"] = name

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "install.iso"
            iso.write_bytes(b"iso")
            virtio = root / "virtio-win.iso"
            virtio.write_bytes(b"virtio")
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=FakeBackend()
            ):
                result = service.vm_create_from_iso(
                    "win-lab",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=32,
                    memory_mib=4096,
                    virtio_iso=str(virtio),
                    start=True,
                    network="default",
                    network_config=str(root / "networks.json"),
                )
            self.assertTrue(result["defined"])
            self.assertTrue(result["started"])
            self.assertEqual(result["windows_image_index"], 2)
            self.assertTrue(Path(result["disk"]).is_file())
            self.assertTrue(Path(result["unattend_iso"]).is_file())
            self.assertEqual(captured["started"], "win-lab")
            domain = ET.fromstring(captured["xml"])
            self.assertEqual(len(domain.findall("./devices/disk")), 4)
            usb_targets = domain.findall("./devices/disk/target[@bus='usb']")
            self.assertEqual(len(usb_targets), 1)
            self.assertEqual(usb_targets[0].get("dev"), "sdz")
            self.assertEqual(usb_targets[0].get("removable"), "on")
            self.assertIsNone(
                domain.find("./devices/controller[@type='fdc']")
            )
            self.assertEqual(
                domain.find("./devices/interface/model").get("type"),
                "e1000e",
            )
            self.assertEqual(
                domain.find("./devices/video/model").get("type"), "vga"
            )
            self.assertEqual(result["firmware"], "bios")
            self.assertTrue(result["hyperv"])
            self.assertEqual(result["video_memory_mib"], 64)
            self.assertIsNotNone(domain.find("./features/hyperv"))
            self.assertEqual(
                domain.find("./devices/video/model").get("vram"), "65536"
            )
            self.assertEqual(result["disk_bus"], "sata")
            self.assertEqual(result["nic_model"], "e1000e")

    def test_vm_create_custom_hardware(self) -> None:
        captured = {}

        class FakeBackend:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def unique_mac(self) -> str:
                return "52:54:00:aa:bb:cc"

            def define(self, xml: str) -> None:
                captured["xml"] = xml

            def start(self, name: str) -> None:
                captured["started"] = name

            def undefine(self, name: str, force: bool = False) -> None:
                captured["undefined"] = name

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "linux.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=FakeBackend()
            ):
                result = service.vm_create_from_iso(
                    "linux-efi",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=8,
                    memory_mib=1024,
                    os_type="linux",
                    firmware="efi",
                    machine="q35",
                    disk_bus="virtio",
                    nic_model="virtio",
                    video="virtio",
                    cpu_mode="host-model",
                    start=False,
                )
            self.assertEqual(result["firmware"], "efi")
            self.assertFalse(result["hyperv"])
            self.assertIsNone(result["video_memory_mib"])
            self.assertEqual(result["disk_bus"], "virtio")
            self.assertEqual(result["nic_model"], "virtio")
            domain = ET.fromstring(captured["xml"])
            self.assertIsNone(domain.find("./features/hyperv"))
            self.assertEqual(domain.find("os").get("firmware"), "efi")
            self.assertEqual(
                domain.find("./devices/disk/target").get("bus"), "virtio"
            )
            self.assertEqual(
                domain.find("./devices/interface/model").get("type"), "virtio"
            )
            self.assertEqual(
                domain.find("./devices/video/model").get("type"), "virtio"
            )
            self.assertEqual(domain.find("cpu").get("mode"), "host-model")

    def _fake_backend(self, captured: dict):
        class FakeBackend:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def unique_mac(self) -> str:
                return "52:54:00:aa:bb:cc"

            def define(self, xml: str) -> None:
                captured["xml"] = xml

            def start(self, name: str) -> None:
                captured["started"] = name

            def undefine(self, name: str, force: bool = False) -> None:
                captured["undefined"] = name

        return FakeBackend()

    def test_linux_defaults_to_vga_video(self) -> None:
        captured: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "linux.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=self._fake_backend(captured),
            ):
                result = service.vm_create_from_iso(
                    "linux-vga",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=8,
                    memory_mib=1024,
                    os_type="linux",
                )
        self.assertEqual(result["video"], "vga")
        domain = ET.fromstring(captured["xml"])
        self.assertEqual(
            domain.find("./devices/video/model").get("type"), "vga"
        )

    def test_vm_create_windows_manual_install(self) -> None:
        captured: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "Win11_24H2.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=self._fake_backend(captured),
            ):
                result = service.vm_create_from_iso(
                    "win11-lab",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=40,
                    memory_mib=4096,
                    os_type="windows",
                    os_variant="windows-client",
                    windows_unattend=False,
                )
        self.assertFalse(result["windows_unattend"])
        self.assertIsNone(result["unattend_floppy"])
        self.assertIsNone(result["unattend_iso"])
        self.assertIsNone(result["admin_password"])
        domain = ET.fromstring(captured["xml"])
        self.assertEqual(len(domain.findall("./devices/disk")), 2)
        self.assertEqual(
            domain.find("./devices/disk/target").get("bus"), "sata"
        )
        self.assertTrue(
            any("interactive" in item for item in result["warnings"])
        )

    def test_vm_create_other_os_defaults(self) -> None:
        captured: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "FreeBSD-14.2.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=self._fake_backend(captured),
            ):
                result = service.vm_create_from_iso(
                    "bsd-lab",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=10,
                    memory_mib=1024,
                    os_type="other",
                    os_variant="freebsd",
                )
        self.assertEqual(result["os_type"], "other")
        self.assertEqual(result["os_variant"], "freebsd")
        self.assertFalse(result["windows_unattend"])
        self.assertEqual(result["disk_bus"], "sata")
        self.assertEqual(result["nic_model"], "e1000e")
        self.assertEqual(result["video"], "vga")
        self.assertIsNone(result["unattend_floppy"])
        domain = ET.fromstring(captured["xml"])
        self.assertEqual(len(domain.findall("./devices/disk")), 2)

    def test_windows_other_variant_defaults_to_manual(self) -> None:
        captured: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "Windows_Server_2022.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=self._fake_backend(captured),
            ):
                result = service.vm_create_from_iso(
                    "win2022-lab",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=40,
                    memory_mib=4096,
                    os_type="windows",
                    os_variant="windows-server-other",
                )
        self.assertFalse(result["windows_unattend"])
        self.assertIsNone(result["unattend_floppy"])

    def test_vm_create_nvme_disk(self) -> None:
        captured: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "linux.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=self._fake_backend(captured),
            ):
                result = service.vm_create_from_iso(
                    "nvme-lab",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=8,
                    memory_mib=1024,
                    os_type="linux",
                    disk_bus="nvme",
                    machine="q35",
                )
        self.assertEqual(result["disk_bus"], "nvme")
        domain = ET.fromstring(captured["xml"])
        target = domain.find("./devices/disk/target")
        self.assertEqual(target.get("bus"), "nvme")
        self.assertEqual(target.get("dev"), "nvme0n1")
        self.assertIsNotNone(
            domain.find("./devices/controller[@type='nvme']")
        )

    def test_vm_create_tpm_efi(self) -> None:
        captured: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "win11.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=self._fake_backend(captured),
            ):
                result = service.vm_create_from_iso(
                    "win11-lab",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=64,
                    memory_mib=4096,
                    os_type="windows",
                    os_variant="windows-11",
                    windows_unattend=False,
                    firmware="efi",
                    tpm=True,
                )
        self.assertTrue(result["tpm"])
        self.assertEqual(result["firmware"], "efi")
        domain = ET.fromstring(captured["xml"])
        self.assertEqual(domain.find("./os").get("firmware"), "efi")
        tpm = domain.find("./devices/tpm")
        self.assertIsNotNone(tpm)
        self.assertEqual(tpm.get("model"), "tpm-crb")

    def test_secure_boot_defaults_on_for_x64_efi(self) -> None:
        captured: dict = {}
        self._install_x64_firmware(standard=True, secure=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "linux.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=self._fake_backend(captured),
            ):
                result = service.vm_create_from_iso(
                    "secure-lab",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=8,
                    memory_mib=1024,
                    os_type="linux",
                    firmware="efi",
                    machine="q35",
                )
        self.assertTrue(result["secure_boot"])
        domain = ET.fromstring(captured["xml"])
        loader = domain.find("./os/loader")
        self.assertIsNotNone(loader)
        self.assertEqual(loader.get("secure"), "yes")
        self.assertIn("OVMF_CODE.secboot.fd", loader.text or "")
        self.assertEqual(domain.find("./features/smm").get("state"), "on")

    def test_secure_boot_can_be_disabled(self) -> None:
        captured: dict = {}
        self._install_x64_firmware(standard=True, secure=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "archlinux.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=self._fake_backend(captured),
            ):
                result = service.vm_create_from_iso(
                    "unsigned-lab",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=8,
                    memory_mib=1024,
                    os_type="linux",
                    firmware="efi",
                    machine="q35",
                    secure_boot=False,
                )
        self.assertFalse(result["secure_boot"])
        domain = ET.fromstring(captured["xml"])
        loader = domain.find("./os/loader")
        self.assertIsNotNone(loader)
        self.assertIsNone(loader.get("secure"))
        self.assertIn("OVMF_CODE.fd", loader.text or "")
        self.assertIsNone(domain.find("./features/smm"))

    def test_secure_boot_falls_back_when_firmware_is_missing(self) -> None:
        captured: dict = {}
        self._install_x64_firmware(standard=True, secure=False)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "linux.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=self._fake_backend(captured),
            ):
                result = service.vm_create_from_iso(
                    "fallback-lab",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=8,
                    memory_mib=1024,
                    os_type="linux",
                    firmware="efi",
                    machine="q35",
                )
        self.assertFalse(result["secure_boot"])
        self.assertTrue(
            any("Secure Boot firmware is not installed" in item
                for item in result["warnings"])
        )
        domain = ET.fromstring(captured["xml"])
        self.assertIn("OVMF_CODE.fd", domain.find("./os/loader").text or "")
        self.assertIsNone(domain.find("./features/smm"))

    def test_vm_create_aarch64_has_no_secure_boot(self) -> None:
        captured: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "debian-arm64.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=self._fake_backend(captured),
            ):
                result = service.vm_create_from_iso(
                    "arm-secure",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=8,
                    memory_mib=1024,
                    os_type="linux",
                    arch="aarch64",
                    firmware="efi",
                    secure_boot=True,
                )
        self.assertFalse(result["secure_boot"])
        self.assertFalse(
            any("Secure Boot firmware is not installed" in item
                for item in result["warnings"])
        )
        domain = ET.fromstring(captured["xml"])
        self.assertIsNone(domain.find("./features/smm"))

    def test_vm_create_nvme_requires_q35(self) -> None:
        with self.assertRaises(ValidationError):
            service.vm_create_from_iso(
                "nvme-lab",
                "/tmp/missing.iso",
                "/tmp/vms",
                os_type="linux",
                disk_bus="nvme",
                machine="pc",
            )

    def test_vm_create_aarch64_tcg(self) -> None:
        captured: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "debian-arm64.iso"
            iso.write_bytes(b"iso")
            with mock.patch(
                "vemu.service.LibvirtBackend",
                return_value=self._fake_backend(captured),
            ):
                result = service.vm_create_from_iso(
                    "arm-lab",
                    str(iso),
                    str(root / "vms"),
                    disk_size_gb=8,
                    memory_mib=1024,
                    os_type="linux",
                    arch="aarch64",
                )
        self.assertEqual(result["arch"], "aarch64")
        self.assertEqual(result["domain_type"], "qemu")
        self.assertEqual(result["firmware"], "efi")
        self.assertEqual(result["machine"], "virt")
        self.assertEqual(result["disk_bus"], "virtio")
        self.assertEqual(result["nic_model"], "virtio")
        self.assertEqual(result["cpu_mode"], "maximum")
        self.assertTrue(any("TCG" in item for item in result["warnings"]))
        domain = ET.fromstring(captured["xml"])
        self.assertEqual(domain.get("type"), "qemu")
        self.assertEqual(domain.find("os/type").get("arch"), "aarch64")
        self.assertEqual(domain.find("os/type").get("machine"), "virt")
        self.assertEqual(domain.find("os").get("firmware"), "efi")
        self.assertEqual(domain.find("cpu").get("mode"), "maximum")
        self.assertEqual(
            domain.find("./devices/disk/target").get("bus"), "virtio"
        )

    def test_tpm_warnings_for_cross_arch(self) -> None:
        for arch, expected in (
            ("aarch64", "validated"),
            ("riscv64", "verify"),
        ):
            captured: dict = {}
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                iso = root / f"{arch}.iso"
                iso.write_bytes(b"iso")
                with mock.patch(
                    "vemu.service.LibvirtBackend",
                    return_value=self._fake_backend(captured),
                ):
                    result = service.vm_create_from_iso(
                        f"{arch}-tpm",
                        str(iso),
                        str(root / "vms"),
                        disk_size_gb=8,
                        memory_mib=1024,
                        os_type="linux",
                        arch=arch,
                        firmware="efi",
                        tpm=True,
                    )
            self.assertTrue(
                any(expected in item for item in result["warnings"])
            )

    def test_vm_create_aarch64_rejects_unattend(self) -> None:
        with self.assertRaises(ValidationError):
            service.vm_create_from_iso(
                "arm-win",
                "/tmp/missing.iso",
                "/tmp/vms",
                os_type="windows",
                arch="aarch64",
                windows_unattend=True,
            )

    def test_unattend_rejected_for_non_windows(self) -> None:
        with self.assertRaises(ValidationError):
            service.vm_create_from_iso(
                "linux-lab",
                "/tmp/missing.iso",
                "/tmp/vms",
                os_type="linux",
                windows_unattend=True,
            )

    def test_unattend_rejected_for_non_2025_windows(self) -> None:
        with self.assertRaises(ValidationError):
            service.vm_create_from_iso(
                "win11-lab",
                "/tmp/missing.iso",
                "/tmp/vms",
                os_type="windows",
                os_variant="windows-client",
                windows_unattend=True,
            )


if __name__ == "__main__":
    unittest.main()
