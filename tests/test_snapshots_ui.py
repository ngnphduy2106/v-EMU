from __future__ import annotations

import importlib.util
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(HAS_PYSIDE, "PySide6 is not installed")
class SnapshotsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _info(self, *, running: bool = False, managed: bool = True) -> dict:
        if not managed:
            return {
                "managed": False,
                "domain": "vm1",
                "running": running,
                "reason": "No chain.json next to the disk.",
            }
        return {
            "managed": True,
            "domain": "vm1",
            "running": running,
            "chain": "/tmp/vm1",
            "active": "0002",
            "active_disk": "/tmp/vm1/disk-0002.qcow2",
            "layers": [
                {
                    "id": "0001",
                    "label": "baseline",
                    "description": "Clean clone",
                    "parent": "base",
                    "actual_size": 1024 * 1024,
                    "created_at": "2026-10-30T09:10:00+00:00",
                    "active": False,
                },
                {
                    "id": "0002",
                    "label": "before-gpo",
                    "description": "Before applying the GPO baseline",
                    "parent": "0001",
                    "actual_size": 2 * 1024 * 1024,
                    "created_at": "2026-10-31T10:20:00+00:00",
                    "active": True,
                },
            ],
        }

    def test_lists_layers_and_enables_actions(self) -> None:
        from vemu.ui.snapshots import SnapshotsDialog

        info = self._info()

        class FakeBackend:
            def snapshots_info(self, _name: str) -> dict:
                return info

        dialog = SnapshotsDialog(FakeBackend(), "vm1")
        try:
            self.assertEqual(dialog.table.topLevelItemCount(), 1)
            self.assertEqual(
                dialog.table.topLevelItem(0).childCount(), 1
            )
            self.assertTrue(dialog.create_button.isEnabled())
            dialog.table.setCurrentItem(dialog.table.topLevelItem(0))
            self.assertTrue(dialog.revert_button.isEnabled())
            self.assertTrue(dialog.delete_button.isEnabled())
        finally:
            dialog.close()

    def test_details_and_edit_for_selected_snapshot(self) -> None:
        from vemu.ui.snapshots import SnapshotsDialog

        info = self._info()

        class FakeBackend:
            def snapshots_info(self, _name: str) -> dict:
                return info

        dialog = SnapshotsDialog(FakeBackend(), "vm1")
        try:
            root = dialog.table.topLevelItem(0)
            dialog.table.setCurrentItem(root.child(0))
            self.assertEqual(root.child(0).text(1), "before-gpo")
            self.assertEqual(root.child(0).text(4), "31-Oct-2026")
            self.assertEqual(
                dialog.details_description.toPlainText(),
                "Before applying the GPO baseline",
            )
            self.assertTrue(dialog.edit_button.isEnabled())
        finally:
            dialog.close()

    def test_snapshot_edit_dialog_values(self) -> None:
        from vemu.ui.snapshots import SnapshotEditDialog

        dialog = SnapshotEditDialog(
            title="Edit snapshot",
            name="before-gpo",
            description="Notes",
            editing=True,
        )
        try:
            self.assertEqual(dialog.values(), ("before-gpo", "Notes"))
            self.assertEqual(dialog.save_button.text(), "Save")
        finally:
            dialog.close()

    def test_running_disables_mutations(self) -> None:
        from vemu.ui.snapshots import SnapshotsDialog

        info = self._info(running=True)

        class FakeBackend:
            def snapshots_info(self, _name: str) -> dict:
                return info

        dialog = SnapshotsDialog(FakeBackend(), "vm1")
        try:
            self.assertFalse(dialog.create_button.isEnabled())
            dialog.table.setCurrentItem(dialog.table.topLevelItem(0))
            self.assertTrue(dialog.edit_button.isEnabled())
            self.assertFalse(dialog.revert_button.isEnabled())
            self.assertFalse(dialog.delete_button.isEnabled())
        finally:
            dialog.close()

    def test_unmanaged_disables_actions(self) -> None:
        from vemu.ui.snapshots import SnapshotsDialog

        info = self._info(managed=False)

        class FakeBackend:
            def snapshots_info(self, _name: str) -> dict:
                return info

        dialog = SnapshotsDialog(FakeBackend(), "vm1")
        try:
            self.assertEqual(dialog.table.topLevelItemCount(), 0)
            self.assertFalse(dialog.create_button.isEnabled())
            self.assertIn("No chain.json", dialog.state_label.text())
        finally:
            dialog.close()

    def test_external_snapshots_are_listed_read_only(self) -> None:
        from vemu.ui.snapshots import SnapshotsDialog

        info = {
            "managed": False,
            "domain": "vm1",
            "running": False,
            "reason": "No chain.json next to the disk.",
            "snapshots": [
                {
                    "name": "SS0",
                    "kind": "external",
                    "disks": [
                        {
                            "target": "vda",
                            "snapshot": "external",
                            "source": "/tmp/vm1.SS0",
                        },
                        {
                            "target": "vdb",
                            "snapshot": "external",
                            "source": "/tmp/vm2.SS0",
                        },
                    ],
                }
            ],
            "external_snapshots": True,
        }

        class FakeBackend:
            def snapshots_info(self, _name: str) -> dict:
                return info

        dialog = SnapshotsDialog(FakeBackend(), "vm1")
        try:
            self.assertEqual(dialog.table.topLevelItemCount(), 1)
            root = dialog.table.topLevelItem(0)
            self.assertEqual(root.text(0), "SS0")
            self.assertEqual(root.text(1), "external")
            self.assertEqual(root.childCount(), 2)
            self.assertEqual(root.child(0).text(0), "vda")
            self.assertEqual(root.child(1).text(0), "vdb")
            self.assertIn("read-only", dialog.state_label.text())
            self.assertFalse(dialog.create_button.isEnabled())
            dialog.table.setCurrentItem(root)
            self.assertFalse(dialog.edit_button.isEnabled())
            self.assertFalse(dialog.revert_button.isEnabled())
            self.assertFalse(dialog.delete_button.isEnabled())
        finally:
            dialog.close()

    def test_branch_tree_is_nested(self) -> None:
        from vemu.ui.snapshots import SnapshotsDialog

        info = self._info()
        info["active"] = "0003"
        info["layers"] = [
            {
                "id": "0001",
                "label": "baseline",
                "parent": "base",
                "actual_size": 1,
                "active": False,
            },
            {
                "id": "0002",
                "label": "left",
                "parent": "0001",
                "actual_size": 1,
                "active": False,
            },
            {
                "id": "0003",
                "label": "right",
                "parent": "0001",
                "actual_size": 1,
                "active": True,
            },
        ]

        class FakeBackend:
            def snapshots_info(self, _name: str) -> dict:
                return info

        dialog = SnapshotsDialog(FakeBackend(), "vm1")
        try:
            root = dialog.table.topLevelItem(0)
            self.assertEqual(root.text(0).removeprefix("* ").strip(), "0001")
            self.assertEqual(root.childCount(), 2)
            self.assertEqual(
                root.child(0).text(0).removeprefix("* ").strip(), "0002"
            )
            self.assertEqual(
                root.child(1).text(0).removeprefix("* ").strip(), "0003"
            )
            self.assertTrue(root.child(1).text(0).startswith("*"))
        finally:
            dialog.close()

    def test_running_requires_offline_snapshot(self) -> None:
        from vemu.ui.snapshots import SnapshotsDialog

        info = self._info(running=True)

        class FakeBackend:
            def snapshots_info(self, _name: str) -> dict:
                return info

        dialog = SnapshotsDialog(FakeBackend(), "vm1")
        try:
            self.assertFalse(dialog.create_button.isEnabled())
            self.assertEqual(dialog.create_button.text(), "New snapshot")
            self.assertIn("Live snapshots are disabled", dialog.state_label.text())
            self.assertIn("offline snapshot", dialog.state_label.text())
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
