from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vemu.errors import Conflict, VemuError
from vemu.shares import (
    ShareAssignment,
    ShareDefinition,
    ShareStore as Store,
)

XML = """<domain type='kvm'>
  <name>vm1</name>
  <memory unit='MiB'>2048</memory>
  <vcpu>2</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type></os>
  <devices>
    <disk type='file' device='disk'>
      <source file='/tmp/a.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
  </devices>
</domain>"""


class ShareStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.catalog = self.root / "shares.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_catalog_and_assignment_round_trip(self) -> None:
        store = Store(self.catalog)
        store.add(
            ShareDefinition(
                name="Pictures",
                path=str(self.root),
                tag="Pictures",
                read_only=True,
            )
        )
        self.assertEqual(store.get("Pictures").tag, "Pictures")
        store.attach(
            ShareAssignment(
                domain="vm1",
                share="Pictures",
                tag="Pictures",
                read_only=True,
                auto_mount=True,
                drive="Z",
            )
        )
        assignment = store.assignments_for("vm1")[0]
        self.assertTrue(assignment.auto_mount)
        self.assertEqual(assignment.drive, "Z")
        self.assertEqual(store.list()[0]["name"], "Pictures")

        with self.assertRaises(Conflict):
            store.update(
                ShareDefinition(
                    name="Pictures",
                    path=str(self.root),
                    tag="Photos",
                    read_only=True,
                )
            )

        store.detach("vm1", "Pictures")
        self.assertEqual(store.assignments_for("vm1"), [])
        store.remove("Pictures")
        self.assertEqual(store.list(), [])


class ShareServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.host = self.root / "Pictures"
        self.host.mkdir()
        self.catalog = self.root / "shares.json"
        self._env = mock.patch.dict(
            "os.environ", {"VEMU_SHARE_CONFIG": str(self.catalog)}
        )
        self._env.start()
        from vemu import service

        self.service = service
        self.service.share_catalog_add(
            "Pictures",
            str(self.host),
            "Pictures",
            read_only=True,
            description="test",
        )

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _fake_backend(self, state: str = "stopped"):
        class FakeBackend:
            def __init__(self) -> None:
                self.xml = XML
                self.state = state

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, _name):
                return SimpleNamespace(state=self.state)

            def config_xml(self, _name):
                return self.xml

            def live_xml(self, _name):
                return self.xml

            def redefine_domain(self, _name, xml):
                self.xml = xml

            def qemu_agent_command(self, *_args, **_kwargs):
                raise VemuError("guest agent unavailable")

        return FakeBackend()

    def test_purge_domain_removes_only_assignments(self) -> None:
        store = Store(self.catalog)
        store.add(
            ShareDefinition(
                name="Data",
                path=str(self.root),
                tag="Data",
            )
        )
        store.attach(ShareAssignment(domain="vm1", share="Data", tag="Data"))
        store.attach(ShareAssignment(domain="vm2", share="Data", tag="Data"))
        self.assertEqual(store.purge_domain("vm1"), 1)
        self.assertEqual(store.assignments_for("vm1"), [])
        self.assertEqual(len(store.assignments_for("vm2")), 1)
        self.assertEqual(store.purge_domain("missing"), 0)

    def test_catalog_and_attach(self) -> None:
        self.assertEqual(len(self.service.shares_list()), 1)
        backend = self._fake_backend()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            payload = self.service.vm_share_attach(
                "vm1",
                "Pictures",
                auto_mount=True,
                drive="Z",
            )
        self.assertTrue(payload["assignment"]["auto_mount"])
        self.assertEqual(payload["assignment"]["drive"], "Z")
        root = ET.fromstring(backend.xml)
        filesystem = root.find("./devices/filesystem")
        self.assertIsNotNone(filesystem)
        self.assertEqual(filesystem.find("target").get("dir"), "Pictures")
        self.assertIsNotNone(filesystem.find("readonly"))
        self.assertIsNotNone(root.find("memoryBacking"))

        backend.state = "running"
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            status = self.service.vm_share_status("vm1")
        self.assertTrue(status["shares"][0]["live"])
        self.assertTrue(status["shares"][0]["auto_mount"])

    def test_mount_and_auto_mount_payload(self) -> None:
        backend = self._fake_backend()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            self.service.vm_share_attach(
                "vm1", "Pictures", auto_mount=True, drive="Y"
            )
        backend.state = "running"
        mounted: list[list[dict]] = []

        def fake_mount(_backend, _domain, payload):
            mounted.append(payload)
            return {"mounts": [{"tag": "Pictures", "drive": "Y:", "mounted": True}]}

        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ), mock.patch(
            "vemu.service.guest_virtiofs_mount", side_effect=fake_mount
        ):
            result = self.service.vm_share_mount("vm1")
            auto = self.service.vm_share_auto_mount("vm1")
        self.assertTrue(result["mounts"][0]["mounted"])
        self.assertEqual(
            mounted[0],
            [{"tag": "Pictures", "drive": "Y", "mountpoint": None}],
        )
        self.assertEqual(auto["mounts"][0]["tag"], "Pictures")

    def test_auto_mount_skips_when_disabled(self) -> None:
        backend = self._fake_backend()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            self.service.vm_share_attach(
                "vm1", "Pictures", auto_mount=False
            )
        backend.state = "running"
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ), mock.patch(
            "vemu.service.guest_virtiofs_mount"
        ) as mount:
            result = self.service.vm_share_auto_mount("vm1")
        self.assertTrue(result["skipped"])
        mount.assert_not_called()


if __name__ == "__main__":
    unittest.main()
