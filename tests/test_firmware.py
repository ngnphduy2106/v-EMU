from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu import service
from vemu.domain import Disk, DomainSpec
from vemu.firmware import firmware_for, secure_boot_available


class FirmwareTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.spec = DomainSpec(
            name="arm1",
            arch="aarch64",
            machine="virt",
            firmware="efi",
            cpu_mode="maximum",
            disks=[Disk(path="/tmp/x.qcow2")],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _touch(self, relative: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fw")
        return path

    def test_platform_version_is_independent_of_app_version(self) -> None:
        from vemu.firmware import firmware_version

        repo = Path(__file__).resolve().parents[1] / "firmware"
        self.assertEqual(
            (repo / "VERSION").read_text(encoding="utf-8").strip(), "1.0"
        )
        with mock.patch.dict(
            os.environ, {"VEMU_FIRMWARE_DIR": str(repo)}
        ):
            self.assertEqual(firmware_version(), "1.0")

    def test_missing_firmware_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"VEMU_FIRMWARE_DIR": str(self.root)}):
            self.assertIsNone(firmware_for("aarch64"))

    def test_detect_all_arches(self) -> None:
        self._touch("x64/OVMF_CODE.fd")
        self._touch("x64/OVMF_VARS.fd")
        self._touch("aarch64/QEMU_EFI-pflash.raw")
        self._touch("aarch64/vars-template.raw")
        self._touch("riscv64/RISCV_VIRT_CODE-pflash.raw")
        self._touch("riscv64/vars-template.raw")
        with mock.patch.dict(os.environ, {"VEMU_FIRMWARE_DIR": str(self.root)}):
            for arch in ("x86_64", "aarch64", "riscv64"):
                files = firmware_for(arch)
                self.assertIsNotNone(files)
                assert files is not None
                self.assertTrue(files.loader.is_file())
                self.assertTrue(files.nvram_template.is_file())
                self.assertEqual(files.width, 1280)
                self.assertEqual(files.height, 800)
            self.assertIsNone(firmware_for("mips64"))

    def test_detect_secure_boot_firmware(self) -> None:
        self._touch("x64/OVMF_CODE.secboot.fd")
        self._touch("x64/OVMF_VARS.secboot.fd")
        with mock.patch.dict(os.environ, {"VEMU_FIRMWARE_DIR": str(self.root)}):
            self.assertIsNone(firmware_for("x86_64"))
            files = firmware_for("x86_64", secure_boot=True)
            self.assertIsNotNone(files)
            assert files is not None
            self.assertEqual(files.loader.name, "OVMF_CODE.secboot.fd")
            self.assertEqual(files.nvram_template.name, "OVMF_VARS.secboot.fd")
            self.assertTrue(secure_boot_available("x86_64"))
            self.assertFalse(secure_boot_available("aarch64"))

    def test_domain_xml_uses_explicit_loader(self) -> None:
        spec = DomainSpec(
            name="arm1",
            arch="aarch64",
            machine="virt",
            firmware="efi",
            cpu_mode="maximum",
            disks=[Disk(path="/tmp/x.qcow2")],
            loader_path="/usr/share/vemu/firmware/aarch64/QEMU_EFI-pflash.raw",
            nvram_template="/usr/share/vemu/firmware/aarch64/vars-template.raw",
        )
        xml = spec.to_xml()
        self.assertIn("<loader readonly=\"yes\" type=\"pflash\" format=\"raw\">", xml)
        self.assertIn("QEMU_EFI-pflash.raw", xml)
        self.assertIn("template=", xml)
        self.assertNotIn("firmware=\"efi\"", xml)
        self.assertNotIn("secure=\"yes\"", xml)
        self.assertNotIn("<smm", xml)

    def test_secure_boot_xml_enables_smm(self) -> None:
        spec = DomainSpec(
            name="secure1",
            machine="q35",
            firmware="efi",
            disks=[Disk(path="/tmp/x.qcow2")],
            loader_path="/usr/share/vemu/firmware/x64/OVMF_CODE.secboot.fd",
            nvram_template="/usr/share/vemu/firmware/x64/OVMF_VARS.secboot.fd",
            loader_secure=True,
        )
        xml = spec.to_xml()
        self.assertIn("secure=\"yes\"", xml)
        self.assertIn("<smm state=\"on\" />", xml)

    def test_service_applies_firmware_to_new_efi_spec(self) -> None:
        self._touch("aarch64/QEMU_EFI-pflash.raw")
        self._touch("aarch64/vars-template.raw")
        with mock.patch.dict(os.environ, {"VEMU_FIRMWARE_DIR": str(self.root)}):
            self.assertFalse(service._apply_custom_firmware(self.spec))
        self.assertEqual(
            self.spec.loader_path, str(self.root / "aarch64/QEMU_EFI-pflash.raw")
        )
        self.assertEqual(
            self.spec.nvram_template, str(self.root / "aarch64/vars-template.raw")
        )
        self.assertEqual(self.spec.loader_format, "raw")
        self.assertEqual(self.spec.nvram_format, "raw")
        self.assertFalse(self.spec.loader_secure)

    def test_service_applies_secure_boot_firmware(self) -> None:
        self._touch("x64/OVMF_CODE.secboot.fd")
        self._touch("x64/OVMF_VARS.secboot.fd")
        spec = DomainSpec(
            name="secure1",
            machine="q35",
            firmware="efi",
            secure_boot=True,
            disks=[Disk(path="/tmp/x.qcow2")],
        )
        with mock.patch.dict(os.environ, {"VEMU_FIRMWARE_DIR": str(self.root)}):
            self.assertTrue(service._apply_custom_firmware(spec))
        self.assertEqual(
            spec.loader_path, str(self.root / "x64/OVMF_CODE.secboot.fd")
        )
        self.assertEqual(
            spec.nvram_template, str(self.root / "x64/OVMF_VARS.secboot.fd")
        )
        self.assertTrue(spec.loader_secure)
        xml = spec.to_xml()
        self.assertIn("secure=\"yes\"", xml)
        self.assertIn("<smm state=\"on\" />", xml)

    def test_secure_boot_falls_back_to_standard_firmware(self) -> None:
        self._touch("x64/OVMF_CODE.fd")
        self._touch("x64/OVMF_VARS.fd")
        spec = DomainSpec(
            name="secure1",
            machine="q35",
            firmware="efi",
            secure_boot=True,
            disks=[Disk(path="/tmp/x.qcow2")],
        )
        with mock.patch.dict(os.environ, {"VEMU_FIRMWARE_DIR": str(self.root)}):
            self.assertFalse(service._apply_custom_firmware(spec))
        self.assertEqual(spec.loader_path, str(self.root / "x64/OVMF_CODE.fd"))
        self.assertFalse(spec.secure_boot)
        self.assertFalse(spec.loader_secure)

    def test_bios_spec_is_not_touched(self) -> None:
        spec = DomainSpec(
            name="bios1",
            disks=[Disk(path="/tmp/x.qcow2")],
        )
        with mock.patch.dict(os.environ, {"VEMU_FIRMWARE_DIR": str(self.root)}):
            self.assertFalse(service._apply_custom_firmware(spec))
        self.assertIsNone(spec.loader_path)
        self.assertIsNone(spec.nvram_template)
