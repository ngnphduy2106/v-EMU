from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu import service
from vemu.backend import DomainInfo
from vemu.errors import Conflict
from vemu.network import NetworkProfile


class FakeExternal:
    def __init__(self, state: str = "stopped") -> None:
        self.state = state
        self.calls: list[str] = []
        self.xml = (
            "<domain><name>vm1</name><devices>"
            "<disk type='file' device='disk'>"
            "<source file='/tmp/disk.qcow2'/>"
            "<target dev='vda' bus='virtio'/>"
            "</disk></devices></domain>"
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def info(self, name: str):
        return DomainInfo(
            name=name,
            uuid="uuid",
            state=self.state,
            disks=["/tmp/disk.qcow2"],
            autostart=False,
            firmware="bios",
            managed_by="External",
        )

    def start(self, _name):
        self.calls.append("start")

    def shutdown(self, *_args, **_kwargs):
        self.calls.append("shutdown")

    def destroy(self, _name):
        self.calls.append("destroy")

    def config_xml(self, _name):
        return self.xml

    def redefine_domain(self, _name, xml):
        self.xml = xml
        self.calls.append("redefine")

    def snapshot_details(self, _name):
        return []


class FakePartial(FakeExternal):
    def info(self, name: str):
        return DomainInfo(
            name=name,
            uuid="uuid",
            state=self.state,
            disks=["/tmp/chain/disk-vda-0001.qcow2"],
            autostart=False,
            firmware="bios",
            managed_by="v-EMU (partial)",
        )

    def list_domains(self):
        return [self.info("vm1")]


class ReadOnlyTests(unittest.TestCase):
    def test_unmanaged_snapshots_info_lists_external(self) -> None:
        details = [
            {
                "name": "SS0",
                "kind": "external",
                "disks": [
                    {
                        "target": "vda",
                        "snapshot": "external",
                        "source": "/tmp/vm1.SS0",
                    }
                ],
            }
        ]
        backend = FakeExternal()

        def snapshot_details(_name):
            return details

        backend.snapshot_details = snapshot_details
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.snapshots_info("vm1")
        self.assertFalse(result["managed"])
        self.assertEqual(result["snapshots"], details)
        self.assertTrue(result["external_snapshots"])

    def test_rescue_start_is_explicit_and_backs_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeExternal()
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ):
                result = service.domain_rescue_start(
                    "vm1", backup_dir=tmp
                )
            self.assertTrue(result["started"])
            self.assertIn("start", backend.calls)
            self.assertTrue(Path(result["backup"]).is_file())
            self.assertEqual(result["managed_by"], "External")

    def test_rescue_shutdown_only_for_running_external(self) -> None:
        backend = FakeExternal(state="running")
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.domain_rescue_shutdown("vm1")
        self.assertTrue(result["shutdown"])
        self.assertFalse(result["force"])
        self.assertIn("shutdown", backend.calls)
        with mock.patch(
            "vemu.service.LibvirtBackend",
            return_value=FakeExternal(state="stopped"),
        ):
            with self.assertRaises(Conflict):
                service.domain_rescue_shutdown("vm1")
        with mock.patch(
            "vemu.service.LibvirtBackend",
            return_value=FakePartial(),
        ):
            with self.assertRaises(Conflict):
                service.domain_rescue_shutdown("vm1")

    def test_adopt_partial_writes_backup_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeExternal()
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ):
                result = service.adopt_domain(
                    "vm1", "partial", backup_dir=tmp
                )
            self.assertTrue(result["changed"])
            self.assertEqual(result["managed_by"], "v-EMU (partial)")
            self.assertTrue(Path(result["backup"]).is_file())
            self.assertIn("v-EMU (partial)", backend.xml)
            self.assertIn("/tmp/disk.qcow2", backend.xml)

    def test_adopt_read_only_does_not_change_xml(self) -> None:
        backend = FakeExternal()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.adopt_domain("vm1", "read-only")
        self.assertFalse(result["changed"])
        self.assertNotIn("redefine", backend.calls)

    def test_partial_blocks_chain_and_disk_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            new_disk = Path(tmp) / "new.qcow2"
            existing = Path(tmp) / "existing.qcow2"
            existing.write_bytes(b"x")
            operations = (
                lambda: service.vm_disk_add("vm1", str(new_disk)),
                lambda: service.vm_disk_remove(
                    "vm1", "/tmp/chain/disk-vda-0001.qcow2"
                ),
                lambda: service.vm_disk_resize("vm1", str(existing), 2),
                lambda: service.seal_template(
                    "vm1", str(Path(tmp) / "tpl")
                ),
            )
            for operation in operations:
                backend = FakePartial()
                with mock.patch(
                    "vemu.service.LibvirtBackend", return_value=backend
                ):
                    with self.assertRaises(Conflict):
                        operation()
            self.assertFalse(new_disk.exists())

    def test_chain_guard_rejects_partial_domain(self) -> None:
        backend = FakePartial()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            with self.assertRaises(Conflict):
                service.require_chain_writable("/tmp/chain")

    def test_start_shutdown_delete_are_blocked(self) -> None:
        for operation in (
            lambda: service.domain_start("vm1"),
            lambda: service.domain_shutdown("vm1"),
            lambda: service.domain_force_off("vm1"),
            lambda: service.domain_set_autostart("vm1", True),
            lambda: service.domain_delete("vm1"),
        ):
            backend = FakeExternal()
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ):
                with self.assertRaises(Conflict):
                    operation()
            self.assertEqual(backend.calls, [])

    def test_config_and_snapshots_are_blocked(self) -> None:
        backend = FakeExternal()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            with self.assertRaises(Conflict):
                service.vm_config_update("vm1", memory_mib=2048)
            with self.assertRaises(Conflict):
                service.snapshots_create("vm1", "snap")

    def test_disk_add_removes_created_file_on_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new.qcow2"
            backend = FakeExternal()
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ):
                with self.assertRaises(Conflict):
                    service.vm_disk_add("vm1", str(path))
            self.assertFalse(path.exists())

    def test_network_attach_is_blocked(self) -> None:
        backend = FakeExternal()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            with self.assertRaises(Conflict):
                service.network_attach("vm1", "NAT")


class FakeNetworkBackend:
    def __init__(self, attached: bool) -> None:
        self.attached = attached
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def list_domains(self):
        if not self.attached:
            return []
        return [
            DomainInfo(
                "vm1", "uuid", "running", ["/tmp/disk.qcow2"],
                False, "bios", "v-EMU (full)",
            )
        ]

    def live_xml(self, _name: str) -> str:
        return (
            "<domain><devices><interface type='network'>"
            "<source network='vemu-NAT'/>"
            "</interface></devices></domain>"
        )

    def define_network(self, _xml):
        self.calls.append("define")

    def stop_network(self, *_args, **_kwargs):
        self.calls.append("stop")

    def start_network(self, _name):
        self.calls.append("start")
        return {"name": "vemu-NAT", "active": True, "bridge": "vbr-test"}


class NetworkRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = Path(self._tmp.name) / "networks.json"
        profile = NetworkProfile(
            name="NAT", mode="nat", subnet="10.213.1.0/24", dhcp=True
        )
        self.config.write_text(
            json.dumps(
                {"version": 1, "networks": [profile.to_dict()]}
            )
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_restart_is_blocked_with_running_vm(self) -> None:
        backend = FakeNetworkBackend(attached=True)
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            with self.assertRaises(Conflict):
                service.network_restart(
                    "NAT", config=str(self.config)
                )
        self.assertEqual(backend.calls, [])

    def test_restart_with_force_or_no_vms(self) -> None:
        for attached, force in ((True, True), (False, False)):
            backend = FakeNetworkBackend(attached=attached)
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ), mock.patch(
                "vemu.service._apply_rules_safe", return_value={}
            ):
                result = service.network_restart(
                    "NAT", config=str(self.config), force=force
                )
            self.assertTrue(result["active"])
            self.assertIn("stop", backend.calls)
            self.assertIn("start", backend.calls)


if __name__ == "__main__":
    unittest.main()
