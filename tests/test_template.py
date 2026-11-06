from __future__ import annotations

import shutil
import stat
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vemu.backend import DomainInfo
from vemu.errors import Conflict, NotFound, ValidationError
from vemu.template import (
    Template,
    list_templates,
    parse_domain_xml,
    resolve_template,
    seal_domain,
    slugify,
    validate_name,
)

QEMU_IMG = shutil.which("qemu-img")


def _domain_xml(
    disk: Path, *, efi: bool = False, secure: bool = False, tpm: bool = False
) -> str:
    firmware = ' firmware="efi"' if efi else ""
    loader_attrs = ' type="pflash"'
    if secure:
        loader_attrs += ' secure="yes"'
    tpm_xml = (
        "<tpm model='tpm-crb'><backend type='emulator' version='2.0'/></tpm>"
        if tpm
        else ""
    )
    return f"""<domain type='kvm'>
  <name>src</name>
  <memory unit='KiB'>4194304</memory>
  <vcpu placement='static'>3</vcpu>
  <os{firmware}>
    <type arch='x86_64' machine='q35'>hvm</type>
    {f'<loader{loader_attrs}>/usr/share/edk2/ovmf/OVMF_CODE.fd</loader>' if efi else ''}
  </os>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{disk}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <source file='/tmp/install.iso'/>
      <target dev='sda' bus='sata'/>
    </disk>
    {tpm_xml}
  </devices>
</domain>"""


def _multi_domain_xml(first: Path, second: Path) -> str:
    extra = (
        "<disk type='file' device='disk'>"
        "<driver name='qemu' type='qcow2'/>"
        f"<source file='{second}'/>"
        "<target dev='vdb' bus='virtio'/>"
        "</disk>"
    )
    return _domain_xml(first).replace("</devices>", extra + "</devices>")


class FakeBackend:
    def __init__(
        self,
        disk: Path,
        *,
        state: str = "stopped",
        efi: bool = False,
        secure: bool = False,
        tpm: bool = False,
    ) -> None:
        self.disk = disk
        self.state = state
        self.efi = efi
        self.secure = secure
        self.tpm = tpm
        self.updated_disk: str | None = None

    def info(self, _name: str) -> DomainInfo:
        return DomainInfo("src", "uuid", self.state, [str(self.disk)])

    def xml(self, _name: str) -> str:
        return _domain_xml(
            self.disk, efi=self.efi, secure=self.secure, tpm=self.tpm
        )

    def shutdown(self, _name: str, timeout: int = 90, force: bool = False) -> None:
        self.state = "stopped"

    def update_disk_source(
        self, _name: str, new_path: str, old_path: str | None = None
    ) -> None:
        self.updated_disk = new_path


class MultiFakeBackend(FakeBackend):
    def __init__(self, disks: list[Path]) -> None:
        super().__init__(disks[0])
        self.disks = disks

    def info(self, _name: str) -> DomainInfo:
        return DomainInfo(
            "src", "uuid", self.state, [str(d) for d in self.disks]
        )

    def xml(self, _name: str) -> str:
        return _multi_domain_xml(self.disks[0], self.disks[1])


@unittest.skipUnless(QEMU_IMG, "qemu-img is required")
class TemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "source.qcow2"
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", str(self.source), "8M"],
            check=True,
            capture_output=True,
        )
        self.dest = self.root / "templates"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_parse_domain_xml(self) -> None:
        parsed = parse_domain_xml(_domain_xml(self.source))
        self.assertEqual(parsed["arch"], "x86_64")
        self.assertEqual(parsed["machine"], "q35")
        self.assertEqual(parsed["firmware"], "bios")
        self.assertEqual(parsed["memory_mib"], 4096)
        self.assertEqual(parsed["vcpus"], 3)
        self.assertEqual(parsed["disks"], [str(self.source)])
        self.assertEqual(parsed["disk_bus"], "virtio")

    def test_parse_efi(self) -> None:
        parsed = parse_domain_xml(_domain_xml(self.source, efi=True))
        self.assertEqual(parsed["firmware"], "efi")
        self.assertFalse(parsed["secure_boot"])
        self.assertFalse(parsed["tpm"])

    def test_parse_template_video_memory(self) -> None:
        xml = _domain_xml(self.source).replace(
            "</devices>",
            "<video><model type='qxl' vgamem='131072'/></video></devices>",
        )
        parsed = parse_domain_xml(xml)
        self.assertEqual(parsed["video"], "qxl")
        self.assertEqual(parsed["video_memory_mib"], 128)

    def test_parse_secure_boot_and_tpm(self) -> None:
        parsed = parse_domain_xml(
            _domain_xml(self.source, efi=True, secure=True, tpm=True)
        )
        self.assertEqual(parsed["firmware"], "efi")
        self.assertTrue(parsed["secure_boot"])
        self.assertTrue(parsed["tpm"])

    def test_seal_and_resolve(self) -> None:
        backend = FakeBackend(self.source, efi=True)
        template = seal_domain(
            backend, "src", self.dest, "src-base", "test template",
            sysprepped=True,
        )
        base = Path(template.base)
        self.assertTrue(base.is_file())
        self.assertEqual(stat.S_IMODE(base.stat().st_mode), 0o444)
        self.assertEqual(template.firmware, "efi")
        self.assertEqual(template.memory_mib, 4096)
        self.assertTrue(template.sysprepped)
        self.assertEqual(template.os_family, "windows")
        self.assertFalse(template.secure_boot)
        self.assertFalse(template.tpm)

        loaded = resolve_template(self.dest / "src-base")
        self.assertEqual(loaded.name, "src-base")
        self.assertEqual(loaded.base, str(base))

        templates = list_templates(self.dest)
        self.assertEqual([item.name for item in templates], ["src-base"])

    def test_seal_running_requires_shutdown_flag(self) -> None:
        backend = FakeBackend(self.source, state="running")
        with self.assertRaises(Conflict):
            seal_domain(backend, "src", self.dest, "src-base")
        template = seal_domain(
            backend, "src", self.dest, "src-base", shutdown=True
        )
        self.assertTrue(Path(template.base).is_file())

    def test_seal_in_place(self) -> None:
        backend = FakeBackend(self.source)
        template = seal_domain(
            backend, "src", self.dest, "src-inplace", in_place=True
        )
        self.assertTrue(template.in_place)
        self.assertEqual(Path(template.base), self.source.resolve())
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o444)
        self.assertIsNotNone(backend.updated_disk)
        overlay = Path(backend.updated_disk or "")
        self.assertTrue(overlay.is_file())
        self.assertTrue((overlay.parent / "chain.json").is_file())

    def test_seal_rejects_existing_dest(self) -> None:
        backend = FakeBackend(self.source)
        seal_domain(backend, "src", self.dest, "src-base")
        with self.assertRaises(Conflict):
            seal_domain(backend, "src", self.dest, "src-base")

    def test_resolve_bare_file(self) -> None:
        template = resolve_template(self.source)
        self.assertEqual(template.base, str(self.source.resolve()))
        self.assertEqual(template.name, "source")

    def test_template_import_security_metadata(self) -> None:
        from vemu import service

        result = service.template_import(
            str(self.source),
            str(self.dest),
            "golden-secure",
            firmware="efi",
            arch="x86_64",
            video="qxl",
            video_memory_mib=128,
            secure_boot=True,
            tpm=True,
            sysprepped=True,
        )
        self.assertTrue(result["secure_boot"])
        self.assertTrue(result["tpm"])
        self.assertEqual(result["os_family"], "windows")
        self.assertEqual(result["video_memory_mib"], 128)
        loaded = resolve_template(self.dest / "golden-secure")
        self.assertTrue(loaded.secure_boot)
        self.assertTrue(loaded.tpm)

    def test_seal_linux_os_family_and_sysprep_guard(self) -> None:
        backend = FakeBackend(self.source)
        template = seal_domain(
            backend,
            "src",
            self.dest,
            "linux-base",
            os_family="linux",
        )
        self.assertEqual(template.os_family, "linux")
        self.assertFalse(template.sysprepped)
        loaded = resolve_template(self.dest / "linux-base")
        self.assertEqual(loaded.os_family, "linux")
        with self.assertRaises(ValidationError):
            seal_domain(
                backend,
                "src",
                self.dest,
                "bad-linux",
                os_family="linux",
                sysprepped=True,
            )

    def test_template_update_metadata(self) -> None:
        from vemu import service

        template = Template(
            name="edit-me",
            base=str(self.source),
            os_family="windows",
            sysprepped=True,
        )
        template_dir = self.dest / "edit-me"
        template.save(template_dir)
        result = service.template_update(
            str(template_dir),
            os_family="linux",
        )
        self.assertEqual(result["os_family"], "linux")
        self.assertFalse(result["sysprepped"])
        loaded = resolve_template(template_dir)
        self.assertEqual(loaded.os_family, "linux")
        self.assertFalse(loaded.sysprepped)
        result = service.template_update(
            str(template_dir),
            os_family="linux",
            sysprepped=True,
        )
        self.assertFalse(result["sysprepped"])

    def test_failed_identity_reset_removes_fresh_chain(self) -> None:
        from vemu import service

        template = Template(
            name="linux-reset-base",
            base=str(self.source),
            os_family="linux",
        )
        template.save(self.dest / "linux-reset-base")
        with mock.patch.object(
            service,
            "reset_linux",
            side_effect=ValidationError("hostname verification failed"),
        ):
            with self.assertRaises(ValidationError):
                service.clone_create(
                    str(self.dest / "linux-reset-base"),
                    str(self.root / "labs"),
                    name="reset-clone",
                    reset_identity=True,
                )
        self.assertFalse((self.root / "labs" / "reset-clone").exists())

    def test_unsupported_identity_reset_is_skipped_with_warning(self) -> None:
        from vemu import service
        from vemu.identity import IdentityResult

        template = Template(
            name="immutable-base",
            base=str(self.source),
            os_family="linux",
        )
        template.save(self.dest / "immutable-base")
        skipped = IdentityResult(
            disk="",
            hostname="clone",
            machine_id="0" * 32,
            skipped=True,
            reason="Offline identity reset is not supported for immutable distribution.",
        )
        with mock.patch.object(
            service, "reset_linux", return_value=skipped
        ):
            result = service.clone_create(
                str(self.dest / "immutable-base"),
                str(self.root / "labs"),
                name="immutable-clone",
                reset_identity=True,
            )
        self.assertFalse(result["created"][0]["identity_reset"])
        self.assertTrue(result["created"][0]["identity_reset_skipped"])
        self.assertTrue(
            any("immutable distribution" in item for item in result["warnings"])
        )
        self.assertTrue(
            (self.root / "labs" / "immutable-clone").is_dir()
        )

    def test_clone_os_family_gates_cloud_init_and_oobe(self) -> None:
        from vemu import service

        windows = Template(
            name="win-base",
            base=str(self.source),
            os_family="windows",
            sysprepped=True,
        )
        windows.save(self.dest / "win-base")
        with self.assertRaises(ValidationError):
            service.clone_create(
                str(self.dest / "win-base"),
                str(self.root / "labs"),
                cloud_init=True,
                cloud_init_password="secret123",
            )
        linux = Template(
            name="linux-base",
            base=str(self.source),
            os_family="linux",
        )
        linux.save(self.dest / "linux-base")
        with self.assertRaises(ValidationError):
            service.clone_create(
                str(self.dest / "linux-base"),
                str(self.root / "labs"),
                windows_oobe=True,
                windows_admin_password="secret123",
            )

    def test_seal_strips_virtiofs_from_source(self) -> None:
        from vemu import service

        source_xml = _domain_xml(self.source).replace(
            "</devices>",
            "  <filesystem type='mount' accessmode='passthrough'>\n"
            "    <driver type='virtiofs'/>\n"
            "    <source dir='/tmp/share'/>\n"
            "    <target dir='Pictures'/>\n"
            "  </filesystem>\n"
            "</devices>",
        ).replace(
            "<vcpu placement='static'>3</vcpu>",
            "<vcpu placement='static'>3</vcpu>\n"
            "  <memoryBacking><source type='memfd'/><access mode='shared'/></memoryBacking>",
        )
        redefined: list[str] = []

        class FakeLibvirt:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, _name):
                from types import SimpleNamespace

                return SimpleNamespace(managed_by="v-EMU (full)")

            def config_xml(self, _name):
                return source_xml

            def redefine_domain(self, _name, xml):
                redefined.append(xml)

        template = Template(
            name="src-base", base=str(self.source), arch="x86_64"
        )
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeLibvirt()
        ), mock.patch(
            "vemu.service.seal_domain", return_value=template
        ):
            result = service.seal_template(
                "src", str(self.dest), name="src-base"
            )
        self.assertEqual(result["removed_shares"], ["Pictures"])
        self.assertTrue(redefined)
        self.assertNotIn("<filesystem", redefined[0])
        self.assertNotIn("<memoryBacking", redefined[0])

    def test_seal_windows_requires_bitlocker_confirmation(self) -> None:
        from vemu import service

        with self.assertRaises(ValidationError):
            service.seal_template(
                "src", str(self.dest), name="src-base", sysprepped=True
            )
        with self.assertRaises(ValidationError):
            service.seal_template(
                "src",
                str(self.dest),
                name="src-base",
                os_family="windows",
            )
        with self.assertRaises(ValidationError):
            service.seal_template(
                "src",
                str(self.dest),
                name="src-base",
                os_family="linux",
                sysprepped=True,
                accept_bitlocker=True,
            )

    def test_template_round_trip(self) -> None:
        template = Template(
            name="t1",
            base=str(self.source),
            arch="x86_64",
            firmware="efi",
            secure_boot=True,
            tpm=True,
        )
        template.save(self.root / "meta")
        loaded = resolve_template(self.root / "meta")
        self.assertEqual(loaded.to_dict(), template.to_dict())
        self.assertTrue(loaded.secure_boot)
        self.assertTrue(loaded.tpm)

    def test_template_version_1_defaults_security_off(self) -> None:
        path = self.root / "legacy.json"
        path.write_text(
            '{"version": 1, "name": "old", "base": "/tmp/old.qcow2"}'
        )
        template = resolve_template(path)
        self.assertFalse(template.secure_boot)
        self.assertFalse(template.tpm)

    def test_seal_multi_and_clone_v2(self) -> None:
        from vemu import service
        from vemu.chain import ChainManager

        second = self.root / "source2.qcow2"
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", str(second), "8M"],
            check=True,
            capture_output=True,
        )
        backend = MultiFakeBackend([self.source, second])
        template = seal_domain(
            backend,
            "src",
            self.dest,
            "multi-base",
            in_place=False,
        )
        self.assertEqual(len(template.disks), 2)
        self.assertEqual(
            [item["target"] for item in template.disks], ["vda", "vdb"]
        )
        self.assertTrue(all(Path(item["base"]).is_file() for item in template.disks))
        result = service.clone_create(
            str(self.dest / "multi-base"),
            str(self.root / "labs"),
            count=1,
            name="multi-clone",
        )
        clone = result["created"][0]
        self.assertEqual(len(clone["disks"]), 2)
        self.assertEqual(
            [item["target"] for item in clone["disks"]], ["vda", "vdb"]
        )
        chain = ChainManager(Path(clone["chain"]).parent).load()
        self.assertEqual(chain.version, 2)
        self.assertEqual(set(chain.disks), {"vda", "vdb"})
        self.assertEqual(chain.active_set(), "0001")

    def test_clone_inherits_secure_boot_and_tpm(self) -> None:
        from vemu import service

        captured: list[str] = []

        class FakeLibvirt:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def unique_mac(self):
                return "52:54:00:aa:bb:cc"

            def define(self, xml):
                captured.append(xml)

        template = Template(
            name="secure-base",
            base=str(self.source),
            arch="x86_64",
            machine="q35",
            firmware="efi",
            secure_boot=True,
            tpm=True,
        )
        template_dir = self.dest / "secure-base"
        template.save(template_dir)
        firmware = SimpleNamespace(
            loader=Path("/tmp/OVMF_CODE.secboot.fd"),
            loader_format="raw",
            nvram_template=Path("/tmp/OVMF_VARS.secboot.fd"),
            nvram_format="raw",
        )
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeLibvirt()
        ), mock.patch(
            "vemu.service.firmware_for", return_value=firmware
        ):
            result = service.clone_create(
                str(template_dir),
                str(self.root / "labs"),
                count=1,
                name="secure-clone-{{n}}",
                define=True,
                network="vemu-Bridge",
            )
        self.assertTrue(result["warnings"])
        root = ET.fromstring(captured[0])
        loader = root.find("./os/loader")
        self.assertIsNotNone(loader)
        self.assertEqual(loader.get("secure"), "yes")
        self.assertIsNotNone(root.find("./features/smm"))
        self.assertIsNotNone(root.find("./devices/tpm"))
        interface = root.find("./devices/interface")
        self.assertIsNotNone(interface)
        self.assertEqual(interface.get("type"), "network")
        self.assertEqual(
            interface.find("source").get("network"), "vemu-Bridge"
        )
        cdroms = root.findall("./devices/disk[@device='cdrom']")
        self.assertEqual(len(cdroms), 1)
        self.assertIsNone(cdroms[0].find("source"))
        self.assertEqual(cdroms[0].find("target").get("dev"), "sdb")
        self.assertIsNone(root.find("./devices/filesystem"))
        self.assertIsNone(root.find("memoryBacking"))

    def test_template_delete_refuses_referenced_base(self) -> None:
        from vemu import service

        self.dest.mkdir()
        template_dir = self.dest / "base-one"
        base_file = template_dir / "base.qcow2"
        base_file.parent.mkdir(parents=True)
        shutil.copyfile(self.source, base_file)
        template = Template(name="base-one", base=str(base_file))
        template.save(template_dir)
        config = self.root / "storage.json"
        config.write_text(
            '{"version": 1, "locations": ['
            f'{{"path": "{self.dest}", "role": "templates"}}]}}'
        )

        class FakeLibvirt:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        base = base_file.resolve()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeLibvirt()
        ), mock.patch(
            "vemu.service._storage_index",
            return_value=([], {base: ["c-1"]}, {}, []),
        ):
            with self.assertRaises(Conflict):
                service.template_delete(
                    str(template_dir), config=str(config)
                )
        self.assertTrue(template_dir.is_dir())

    def test_template_delete_and_storage_guard(self) -> None:
        from vemu import service

        self.dest.mkdir()
        template_dir = self.dest / "base-two"
        base_file = template_dir / "base.qcow2"
        base_file.parent.mkdir(parents=True)
        shutil.copyfile(self.source, base_file)
        template = Template(name="base-two", base=str(base_file))
        template.save(template_dir)
        config = self.root / "storage.json"
        config.write_text(
            '{"version": 1, "locations": ['
            f'{{"path": "{self.dest}", "role": "templates"}}]}}'
        )

        class FakeLibvirt:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeLibvirt()
        ), mock.patch(
            "vemu.service._storage_index", return_value=([], {}, {}, [])
        ):
            result = service.template_delete(
                str(template_dir), config=str(config)
            )
        self.assertTrue(result["deleted"])
        self.assertFalse(template_dir.exists())
        with self.assertRaises(Conflict):
            service.storage_delete_file(
                str(template_dir / "base.qcow2"), config=str(config)
            )

    def test_invalid_version(self) -> None:
        path = self.root / "bad.json"
        path.write_text('{"version": 99, "name": "x", "base": "/tmp/x"}')
        with self.assertRaises(ValidationError):
            resolve_template(path)

    def test_slug_and_name(self) -> None:
        self.assertEqual(slugify("[0]-[TPL]-Rocky 10.2"), "0-TPL-Rocky-10.2")
        with self.assertRaises(ValidationError):
            validate_name("-bad")
        with self.assertRaises(NotFound):
            resolve_template(self.root / "missing")


if __name__ == "__main__":
    unittest.main()
