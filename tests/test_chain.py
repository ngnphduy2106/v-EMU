from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from vemu.chain import ChainManager
from vemu.errors import Conflict

QEMU_IMG = shutil.which("qemu-img")


@unittest.skipUnless(QEMU_IMG, "qemu-img is required")
class ChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.base = self.root / "base.qcow2"
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", str(self.base), "16M"],
            check=True,
            capture_output=True,
        )
        self.chain_dir = self.root / "chain"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create(self) -> ChainManager:
        return ChainManager.create(self.chain_dir, self.base)

    def test_create(self) -> None:
        manager = self._create()
        chain = manager.load()
        self.assertEqual(chain.active, "0001")
        self.assertEqual(len(chain.layers), 1)
        self.assertEqual(chain.version, 2)
        self.assertIn("vda", chain.disks)
        self.assertTrue(Path(chain.active_disk()).is_file())
        self.assertTrue(manager.chain_file.is_file())
        self.assertEqual(manager.verify(), [])

    def test_snapshot_and_revert(self) -> None:
        manager = self._create()
        first = manager.load().active
        layer = manager.snapshot("before-change")
        self.assertEqual(layer.id, "0002")
        self.assertEqual(manager.load().active, "0002")

        manager.revert(first or "0001")
        self.assertEqual(manager.load().active, "0001")
        self.assertEqual(manager.verify(), [])

    def test_snapshot_description_and_update(self) -> None:
        manager = self._create()
        layer = manager.snapshot(
            "before-change", "Clean state before the upgrade"
        )
        self.assertEqual(layer.description, "Clean state before the upgrade")
        info = manager.info()
        self.assertEqual(
            info["layers"][-1]["description"],
            "Clean state before the upgrade",
        )

        updated = manager.update_snapshot(
            layer.id,
            label="before-upgrade",
            description="Retry after the network fix",
        )
        self.assertEqual(updated.label, "before-upgrade")
        self.assertEqual(updated.description, "Retry after the network fix")
        self.assertEqual(manager.load().active, layer.id)
        self.assertEqual(manager.verify(), [])

    def test_snapshot_branch_after_revert(self) -> None:
        manager = self._create()
        manager.snapshot("first-branch")
        manager.revert("0001")
        branch = manager.snapshot("second-branch")
        self.assertEqual(branch.id, "0003")
        self.assertEqual(branch.parent, "0001")
        chain = manager.load()
        self.assertEqual(chain.active, "0003")
        self.assertEqual(len(chain.children("0001")), 2)
        self.assertEqual(manager.verify(), [])
        self.assertTrue(Path(chain.layer("0002").path).is_file())
        self.assertTrue(Path(chain.layer("0003").path).is_file())

        manager.revert("0002")
        self.assertEqual(manager.load().active, "0002")
        manager.delete("0003")
        self.assertEqual([layer.id for layer in manager.load().layers], ["0001", "0002"])
        self.assertEqual(manager.verify(), [])

    def test_delete_branched_parent_requires_leaf_cleanup(self) -> None:
        manager = self._create()
        manager.snapshot("first-branch")
        manager.revert("0001")
        manager.snapshot("second-branch")
        with self.assertRaises(Conflict) as raised:
            manager.delete("0001")
        self.assertIn("child snapshots", str(raised.exception))

    def test_delete_middle(self) -> None:
        manager = self._create()
        manager.snapshot("b1")
        manager.snapshot("b2")
        self.assertEqual([layer.id for layer in manager.load().layers], ["0001", "0002", "0003"])

        manager.delete("0002")
        chain = manager.load()
        self.assertEqual([layer.id for layer in chain.layers], ["0001", "0003"])
        self.assertEqual(chain.layer("0003").parent, "0001")
        self.assertEqual(chain.active, "0003")
        self.assertTrue(Path(chain.layer("0003").path).is_file())
        self.assertFalse((self.chain_dir / "disk-vda-0002.qcow2").exists())
        self.assertEqual(manager.verify(), [])

    def test_delete_first_with_children_fails(self) -> None:
        manager = self._create()
        manager.snapshot("b1")
        with self.assertRaises(Conflict):
            manager.delete("0001")

    def test_delete_only_layer_fails(self) -> None:
        manager = self._create()
        with self.assertRaises(Conflict):
            manager.delete("0001")

    def test_delete_active_requires_force(self) -> None:
        manager = self._create()
        manager.snapshot("b1")
        with self.assertRaises(Conflict):
            manager.delete("0002")
        manager.delete("0002", force=True)
        chain = manager.load()
        self.assertEqual(chain.active, "0001")
        self.assertEqual(len(chain.layers), 1)

    def test_info_reports_sizes(self) -> None:
        manager = self._create()
        manager.snapshot("b1")
        info = manager.info()
        self.assertEqual(info["active"], "0002")
        self.assertEqual(len(info["layers"]), 2)
        self.assertGreaterEqual(info["layers"][0]["actual_size"], 0)

    def test_chain_backup_written(self) -> None:
        manager = self._create()
        manager.snapshot("b1")
        backup = manager.chain_file.with_suffix(".json.bak")
        self.assertTrue(backup.is_file())

    def test_find_orphan_layers(self) -> None:
        from vemu.chain import find_orphan_layers

        manager = self._create()
        manager.snapshot("b1")
        orphan = self.chain_dir / "disk-9999.qcow2"
        orphan.write_bytes(b"orphan")
        template = self.root / "template.qcow2"
        template.write_bytes(b"template")
        self.assertEqual(find_orphan_layers(self.root), [orphan])

    def test_recover_interrupted_commit(self) -> None:
        from unittest import mock

        from vemu.chain import COMMIT_JOURNAL, find_commit_journals

        manager = self._create()
        manager.snapshot("b1")
        manager.snapshot("b2")
        with mock.patch.object(
            manager, "_run", side_effect=RuntimeError("qemu killed")
        ):
            with self.assertRaises(RuntimeError):
                manager.delete("0002")
        journal = self.chain_dir / COMMIT_JOURNAL
        self.assertTrue(journal.is_file())
        self.assertIn("commit journal", "\n".join(manager.verify()))
        self.assertEqual(find_commit_journals(self.root), [journal])

        result = manager.recover_commit()
        self.assertIsNotNone(result)
        self.assertTrue(result["recovered"])
        chain = manager.load()
        self.assertEqual([layer.id for layer in chain.layers], ["0001", "0003"])
        self.assertEqual(chain.layer("0003").parent, "0001")
        self.assertEqual(chain.active, "0003")
        self.assertFalse(journal.exists())
        self.assertEqual(manager.verify(), [])


if __name__ == "__main__":
    unittest.main()
