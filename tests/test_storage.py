from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from vemu.errors import Conflict, NotFound, ValidationError
from vemu.storage import (
    Location,
    StorageStore,
    apply_fix,
    expected_label,
    inspect_location,
    plan_fix,
    validate_role,
)


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.location = self.root / "store"
        self.location.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roles(self) -> None:
        self.assertEqual(validate_role("labs"), "labs")
        with self.assertRaises(ValidationError):
            validate_role("bogus")

    def test_expected_labels(self) -> None:
        self.assertEqual(expected_label("labs"), "virt_image_t")
        self.assertEqual(expected_label("templates"), "virt_image_t")
        self.assertEqual(expected_label("isos"), "virt_content_t")

    def test_inspect_missing(self) -> None:
        location = inspect_location(self.root / "missing")
        self.assertFalse(location.exists)
        self.assertFalse(location.healthy)
        self.assertIn("Directory does not exist.", location.issues)

    def test_inspect_existing(self) -> None:
        location = inspect_location(self.location)
        self.assertTrue(location.exists)
        self.assertGreater(location.free_bytes, 0)
        self.assertNotEqual(location.filesystem, "")
        self.assertEqual(location.role, "labs")
        if location.filesystem == "btrfs" and location.nodatacow is False:
            self.assertTrue(
                any("nodatacow" in warning for warning in location.warnings)
            )
            self.assertFalse(
                any("nodatacow" in issue for issue in location.issues)
            )

    def test_plan_fix(self) -> None:
        location = inspect_location(self.location)
        actions = plan_fix(location)
        descriptions = [action.description for action in actions]
        self.assertTrue(any("ownership" in item.lower() for item in descriptions))
        self.assertTrue(any("mode" in item.lower() for item in descriptions))
        if (
            location.selinux_mode == "Enforcing"
            and location.selinux_label != location.expected_label
        ):
            self.assertTrue(
                any("SELinux file context" in item for item in descriptions)
            )

    def _selinux_location(
        self, *, mode: str = "Enforcing", label: str = "virt_image_t"
    ) -> Location:
        return Location(
            path=str(self.location),
            role="labs",
            exists=True,
            filesystem="xfs",
            total_bytes=1024**3,
            free_bytes=512 * 1024**3,
            selinux_mode=mode,
            selinux_label=label,
            expected_label="virt_image_t",
            owner="qemu:qemu",
            mode="750",
            nodatacow=False,
            qemu_traverse=True,
            qemu_read=True,
            qemu_write=True,
        )

    def test_force_selinux_relabels_even_when_dir_label_matches(self) -> None:
        location = self._selinux_location()
        actions = plan_fix(location, force_selinux=True)
        descriptions = [action.description for action in actions]
        self.assertIn("Add SELinux file context", descriptions)
        self.assertIn("Apply SELinux labels", descriptions)
        restore = next(
            action
            for action in actions
            if action.description == "Apply SELinux labels"
        )
        self.assertIn("-R", restore.command)

    def test_force_selinux_relabels_in_permissive_mode(self) -> None:
        location = self._selinux_location(mode="Permissive")
        actions = plan_fix(location, force_selinux=True)
        self.assertTrue(
            any(
                action.description == "Apply SELinux labels"
                for action in actions
            )
        )

    def test_plan_fix_uses_dynamic_qemu_owner(self) -> None:
        from unittest import mock

        location = self._selinux_location()
        identity = mock.Mock(
            user="libvirt-qemu", group="kvm", owner="libvirt-qemu:kvm"
        )
        with mock.patch(
            "vemu.storage.qemu_identity", return_value=identity
        ):
            actions = plan_fix(location)
        ownership = [
            action.command
            for action in actions
            if action.description == "Set ownership"
        ]
        self.assertEqual(ownership, [["chown", "libvirt-qemu:kvm", location.path]])

    def test_storage_repair_force_requires_selinux(self) -> None:
        from unittest import mock

        from vemu import service

        disabled = self._selinux_location(mode="Disabled", label="unlabeled_t")
        with mock.patch(
            "vemu.service.inspect_location", return_value=disabled
        ):
            with self.assertRaises(ValidationError):
                service.storage_repair(
                    str(self.location), force_selinux=True
                )

    def test_storage_repair_passes_force_selinux(self) -> None:
        from unittest import mock

        from vemu import service

        location = self._selinux_location()
        with mock.patch(
            "vemu.service.inspect_location", return_value=location
        ), mock.patch(
            "vemu.service.find_which", return_value="/usr/bin/tool"
        ), mock.patch(
            "vemu.service.apply_fix", return_value=(location, [])
        ) as fix:
            service.storage_repair(
                str(self.location), force_selinux=True
            )
        fix.assert_called_once_with(
            location, dry_run=False, force_selinux=True
        )

    def test_iso_storage_gets_readable_mode(self) -> None:
        iso = self.root / "iso"
        iso.mkdir()
        iso.chmod(0o700)
        location = inspect_location(iso, "isos")
        actions = plan_fix(location)
        mode_commands = [
            action.command
            for action in actions
            if "mode" in action.description.lower()
        ]
        self.assertTrue(mode_commands)
        self.assertIn("0755", mode_commands[0])

    def test_dry_run_does_not_apply(self) -> None:
        location = inspect_location(self.location)
        _updated, actions = apply_fix(location, dry_run=True)
        self.assertTrue(actions)
        self.assertFalse(any(action.applied for action in actions))

    @unittest.skipIf(os.geteuid() == 0, "running as root")
    def test_apply_requires_root(self) -> None:
        location = inspect_location(self.location)
        with self.assertRaises(ValidationError):
            apply_fix(location, dry_run=False)

    def test_store(self) -> None:
        config = self.root / "config.json"
        store = StorageStore(config)
        self.assertEqual(store.load(), [])
        entry = store.add(self.location, "labs")
        self.assertEqual(entry["role"], "labs")
        self.assertEqual(len(store.load()), 1)
        with self.assertRaises(Conflict):
            store.add(self.location, "labs")
        removed = store.remove(self.location)
        self.assertEqual(removed["path"], str(self.location.resolve()))
        self.assertEqual(store.load(), [])
        with self.assertRaises(NotFound):
            store.remove(self.location)


if __name__ == "__main__":
    unittest.main()


class StorageFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.location = self.root / "labs"
        self.location.mkdir()
        self.config = self.root / "storage.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _qcow2(self, name: str, backing: str | None = None) -> Path:
        import subprocess

        target = self.location / name
        command = ["qemu-img", "create", "-f", "qcow2"]
        if backing:
            command += ["-b", backing, "-F", "qcow2"]
        command += [str(target), "1M"]
        subprocess.run(command, check=True, capture_output=True)
        return target

    def test_iter_files_ignores_symlinks_and_other_files(self) -> None:
        from vemu.storage import iter_location_files

        iso = self.location / "alpine.iso"
        iso.write_bytes(b"iso")
        (self.location / "notes.txt").write_text("x")
        (self.location / "link.iso").symlink_to(iso)
        files = iter_location_files(self.location)
        self.assertEqual(files, [iso.resolve()])

    def test_backing_file_and_chain_scan(self) -> None:
        from vemu.storage import (
            build_storage_entries,
            iter_location_files,
            qcow2_backing_file,
            scan_chains,
        )

        base = self._qcow2("base.qcow2")
        chain_dir = self.location / "vm1"
        chain_dir.mkdir()
        layer = self._qcow2("vm1/disk-0001.qcow2", str(base))
        (chain_dir / "chain.json").write_text(
            '{"version": 1, "base": "%s", "layers": ['
            '{"id": "layer-1", "path": "%s", "parent": "base"}], '
            '"active": "layer-1"}' % (base, layer)
        )
        iso = self.location / "install.iso"
        iso.write_bytes(b"iso")
        self.assertEqual(qcow2_backing_file(layer), base.resolve())
        chains = scan_chains(self.location)
        self.assertEqual(len(chains), 1)
        self.assertIn(layer.resolve(), chains[0]["files"])

        sources = {layer.resolve(): ["vm1"]}
        backing = {base.resolve(): [layer.resolve()]}
        entries = build_storage_entries(
            self.location,
            "labs",
            iter_location_files(self.location),
            sources,
            backing,
            chains,
        )
        by_name = {item["name"]: item for item in entries["files"]}
        self.assertTrue(by_name["install.iso"]["deletable"])
        self.assertFalse(by_name["base.qcow2"]["deletable"])
        self.assertIn("backing file", by_name["base.qcow2"]["status"])
        self.assertFalse(by_name["vm1/disk-0001.qcow2"]["deletable"])
        self.assertEqual(len(entries["chains"]), 1)
        self.assertFalse(entries["chains"][0]["deletable"])

        orphan = build_storage_entries(
            self.location,
            "labs",
            iter_location_files(self.location),
            {},
            {},
            chains,
        )
        self.assertTrue(orphan["chains"][0]["deletable"])
        self.assertIn("orphan chain", orphan["chains"][0]["status"])

    def test_service_list_delete_import(self) -> None:
        from unittest import mock

        from vemu import service
        from vemu.storage import StorageStore

        StorageStore(str(self.config)).add(self.location, "labs")

        class FakeBackend:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def __enter__(self) -> "FakeBackend":
                return self

            def __exit__(self, *_exc) -> None:
                return None

            def list_domains(self) -> list:
                return []

            def config_xml(self, _name: str) -> str:
                return "<domain><devices/></domain>"

        iso = self.location / "install.iso"
        iso.write_bytes(b"iso")
        orphan = self._qcow2("orphan.qcow2")
        used = self._qcow2("used.qcow2")

        with mock.patch("vemu.service.LibvirtBackend", FakeBackend):
            payload = service.storage_files(
                str(self.location), config=str(self.config)
            )
            names = {item["name"] for item in payload["files"]}
            self.assertIn("install.iso", names)
            self.assertIn("orphan.qcow2", names)

            service.storage_delete_file(
                str(iso), config=str(self.config)
            )
            self.assertFalse(iso.exists())
            service.storage_delete_file(
                str(orphan), config=str(self.config)
            )
            self.assertFalse(orphan.exists())

            service.storage_delete_file(
                str(used), config=str(self.config)
            )
            self.assertFalse(used.exists())

            source = self.root / "new.iso"
            source.write_bytes(b"payload")
            imported = service.storage_import_file(
                str(source), str(self.location), config=str(self.config)
            )
            self.assertTrue(Path(imported["path"]).is_file())
            with self.assertRaises(Conflict):
                service.storage_import_file(
                    str(source), str(self.location), config=str(self.config)
                )

            outside = self.root / "outside.iso"
            outside.write_bytes(b"x")
            with self.assertRaises(NotFound):
                service.storage_delete_file(
                    str(outside), config=str(self.config)
                )
