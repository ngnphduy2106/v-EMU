from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu.errors import Conflict, NotFound, ValidationError
from vemu.network import (
    NetworkProfile,
    NetworkStore,
    PortForward,
    allocate_subnet,
    apply_nft,
    nft_ruleset,
    resolve_profile,
)


def nat_profile(name: str = "labnet", **kwargs) -> NetworkProfile:
    return NetworkProfile(name=name, mode="nat", subnet="10.213.0.0/24", **kwargs)


class NetworkProfileTests(unittest.TestCase):
    def test_nat_xml(self) -> None:
        from vemu.network import HostReservation

        profile = nat_profile(
            port_forwards=[
                PortForward("tcp", 2222, "10.213.0.10", 22, "ssh")
            ],
            host_reservations=[
                HostReservation("52:54:00:aa:bb:cc", "10.213.0.20", "router")
            ],
        )
        xml = profile.libvirt_xml()
        self.assertIn("<name>vemu-labnet</name>", xml)
        self.assertIn('<forward mode="nat"', xml)
        self.assertIn('address="10.213.0.1"', xml)
        self.assertIn('prefix="24"', xml)
        self.assertIn('start="10.213.0.2"', xml)
        self.assertIn('end="10.213.0.254"', xml)
        self.assertIn('name="vemu.local"', xml)
        self.assertIn('bridge name="vbr-', xml)
        self.assertIn('mac="52:54:00:aa:bb:cc"', xml)
        self.assertIn('ip="10.213.0.20"', xml)
        self.assertIn('name="router"', xml)

    def test_isolated_without_dhcp_has_no_host_ip(self) -> None:
        profile = NetworkProfile(
            name="isolated", mode="isolated", subnet="192.168.10.0/24", dhcp=False
        )
        xml = profile.libvirt_xml()
        self.assertIn('<forward mode="none"', xml)
        self.assertNotIn("<ip ", xml)
        self.assertFalse(profile.has_host_ip)
        self.assertIsNone(profile.gateway)

    def test_isolated_with_dhcp_has_host_ip(self) -> None:
        profile = NetworkProfile(
            name="isolated", mode="isolated", subnet="192.168.10.0/24"
        )
        self.assertTrue(profile.has_host_ip)
        self.assertIn("<dhcp>", profile.libvirt_xml())

    def test_bridge_profile_uses_existing_bridge(self) -> None:
        profile = NetworkProfile(
            name="br-lab",
            mode="bridge",
            bridge_interface="bridge0",
            dhcp=False,
        )
        xml = profile.libvirt_xml()
        self.assertIn('<forward mode="bridge"', xml)
        self.assertIn('<bridge name="bridge0"', xml)
        self.assertNotIn("<ip ", xml)
        self.assertFalse(profile.has_host_ip)
        self.assertEqual(profile.bridge_name, "bridge0")
        restored = NetworkProfile.from_dict(profile.to_dict())
        self.assertEqual(restored.bridge_interface, "bridge0")

    def test_auto_subnet_allocation(self) -> None:
        first = resolve_profile(
            NetworkProfile(name="a", mode="nat"), []
        )
        second = resolve_profile(
            NetworkProfile(name="b", mode="nat"), [first]
        )
        self.assertEqual(first.subnet, "10.213.0.0/24")
        self.assertEqual(second.subnet, "10.213.1.0/24")
        self.assertNotEqual(allocate_subnet([first, second]), first.subnet)

    def test_subnet_conflict_for_host_ip(self) -> None:
        existing = nat_profile("one")
        with self.assertRaises(Conflict):
            resolve_profile(nat_profile("two"), [existing])

    def test_isolated_duplicate_subnet_allowed(self) -> None:
        existing = NetworkProfile(
            name="one", mode="isolated", subnet="192.168.10.0/24", dhcp=False
        )
        duplicate = NetworkProfile(
            name="two", mode="isolated", subnet="192.168.10.0/24", dhcp=False
        )
        resolve_profile(duplicate, [existing])
        self.assertEqual(duplicate.subnet, "192.168.10.0/24")

    def test_port_forward_validation(self) -> None:
        profile = nat_profile(
            port_forwards=[
                PortForward("tcp", 2222, "10.213.0.10", 22),
                PortForward("tcp", 2222, "10.213.0.11", 22),
            ]
        )
        with self.assertRaises(ValidationError):
            profile.validate()

        outside = nat_profile(
            port_forwards=[
                PortForward("tcp", 2222, "10.213.9.10", 22)
            ]
        )
        with self.assertRaises(ValidationError):
            outside.validate()

        isolated = NetworkProfile(
            name="iso",
            mode="isolated",
            subnet="192.168.10.0/24",
            port_forwards=[PortForward("tcp", 2222, "192.168.10.10", 22)],
        )
        with self.assertRaises(ValidationError):
            isolated.validate()

    def test_reservation_validation(self) -> None:
        from vemu.network import HostReservation

        outside = nat_profile(
            host_reservations=[
                HostReservation("52:54:00:aa:bb:cc", "10.214.0.20")
            ]
        )
        with self.assertRaises(ValidationError):
            outside.validate()

        gateway = nat_profile(
            host_reservations=[
                HostReservation("52:54:00:aa:bb:cc", "10.213.0.1")
            ]
        )
        with self.assertRaises(ValidationError):
            gateway.validate()

        duplicate = nat_profile(
            host_reservations=[
                HostReservation("52:54:00:aa:bb:cc", "10.213.0.20"),
                HostReservation("52:54:00:aa:bb:cc", "10.213.0.21"),
            ]
        )
        with self.assertRaises(ValidationError):
            duplicate.validate()

        no_dhcp = NetworkProfile(
            name="iso",
            mode="isolated",
            subnet="192.168.10.0/24",
            dhcp=False,
            host_reservations=[
                HostReservation("52:54:00:aa:bb:cc", "192.168.10.20")
            ],
        )
        with self.assertRaises(ValidationError):
            no_dhcp.validate()

    def test_nft_ruleset(self) -> None:
        profile = nat_profile(
            port_forwards=[
                PortForward("tcp", 2222, "10.213.0.10", 22),
                PortForward("udp", 5353, "10.213.0.11", 53),
            ]
        )
        rules = nft_ruleset([profile])
        self.assertIn("table inet vemu {", rules)
        self.assertIn("tcp dport 2222 dnat ip to 10.213.0.10:22", rules)
        self.assertIn("udp dport 5353 dnat ip to 10.213.0.11:53", rules)
        self.assertIn("ct status dnat accept", rules)
    def test_apply_nft_dry_run(self) -> None:
        result = apply_nft([nat_profile()], dry_run=True)
        self.assertFalse(result["applied"])
        self.assertIn("table inet vemu", result["ruleset"])

    def test_apply_nft_with_fake_runner(self) -> None:
        calls: list[list[str]] = []

        def runner(command, input_text=None):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("vemu.network.shutil.which", return_value="/usr/sbin/nft"), mock.patch(
            "vemu.network.os.geteuid", return_value=0
        ):
            result = apply_nft([nat_profile()], runner=runner)
        self.assertTrue(result["applied"])
        self.assertIn(["nft", "flush", "table", "inet", "vemu"], calls)
        self.assertIn(["nft", "-f", "-"], calls)

    def test_libvirt_forward_rules(self) -> None:
        from vemu.network import apply_libvirt_forward_rules

        profile = nat_profile(
            port_forwards=[
                PortForward("tcp", 2222, "10.213.0.10", 22)
            ]
        )
        calls: list[list[str]] = []

        def runner(command, input_text=None):
            calls.append(command)
            if "list" in command:
                stdout = (
                    "chain guest_input {\n"
                    '  oif "vbr-old" ip daddr 10.214.0.0/24 ct status dnat '
                    'accept comment "vemu:forward:old" # handle 7\n'
                    "}\n"
                )
                return subprocess.CompletedProcess(command, 0, stdout, "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("vemu.network.shutil.which", return_value="/usr/sbin/nft"), mock.patch(
            "vemu.network.os.geteuid", return_value=0
        ):
            result = apply_libvirt_forward_rules([profile], runner=runner)
        self.assertTrue(result["applied"])
        self.assertIn(
            [
                "nft",
                "delete",
                "rule",
                "ip",
                "libvirt_network",
                "guest_input",
                "handle",
                "7",
            ],
            calls,
        )
        insert = next(
            command for command in calls if "insert" in command
        )
        self.assertIn('"vemu:forward:labnet"', insert)
        self.assertIn("10.213.0.0/24", insert)

    def test_libvirt_forward_rules_missing_chain(self) -> None:
        from vemu.network import apply_libvirt_forward_rules

        def runner(command, input_text=None):
            return subprocess.CompletedProcess(command, 1, "", "not found")

        with mock.patch("vemu.network.shutil.which", return_value="/usr/sbin/nft"), mock.patch(
            "vemu.network.os.geteuid", return_value=0
        ):
            result = apply_libvirt_forward_rules([nat_profile()], runner=runner)
        self.assertFalse(result["applied"])
        self.assertIn("guest_input", result["reason"])

    def test_port_forward_lease_warning(self) -> None:
        from vemu.network import HostReservation
        from vemu.service import _port_forward_warning

        profile = nat_profile(
            host_reservations=[
                HostReservation("52:54:00:aa:bb:cc", "10.213.0.20")
            ]
        )
        self.assertIsNone(
            _port_forward_warning(profile, "10.213.0.20", [])
        )
        self.assertIsNone(
            _port_forward_warning(
                profile,
                "10.213.0.30",
                [{"ip": "10.213.0.30", "mac": "52:54:00:aa:bb:cd"}],
            )
        )
        warning = _port_forward_warning(profile, "10.213.0.99", [])
        self.assertIn("No DHCP reservation", warning)


class NetworkStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = Path(self._tmp.name) / "networks.json"
        self.store = NetworkStore(self.config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_list_remove(self) -> None:
        self.assertEqual(self.store.list(), [])
        self.store.add(NetworkProfile(name="lab", mode="nat"))
        profiles = self.store.list()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["subnet"], "10.213.0.0/24")
        self.assertEqual(
            self.store.find_libvirt_name("lab"), "vemu-lab"
        )
        removed = self.store.remove("lab")
        self.assertEqual(removed["name"], "lab")
        self.assertEqual(self.store.list(), [])
        with self.assertRaises(NotFound):
            self.store.remove("lab")

    def test_duplicate_name(self) -> None:
        self.store.add(NetworkProfile(name="lab", mode="nat"))
        with self.assertRaises(Conflict):
            self.store.add(NetworkProfile(name="lab", mode="nat"))

    def test_duplicate_host_port_across_networks(self) -> None:
        first = nat_profile("one")
        first.port_forwards = [
            PortForward("tcp", 2222, "10.213.0.10", 22)
        ]
        self.store.add(first)
        second = NetworkProfile(name="two", mode="nat")
        second.port_forwards = [
            PortForward("tcp", 2222, "10.213.1.10", 22)
        ]
        with self.assertRaises(Conflict):
            self.store.add(second)

    def test_get_and_update(self) -> None:
        self.store.add(NetworkProfile(name="lab", mode="nat"))
        profile = self.store.get("lab")
        profile.description = "changed"
        self.store.update(profile)
        self.assertEqual(self.store.get("lab").description, "changed")
        with self.assertRaises(NotFound):
            self.store.get("missing")


class NetworkIntegrationTests(unittest.TestCase):
    def test_backend_network_info(self) -> None:
        from vemu.backend import LibvirtBackend

        class FakeNetwork:
            def name(self) -> str:
                return "vemu-lab"

            def UUIDString(self) -> str:
                return "uuid"

            def XMLDesc(self, _flags) -> str:
                return (
                    "<network><name>vemu-lab</name>"
                    "<forward mode='nat'/>"
                    "<bridge name='vbr-1'/>"
                    "<domain name='vemu.local'/>"
                    "<ip address='10.213.0.1' prefix='24'>"
                    "<dhcp><range start='10.213.0.2' end='10.213.0.9'/>"
                    "</dhcp></ip></network>"
                )

            def isActive(self) -> bool:
                return True

            def autostart(self) -> int:
                return 1

        info = LibvirtBackend._network_info(None, FakeNetwork())
        self.assertEqual(info["name"], "vemu-lab")
        self.assertEqual(info["subnet"], "10.213.0.0/24")
        self.assertTrue(info["dhcp"])
        self.assertEqual(info["dns_domain"], "vemu.local")
        self.assertEqual(info["forward"], "nat")
        self.assertTrue(info["active"])

    def test_network_restart(self) -> None:
        from vemu import service

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "networks.json"
            store = NetworkStore(config)
            store.add(NetworkProfile(name="lab", mode="nat"))
            calls: list[tuple[str, str]] = []

            class FakeBackend:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def list_domains(self):
                    return []

                def define_network(self, xml):
                    calls.append(("define", "vemu-lab"))
                    return {"name": "vemu-lab"}

                def stop_network(self, name):
                    calls.append(("stop", name))

                def start_network(self, name):
                    calls.append(("start", name))
                    return {
                        "name": name,
                        "active": True,
                        "bridge": "vbr-1",
                    }

            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=FakeBackend()
            ):
                result = service.network_restart("lab", config=str(config))
            self.assertEqual(
                calls,
                [
                    ("define", "vemu-lab"),
                    ("stop", "vemu-lab"),
                    ("start", "vemu-lab"),
                ],
            )
            self.assertTrue(result["active"])

    def test_cli_parsers(self) -> None:
        from vemu.cli import build_parser

        parser = build_parser()
        add = parser.parse_args(
            ["network", "add", "--name", "lab", "--mode", "nat"]
        )
        self.assertEqual(add.func.__name__, "_cmd_network_add")
        self.assertEqual(add.subnet, "auto")

        forward = parser.parse_args(
            [
                "network",
                "forward",
                "add",
                "--network",
                "lab",
                "--host-port",
                "2222",
                "--guest-ip",
                "10.213.0.10",
                "--guest-port",
                "22",
            ]
        )
        self.assertEqual(forward.func.__name__, "_cmd_network_forward_add")
        self.assertEqual(forward.host_port, 2222)

        restart = parser.parse_args(["network", "restart", "--name", "lab"])
        self.assertEqual(restart.func.__name__, "_cmd_network_restart")

        leases = parser.parse_args(
            ["network", "leases", "--network", "lab"]
        )
        self.assertEqual(leases.func.__name__, "_cmd_network_leases")

        reservation = parser.parse_args(
            [
                "network",
                "reservation",
                "add",
                "--network",
                "lab",
                "--mac",
                "52:54:00:aa:bb:cc",
                "--ip",
                "10.213.0.20",
            ]
        )
        self.assertEqual(
            reservation.func.__name__, "_cmd_network_reservation_add"
        )


if __name__ == "__main__":
    unittest.main()
