from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu import hostbridge, service
from vemu.errors import Conflict, ValidationError, VemuError
from vemu.network import NetworkStore


def result(
    args: list[str], code: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, code, stdout, stderr)


class FakeRunner:
    def __init__(self, fail_up: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail_up = fail_up

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        command = " ".join(args)
        if command in {
            "nmcli -t -f GENERAL.STATE connection show vemu-br-br-vemu",
            "nmcli -t -f GENERAL.STATE connection show vemu-port-br-vemu",
        }:
            return result(args, stdout="GENERAL.STATE:activated\n")
        if command.startswith("nmcli connection show "):
            return result(args, 10, stderr="not found")
        if command.startswith(
            "nmcli -t -f connection.autoconnect connection show uuid"
        ):
            return result(args, stdout="connection.autoconnect:yes\n")
        if command == "ip -j address show":
            return result(
                args,
                stdout=json.dumps(
                    [
                        {
                            "ifname": "br-vemu",
                            "addr_info": [
                                {"local": "192.168.1.50", "prefixlen": 24}
                            ],
                        }
                    ]
                ),
            )
        if command.startswith("nmcli connection up vemu-br-br-vemu"):
            if self.fail_up:
                return result(args, 1, stderr="activation failed")
            return result(args)
        return result(args)


def inventory() -> dict:
    return {
        "nmcli": True,
        "reason": "",
        "bridges": [
            {
                "name": "bridge0",
                "state": "up",
                "interfaces": ["enp3s0"],
                "addresses": ["192.168.80.1/24"],
                "connection": {"name": "bridge0", "active": True},
            }
        ],
        "uplinks": [
            {
                "name": "enp4s0",
                "state": "up",
                "addresses": ["10.0.0.20/24"],
                "connection": {
                    "name": "Office LAN",
                    "uuid": "abc-123",
                    "active": True,
                },
            }
        ],
    }


class PlanTests(unittest.TestCase):
    def test_plan_dhcp_and_warnings(self) -> None:
        plan = hostbridge.plan_bridge(
            "br-vemu",
            "enp4s0",
            "dhcp",
            inventory=inventory(),
            run=FakeRunner(),
        )
        self.assertEqual(plan["name"], "br-vemu")
        self.assertEqual(plan["old_connection"]["name"], "Office LAN")
        self.assertTrue(
            any("briefly interrupt" in item for item in plan["warnings"])
        )
        self.assertIn("Office LAN", plan["warnings"][1])

    def test_plan_rejects_used_port_and_invalid_static(self) -> None:
        with self.assertRaises(ValidationError):
            hostbridge.plan_bridge(
                "br-vemu", "enp3s0", "dhcp", inventory=inventory()
            )
        with self.assertRaises(ValidationError):
            hostbridge.plan_bridge(
                "br-vemu",
                "enp4s0",
                "static",
                address="192.168.1.50/24",
                gateway="10.0.0.1",
                inventory=inventory(),
            )
        with self.assertRaises(Conflict):
            hostbridge.plan_bridge(
                "bridge0", "enp4s0", "dhcp", inventory=inventory()
            )

    def test_plan_rejects_long_bridge_name(self) -> None:
        with self.assertRaises(ValidationError):
            hostbridge.plan_bridge(
                "br-" + "x" * 20, "enp4s0", inventory=inventory()
            )


class CreateBridgeTests(unittest.TestCase):
    def test_create_records_backup_and_disables_old_profile(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            plan = hostbridge.plan_bridge(
                "br-vemu", "enp4s0", "dhcp", inventory=inventory()
            )
            payload = hostbridge.create_bridge(
                plan,
                run=runner,
                backup_dir=tmp,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(payload["name"], "br-vemu")
            self.assertTrue(Path(payload["backup"]).is_file())
            backup = json.loads(Path(payload["backup"]).read_text())
            self.assertEqual(backup["uplink"], "enp4s0")
        commands = [" ".join(call) for call in runner.calls]
        self.assertTrue(
            any("connection down uuid abc-123" in c for c in commands)
        )
        self.assertTrue(
            any("connection add type bridge" in c for c in commands)
        )
        bridge_command = next(
            c for c in commands if "connection add type bridge" in c
        )
        self.assertIn("bridge.stp no", bridge_command)
        port = next(
            c for c in commands
            if "connection add type ethernet" in c
        )
        self.assertIn("master vemu-br-br-vemu", port)
        self.assertNotIn("ipv4.method", port)
        self.assertTrue(
            any(
                "connection modify uuid abc-123 connection.autoconnect no"
                in c
                for c in commands
            )
        )

    def test_create_rolls_back_when_activation_fails(self) -> None:
        runner = FakeRunner(fail_up=True)
        with tempfile.TemporaryDirectory() as tmp:
            plan = hostbridge.plan_bridge(
                "br-vemu", "enp4s0", "dhcp", inventory=inventory()
            )
            with self.assertRaises(VemuError):
                hostbridge.create_bridge(
                    plan,
                    run=runner,
                    backup_dir=tmp,
                    sleep=lambda _seconds: None,
                )
        commands = [" ".join(call) for call in runner.calls]
        self.assertTrue(
            any("connection delete vemu-br-br-vemu" in c for c in commands)
        )
        self.assertTrue(
            any("connection delete vemu-port-br-vemu" in c for c in commands)
        )
        self.assertTrue(
            any("connection up uuid abc-123" in c for c in commands)
        )

    def test_bridge_ready_requires_ipv4_address(self) -> None:
        class Runner(FakeRunner):
            def __init__(self, address: str) -> None:
                super().__init__()
                self.address = address

            def __call__(self, args, **kwargs):
                if " ".join(args) == "ip -j address show":
                    return result(
                        args,
                        stdout=json.dumps(
                            [
                                {
                                    "ifname": "br-vemu",
                                    "addr_info": [
                                        {
                                            "local": self.address,
                                            "prefixlen": 64,
                                        }
                                    ],
                                }
                            ]
                        ),
                    )
                return super().__call__(args, **kwargs)

        ipv6 = Runner("fe80::1")
        self.assertFalse(
            hostbridge._bridge_ready(
                "vemu-br-br-vemu", "br-vemu", "dhcp",
                "vemu-port-br-vemu", ipv6,
            )
        )
        ipv4 = Runner("192.168.1.50")
        self.assertTrue(
            hostbridge._bridge_ready(
                "vemu-br-br-vemu", "br-vemu", "dhcp",
                "vemu-port-br-vemu", ipv4,
            )
        )

    def test_rollback_restores_autoconnect_after_disable(self) -> None:
        runner = FakeRunner()
        plan = hostbridge.plan_bridge(
            "br-vemu",
            "enp4s0",
            "dhcp",
            inventory=inventory(),
            run=runner,
        )
        plan["_old_autoconnect_changed"] = True
        hostbridge._rollback(plan, runner)
        commands = [" ".join(call) for call in runner.calls]
        self.assertTrue(
            any(
                "connection modify uuid abc-123 connection.autoconnect yes"
                in c
                for c in commands
            )
        )


class ServiceBridgeTests(unittest.TestCase):
    def test_service_registers_bridge_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = str(Path(tmp) / "networks.json")
            plan = {
                "name": "br-vemu",
                "uplink": "enp4s0",
                "addressing": "dhcp",
                "address": "",
                "gateway": "",
                "dns": "",
                "old_connection": {},
                "warnings": [],
                "bridge_connection": "vemu-br-br-vemu",
                "port_connection": "vemu-port-br-vemu",
            }
            with mock.patch.object(
                hostbridge, "plan_bridge", return_value=plan
            ), mock.patch.object(
                hostbridge,
                "create_bridge",
                return_value={
                    "name": "br-vemu",
                    "backup": "/tmp/backup.json",
                    "warnings": [],
                },
            ):
                payload = service.network_bridge_create(
                    "lan-bridge",
                    "br-vemu",
                    "enp4s0",
                    "dhcp",
                    config=config,
                )
            profiles = NetworkStore(config).load()
            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0].mode, "bridge")
            self.assertEqual(profiles[0].bridge_interface, "br-vemu")
            self.assertEqual(payload["profile"]["name"], "lan-bridge")

    def test_service_rejects_duplicate_profile_before_host_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = str(Path(tmp) / "networks.json")
            service.network_add(
                "lan-bridge",
                "bridge",
                "auto",
                False,
                "",
                bridge="br-vemu",
                config=config,
            )
            with mock.patch.object(
                hostbridge, "create_bridge"
            ) as create:
                with self.assertRaises(Conflict):
                    service.network_bridge_create(
                        "lan-bridge",
                        "br-vemu",
                        "enp4s0",
                        config=config,
                    )
                create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
