from __future__ import annotations

import os
import unittest
from unittest import mock

from vemu.lowprio import qemu_convert_command
from vemu.service import _migration_lock


class LowPriorityTests(unittest.TestCase):
    def test_convert_command_keeps_host_headroom(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"VEMU_MIGRATE_PARALLEL": "1", "VEMU_MIGRATE_DIRECT": "1"},
            clear=False,
        ):
            command = qemu_convert_command(
                "/usr/bin/qemu-img", "/src.qcow2", "/dst.qcow2"
            )
        self.assertIn("convert", command)
        self.assertIn("-m", command)
        self.assertEqual(command[command.index("-m") + 1], "1")
        self.assertIn("-T", command)
        self.assertEqual(command[command.index("-T") + 1], "none")
        self.assertEqual(command[-2:], ["/src.qcow2", "/dst.qcow2"])

    def test_env_can_disable_direct_and_raise_parallel(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"VEMU_MIGRATE_PARALLEL": "2", "VEMU_MIGRATE_DIRECT": "0"},
            clear=False,
        ):
            command = qemu_convert_command(
                "/usr/bin/qemu-img", "/src.qcow2", "/dst.qcow2"
            )
        self.assertEqual(command[command.index("-m") + 1], "2")
        self.assertNotIn("-T", command)

    def test_migration_lock_is_reentrant_after_release(self) -> None:
        with _migration_lock():
            pass
        with _migration_lock():
            pass


if __name__ == "__main__":
    unittest.main()
