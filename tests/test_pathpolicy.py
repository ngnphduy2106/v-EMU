from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vemu.errors import ValidationError
from vemu.pathpolicy import PathPolicy, validate_storage_root


class PathPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.labs = self.root / "labs"
        self.templates = self.root / "templates"
        self.labs.mkdir()
        self.templates.mkdir()
        self.policy = PathPolicy(
            [
                {"path": str(self.labs), "role": "labs"},
                {"path": str(self.templates), "role": "templates"},
            ]
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_accepts_registered_path(self) -> None:
        target = self.labs / "disk.qcow2"
        self.assertEqual(
            self.policy.resolve(target), target.resolve()
        )

    def test_rejects_outside_path(self) -> None:
        with self.assertRaises(ValidationError):
            self.policy.resolve(self.root / "outside.qcow2")

    def test_rejects_symlink_component(self) -> None:
        link = self.labs / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValidationError):
            self.policy.resolve(link / "disk.qcow2")

    def test_must_exist(self) -> None:
        missing = self.labs / "missing.qcow2"
        with self.assertRaises(ValidationError):
            self.policy.resolve(missing, must_exist=True)

    def test_storage_root_rejects_system_directory(self) -> None:
        with self.assertRaises(ValidationError):
            validate_storage_root("/etc")
        with self.assertRaises(ValidationError):
            validate_storage_root("/var/lib/libvirt/images")


if __name__ == "__main__":
    unittest.main()
