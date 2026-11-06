from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from vemu import performance


def _xml(video: str = "vga") -> str:
    return (
        "<domain>"
        "<metadata><ns0:management "
        "xmlns:ns0='https://vemu.local/management' "
        "managed_by='v-EMU (full)'/></metadata>"
        "<devices><video>"
        f"<model type='{video}' heads='1' primary='yes'/>"
        "</video></devices></domain>"
    )


class PerformanceProbeTests(unittest.TestCase):
    def _probe(self, os_release: dict, **patches):
        with mock.patch(
            "vemu.performance._virt_tools",
            return_value=("virt-cat", "virt-customize", "virt-ls", {}),
        ), mock.patch(
            "vemu.performance.identity._detect_os",
            return_value=os_release,
        ), mock.patch(
            "vemu.performance._has_systemd",
            return_value=patches.get("systemd", True),
        ), mock.patch(
            "vemu.performance._has_tuned",
            return_value=patches.get("tuned", False),
        ), mock.patch(
            "vemu.performance.identity._supported_os",
            return_value=patches.get("supported", True),
        ), mock.patch(
            "vemu.performance.identity._is_immutable",
            return_value=patches.get("immutable", False),
        ):
            return performance.probe("vm1", "/tmp/disk.qcow2", _xml())

    def test_supported_vga_guest(self) -> None:
        result = self._probe(
            {"ID": "fedora", "PRETTY_NAME": "Fedora Linux 44"}
        )
        self.assertTrue(result.supported)
        self.assertTrue(result.tested)
        self.assertTrue(result.systemd)
        self.assertTrue(result.needs_video_change)
        self.assertEqual(result.video, "vga")
        self.assertEqual(result.distro, "Fedora Linux 44")

    def test_sles_is_in_tested_set(self) -> None:
        result = self._probe(
            {"ID": "sles", "PRETTY_NAME": "SUSE Linux Enterprise Server 16.0"}
        )
        self.assertTrue(result.supported)
        self.assertTrue(result.tested)

    def test_untested_systemd_guest_warns(self) -> None:
        result = self._probe(
            {"ID": "arch", "PRETTY_NAME": "Arch Linux"}
        )
        self.assertTrue(result.supported)
        self.assertFalse(result.tested)
        self.assertTrue(result.warnings)

    def test_immutable_guest_is_rejected(self) -> None:
        result = self._probe(
            {"ID": "fedora", "PRETTY_NAME": "Fedora Silverblue"},
            immutable=True,
        )
        self.assertFalse(result.supported)
        self.assertIn("immutable", result.reason)

    def test_non_systemd_guest_is_rejected(self) -> None:
        result = self._probe(
            {"ID": "alpine", "PRETTY_NAME": "Alpine Linux"},
            systemd=False,
        )
        self.assertFalse(result.supported)
        self.assertIn("systemd", result.reason)

    def test_apply_switches_video_to_virtio(self) -> None:
        supported = performance.PerformanceProbe(
            domain="vm1",
            disk="/tmp/disk.qcow2",
            supported=True,
            video="vga",
            needs_video_change=True,
        )
        with mock.patch(
            "vemu.performance.probe", return_value=supported
        ), mock.patch(
            "vemu.performance._virt_tools",
            return_value=("virt-cat", "virt-customize", "virt-ls", {}),
        ), mock.patch(
            "vemu.performance._write_tuning"
        ), mock.patch(
            "vemu.performance._verify_tuning"
        ):
            result = performance.apply(
                "vm1", "/tmp/disk.qcow2", _xml("vga")
            )
        self.assertTrue(result["xml_changed"])
        self.assertTrue(result["video_changed"])
        root = ET.fromstring(result["xml"])
        self.assertEqual(
            root.find("./devices/video/model").get("type"), "virtio"
        )
        self.assertIsNone(
            root.find("./devices/video/model").get("vram")
        )


if __name__ == "__main__":
    unittest.main()
