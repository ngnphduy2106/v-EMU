from __future__ import annotations

import unittest
from unittest import mock

from vemu import diagnose


class SecurityStatusTests(unittest.TestCase):
    @mock.patch.object(diagnose, "_apparmor_profiles", return_value=42)
    @mock.patch.object(diagnose, "_selinux_mode", return_value="Disabled")
    @mock.patch.object(
        diagnose, "_active_lsms", return_value=["lockdown", "apparmor"]
    )
    def test_apparmor_is_primary_on_debian(self, *_mocks) -> None:
        status = diagnose._security_status()
        self.assertEqual(status["active"], "apparmor")
        self.assertFalse(status["selinux"]["active"])
        self.assertTrue(status["apparmor"]["active"])
        self.assertEqual(status["apparmor"]["profiles"], 42)

    @mock.patch.object(diagnose, "_apparmor_profiles", return_value=None)
    @mock.patch.object(diagnose, "_selinux_mode", return_value="Enforcing")
    @mock.patch.object(
        diagnose, "_active_lsms", return_value=["lockdown", "selinux"]
    )
    def test_selinux_is_primary(self, *_mocks) -> None:
        status = diagnose._security_status()
        self.assertEqual(status["active"], "selinux")
        self.assertTrue(status["selinux"]["active"])
        self.assertFalse(status["apparmor"]["active"])

    @mock.patch.object(diagnose, "_apparmor_enabled", return_value=False)
    @mock.patch.object(diagnose, "_apparmor_profiles", return_value=None)
    @mock.patch.object(diagnose, "_selinux_mode", return_value="not installed")
    @mock.patch.object(diagnose, "_active_lsms", return_value=[])
    def test_no_lsm(self, *_mocks) -> None:
        status = diagnose._security_status()
        self.assertEqual(status["active"], "none")
        self.assertFalse(status["selinux"]["active"])
        self.assertFalse(status["apparmor"]["active"])


class IpForwardCheckTests(unittest.TestCase):
    def test_enabled_is_ok(self) -> None:
        check = diagnose._ip_forward_check("1", nat_registered=False)
        self.assertEqual(check["level"], "ok")
        self.assertEqual(check["detail"], "1")

    def test_unknown_without_nat_is_warning(self) -> None:
        check = diagnose._ip_forward_check("unknown", nat_registered=False)
        self.assertEqual(check["level"], "warn")
        self.assertFalse(check["required"])
        self.assertIn("no NAT network", check["detail"])

    def test_zero_without_nat_is_warning(self) -> None:
        check = diagnose._ip_forward_check("0", nat_registered=False)
        self.assertEqual(check["level"], "warn")
        self.assertFalse(check["required"])

    def test_zero_with_nat_is_failure(self) -> None:
        check = diagnose._ip_forward_check("0", nat_registered=True)
        self.assertEqual(check["level"], "fail")
        self.assertTrue(check["required"])
        self.assertIn("NAT network registered", check["detail"])

    def test_unknown_with_nat_is_warning(self) -> None:
        check = diagnose._ip_forward_check("unknown", nat_registered=True)
        self.assertEqual(check["level"], "warn")
        self.assertFalse(check["required"])


if __name__ == "__main__":
    unittest.main()
