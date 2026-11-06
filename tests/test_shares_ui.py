from __future__ import annotations

import importlib.util
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(HAS_PYSIDE, "PySide6 is not installed")
class ShareUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_catalog_page_lists_shares(self) -> None:
        from vemu.ui.shares_page import SharesPage

        class FakeBackend:
            def shares_list(self) -> list[dict]:
                return [
                    {
                        "name": "Pictures",
                        "path": "/home/user/Pictures",
                        "tag": "Pictures",
                        "read_only": True,
                        "description": "",
                    }
                ]

        page = SharesPage(FakeBackend())
        try:
            page.refresh()
            self.assertEqual(page.table.rowCount(), 1)
            self.assertEqual(page.table.item(0, 0).text(), "Pictures")
            self.assertEqual(page.table.item(0, 3).text(), "read-only")
        finally:
            page.close()

    def test_pending_restart_is_visible(self) -> None:
        from vemu.ui.virtiofs import VirtioFsDialog

        class FakeBackend:
            def vm_shares(self, _name: str) -> dict:
                return {
                    "domain": "vm1",
                    "state": "running",
                    "pending_restart": True,
                    "shares": [
                        {
                            "tag": "Pictures",
                            "source": "/home/user/Pictures",
                            "read_only": True,
                            "share": "Pictures",
                            "auto_mount": True,
                            "drive": "Z",
                            "pending_restart": True,
                        }
                    ],
                }

        dialog = VirtioFsDialog(FakeBackend(), "vm1")
        try:
            self.assertFalse(dialog.banner.isHidden())
            self.assertIn("start the VM again", dialog.banner.text())
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertEqual(dialog.table.item(0, 3).text(), "Z")
            self.assertEqual(dialog.table.item(0, 4).text(), "Restart required")
            self.assertFalse(dialog.mount_button.isEnabled())
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
