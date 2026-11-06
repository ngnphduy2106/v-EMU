from __future__ import annotations

import os
import unittest
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from vemu.ui.single_instance import SingleInstanceGuard


class SingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_second_instance_requests_show_and_exits(self) -> None:
        name = f"vemu-test-{os.getpid()}-{uuid.uuid4().hex}"
        first = SingleInstanceGuard(name=name)
        second = SingleInstanceGuard(name=name)
        received: list[bool] = []
        first.show_requested.connect(lambda: received.append(True))
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            QTest.qWait(100)
            self.assertEqual(received, [True])
        finally:
            second.close()
            first.close()

    def test_closed_guard_releases_the_name(self) -> None:
        name = f"vemu-test-{os.getpid()}-{uuid.uuid4().hex}"
        first = SingleInstanceGuard(name=name)
        second = SingleInstanceGuard(name=name)
        try:
            self.assertTrue(first.acquire())
            first.close()
            self.assertTrue(second.acquire())
        finally:
            second.close()
            first.close()


if __name__ == "__main__":
    unittest.main()
