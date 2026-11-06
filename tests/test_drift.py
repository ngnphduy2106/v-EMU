from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vemu import drift, service
from vemu.backend import LibvirtBackend

XML = """<domain type='kvm'>
  <name>vm1</name>
  <metadata>
    <vemu:management xmlns:vemu="https://vemu.local/management"
      managed_by="v-EMU (full)" mode="full" origin="vemu"/>
  </metadata>
  <devices>
    <disk type='file' device='disk'>
      <source file='/tmp/a.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
  </devices>
</domain>"""


class DriftTests(unittest.TestCase):
    def test_baseline_clean_and_drifted(self) -> None:
        marked = drift.set_baseline(XML)
        self.assertEqual(drift.status(marked), "clean")
        changed = marked.replace("/tmp/a.qcow2", "/tmp/b.qcow2")
        self.assertEqual(drift.status(changed), "drifted")
        details = drift.details(changed)
        self.assertEqual(details["status"], "drifted")
        self.assertNotEqual(details["baseline"], details["current"])

    def test_order_and_metadata_are_ignored(self) -> None:
        marked = drift.set_baseline(XML)
        reordered = marked.replace(
            "    <disk type='file' device='disk'>\n"
            "      <source file='/tmp/a.qcow2'/>\n"
            "      <target dev='vda' bus='virtio'/>\n"
            "    </disk>",
            "    <disk device='disk' type='file'>\n"
            "      <target bus='virtio' dev='vda'/>\n"
            "      <source file='/tmp/a.qcow2'/>\n"
            "    </disk>",
        )
        self.assertEqual(drift.status(reordered), "clean")

    def test_missing_metadata_is_unknown(self) -> None:
        xml = XML.replace("<metadata>", "<metadata-removed>").replace(
            "</metadata>", "</metadata-removed>"
        )
        self.assertEqual(drift.status(xml), "unknown")

    def test_diff_reports_changed_attributes(self) -> None:
        baseline = drift.set_baseline(XML)
        changed = baseline.replace("/tmp/a.qcow2", "/tmp/b.qcow2")
        changes = drift.diff(baseline, changed)
        self.assertEqual(len(changes), 1)
        self.assertIn("source/@file", changes[0])
        self.assertIn("/tmp/a.qcow2", changes[0])
        self.assertIn("/tmp/b.qcow2", changes[0])

    def test_diff_ignores_order_and_metadata(self) -> None:
        baseline = drift.set_baseline(XML)
        reordered = baseline.replace(
            "    <disk type='file' device='disk'>\n"
            "      <source file='/tmp/a.qcow2'/>\n"
            "      <target dev='vda' bus='virtio'/>\n"
            "    </disk>",
            "    <disk device='disk' type='file'>\n"
            "      <target bus='virtio' dev='vda'/>\n"
            "      <source file='/tmp/a.qcow2'/>\n"
            "    </disk>",
        )
        self.assertEqual(drift.diff(baseline, reordered), [])

    def test_baseline_sidecar_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ, {"VEMU_BASELINE_DIR": tmp}
            ):
                marked = drift.set_baseline(XML)
                path = drift.store_baseline("vm1", marked)
                self.assertIsNotNone(path)
                self.assertTrue(Path(str(path)).is_file())
                self.assertEqual(drift.load_baseline("vm1"), marked)
                self.assertIsNone(drift.load_baseline("missing"))

    def test_domain_drift_includes_diff(self) -> None:
        marked = drift.set_baseline(XML)
        changed = marked.replace("/tmp/a.qcow2", "/tmp/b.qcow2")

        class FakeBackend:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def info(self, _name):
                return SimpleNamespace(managed_by="v-EMU (full)")

            def config_xml(self, _name):
                return changed

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ, {"VEMU_BASELINE_DIR": tmp}
            ), mock.patch(
                "vemu.service.LibvirtBackend", return_value=FakeBackend()
            ):
                drift.store_baseline("vm1", marked)
                payload = service.domain_drift("vm1")
        self.assertEqual(payload["status"], "drifted")
        self.assertTrue(payload["changes"])
        self.assertIn("source/@file", payload["changes"][0])

    def test_normalize_baseline_writes_sidecar(self) -> None:
        marked = drift.set_baseline(XML)

        class Domain:
            def name(self):
                return "vm1"

            def XMLDesc(self, _flag):
                return marked

        backend = LibvirtBackend.__new__(LibvirtBackend)
        backend._libvirt = SimpleNamespace(
            VIR_DOMAIN_XML_INACTIVE=1, libvirtError=Exception
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ, {"VEMU_BASELINE_DIR": tmp}
            ):
                backend._normalize_baseline(Domain())
                self.assertEqual(drift.load_baseline("vm1"), marked)

    def test_backup_xml_writes_and_prunes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ, {"VEMU_BACKUP_DIR": tmp}
            ):
                backend = LibvirtBackend.__new__(LibvirtBackend)
                path = backend._backup_xml("vm1", XML, reason="test")
                self.assertTrue(path.is_file())
                self.assertEqual(path.read_text(), XML)
                self.assertTrue(path.name.endswith("-test.xml"))


if __name__ == "__main__":
    unittest.main()
