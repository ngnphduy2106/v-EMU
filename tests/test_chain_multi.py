from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu.chain import ChainManager, SnapshotSet, find_orphan_layers
from vemu.errors import Conflict, ValidationError

QEMU_IMG = shutil.which("qemu-img")


@unittest.skipUnless(QEMU_IMG, "qemu-img is required")
class MultiDiskChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.chain_dir = self.root / "chain"
        self.bases: dict[str, Path] = {}
        for target in ("sda", "sdc"):
            base = self.root / f"base-{target}.qcow2"
            subprocess.run(
                [QEMU_IMG, "create", "-f", "qcow2", str(base), "16M"],
                check=True,
                capture_output=True,
            )
            self.bases[target] = base

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create(self) -> ChainManager:
        return ChainManager.create_multi(
            self.chain_dir,
            [
                {"target": target, "bus": "sata", "base": base}
                for target, base in self.bases.items()
            ],
        )

    def test_create_multi(self) -> None:
        manager = self._create()
        data = json.loads(manager.chain_file.read_text())
        self.assertEqual(data["version"], 2)
        self.assertEqual(set(data["disks"]), {"sda", "sdc"})
        chain = manager.load()
        self.assertTrue(getattr(chain, "multi_disk", False))
        self.assertEqual(chain.active_set(), "0001")
        self.assertEqual(
            set(chain.active_disks()),
            {"sda", "sdc"},
        )
        for target in ("sda", "sdc"):
            self.assertTrue(
                (self.chain_dir / f"disk-{target}-0001.qcow2").is_file()
            )
        info = manager.info()
        self.assertTrue(info["multi_disk"])
        self.assertEqual(info["disk_count"], 2)
        self.assertEqual(len(info["disks"]), 2)
        self.assertEqual(manager.verify(), [])

    def test_create_single_disk_v2(self) -> None:
        manager = ChainManager.create_multi(
            self.chain_dir,
            [{"target": "vda", "bus": "virtio", "base": self.bases["sda"]}],
        )
        data = json.loads(manager.chain_file.read_text())
        self.assertEqual(data["version"], 2)
        self.assertEqual(set(data["disks"]), {"vda"})
        chain = manager.load()
        self.assertEqual(len(chain.disks), 1)
        self.assertTrue((self.chain_dir / "disk-vda-0001.qcow2").is_file())
        snapshot = manager.snapshot("one")
        self.assertEqual(snapshot.id, "0002")
        manager.revert("0001")
        self.assertEqual(manager.load().active_set(), "0001")
        manager.delete("0002", force=True)
        self.assertEqual(manager.load().active_set(), "0001")

    def test_snapshot_set_and_revert(self) -> None:
        manager = self._create()
        snapshot = manager.snapshot("before-change")
        self.assertIsInstance(snapshot, SnapshotSet)
        self.assertEqual(snapshot.id, "0002")
        self.assertEqual(set(snapshot.layers), {"sda", "sdc"})
        chain = manager.load()
        self.assertEqual(chain.active_set(), "0002")
        for target in ("sda", "sdc"):
            self.assertTrue(
                (self.chain_dir / f"disk-{target}-0002.qcow2").is_file()
            )
        manager.revert("0001")
        self.assertEqual(manager.load().active_set(), "0001")
        manager.reset()
        self.assertEqual(manager.load().active_set(), "0001")
        self.assertEqual(manager.verify(), [])

    def test_multi_snapshot_description_is_shared_and_updatable(self) -> None:
        manager = self._create()
        snapshot = manager.snapshot(
            "before-change", "Shared state across both disks"
        )
        self.assertEqual(
            snapshot.description, "Shared state across both disks"
        )
        for layer in snapshot.layers.values():
            self.assertEqual(layer.description, "Shared state across both disks")

        updated = manager.update_snapshot(
            snapshot.id,
            label="before-upgrade",
            description="Updated notes",
        )
        self.assertEqual(updated.label, "before-upgrade")
        self.assertEqual(updated.description, "Updated notes")
        for layer in updated.layers.values():
            self.assertEqual(layer.description, "Updated notes")
        info = manager.info()
        merged = {
            layer["id"]: layer for layer in info["layers"]
        }
        self.assertEqual(merged[snapshot.id]["description"], "Updated notes")
        self.assertEqual(manager.verify(), [])

    def test_import_multi_preserves_external_snapshot_layers(self) -> None:
        base = self.bases["sda"]
        overlay1 = self.root / "disk.SS0"
        overlay2 = self.root / "disk.SS1"
        qemu_io = shutil.which("qemu-io")
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", "-F", "qcow2",
             "-b", str(base), str(overlay1)],
            check=True, capture_output=True,
        )
        if qemu_io:
            subprocess.run(
                [qemu_io, "-f", "qcow2", "-c", "write -P 0x11 0 4k",
                 str(overlay1)],
                check=True, capture_output=True,
            )
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", "-F", "qcow2",
             "-b", str(overlay1), str(overlay2)],
            check=True, capture_output=True,
        )
        if qemu_io:
            subprocess.run(
                [qemu_io, "-f", "qcow2", "-c", "write -P 0x22 8k 4k",
                 str(overlay2)],
                check=True, capture_output=True,
            )
        manager = ChainManager.import_multi(
            self.chain_dir,
            [
                {
                    "target": "sda",
                    "bus": "sata",
                    "base": str(base),
                    "layers": [
                        {"source": None, "label": "SS0", "active": False},
                        {
                            "source": str(overlay1),
                            "label": "SS1",
                            "active": False,
                        },
                        {
                            "source": str(overlay2),
                            "label": "current",
                            "active": True,
                        },
                    ],
                }
            ],
        )
        chain = manager.load()
        self.assertEqual(chain.active_set(), "0003")
        disk = chain.disk("sda")
        self.assertEqual(
            [layer.label for layer in disk.layers],
            ["SS0", "SS1", "current"],
        )
        self.assertEqual(
            [layer.id for layer in disk.layers],
            ["0001", "0002", "0003"],
        )
        self.assertTrue((self.chain_dir / "disk-sda-0001.qcow2").is_file())
        compare = subprocess.run(
            [QEMU_IMG, "compare", str(overlay2), disk.active_disk()],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(compare.returncode, 0, compare.stderr)
        manager.snapshot("after-import")
        self.assertEqual(manager.load().active_set(), "0004")
        manager.revert("0002")
        self.assertEqual(manager.load().active_set(), "0002")
        self.assertEqual(manager.verify(), [])

    def test_import_multi_rejects_unaligned_layers(self) -> None:
        with self.assertRaises(ValidationError):
            ChainManager.import_multi(
                self.chain_dir,
                [
                    {
                        "target": "sda",
                        "base": str(self.bases["sda"]),
                        "layers": [
                            {"source": None, "label": "SS0", "active": True}
                        ],
                    },
                    {
                        "target": "sdc",
                        "base": str(self.bases["sdc"]),
                        "layers": [],
                    },
                ],
            )

    def test_add_and_remove_disk_aligns_existing_snapshots(self) -> None:
        manager = self._create()
        manager.snapshot("before-add")
        new_base = self.root / "base-sdb.qcow2"
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", str(new_base), "16M"],
            check=True,
            capture_output=True,
        )
        added = manager.add_disk("sdb", new_base, bus="sata")
        self.assertEqual(added.active, "0002")
        chain = manager.load()
        self.assertEqual(set(chain.disks), {"sda", "sdb", "sdc"})
        for target in ("sda", "sdb", "sdc"):
            self.assertEqual(
                [layer.id for layer in chain.disks[target].layers],
                ["0001", "0002"],
            )
        self.assertTrue((self.chain_dir / "disk-sdb-0001.qcow2").is_file())
        self.assertTrue((self.chain_dir / "disk-sdb-0002.qcow2").is_file())
        manager.remove_disk("sdb")
        self.assertEqual(set(manager.load().disks), {"sda", "sdc"})

    def test_delete_leaf_set(self) -> None:
        manager = self._create()
        manager.snapshot("one")
        manager.snapshot("two")
        manager.revert("0002")
        manager.delete("0003", force=True)
        chain = manager.load()
        self.assertEqual(chain.active_set(), "0002")
        for target in ("sda", "sdc"):
            self.assertFalse(
                (self.chain_dir / f"disk-{target}-0003.qcow2").exists()
            )
        self.assertEqual(manager.verify(), [])

    def test_delete_middle_set_commits_every_disk(self) -> None:
        manager = self._create()
        manager.snapshot("one")
        manager.snapshot("two")
        manager.delete("0002")
        chain = manager.load()
        self.assertEqual(chain.active_set(), "0003")
        for target in ("sda", "sdc"):
            self.assertEqual([layer.id for layer in chain.disks[target].layers], ["0001", "0003"])
            self.assertEqual(chain.disks[target].layer("0003").parent, "0001")
            self.assertTrue((self.chain_dir / f"disk-{target}-0003.qcow2").is_file())
            self.assertFalse((self.chain_dir / f"disk-{target}-0002.qcow2").exists())
        self.assertEqual(manager.verify(), [])

    def test_recover_interrupted_multi_commit(self) -> None:
        from vemu.chain import find_commit_journals

        manager = self._create()
        manager.snapshot("one")
        manager.snapshot("two")
        with mock.patch.object(
            manager, "_run", side_effect=RuntimeError("qemu killed")
        ):
            with self.assertRaises(RuntimeError):
                manager.delete("0002")
        self.assertEqual(len(find_commit_journals(self.root)), 1)
        result = manager.recover_commit()
        self.assertIsNotNone(result)
        self.assertTrue(result["recovered"])
        self.assertEqual(result["action"], "commit_multi")
        chain = manager.load()
        self.assertEqual(chain.active_set(), "0003")
        for target in ("sda", "sdc"):
            self.assertEqual([layer.id for layer in chain.disks[target].layers], ["0001", "0003"])
            self.assertEqual(chain.disks[target].layer("0003").parent, "0001")
        self.assertEqual(manager.verify(), [])

    def test_find_orphan_layers_v2(self) -> None:
        self._create()
        orphan = self.chain_dir / "disk-9999.qcow2"
        orphan.write_bytes(b"orphan")
        self.assertEqual(find_orphan_layers(self.root), [orphan])


if __name__ == "__main__":
    unittest.main()
