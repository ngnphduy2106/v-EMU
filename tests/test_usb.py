from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from vemu.backend import LibvirtBackend
from vemu.errors import Conflict

HOSTDEV_XML = """<domain>
  <name>demo</name>
  <devices>
    <hostdev mode='subsystem' type='usb' managed='yes'>
      <source>
        <vendor id='0x30de'/>
        <product id='0x6544'/>
      </source>
    </hostdev>
  </devices>
</domain>"""


class FakeLibvirtError(Exception):
    pass


class FakeDevice:
    def __init__(self, xml: str) -> None:
        self.xml = xml

    def XMLDesc(self, _flags: int) -> str:
        return self.xml


class FakeConnection:
    def __init__(self, devices: list[FakeDevice], error: Exception | None = None):
        self.devices = devices
        self.error = error

    def listAllDevices(self, _flags: int) -> list[FakeDevice]:
        if self.error is not None:
            raise self.error
        return self.devices


def fake_backend(devices=None, error=None):
    backend = LibvirtBackend.__new__(LibvirtBackend)
    backend.connection = FakeConnection(devices or [], error)
    backend._libvirt = type(
        "FakeLibvirt",
        (),
        {"libvirtError": FakeLibvirtError},
    )
    backend.config_xml = lambda _name: HOSTDEV_XML
    return backend


class UsbBackendTests(unittest.TestCase):
    def test_missing_hostdev_is_listed_and_removable(self) -> None:
        backend = fake_backend()
        devices = backend.usb_devices("demo")
        self.assertEqual(len(devices), 1)
        self.assertTrue(devices[0]["missing"])
        self.assertTrue(devices[0]["attached"])
        self.assertFalse(devices[0]["present"])

        captured: dict[str, str] = {}

        def redefine(_name: str, xml: str) -> None:
            captured["xml"] = xml

        backend.redefine_domain = redefine
        removed = backend.drop_missing_usb_devices("demo")
        self.assertEqual(
            removed, [{"vendor": "0x30de", "product": "0x6544"}]
        )
        root = ET.fromstring(captured["xml"])
        self.assertEqual(
            root.findall("./devices/hostdev[@type='usb']"), []
        )

    def test_nodedev_socket_error_is_actionable(self) -> None:
        backend = fake_backend(
            error=FakeLibvirtError(
                "Failed to connect socket to "
                "'/var/run/libvirt/virtnodedevd-sock': No such file"
            )
        )
        with self.assertRaises(Conflict) as raised:
            backend.usb_devices("demo")
        message = str(raised.exception)
        self.assertIn("virtnodedevd.socket", message)
        self.assertIn("vemu doctor", message)


if __name__ == "__main__":
    unittest.main()
