from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vemu import service
from vemu.chain import ChainManager
from vemu.network import NetworkProfile

QEMU_IMG = shutil.which("qemu-img")


@unittest.skipUnless(QEMU_IMG, "qemu-img is required")
class ImportTests(unittest.TestCase):
    def test_vcsa_preset_is_tiny(self) -> None:
        specs = service._import_specs("vcsa")
        self.assertEqual(specs["vcpus"], 2)
        self.assertEqual(specs["memory_mib"], 14336)
        self.assertEqual(specs["firmware"], "efi")
        self.assertEqual(specs["disk_bus"], "sata")
        self.assertEqual(specs["nic_model"], "e1000e")
        self.assertEqual(specs["video"], "virtio")
        self.assertEqual(specs["nic_count"], 1)
        self.assertFalse(specs["serial_console"])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.labs = self.root / "labs"
        self.labs.mkdir()
        self.storage = self.root / "storage.json"
        self.storage.write_text(
            json.dumps(
                {
                    "version": 1,
                    "locations": [{"path": str(self.labs), "role": "labs"}],
                }
            )
        )
        self.network = self.root / "networks.json"
        self.network.write_text(
            json.dumps(
                {
                    "version": 1,
                    "networks": [
                        NetworkProfile(name="NAT", mode="nat").to_dict()
                    ],
                }
            )
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _backend(self):
        class FakeBackend:
            def __init__(self) -> None:
                self.xml = ""
                self.started = False
                self._mac = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def define(self, xml):
                self.xml = xml
                return SimpleNamespace(name="imported")

            def start(self, _name):
                self.started = True

            def unique_mac(self):
                self._mac += 1
                return f"52:54:00:00:00:{self._mac:02x}"

        return FakeBackend()

    def test_import_qcow2_with_appliance_preset(self) -> None:
        source = self.root / "appliance.qcow2"
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", str(source), "16M"],
            check=True,
            capture_output=True,
        )
        backend = self._backend()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.vm_import(
                "disk",
                str(source),
                "appliance-test",
                preset="appliance",
                network="NAT",
                network_config=str(self.network),
                storage_config=str(self.storage),
                data_disk_gb=1,
            )
        self.assertTrue(result["defined"])
        destination = Path(result["destination"])
        data = json.loads((destination / "chain.json").read_text())
        self.assertEqual(data["version"], 2)
        self.assertEqual(set(data["disks"]), {"vda", "vdb"})
        self.assertTrue((destination / "base-vda.qcow2").is_file())
        self.assertTrue((destination / "base-vdb.qcow2").is_file())
        self.assertIn("<interface", backend.xml)
        self.assertEqual(backend.xml.count("<interface"), 2)
        self.assertIn('<target dev="vdb"', backend.xml)
        self.assertEqual(ChainManager(destination).verify(), [])

    def test_import_single_ova(self) -> None:
        vmdk = self.root / "disk1.vmdk"
        subprocess.run(
            [QEMU_IMG, "create", "-f", "vmdk", str(vmdk), "16M"],
            check=True,
            capture_output=True,
        )
        ovf = """<?xml version="1.0"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
 xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
 xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
 xmlns:vmw="http://www.vmware.com/schema/ovf">
 <References><File ovf:href="disk1.vmdk" ovf:id="file1" ovf:size="16777216"/></References>
 <DiskSection><Disk ovf:capacity="16" ovf:capacityAllocationUnits="byte * 2^20"
   ovf:diskId="vmdisk1" ovf:fileRef="file1"/></DiskSection>
 <VirtualSystem><Name>ova-test</Name>
  <OperatingSystemSection ovf:id="100" vmw:osType="otherLinux64Guest"/>
  <VirtualHardwareSection>
   <Item><ResourceType>3</ResourceType><VirtualQuantity>1</VirtualQuantity></Item>
   <Item><ResourceType>4</ResourceType><VirtualQuantity>1024</VirtualQuantity>
     <AllocationUnits>byte * 2^20</AllocationUnits></Item>
   <Item><ResourceType>17</ResourceType><HostResource>ovf:/disk/vmdisk1</HostResource></Item>
   <Item><ResourceType>10</ResourceType><Connection>VM Network</Connection>
     <ResourceSubType>VmxNet3</ResourceSubType></Item>
   <vmw:Config vmw:key="firmware" vmw:value="bios"/>
  </VirtualHardwareSection>
 </VirtualSystem>
</Envelope>"""
        archive = self.root / "appliance.ova"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("appliance.ovf", ovf)
            z.writestr("disk1.vmdk", vmdk.read_bytes())
        backend = self._backend()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            result = service.vm_import(
                "ova",
                str(archive),
                "ova-import-test",
                preset="appliance",
                network="NAT",
                network_config=str(self.network),
                storage_config=str(self.storage),
                vcpus=1,
                memory_mib=1024,
                nic_count=1,
                video="virtio",
            )
        self.assertTrue(result["defined"])
        self.assertEqual(result["specs"]["video"], "virtio")
        destination = Path(result["destination"])
        data = json.loads((destination / "chain.json").read_text())
        self.assertEqual(data["version"], 2)
        self.assertIn("<source file=", backend.xml)
        self.assertEqual(backend.xml.count("<interface"), 1)
        self.assertEqual(ChainManager(destination).verify(), [])


if __name__ == "__main__":
    unittest.main()
