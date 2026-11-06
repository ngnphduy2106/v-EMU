"""Path policy for privileged daemon operations.

The daemon is the root boundary.  Client-supplied write paths must stay inside
one of the storage locations registered by the user, and symlinks are rejected
instead of being followed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .errors import ValidationError
from .storage import StorageStore


FORBIDDEN_ROOTS = (
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var/lib/libvirt",
)


def validate_storage_root(value: str | Path) -> Path:
    """Reject storage roots that would expose system configuration paths."""
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = (Path.cwd() / raw).absolute()
    path = raw.resolve(strict=False)
    for forbidden in FORBIDDEN_ROOTS:
        root = Path(forbidden)
        if path == root or root in path.parents:
            raise ValidationError(
                f"Storage location is not allowed: {path}"
            )
    current = raw.absolute()
    while current != current.parent:
        if current.is_symlink():
            raise ValidationError(
                f"Storage location contains a symlink: {current}"
            )
        current = current.parent
    return path


class PathPolicy:
    def __init__(self, locations: Iterable[dict[str, str]]) -> None:
        self.roots: dict[str, list[Path]] = {}
        for item in locations:
            role = str(item.get("role") or "")
            raw = str(item.get("path") or "")
            if not role or not raw:
                continue
            try:
                root = Path(raw).expanduser().resolve()
            except OSError:
                continue
            self.roots.setdefault(role, []).append(root)

    @classmethod
    def from_config(cls, config: str | Path | None) -> "PathPolicy":
        return cls(StorageStore(config).load())

    def _candidate_roots(self, roles: Iterable[str]) -> list[Path]:
        roots: list[Path] = []
        for role in roles:
            roots.extend(self.roots.get(role, []))
        return list(dict.fromkeys(roots))

    @staticmethod
    def _reject_symlinks(path: Path, root: Path) -> None:
        """Reject a symlink in any existing component between root and path."""
        current = path
        while current != root:
            try:
                if current.is_symlink():
                    raise ValidationError(
                        f"Refusing a symbolic link in path: {current}"
                    )
            except OSError:
                pass
            if current == current.parent:
                break
            current = current.parent

    def resolve(
        self,
        value: str | Path,
        *,
        roles: Iterable[str] = ("labs",),
        must_exist: bool = False,
        create_parent: bool = False,
    ) -> Path:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raw = (Path.cwd() / raw).absolute()
        path = raw.resolve(strict=False)
        roots = self._candidate_roots(roles)
        if not roots:
            raise ValidationError(
                "No storage location is registered for this operation."
            )
        root = next(
            (
                candidate
                for candidate in roots
                if path == candidate or candidate in path.parents
            ),
            None,
        )
        if root is None:
            allowed = ", ".join(str(item) for item in roots)
            raise ValidationError(
                f"Path is outside the registered storage locations: {path} "
                f"(allowed: {allowed})"
            )
        self._reject_symlinks(raw, root)
        if must_exist and not path.exists():
            raise ValidationError(f"Path does not exist: {path}")
        if create_parent:
            parent = path.parent
            if not parent.exists():
                raise ValidationError(
                    f"Parent directory does not exist: {parent}"
                )
        return path


def policy_for(config: str | Path | None) -> PathPolicy:
    return PathPolicy.from_config(config)
