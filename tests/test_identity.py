from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from vemu.errors import ValidationError
from vemu.identity import (
    _is_immutable,
    _parse_os_release,
    _supported_os,
    new_machine_id,
    reset_linux,
    reset_windows,
    validate_hostname,
    validate_machine_id,
)


class IdentityTests(unittest.TestCase):
    def test_new_machine_id(self) -> None:
        value = new_machine_id()
        self.assertEqual(len(value), 32)
        self.assertEqual(validate_machine_id(value), value)

    def test_machine_id_invalid(self) -> None:
        with self.assertRaises(ValidationError):
            validate_machine_id("not-a-machine-id")
        with self.assertRaises(ValidationError):
            validate_machine_id("a" * 31)

    def test_hostname_valid(self) -> None:
        self.assertEqual(validate_hostname("sds-1"), "sds-1")
        self.assertEqual(validate_hostname("node01"), "node01")

    def test_hostname_invalid(self) -> None:
        for value in ("", "-node", "node-", "no de", "a" * 64):
            with self.assertRaises(ValidationError):
                validate_hostname(value)

    def test_dry_run(self) -> None:
        result = reset_linux(
            "/tmp/disk.qcow2",
            "sds-1",
            "0123456789abcdef0123456789abcdef",
            dry_run=True,
        )
        self.assertFalse(result.executed)
        self.assertIn("virt-customize", result.command)
        self.assertIn("--hostname", result.command)
        self.assertIn("sds-1", result.command)
        self.assertIn("rm -f /etc/ssh/ssh_host_*", result.command)
        self.assertIn("ssh-keygen -A", result.command)
        self.assertTrue(
            any("/etc/hostname" in part for part in result.command)
        )
        self.assertTrue(
            any("/etc/HOSTNAME" in part for part in result.command)
        )
        self.assertTrue(
            any("systemctl enable ssh 2>/dev/null" in part for part in result.command)
        )
        self.assertTrue(
            any("systemctl enable sshd" in part for part in result.command)
        )
        self.assertTrue(
            any("0123456789abcdef0123456789abcdef" in part for part in result.command)
        )

    def test_identity_reset_supported_os_detection(self) -> None:
        self.assertTrue(
            _supported_os(_parse_os_release("ID=ubuntu\nID_LIKE=debian\n"))
        )
        self.assertTrue(
            _supported_os(_parse_os_release("ID=opensuse-leap\n"))
        )
        self.assertTrue(
            _supported_os(_parse_os_release("ID=almalinux\nID_LIKE=rhel\n"))
        )
        self.assertFalse(
            _supported_os(_parse_os_release("ID=alpine\n"))
        )
        self.assertFalse(
            _supported_os(_parse_os_release("ID=void\n"))
        )

    def test_immutable_distro_detection(self) -> None:
        self.assertTrue(
            _is_immutable(
                _parse_os_release("ID=fedora\nVARIANT_ID=coreos\n")
            )
        )
        self.assertTrue(
            _is_immutable(
                _parse_os_release("ID=fedora\nVARIANT_ID=silverblue\n")
            )
        )
        self.assertTrue(
            _is_immutable(_parse_os_release("ID=opensuse-microos\n"))
        )
        self.assertFalse(
            _is_immutable(_parse_os_release("ID=fedora\n"))
        )

    def test_unsupported_identity_reset_can_be_skipped(self) -> None:
        machine_id = "0123456789abcdef0123456789abcdef"
        hostname = "dev-alpine"

        def fake_run(command, env=None):
            if command[-1] == "/etc/os-release":
                return SimpleNamespace(
                    stdout='ID=alpine\nNAME="Alpine Linux"\n', stderr=""
                )
            raise AssertionError(command)

        with mock.patch(
            "vemu.identity._require", return_value="/usr/bin/virt-customize"
        ), mock.patch("vemu.identity._run", side_effect=fake_run):
            result = reset_linux(
                "/tmp/disk.qcow2",
                hostname,
                machine_id,
                allow_unsupported=True,
            )
            self.assertTrue(result.skipped)
            self.assertFalse(result.executed)
            self.assertIn("Alpine", result.reason)
            with self.assertRaises(ValidationError):
                reset_linux("/tmp/disk.qcow2", hostname, machine_id)

    def test_hostname_verification_accepts_suse_file(self) -> None:
        machine_id = "0123456789abcdef0123456789abcdef"
        hostname = "dev-leap-16-1"

        def fake_run(command, env=None):
            if command[0] == "virt-customize":
                return SimpleNamespace(stdout="", stderr="")
            if command[-1] == "/etc/os-release":
                return SimpleNamespace(stdout="ID=opensuse-leap\n", stderr="")
            if command[-1] == "/etc/machine-id":
                return SimpleNamespace(stdout=machine_id, stderr="")
            if command[-1] == "/etc/hostname":
                return SimpleNamespace(stdout="", stderr="")
            if command[-1] == "/etc/HOSTNAME":
                return SimpleNamespace(stdout=hostname + "\n", stderr="")
            if command[0] == "virt-ls":
                return SimpleNamespace(
                    stdout="ssh_host_ed25519_key\n", stderr=""
                )
            raise AssertionError(command)

        with mock.patch(
            "vemu.identity._require", return_value="/usr/bin/virt-customize"
        ), mock.patch("vemu.identity._run", side_effect=fake_run):
            result = reset_linux(
                "/tmp/disk.qcow2", hostname, machine_id
            )
        self.assertTrue(result.executed)
        self.assertEqual(result.hostname, hostname)

    def test_windows_not_supported(self) -> None:
        with self.assertRaises(ValidationError):
            reset_windows()


if __name__ == "__main__":
    unittest.main()
