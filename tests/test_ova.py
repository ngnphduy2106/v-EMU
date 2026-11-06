from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from vemu import ova
from vemu.errors import ValidationError

OVF = """<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
  xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
  xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
  xmlns:vmw="http://www.vmware.com/schema/ovf">
  <References>
    <File ovf:href="disk1.vmdk" ovf:id="file1" ovf:size="100"/>
    <File ovf:href="disk2.vmdk" ovf:id="file2" ovf:size="200"/>
  </References>
  <DiskSection>
    <Disk ovf:capacity="60" ovf:capacityAllocationUnits="byte * 2^30"
      ovf:diskId="vmdisk1" ovf:fileRef="file1"
      ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"/>
    <Disk ovf:capacity="2" ovf:capacityAllocationUnits="byte * 2^30"
      ovf:diskId="vmdisk2" ovf:fileRef="file2"/>
  </DiskSection>
  <VirtualSystem ovf:id="appliance">
    <Name>appliance-vm</Name>
    <OperatingSystemSection ovf:id="107" vmw:osType="centos64Guest"/>
    <VirtualHardwareSection>
      <Item><ResourceType>3</ResourceType><VirtualQuantity>2</VirtualQuantity></Item>
      <Item><ResourceType>4</ResourceType><VirtualQuantity>8192</VirtualQuantity>
        <AllocationUnits>byte * 2^20</AllocationUnits></Item>
      <Item><ResourceType>17</ResourceType><HostResource>ovf:/disk/vmdisk1</HostResource>
        <AddressOnParent>0</AddressOnParent></Item>
      <Item><ResourceType>17</ResourceType><HostResource>ovf:/disk/vmdisk2</HostResource>
        <AddressOnParent>1</AddressOnParent></Item>
      <Item><ResourceType>10</ResourceType><Connection>MGMT</Connection>
        <ResourceSubType>VmxNet3</ResourceSubType><Address>52:54:00:aa:bb:cc</Address></Item>
      <Item><ResourceType>10</ResourceType><Connection>DATA</Connection>
        <ResourceSubType>E1000e</ResourceSubType></Item>
      <Item><ResourceType>10</ResourceType><Connection>DMZ</Connection>
        <ResourceSubType>VmxNet3</ResourceSubType></Item>
      <Item><ResourceType>21</ResourceType></Item>
      <vmw:Config vmw:key="firmware" vmw:value="efi"/>
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>"""


PROFILE_OVF = """<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
  xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
  xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
  xmlns:vmw="http://www.vmware.com/schema/ovf">
  <References>
    <File ovf:href="system.vmdk" ovf:id="file1" ovf:size="100"/>
    <File ovf:href="cloud.vmdk" ovf:id="file2" ovf:size="200"/>
    <File ovf:href="swap.vmdk" ovf:id="file3" ovf:size="300"/>
  </References>
  <DiskSection>
    <Disk ovf:capacity="49728" ovf:capacityAllocationUnits="byte * 2^20"
      ovf:diskId="system" ovf:fileRef="file1"/>
    <Disk ovf:capacity="7444" ovf:capacityAllocationUnits="byte * 2^20"
      ovf:diskId="cloudcomponents" ovf:fileRef="file2"/>
    <Disk ovf:capacity="2112" ovf:capacityAllocationUnits="byte * 2^20"
      ovf:diskId="swap" ovf:fileRef="file3"/>
    <Disk ovf:capacity="100" ovf:capacityAllocationUnits="byte * 2^30"
      ovf:diskId="disk100GB"/>
  </DiskSection>
  <DeploymentOptionSection>
    <Configuration ovf:id="large"><Label>Large</Label></Configuration>
    <Configuration ovf:default="true" ovf:id="tiny"><Label>Tiny</Label></Configuration>
  </DeploymentOptionSection>
  <VirtualSystem ovf:id="appliance">
    <Name>profile-vm</Name>
    <VirtualHardwareSection>
      <Item configuration="large"><ResourceType>3</ResourceType>
        <VirtualQuantity>8</VirtualQuantity></Item>
      <Item configuration="tiny"><ResourceType>3</ResourceType>
        <VirtualQuantity>2</VirtualQuantity></Item>
      <Item configuration="large"><ResourceType>4</ResourceType>
        <VirtualQuantity>30720</VirtualQuantity>
        <AllocationUnits>byte * 2^20</AllocationUnits></Item>
      <Item configuration="tiny"><ResourceType>4</ResourceType>
        <VirtualQuantity>14336</VirtualQuantity>
        <AllocationUnits>byte * 2^20</AllocationUnits></Item>
      <Item><ResourceType>17</ResourceType>
        <HostResource>ovf:/disk/system</HostResource>
        <AddressOnParent>0</AddressOnParent></Item>
      <Item><ResourceType>17</ResourceType>
        <HostResource>ovf:/disk/cloudcomponents</HostResource>
        <AddressOnParent>1</AddressOnParent></Item>
      <Item configuration="tiny"><ResourceType>17</ResourceType>
        <HostResource>ovf:/disk/disk100GB</HostResource>
        <AddressOnParent>2</AddressOnParent></Item>
      <Item configuration="tiny"><ResourceType>10</ResourceType>
        <Connection>MGMT</Connection>
        <ResourceSubType>VmxNet3</ResourceSubType></Item>
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>"""


class OvaProfileTests(unittest.TestCase):
    def test_default_profile_filters_items(self) -> None:
        info = ova.parse_ovf(
            PROFILE_OVF,
            member_sizes={
                "system.vmdk": 100,
                "cloud.vmdk": 200,
                "swap.vmdk": 300,
            },
        )
        self.assertEqual(info.profile, "tiny")
        self.assertEqual(info.vcpus, 2)
        self.assertEqual(info.memory_mib, 14336)
        self.assertFalse(info.firmware_explicit)
        self.assertFalse(info.serial_explicit)
        self.assertEqual(len(info.nics), 1)

    def test_unattached_file_disk_is_skipped(self) -> None:
        info = ova.parse_ovf(
            PROFILE_OVF,
            member_sizes={
                "system.vmdk": 100,
                "cloud.vmdk": 200,
                "swap.vmdk": 300,
            },
        )
        self.assertEqual(
            [disk.disk_id for disk in info.disks],
            ["system", "cloudcomponents", "disk100GB"],
        )
        self.assertTrue(info.disks[2].synthetic)
        self.assertTrue(
            any("does not attach" in warning for warning in info.warnings)
        )
        self.assertTrue(
            any("deployment profiles" in warning for warning in info.warnings)
        )

    def test_empty_disks_are_created(self) -> None:
        info = ova.parse_ovf(
            PROFILE_OVF,
            member_sizes={
                "system.vmdk": 100,
                "cloud.vmdk": 200,
                "swap.vmdk": 300,
            },
        )
        empty = [disk for disk in info.disks if disk.synthetic]
        self.assertEqual([disk.disk_id for disk in empty], ["disk100GB"])
        self.assertEqual(empty[0].capacity, 100 * 1024**3)
        self.assertTrue(
            any("empty disk" in warning for warning in info.warnings)
        )

    def test_each_empty_disk_item_is_created(self) -> None:
        ovf = PROFILE_OVF.replace(
            '<Item configuration="tiny"><ResourceType>17</ResourceType>\n'
            '        <HostResource>ovf:/disk/disk100GB</HostResource>\n'
            '        <AddressOnParent>2</AddressOnParent></Item>',
            '<Item configuration="tiny"><ResourceType>17</ResourceType>'
            '<HostResource>ovf:/disk/disk100GB</HostResource></Item>'
            '<Item configuration="tiny"><ResourceType>17</ResourceType>'
            '<HostResource>ovf:/disk/disk100GB</HostResource></Item>',
        )
        info = ova.parse_ovf(
            ovf,
            member_sizes={
                "system.vmdk": 100,
                "cloud.vmdk": 200,
                "swap.vmdk": 300,
            },
        )
        empty = [disk for disk in info.disks if disk.synthetic]
        self.assertEqual(len(empty), 2)
        self.assertEqual(empty[0].capacity, 100 * 1024**3)


class OvaTests(unittest.TestCase):
    def test_parse_ovf(self) -> None:
        info = ova.parse_ovf(
            OVF,
            member_sizes={"disk1.vmdk": 100, "disk2.vmdk": 200},
        )
        self.assertEqual(info.name, "appliance-vm")
        self.assertEqual(info.vcpus, 2)
        self.assertEqual(info.memory_mib, 8192)
        self.assertEqual(info.firmware, "efi")
        self.assertTrue(info.firmware_explicit)
        self.assertTrue(info.serial_explicit)
        self.assertTrue(info.serial)
        self.assertEqual([d.file for d in info.disks], ["disk1.vmdk", "disk2.vmdk"])
        self.assertEqual(info.disks[0].capacity, 60 * 1024**3)
        self.assertEqual(info.disks[0].file_size, 100)
        self.assertEqual(len(info.nics), 3)
        self.assertEqual(info.nics[0].mac, "52:54:00:aa:bb:cc")
        self.assertEqual(info.nics[2].model, "VmxNet3")

    def test_inspect_and_extract_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "appliance.ova"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("appliance.ovf", OVF)
                z.writestr("disk1.vmdk", b"x" * 100)
                z.writestr("disk2.vmdk", b"y" * 200)
            info, choices = ova.inspect_archive(archive)
            self.assertEqual(info.ovf_name, "appliance.ovf")
            self.assertEqual(choices, ["appliance.ovf"])
            extracted, extracted_info = ova.extract_archive(
                archive, root / "extract"
            )
            self.assertEqual(extracted_info.name, "appliance-vm")
            self.assertTrue((root / "extract" / "disk1.vmdk").is_file())
            self.assertTrue((root / "extract" / "disk2.vmdk").is_file())

    def test_unsafe_archive_path_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bad.ova"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("../evil.vmdk", b"x")
                z.writestr("appliance.ovf", OVF)
            with self.assertRaises(ValidationError):
                ova.extract_archive(archive, root / "extract")

    def test_inspect_disk_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "kvm.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("fortios.qcow2", b"QFI" + b"\x00" * 20)
            name, size, fmt = ova.inspect_disk_archive(archive)
            self.assertEqual(name, "fortios.qcow2")
            self.assertEqual(size, 23)
            self.assertEqual(fmt, "qcow2")
            path, source_format = ova.extract_disk_archive(
                archive, root / "extract"
            )
            self.assertEqual(source_format, "qcow2")
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
