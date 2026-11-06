from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vemu.chain import ChainManager
from vemu.client import DaemonClient, DaemonError, socket_available
from vemu.daemon import VemuDaemon, make_server, peer_allowed
from vemu.template import Template

QEMU_IMG = shutil.which("qemu-img")



class InstallerMediaTests(unittest.TestCase):
    def test_eject_installer_media(self) -> None:
        from vemu import service

        calls: list[tuple] = []
        promotions: list[str] = []

        class FakeBackend:
            installed = True

            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def __enter__(self) -> "FakeBackend":
                return self

            def __exit__(self, *_exc) -> None:
                return None

            def info(self, _name: str):
                return SimpleNamespace(managed_by="v-EMU (full)")

            def installed_disk_target(self, _name: str):
                return "vda" if self.installed else None

            def promote_boot_order(self, name: str) -> bool:
                promotions.append(name)
                return True

            def xml(self, _name: str) -> str:
                return (
                    "<domain><devices><disk type='file' device='cdrom'>"
                    "<source file='/tmp/install.iso'/>"
                    "<target dev='sdb' bus='usb'/></disk></devices></domain>"
                )

            def update_media(self, name, path, target, force=False):
                calls.append((name, path, target, force))
                return {"target": target}

        with mock.patch("vemu.service.LibvirtBackend", FakeBackend):
            result = service.vm_eject_installer_media("vm1")
        self.assertEqual(result["ejected"], ["sdb"])
        self.assertEqual(calls, [("vm1", None, "sdb", True)])
        self.assertEqual(promotions, ["vm1"])

        calls.clear()
        promotions.clear()
        FakeBackend.installed = False
        with mock.patch("vemu.service.LibvirtBackend", FakeBackend):
            result = service.vm_eject_installer_media("vm1")
        self.assertEqual(result["ejected"], [])
        self.assertEqual(calls, [])
        self.assertEqual(promotions, [])

@unittest.skipUnless(QEMU_IMG, "qemu-img is required")
class DaemonTests(unittest.TestCase):
    def test_socket_available_handles_permission_error(self) -> None:
        with mock.patch(
            "vemu.client.Path.exists",
            side_effect=PermissionError(13, "Permission denied"),
        ):
            self.assertFalse(socket_available("/run/vemu/vemud.sock"))

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.socket_path = self.root / "vemud.sock"
        self.base = self.root / "base.qcow2"
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", str(self.base), "8M"],
            check=True,
            capture_output=True,
        )
        self.chain_dir = self.root / "chain"
        ChainManager.create(self.chain_dir, self.base)
        self.templates = self.root / "templates"
        self.templates.mkdir()
        self.isos = self.root / "isos"
        self.isos.mkdir()
        self.template_base = self.templates / "base.qcow2"
        shutil.copyfile(self.base, self.template_base)
        self.storage_config = self.root / "storage.json"
        self.storage_config.write_text(
            '{"version": 1, "locations": ['
            f'{{"path": "{self.root}", "role": "labs"}},'
            f'{{"path": "{self.templates}", "role": "templates"}},'
            f'{{"path": "{self.isos}", "role": "isos"}}'
            ']}',
            encoding="utf-8",
        )
        self.network_config = str(self.root / "networks.json")
        daemon = VemuDaemon(
            config=str(self.storage_config),
            network_config=self.network_config,
            socket_path=str(self.socket_path),
        )
        self.server = make_server(self.socket_path, daemon, mode=0o660)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = DaemonClient(self.socket_path, timeout=10)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.socket_path.unlink(missing_ok=True)
        self._tmp.cleanup()

    def test_ping_and_status(self) -> None:
        self.assertTrue(self.client.ping()["pong"])
        status = self.client.status()
        self.assertEqual(status["socket"], str(self.socket_path))
        self.assertIn("version", status)

    def test_peer_allowed(self) -> None:
        pid = os.getpid()
        self.assertTrue(peer_allowed(0, 1, pid, 5, 1000))
        self.assertTrue(peer_allowed(1000, 1000, pid, 1000, 500))
        self.assertTrue(peer_allowed(500, 1000, pid, 9999, 500))
        self.assertFalse(peer_allowed(1000, 1001, pid, 9999, 500))
        groups = os.getgroups()
        if groups:
            self.assertTrue(peer_allowed(1000, 9999, pid, groups[0], 500))

    def test_chain_methods(self) -> None:
        show = self.client.call("chain.show", {"chain": str(self.chain_dir)})
        self.assertEqual(show["active"], "0001")

        layer = self.client.call(
            "chain.snapshot", {"chain": str(self.chain_dir), "label": "s1"}
        )
        self.assertEqual(layer["id"], "0002")

        verify = self.client.call("chain.verify", {"chain": str(self.chain_dir)})
        self.assertTrue(verify["ok"], verify["issues"])

        reverted = self.client.call(
            "chain.revert", {"chain": str(self.chain_dir), "layer": "0001"}
        )
        self.assertEqual(reverted["id"], "0001")

        deleted = self.client.call(
            "chain.delete", {"chain": str(self.chain_dir), "layer": "0002"}
        )
        self.assertEqual(deleted["deleted"], "0002")
        self.assertEqual(
            len(ChainManager(self.chain_dir).load().layers), 1
        )

    def test_clone_create_without_libvirt(self) -> None:
        result = self.client.call(
            "clone.create",
            {
                "template": str(self.template_base),
                "dest": str(self.root / "labs"),
                "count": 2,
                "name": "c-{{n}}",
            },
        )
        self.assertEqual(len(result["created"]), 2)
        self.assertFalse(result["created"][0]["defined"])

    def test_storage_inspect(self) -> None:
        result = self.client.call(
            "storage.inspect", {"path": str(self.root), "role": "labs"}
        )
        self.assertEqual(result["role"], "labs")
        self.assertTrue(result["exists"])

    def test_templates_list(self) -> None:
        templates_root = self.templates
        Template(name="t1", base=str(self.template_base)).save(
            templates_root / "t1"
        )
        result = self.client.call("templates.list", {"root": str(templates_root)})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "t1")

    def test_network_methods(self) -> None:
        config = str(self.root / "networks.json")
        added = self.client.call(
            "network.add",
            {
                "name": "labnet",
                "mode": "nat",
                "subnet": "auto",
                "config": config,
            },
        )
        self.assertEqual(added["name"], "labnet")
        self.assertEqual(added["subnet"], "10.213.0.0/24")
        self.assertEqual(added["libvirt_name"], "vemu-labnet")

        with mock.patch(
            "vemu.service._network_runtime", return_value=({}, None)
        ):
            listed = self.client.call(
                "networks.list", {"config": config}
            )
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["name"], "labnet")
            self.assertIn("runtime", listed[0])

            plan = self.client.call(
                "network.plan", {"config": config}
            )
        self.assertEqual(len(plan["actions"]), 1)
        self.assertIn("table inet vemu", plan["nft"])

        with mock.patch("vemu.service._dhcp_leases", return_value=[]):
            forward = self.client.call(
                "network.port_forward_add",
                {
                    "name": "labnet",
                    "protocol": "tcp",
                    "host_port": 2222,
                    "guest_ip": "10.213.0.10",
                    "guest_port": 22,
                    "config": config,
                },
            )
        self.assertEqual(
            len(forward["profile"]["port_forwards"]), 1
        )
        removed_forward = self.client.call(
            "network.port_forward_remove",
            {
                "name": "labnet",
                "protocol": "tcp",
                "host_port": 2222,
                "config": config,
            },
        )
        self.assertEqual(
            removed_forward["profile"]["port_forwards"], []
        )

        reservation = self.client.call(
            "network.reservation_add",
            {
                "name": "labnet",
                "mac": "52:54:00:aa:bb:cc",
                "ip": "10.213.0.20",
                "hostname": "router",
                "config": config,
            },
        )
        self.assertEqual(
            reservation["profile"]["host_reservations"][0]["ip"],
            "10.213.0.20",
        )
        self.assertTrue(reservation["restart_required"])
        removed_reservation = self.client.call(
            "network.reservation_remove",
            {
                "name": "labnet",
                "mac": "52:54:00:aa:bb:cc",
                "config": config,
            },
        )
        self.assertEqual(
            removed_reservation["profile"]["host_reservations"], []
        )

        removed = self.client.call(
            "network.remove", {"name": "labnet", "config": config}
        )
        self.assertEqual(removed["removed"]["name"], "labnet")
        self.assertEqual(
            self.client.call("networks.list", {"config": config}), []
        )

    def test_unknown_method(self) -> None:
        with self.assertRaises(DaemonError) as context:
            self.client.call("nope", {})
        self.assertEqual(context.exception.type_name, "ValidationError")

    def test_missing_parameter(self) -> None:
        with self.assertRaises(DaemonError) as context:
            self.client.call("chain.show", {})
        self.assertEqual(context.exception.type_name, "ValidationError")


if __name__ == "__main__":
    unittest.main()
