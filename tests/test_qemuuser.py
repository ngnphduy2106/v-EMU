from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vemu.errors import ValidationError
from vemu.qemuuser import qemu_identity, _distro_candidates

_USERS = {
    "qemu": (100, 100),
    "libvirt-qemu": (64055, 993),
    "root": (0, 0),
}
_GROUPS = {
    "qemu": 100,
    "kvm": 993,
    "libvirt-qemu": 64055,
    "root": 0,
}


def _getpwnam(name: str):
    if name not in _USERS:
        raise KeyError(name)
    uid, gid = _USERS[name]
    return SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=gid)


def _getpwuid(uid: int):
    for name, (candidate, _gid) in _USERS.items():
        if candidate == uid:
            return _getpwnam(name)
    raise KeyError(uid)


def _getgrnam(name: str):
    if name not in _GROUPS:
        raise KeyError(name)
    return SimpleNamespace(gr_name=name, gr_gid=_GROUPS[name])


def _getgrgid(gid: int):
    for name, candidate in _GROUPS.items():
        if candidate == gid:
            return _getgrnam(name)
    raise KeyError(gid)


class QemuIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.qemu_conf = self.root / "qemu.conf"
        self.qemu_conf.write_text("", encoding="utf-8")
        self.debian_release = self.root / "debian-release"
        self.debian_release.write_text(
            'ID=debian\nID_LIKE=""\n', encoding="utf-8"
        )
        self.fedora_release = self.root / "fedora-release"
        self.fedora_release.write_text(
            'ID=fedora\nID_LIKE=""\n', encoding="utf-8"
        )
        patcher = mock.patch.multiple(
            "vemu.qemuuser.pwd",
            getpwnam=_getpwnam,
            getpwuid=_getpwuid,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.multiple(
            "vemu.qemuuser.grp",
            getgrnam=_getgrnam,
            getgrgid=_getgrgid,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _identity(self, *, release: Path | None = None, env: dict | None = None):
        return qemu_identity(
            qemu_conf=self.qemu_conf,
            os_release=release or self.debian_release,
            env=env or {},
        )

    def test_debian_default_is_libvirt_qemu(self) -> None:
        identity = self._identity()
        self.assertEqual(identity.user, "libvirt-qemu")
        self.assertEqual(identity.group, "kvm")
        self.assertEqual(identity.owner, "libvirt-qemu:kvm")
        self.assertEqual(identity.source, "auto")

    def test_fedora_default_is_qemu(self) -> None:
        identity = self._identity(release=self.fedora_release)
        self.assertEqual(identity.user, "qemu")
        self.assertEqual(identity.group, "qemu")

    def test_environment_override_wins(self) -> None:
        identity = self._identity(
            env={
                "VEMU_QEMU_USER": "libvirt-qemu",
                "VEMU_QEMU_GROUP": "kvm",
            }
        )
        self.assertEqual(identity.owner, "libvirt-qemu:kvm")
        self.assertEqual(identity.source, "environment")

    def test_environment_group_only_keeps_detected_user(self) -> None:
        identity = self._identity(env={"VEMU_QEMU_GROUP": "kvm"})
        self.assertEqual(identity.user, "libvirt-qemu")
        self.assertEqual(identity.group, "kvm")

    def test_qemu_conf_active_values_win_over_distro(self) -> None:
        self.qemu_conf.write_text(
            '#user = "ignored"\nuser = "qemu"\ngroup = "qemu"\n',
            encoding="utf-8",
        )
        identity = self._identity()
        self.assertEqual(identity.owner, "qemu:qemu")
        self.assertEqual(identity.source, "qemu.conf")

    def test_invalid_environment_user_fails(self) -> None:
        with self.assertRaises(ValidationError):
            self._identity(env={"VEMU_QEMU_USER": "does-not-exist"})

    def test_root_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self._identity(env={"VEMU_QEMU_USER": "root"})

    def test_missing_user_fails(self) -> None:
        empty = self.root / "empty-release"
        empty.write_text("ID=unknown\n", encoding="utf-8")
        with mock.patch(
            "vemu.qemuuser._lookup_user", return_value=None
        ):
            with self.assertRaises(ValidationError):
                self._identity(release=empty)

    def test_distro_candidates_order(self) -> None:
        self.assertEqual(
            _distro_candidates(self.debian_release)[0],
            ("libvirt-qemu", "kvm"),
        )
        self.assertEqual(
            _distro_candidates(self.fedora_release)[0],
            ("qemu", "qemu"),
        )


if __name__ == "__main__":
    unittest.main()
