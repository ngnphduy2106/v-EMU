"""Resolve the user and group libvirt uses to run QEMU.

Fedora/RHEL usually use ``qemu:qemu`` while Debian/Ubuntu use
``libvirt-qemu:kvm``.  Storage ownership and qemu access checks must use the
identity that libvirt actually selected, otherwise disks are created for a
user that does not exist on the host.

Resolution order:

1. ``VEMU_QEMU_USER`` / ``VEMU_QEMU_GROUP`` environment overrides.
2. Active ``user``/``group`` values in ``/etc/libvirt/qemu.conf``.
3. Distribution defaults (Debian/Ubuntu first, then Fedora/RHEL/SUSE).
4. Fallback candidates ``qemu`` and ``libvirt-qemu``.

The daemon runs as root, so explicit overrides are validated and root/UID 0 is
always rejected.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .errors import ValidationError

DEFAULT_QEMU_CONF = "/etc/libvirt/qemu.conf"
DEFAULT_OS_RELEASE = "/etc/os-release"

_ASSIGNMENT = re.compile(
    r'^\s*(user|group)\s*=\s*["\']?([^"\'\s#]+)'
)


@dataclass(frozen=True)
class QemuIdentity:
    user: str
    group: str
    uid: int
    gid: int
    source: str

    @property
    def owner(self) -> str:
        return f"{self.user}:{self.group}"


def _read_os_release(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _qemu_conf_values(path: str | Path) -> tuple[str | None, str | None]:
    user: str | None = None
    group: str | None = None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if key == "user":
            user = value
        else:
            group = value
    return user, group


def _distro_candidates(path: str | Path) -> list[tuple[str, str]]:
    values = _read_os_release(path)
    family = f"{values.get('ID', '')} {values.get('ID_LIKE', '')}".lower()
    if "debian" in family or "ubuntu" in family:
        return [("libvirt-qemu", "kvm"), ("qemu", "qemu")]
    return [("qemu", "qemu"), ("libvirt-qemu", "kvm")]


def _lookup_user(name: str) -> pwd.struct_passwd | None:
    value = name.strip()
    if not value:
        return None
    if value.isdigit():
        try:
            return pwd.getpwuid(int(value))
        except KeyError:
            return None
    try:
        return pwd.getpwnam(value)
    except KeyError:
        return None


def _lookup_group(name: str) -> grp.struct_group | None:
    value = name.strip()
    if not value:
        return None
    if value.isdigit():
        try:
            return grp.getgrgid(int(value))
        except KeyError:
            return None
    try:
        return grp.getgrnam(value)
    except KeyError:
        return None


def _identity(
    user_name: str,
    group_name: str | None,
    source: str,
) -> QemuIdentity:
    account = _lookup_user(user_name)
    if account is None:
        raise ValidationError(f"QEMU user was not found: {user_name}")
    if account.pw_uid == 0 or account.pw_name == "root":
        raise ValidationError(
            "QEMU user must not be root. Set VEMU_QEMU_USER to the libvirt "
            "QEMU account (qemu or libvirt-qemu)."
        )
    if group_name:
        group = _lookup_group(group_name)
        if group is None:
            raise ValidationError(f"QEMU group was not found: {group_name}")
    else:
        group = _lookup_group(str(account.pw_gid))
        if group is None:
            raise ValidationError(
                f"Primary group for QEMU user {account.pw_name} was not found"
            )
    if group.gr_gid == 0 or group.gr_name == "root":
        raise ValidationError("QEMU group must not be root.")
    return QemuIdentity(
        user=account.pw_name,
        group=group.gr_name,
        uid=account.pw_uid,
        gid=group.gr_gid,
        source=source,
    )


def qemu_identity(
    *,
    qemu_conf: str | Path | None = None,
    os_release: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> QemuIdentity:
    """Return the validated QEMU user/group for this host."""
    environment = os.environ if env is None else env
    override_user = str(environment.get("VEMU_QEMU_USER", "")).strip()
    override_group = str(environment.get("VEMU_QEMU_GROUP", "")).strip()

    if override_user:
        return _identity(
            override_user,
            override_group or None,
            "environment",
        )

    conf_user, conf_group = _qemu_conf_values(
        qemu_conf or DEFAULT_QEMU_CONF
    )
    if conf_user:
        return _identity(
            conf_user,
            override_group or conf_group or None,
            "qemu.conf",
        )

    candidates = _distro_candidates(os_release or DEFAULT_OS_RELEASE)
    if conf_group:
        candidates = [(name, conf_group) for name, _group in candidates]

    for user, group in candidates:
        if _lookup_user(user) is None:
            continue
        selected_group = override_group or group
        if selected_group:
            try:
                return _identity(user, selected_group, "auto")
            except ValidationError:
                # A distro default group can be absent on a derivative. Fall
                # back to the account's primary group below. An explicit
                # override is authoritative and must fail loudly.
                if override_group:
                    raise
        try:
            return _identity(user, None, "auto")
        except ValidationError:
            continue

    raise ValidationError(
        "No QEMU user was found. Install libvirt/qemu or set "
        "VEMU_QEMU_USER and VEMU_QEMU_GROUP explicitly."
    )
