from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from vemu import service
from vemu.errors import ValidationError
from vemu.runtime_profile import (
    DEFAULT_PROFILE,
    load_profile,
    profile_payload,
    save_profile,
)


class RuntimeProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "runtime.json")
        patcher = mock.patch.dict(
            os.environ, {"VEMU_RUNTIME_CONFIG": self.path}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_profile(self) -> None:
        self.assertEqual(load_profile(), DEFAULT_PROFILE)
        payload = profile_payload()
        self.assertEqual(payload["profile"], "standard")
        self.assertTrue(payload["best_effort"])
        self.assertEqual(
            {item["profile"] for item in payload["profiles"]},
            {"standard", "high", "ultra"},
        )

    def test_save_and_load(self) -> None:
        self.assertEqual(save_profile("ultra"), "ultra")
        self.assertEqual(load_profile(), "ultra")
        payload = profile_payload()
        self.assertEqual(payload["label"], "Ultra")
        self.assertEqual(payload["cpu_shares"], 2048)
        self.assertEqual(payload["io_weight"], 1000)

    def test_invalid_profile(self) -> None:
        with self.assertRaises(ValidationError):
            save_profile("maximum")

    def test_service_applies_to_managed_domains(self) -> None:
        applied: list[tuple[str, str]] = []

        class FakeBackend:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def list_domains(self):
                return [
                    SimpleNamespace(name="vm1", managed_by="v-EMU (full)"),
                    SimpleNamespace(name="external", managed_by="External"),
                ]

            def apply_resource_profile(self, name, profile=None):
                applied.append((name, profile or ""))
                return {"warnings": []}

        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeBackend()
        ):
            payload = service.runtime_profile_set("high")
        self.assertEqual(payload["profile"], "high")
        self.assertEqual(payload["applied"], ["vm1"])
        self.assertEqual(applied, [("vm1", "high")])
        self.assertEqual(load_profile(), "high")

    def test_cli_parser(self) -> None:
        from vemu.cli import build_parser

        args = build_parser().parse_args(["config", "profile", "ultra"])
        self.assertEqual(args.profile, "ultra")
        self.assertEqual(args.func.__name__, "_cmd_config_profile")
        show = build_parser().parse_args(["config", "profile"])
        self.assertIsNone(show.profile)
