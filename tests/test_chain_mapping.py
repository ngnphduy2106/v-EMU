from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory

from vemu import service
from vemu.chain import Chain, Layer, MultiDiskChain
from vemu.errors import Conflict, NotFound


def _chain(root: Path, active: str = "0002") -> Chain:
    return Chain(
        version=1,
        base=str(root / "base.qcow2"),
        layers=[
            Layer("0001", str(root / "disk-0001.qcow2"), "base"),
            Layer("0002", str(root / "disk-0002.qcow2"), "0001"),
        ],
        active=active,
    )


def _multi(root: Path) -> MultiDiskChain:
    return MultiDiskChain(
        version=2,
        disks={
            "vda": Chain(
                version=1,
                base=str(root / "base-vda.qcow2"),
                layers=[
                    Layer("0001", str(root / "disk-vda-0001.qcow2"), "base"),
                    Layer("0002", str(root / "disk-vda-0002.qcow2"), "0001"),
                ],
                active="0002",
                target="vda",
            ),
            "vdb": Chain(
                version=1,
                base=str(root / "base-vdb.qcow2"),
                layers=[
                    Layer("0001", str(root / "disk-vdb-0001.qcow2"), "base"),
                    Layer("0002", str(root / "disk-vdb-0002.qcow2"), "0001"),
                ],
                active="0002",
                target="vdb",
            ),
        },
    )


def _xml(disks: list[tuple[str, str]]) -> str:
    root = ET.Element("domain")
    devices = ET.SubElement(root, "devices")
    for target, source in disks:
        disk = ET.SubElement(devices, "disk", type="file", device="disk")
        ET.SubElement(disk, "source", file=source)
        ET.SubElement(disk, "target", dev=target)
    return ET.tostring(root, encoding="unicode")


class FakeBackend:
    def __init__(self, xml: str) -> None:
        self.xml = xml

    def config_xml(self, _name: str) -> str:
        return self.xml


class ChainMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_v1_matches_chain_disk_not_first_disk(self) -> None:
        chain = _chain(self.root, active="0002")
        extra = str(self.root / "extra.qcow2")
        backend = FakeBackend(
            _xml(
                [
                    ("vda", extra),
                    ("vdb", str(self.root / "disk-0001.qcow2")),
                ]
            )
        )
        changes = service._chain_source_changes(backend, "vm1", chain)
        self.assertEqual(
            changes,
            {str(self.root / "disk-0001.qcow2"): str(self.root / "disk-0002.qcow2")},
        )

    def test_v1_rejects_duplicate_chain_layers(self) -> None:
        chain = _chain(self.root)
        backend = FakeBackend(
            _xml(
                [
                    ("vda", str(self.root / "disk-0002.qcow2")),
                    ("vdb", str(self.root / "disk-0001.qcow2")),
                ]
            )
        )
        with self.assertRaises(Conflict):
            service._chain_source_changes(backend, "vm1", chain)

    def test_v1_rejects_domain_without_chain_disk(self) -> None:
        chain = _chain(self.root)
        backend = FakeBackend(
            _xml([("vda", str(self.root / "extra.qcow2"))])
        )
        with self.assertRaises(NotFound):
            service._chain_source_changes(backend, "vm1", chain)

    def test_v2_maps_each_target_and_ignores_extra_disk(self) -> None:
        chain = _multi(self.root)
        extra = str(self.root / "extra.qcow2")
        backend = FakeBackend(
            _xml(
                [
                    ("vda", str(self.root / "disk-vda-0001.qcow2")),
                    ("vdb", str(self.root / "disk-vdb-0001.qcow2")),
                    ("vdc", extra),
                ]
            )
        )
        changes = service._chain_source_changes(backend, "vm1", chain)
        self.assertEqual(
            changes,
            {
                str(self.root / "disk-vda-0001.qcow2"): str(
                    self.root / "disk-vda-0002.qcow2"
                ),
                str(self.root / "disk-vdb-0001.qcow2"): str(
                    self.root / "disk-vdb-0002.qcow2"
                ),
            },
        )

    def test_v2_rejects_chain_layer_on_unmapped_target(self) -> None:
        chain = _multi(self.root)
        backend = FakeBackend(
            _xml(
                [
                    ("vda", str(self.root / "disk-vda-0002.qcow2")),
                    ("vdb", str(self.root / "disk-vdb-0002.qcow2")),
                    ("vdc", str(self.root / "disk-vda-0001.qcow2")),
                ]
            )
        )
        with self.assertRaises(Conflict):
            service._chain_source_changes(backend, "vm1", chain)


if __name__ == "__main__":
    unittest.main()
