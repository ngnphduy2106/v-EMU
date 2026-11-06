from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vemu import cli, service
from vemu.errors import ValidationError

QEMU_IMG = shutil.which("qemu-img")

XML = """<domain type='kvm'>
  <name>vm1</name>
  <uuid>11111111-2222-3333-4444-555555555555</uuid>
  <memory unit='MiB'>1024</memory>
  <vcpu>1</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <loader secure='yes' type='pflash'>/usr/share/OVMF_CODE.fd</loader>
    <nvram template='/usr/share/OVMF_VARS.fd'>/var/lib/libvirt/qemu/nvram/vm1_VARS.fd</nvram>
  </os>
  <devices>
    <disk type='file' device='disk'>
      <source file='{disk}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <tpm model='tpm-crb'>
      <backend type='emulator' version='2.0'/>
    </tpm>
  </devices>
</domain>"""


@unittest.skipUnless(QEMU_IMG, "qemu-img is required")
class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "source.qcow2"
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", str(self.source), "16M"],
            check=True,
            capture_output=True,
        )
        self.labs = self.root / "labs"
        self.labs.mkdir()
        self.storage = self.root / "storage.json"
        self.storage.write_text(
            json.dumps(
                {
                    "version": 1,
                    "locations": [
                        {"path": str(self.labs), "role": "labs"}
                    ],
                }
            )
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _backend(
        self,
        xml: str,
        state: str = "stopped",
        snapshots=None,
        snapshot_details=None,
    ):
        class FakeBackend:
            def __init__(self) -> None:
                self.xml = xml

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, _name):
                root = ET.fromstring(self.xml)
                disks = []
                for disk in root.findall("./devices/disk"):
                    if disk.get("device") != "disk":
                        continue
                    source = disk.find("source")
                    if source is not None and source.get("file"):
                        disks.append(source.get("file"))
                metadata = root.find(
                    ".//{https://vemu.local/management}management"
                )
                managed = (
                    metadata.get("managed_by")
                    if metadata is not None
                    else "External"
                )
                return SimpleNamespace(
                    state=state, managed_by=managed, disks=disks
                )

            def config_xml(self, _name):
                return self.xml

            def snapshot_names(self, _name):
                return list(snapshots or [])

            def snapshot_details(self, _name):
                return list(snapshot_details or [])

            def redefine_domain(self, _name, xml):
                self.xml = xml

            def update_disk_sources(self, _name, changes):
                root = ET.fromstring(self.xml)
                for disk in root.findall("./devices/disk"):
                    source = disk.find("source")
                    if source is None:
                        continue
                    old = source.get("file")
                    if old in changes:
                        source.set("file", changes[old])
                self.xml = ET.tostring(root, encoding="unicode")
                return self.info(_name)

            def start(self, _name):
                self.started = True

        return FakeBackend()

    def test_plan_estimates_and_detects_snapshots(self) -> None:
        backend = self._backend(
            XML.format(disk=self.source), snapshots=["SS0", "SS1"]
        )
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            plan = service.vm_migration_plan(
                "vm1", storage_config=str(self.storage)
            )
        self.assertTrue(plan["supported"])
        self.assertTrue(plan["snapshot_loss_required"])
        self.assertEqual(plan["snapshots"], ["SS0", "SS1"])
        self.assertGreater(plan["estimated_bytes"], 0)
        self.assertGreater(plan["required_bytes"], plan["estimated_bytes"])
        self.assertTrue(plan["sufficient"])
        self.assertEqual(plan["data_disk_count"], 1)

    def _multi_xml(self) -> str:
        return XML.format(disk=self.source).replace(
            "</devices>",
            "<disk type='file' device='disk'>"
            f"<source file='{self.source}'/>"
            "<target dev='vdb' bus='virtio'/>"
            "</disk></devices>",
        )

    def test_plan_supports_multi_disk(self) -> None:
        backend = self._backend(self._multi_xml())
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            plan = service.vm_migration_plan(
                "vm1", storage_config=str(self.storage)
            )
        self.assertTrue(plan["supported"])
        self.assertTrue(plan["multi_disk"])
        self.assertEqual(plan["data_disk_count"], 2)
        self.assertEqual(
            [disk["target"] for disk in plan["disks"]], ["vda", "vdb"]
        )

    def test_full_migration_multi_disk(self) -> None:
        second = self.root / "second.qcow2"
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", str(second), "16M"],
            check=True,
            capture_output=True,
        )
        xml = self._multi_xml().replace(
            f"<source file='{self.source}'/>"
            "<target dev='vdb'",
            f"<source file='{second}'/>"
            "<target dev='vdb'",
        )
        backend = self._backend(xml)
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.vm_migrate_full(
                "vm1",
                accept_snapshot_loss=True,
                storage_config=str(self.storage),
                backup_dir=str(self.root / "backups"),
            )
        chain_file = Path(result["destination"]) / "chain.json"
        data = json.loads(chain_file.read_text())
        self.assertEqual(data["version"], 2)
        self.assertEqual(set(data["disks"]), {"vda", "vdb"})
        self.assertEqual(len(result["disks"]), 2)
        for target in ("vda", "vdb"):
            self.assertTrue(
                (Path(result["destination"]) / f"base-{target}.qcow2").is_file()
            )
            self.assertTrue(
                (Path(result["destination"]) / f"disk-{target}-0001.qcow2").is_file()
            )
        root = ET.fromstring(backend.xml)
        sources = {
            disk.find("target").get("dev"): disk.find("source").get("file")
            for disk in root.findall("./devices/disk")
        }
        self.assertEqual(
            sources["vda"],
            str(Path(result["destination"]) / "disk-vda-0001.qcow2"),
        )
        self.assertEqual(
            sources["vdb"],
            str(Path(result["destination"]) / "disk-vdb-0001.qcow2"),
        )
        metadata = root.find(
            ".//{https://vemu.local/management}management"
        )
        self.assertEqual(metadata.get("managed_by"), "v-EMU (full)")
        from vemu.chain import ChainManager

        self.assertEqual(
            ChainManager(Path(result["destination"])).verify(), []
        )

    def test_full_migration_flattens_and_keeps_tpm_xml(self) -> None:
        xml = XML.format(disk=self.source)
        backend = self._backend(xml, snapshots=["SS0"])
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            with self.assertRaises(Exception):
                service.vm_migrate_full(
                    "vm1",
                    storage_config=str(self.storage),
                    backup_dir=str(self.root / "backups"),
                )
            result = service.vm_migrate_full(
                "vm1",
                accept_snapshot_loss=True,
                storage_config=str(self.storage),
                backup_dir=str(self.root / "backups"),
            )
        base = Path(result["base"])
        active = Path(result["active_disk"])
        self.assertTrue(base.is_file())
        self.assertTrue(active.is_file())
        self.assertTrue((Path(result["destination"]) / "chain.json").is_file())
        root = ET.fromstring(backend.xml)
        source = root.find("./devices/disk/source")
        self.assertEqual(source.get("file"), str(active))
        metadata = root.find(
            ".//{https://vemu.local/management}management"
        )
        self.assertEqual(metadata.get("mode"), "full")
        self.assertEqual(metadata.get("managed_by"), "v-EMU (full)")
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertFalse(Path(result["destination"]).name == "")
        loader = root.find("./os/loader")
        nvram = root.find("./os/nvram")
        tpm = root.find("./devices/tpm")
        self.assertEqual(loader.get("secure"), "yes")
        self.assertEqual(
            nvram.text,
            "/var/lib/libvirt/qemu/nvram/vm1_VARS.fd",
        )
        self.assertEqual(tpm.find("backend").get("version"), "2.0")
        self.assertEqual(root.findtext("uuid"), "11111111-2222-3333-4444-555555555555")

    def test_snapshot_import_plan_and_migration(self) -> None:
        overlay1 = self.root / "source.SS0"
        overlay2 = self.root / "source.SS1"
        qemu_io = shutil.which("qemu-io")
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", "-F", "qcow2",
             "-b", str(self.source), str(overlay1)],
            check=True, capture_output=True,
        )
        if qemu_io:
            subprocess.run(
                [qemu_io, "-f", "qcow2", "-c", "write -P 0x11 0 4k",
                 str(overlay1)],
                check=True, capture_output=True,
            )
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", "-F", "qcow2",
             "-b", str(overlay1), str(overlay2)],
            check=True, capture_output=True,
        )
        if qemu_io:
            subprocess.run(
                [qemu_io, "-f", "qcow2", "-c", "write -P 0x22 8k 4k",
                 str(overlay2)],
                check=True, capture_output=True,
            )
        xml = XML.format(disk=overlay2)
        details = [
            {
                "name": "SS0",
                "kind": "external",
                "parent": "",
                "memory": "no",
                "state_sources": {"vda": str(self.source)},
            },
            {
                "name": "SS1",
                "kind": "external",
                "parent": "SS0",
                "memory": "no",
                "state_sources": {"vda": str(overlay1)},
            },
        ]
        backend = self._backend(xml, snapshot_details=details)
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            plan = service.vm_migration_plan(
                "vm1",
                import_snapshots=True,
                storage_config=str(self.storage),
            )
            self.assertTrue(plan["snapshot_import"]["supported"])
            self.assertFalse(plan["snapshot_loss_required"])
            self.assertEqual(plan["snapshot_import"]["layer_count"], 3)
            result = service.vm_migrate_full(
                "vm1",
                import_snapshots=True,
                storage_config=str(self.storage),
                backup_dir=str(self.root / "backups"),
            )
            info = service.snapshots_info("vm1")
        self.assertEqual(result["imported_snapshots"], ["SS0", "SS1"])
        self.assertTrue(info["managed"])
        labels = [layer["label"] for layer in info["layers"]]
        self.assertEqual(labels, ["SS0", "SS1", "current"])
        self.assertEqual(info["active"], "0003")
        self.assertTrue(self.source.is_file())
        self.assertTrue(overlay1.is_file())
        self.assertTrue(overlay2.is_file())
        root = ET.fromstring(backend.xml)
        source = root.find("./devices/disk/source")
        self.assertEqual(source.get("file"), result["active_disk"])

    def test_snapshot_import_requires_external_vm(self) -> None:
        from vemu.vmconfig import set_management_metadata

        partial_xml = set_management_metadata(
            XML.format(disk=self.source),
            mode="partial",
            managed_by="v-EMU (partial)",
            origin="external",
        )
        details = [
            {
                "name": "SS0",
                "kind": "external",
                "parent": "",
                "memory": "no",
                "state_sources": {"vda": str(self.source)},
                "disks": [{"target": "vda", "snapshot": "external"}],
            }
        ]
        backend = self._backend(partial_xml, snapshot_details=details)
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            plan = service.vm_migration_plan(
                "vm1",
                import_snapshots=True,
                storage_config=str(self.storage),
            )
        self.assertFalse(plan["supported"])
        self.assertIn("only available for External", " ".join(plan["reasons"]))

    def test_snapshot_import_rejects_memory_snapshot(self) -> None:
        details = [
            {
                "name": "SS0",
                "kind": "internal",
                "parent": "",
                "memory": "internal",
                "state_sources": {"vda": str(self.source)},
            }
        ]
        backend = self._backend(
            XML.format(disk=self.source), snapshot_details=details
        )
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            plan = service.vm_migration_plan(
                "vm1",
                import_snapshots=True,
                storage_config=str(self.storage),
            )
        self.assertFalse(plan["supported"])
        self.assertTrue(plan["snapshot_loss_required"])
        self.assertIn("memory snapshots", " ".join(plan["reasons"]))

    def test_snapshot_import_rejects_mixed_snapshot_modes(self) -> None:
        details = [
            {
                "name": "SS0",
                "kind": "external",
                "parent": "",
                "memory": "no",
                "state_sources": {"vda": str(self.source)},
                "disks": [
                    {"target": "vda", "snapshot": "external"},
                    {"target": "vdb", "snapshot": "internal"},
                ],
            }
        ]
        backend = self._backend(
            XML.format(disk=self.source), snapshot_details=details
        )
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            plan = service.vm_migration_plan(
                "vm1",
                import_snapshots=True,
                storage_config=str(self.storage),
            )
        self.assertFalse(plan["supported"])
        self.assertIn("same snapshot mode", " ".join(plan["reasons"]))

    def test_snapshot_import_keeps_external_branches(self) -> None:
        overlay1 = self.root / "source.SS0"
        overlay2 = self.root / "source.branch"
        current = self.root / "source.SS2"
        for base, path in (
            (self.source, overlay1),
            (self.source, overlay2),
            (overlay2, current),
        ):
            subprocess.run(
                [QEMU_IMG, "create", "-f", "qcow2", "-F", "qcow2",
                 "-b", str(base), str(path)],
                check=True, capture_output=True,
            )
        details = [
            {
                "name": "SS0",
                "kind": "external",
                "parent": "",
                "memory": "no",
                "state_sources": {"vda": str(self.source)},
                "disks": [{"target": "vda", "snapshot": "external"}],
            },
            {
                "name": "SS1",
                "kind": "external",
                "parent": "SS0",
                "memory": "no",
                "state_sources": {"vda": str(overlay1)},
                "disks": [{"target": "vda", "snapshot": "external"}],
            },
            {
                "name": "SS2",
                "kind": "external",
                "parent": "SS0",
                "memory": "no",
                "state_sources": {"vda": str(overlay2)},
                "disks": [{"target": "vda", "snapshot": "external"}],
            },
        ]
        backend = self._backend(
            XML.format(disk=current), snapshot_details=details
        )
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.vm_migrate_full(
                "vm1",
                import_snapshots=True,
                storage_config=str(self.storage),
                backup_dir=str(self.root / "backups"),
            )
        from vemu.chain import ChainManager

        chain = ChainManager(Path(result["destination"])).load()
        disk = chain.disk("vda")
        self.assertEqual(
            [(layer.id, layer.label, layer.parent) for layer in disk.layers],
            [
                ("0001", "SS0", "base"),
                ("0002", "SS1", "0001"),
                ("0003", "SS2", "0001"),
                ("0004", "current", "0003"),
            ],
        )
        self.assertEqual(chain.active_set(), "0004")
        self.assertEqual(ChainManager(Path(result["destination"])).verify(), [])

    def test_internal_snapshot_import(self) -> None:
        qemu_io = shutil.which("qemu-io")
        if qemu_io:
            subprocess.run(
                [qemu_io, "-f", "qcow2", "-c", "write -P 0x10 0 4k",
                 str(self.source)],
                check=True, capture_output=True,
            )
        subprocess.run(
            [QEMU_IMG, "snapshot", "-c", "I0", str(self.source)],
            check=True, capture_output=True,
        )
        if qemu_io:
            subprocess.run(
                [qemu_io, "-f", "qcow2", "-c", "write -P 0x11 4k 4k",
                 str(self.source)],
                check=True, capture_output=True,
            )
        subprocess.run(
            [QEMU_IMG, "snapshot", "-c", "I1", str(self.source)],
            check=True, capture_output=True,
        )
        if qemu_io:
            subprocess.run(
                [qemu_io, "-f", "qcow2", "-c", "write -P 0x22 8k 4k",
                 str(self.source)],
                check=True, capture_output=True,
            )
        states = {}
        for name in ("I0", "I1", "current"):
            target = self.root / f"{name}.qcow2"
            command = [QEMU_IMG, "convert", "-O", "qcow2"]
            if name != "current":
                command += ["-l", name]
            command += [str(self.source), str(target)]
            subprocess.run(command, check=True, capture_output=True)
            states[name] = target
        details = [
            {
                "name": "I0",
                "kind": "internal",
                "parent": "",
                "memory": "no",
                "state_sources": {"vda": str(self.source)},
                "disks": [{"target": "vda", "snapshot": "internal"}],
            },
            {
                "name": "I1",
                "kind": "internal",
                "parent": "I0",
                "memory": "no",
                "state_sources": {"vda": str(self.source)},
                "disks": [{"target": "vda", "snapshot": "internal"}],
            },
        ]
        backend = self._backend(
            XML.format(disk=self.source), snapshot_details=details
        )
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.vm_migrate_full(
                "vm1",
                import_snapshots=True,
                storage_config=str(self.storage),
                backup_dir=str(self.root / "backups"),
            )
        from vemu.chain import ChainManager

        chain_dir = Path(result["destination"])
        chain = ChainManager(chain_dir).load()
        disk = chain.disk("vda")
        self.assertEqual(
            [(layer.id, layer.label, layer.parent) for layer in disk.layers],
            [
                ("0001", "I0", "base"),
                ("0002", "I1", "0001"),
                ("0003", "current", "0002"),
            ],
        )

        def compare(left: Path, right: str) -> None:
            check = subprocess.run(
                [QEMU_IMG, "compare", str(left), right],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(check.returncode, 0, check.stderr)

        compare(states["current"], chain.active_disks()["vda"])
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            service.snapshots_revert("vm1", "0001")
            compare(
                states["I0"],
                ChainManager(chain_dir).load().active_disks()["vda"],
            )
            service.snapshots_revert("vm1", "0002")
            compare(
                states["I1"],
                ChainManager(chain_dir).load().active_disks()["vda"],
            )
            service.snapshots_revert("vm1", "0003")
        compare(
            states["current"],
            ChainManager(chain_dir).load().active_disks()["vda"],
        )

    def test_snapshot_set_repoints_all_disks(self) -> None:
        second = self.root / "second.qcow2"
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", str(second), "16M"],
            check=True,
            capture_output=True,
        )
        xml = self._multi_xml().replace(
            f"<source file='{self.source}'/>"
            "<target dev='vdb'",
            f"<source file='{second}'/>"
            "<target dev='vdb'",
        )
        backend = self._backend(xml)
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.vm_migrate_full(
                "vm1",
                accept_snapshot_loss=True,
                storage_config=str(self.storage),
                backup_dir=str(self.root / "backups"),
            )
            snapshot = service.snapshots_create("vm1", "before-change")
        self.assertTrue(snapshot["multi_disk"])
        self.assertEqual(snapshot["active"], "0002")
        root = ET.fromstring(backend.xml)
        sources = {
            disk.find("target").get("dev"): disk.find("source").get("file")
            for disk in root.findall("./devices/disk")
        }
        for target in ("vda", "vdb"):
            self.assertEqual(
                sources[target],
                str(
                    Path(result["destination"])
                    / f"disk-{target}-0002.qcow2"
                ),
            )
        from vemu.chain import ChainManager

        chain = ChainManager(Path(result["destination"])).load()
        self.assertEqual(chain.active_set(), "0002")

    def test_plan_new_firmware(self) -> None:
        backend = self._backend(XML.format(disk=self.source))
        files = SimpleNamespace(
            loader="/fw/loader.fd",
            loader_format="raw",
            nvram_template="/fw/vars.fd",
            nvram_format="raw",
        )
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ), mock.patch(
            "vemu.service.firmware_for", return_value=files
        ):
            plan = service.vm_migration_plan(
                "vm1",
                new_firmware=True,
                storage_config=str(self.storage),
            )
        self.assertTrue(plan["supported"])
        self.assertTrue(plan["new_firmware"])
        self.assertEqual(plan["firmware"]["loader"], "/fw/loader.fd")
        self.assertEqual(plan["firmware"]["secure_boot"], True)
        self.assertTrue(
            plan["firmware"]["nvram_path"].endswith("vm1_VARS.raw")
        )

    def test_full_migration_resets_firmware(self) -> None:
        backend = self._backend(XML.format(disk=self.source))
        files = SimpleNamespace(
            loader="/fw/loader.fd",
            loader_format="raw",
            nvram_template="/fw/vars.fd",
            nvram_format="raw",
        )
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ), mock.patch(
            "vemu.service.firmware_for", return_value=files
        ):
            result = service.vm_migrate_full(
                "vm1",
                new_firmware=True,
                storage_config=str(self.storage),
                backup_dir=str(self.root / "backups"),
            )
        self.assertTrue(result["firmware_applied"])
        root = ET.fromstring(backend.xml)
        self.assertIsNone(root.find("./os").get("firmware"))
        self.assertIsNone(root.find("./os/firmware"))
        loader = root.find("./os/loader")
        nvram = root.find("./os/nvram")
        self.assertEqual(loader.text, "/fw/loader.fd")
        self.assertEqual(loader.get("format"), "raw")
        self.assertEqual(loader.get("secure"), "yes")
        self.assertEqual(nvram.get("template"), "/fw/vars.fd")
        self.assertEqual(nvram.get("format"), "raw")
        self.assertTrue(str(nvram.text).endswith("vm1_VARS.raw"))
        smm = root.find("./features/smm")
        self.assertIsNotNone(smm)
        self.assertEqual(smm.get("state"), "on")

    def test_cli_migrate_requires_yes_before_calling_service(self) -> None:
        args = SimpleNamespace(mode="full", dry_run=False, yes=False)
        with mock.patch(
            "vemu.cli._daemon_enabled", return_value=False
        ), mock.patch("vemu.cli.service.vm_migrate_full") as migrate:
            with self.assertRaises(ValidationError):
                cli._cmd_migrate(args)
            migrate.assert_not_called()

    def test_full_migration_blocks_on_space(self) -> None:
        backend = self._backend(XML.format(disk=self.source))
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ), mock.patch(
            "vemu.service.shutil.disk_usage",
            return_value=SimpleNamespace(total=1, used=1, free=1),
        ):
            with self.assertRaises(Exception):
                service.vm_migrate_full(
                    "vm1",
                    storage_config=str(self.storage),
                    backup_dir=str(self.root / "backups"),
                )


if __name__ == "__main__":
    unittest.main()
