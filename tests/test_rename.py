from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vemu import cli, service
from vemu.chain import ChainManager
from vemu.errors import ValidationError
from vemu.shares import ShareAssignment, ShareStore

QEMU_IMG = shutil.which("qemu-img")


def _create_image(path: Path) -> None:
    subprocess.run(
        [QEMU_IMG, "create", "-f", "qcow2", str(path), "16M"],
        check=True,
        capture_output=True,
    )


XML = """<domain type='kvm'>
  <name>{name}</name>
  <uuid>11111111-2222-3333-4444-555555555555</uuid>
  <memory unit='MiB'>1024</memory>
  <vcpu>1</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type></os>
  <metadata>
    <vemu:management xmlns:vemu="https://vemu.local/management"
      managed_by="v-EMU (full)" mode="full" origin="external"/>
  </metadata>
  <devices>
    <disk type='file' device='disk'>
      <source file='{disk}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
  </devices>
</domain>"""


@unittest.skipUnless(QEMU_IMG, "qemu-img is required")
class RenameTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.labs = self.root / "labs"
        self.labs.mkdir()
        self.base = self.labs / "old" / "base.qcow2"
        self.base.parent.mkdir()
        _create_image(self.base)
        ChainManager.create(self.base.parent, self.base)
        self.fake: FakeBackend | None = None

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fake(self) -> "FakeBackend":
        chain = ChainManager(self.base.parent).load()
        xml = XML.format(name="old", disk=chain.active_disk())
        backend = FakeBackend(xml)
        self.fake = backend
        return backend

    def test_chain_rename_updates_backing_paths(self) -> None:
        manager = ChainManager(self.base.parent)
        manager.snapshot("one")
        new = manager.rename(self.labs / "new")
        self.assertFalse(self.base.parent.exists())
        self.assertTrue(new.chain_file.is_file())
        self.assertEqual(new.verify(), [])
        info = new.info()
        self.assertIn(str(self.labs / "new"), info["active_disk"])
        result = subprocess.run(
            [QEMU_IMG, "info", "--backing-chain", "--output=json",
             info["active_disk"]],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn(str(self.labs / "new" / "base.qcow2"), result.stdout)

    def test_service_rename_moves_chain_and_xml(self) -> None:
        backend = self._fake()
        shares = self.root / "shares.json"
        shares.write_text(
            json.dumps(
                {
                    "version": 1,
                    "shares": [],
                    "assignments": [
                        ShareAssignment(
                            domain="old", share="docs", tag="docs"
                        ).to_dict()
                    ],
                }
            )
        )
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ), mock.patch.dict(
            os.environ, {"VEMU_SHARE_CONFIG": str(shares)}
        ):
            result = service.vm_rename(
                "old",
                "new",
                backup_dir=str(self.root / "backups"),
            )
        self.assertEqual(result["domain"], "new")
        new_dir = self.labs / "new"
        self.assertTrue((new_dir / "chain.json").is_file())
        self.assertFalse((self.labs / "old").exists())
        root = ET.fromstring(backend.xml)
        self.assertEqual(root.findtext("name"), "new")
        disk = root.find("./devices/disk/source").get("file")
        self.assertTrue(disk.startswith(str(new_dir)))
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertEqual(
            ShareStore(str(shares)).assignments_for("new")[0].share,
            "docs",
        )
        self.assertEqual(ChainManager(new_dir).verify(), [])

    def test_domain_only_rename_keeps_disk(self) -> None:
        backend = self._fake()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            plan = service.vm_rename_plan(
                "old", "new", rename_disks=False
            )
            self.assertTrue(plan["supported"])
            self.assertFalse(plan["rename_disks"])
            result = service.vm_rename(
                "old",
                "new",
                rename_disks=False,
                backup_dir=str(self.root / "backups"),
            )
        self.assertEqual(result["domain"], "new")
        self.assertTrue((self.labs / "old" / "chain.json").is_file())
        root = ET.fromstring(backend.xml)
        self.assertIn(str(self.labs / "old"), root.find("./devices/disk/source").get("file"))

    def test_plan_blocks_running_and_existing_name(self) -> None:
        backend = self._fake()
        backend.state = "running"
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            plan = service.vm_rename_plan("old", "new")
        self.assertFalse(plan["supported"])
        self.assertTrue(
            any("shut down" in reason for reason in plan["reasons"])
        )

    def test_cli_rename_requires_yes(self) -> None:
        args = SimpleNamespace(mode=None, dry_run=False, yes=False)
        with mock.patch(
            "vemu.cli._daemon_enabled", return_value=False
        ), mock.patch("vemu.cli.service.vm_rename") as rename:
            with self.assertRaises(ValidationError):
                cli._cmd_rename(args)
            rename.assert_not_called()


class FakeBackend:
    def __init__(self, xml: str) -> None:
        self.xml = xml
        self.name = "old"
        self.state = "stopped"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def info(self, _name: str):
        root = ET.fromstring(self.xml)
        disks = [
            disk.find("source").get("file")
            for disk in root.findall("./devices/disk")
            if disk.get("device") == "disk"
            and disk.find("source") is not None
        ]
        return SimpleNamespace(
            state=self.state,
            managed_by="v-EMU (full)",
            disks=disks,
        )

    def config_xml(self, _name: str) -> str:
        return self.xml

    def list_domains(self):
        return [SimpleNamespace(name=self.name)]

    def snapshot_names(self, _name: str):
        return []

    def rename_domain(self, _name: str, new_name: str):
        root = ET.fromstring(self.xml)
        root.find("name").text = new_name
        self.xml = ET.tostring(root, encoding="unicode")
        self.name = new_name
        return self.info(new_name)

    def redefine_domain(self, _name: str, xml: str):
        self.xml = xml
        return self.info(_name)


if __name__ == "__main__":
    unittest.main()
