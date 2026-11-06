"""Global VirtioFS share catalog and per-VM assignments.

The libvirt domain XML remains the authority for the actual ``<filesystem>``
device.  This module only stores reusable host-share definitions and the
per-VM options that cannot live in libvirt XML (Windows auto-mount and the
preferred guest drive letter).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import Conflict, NotFound, ValidationError

DEFAULT_CONFIG = "/etc/vemu/shares.json"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass
class ShareDefinition:
    name: str
    path: str
    tag: str
    read_only: bool = False
    description: str = ""

    def validate(self) -> None:
        if not NAME_RE.match(self.name):
            raise ValidationError(
                "Share name must start with a letter or digit and contain "
                "letters, digits, '.', '_', '-' or spaces."
            )
        if not TAG_RE.match(self.tag):
            raise ValidationError(
                "Guest tag must start with a letter or digit and contain "
                "letters, digits, '.', '_' or '-'."
            )
        if not str(self.path).strip():
            raise ValidationError("Host path must not be empty.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "tag": self.tag,
            "read_only": self.read_only,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ShareDefinition":
        item = cls(
            name=str(data.get("name", "")),
            path=str(data.get("path", "")),
            tag=str(data.get("tag", "")),
            read_only=bool(data.get("read_only", False)),
            description=str(data.get("description", "")),
        )
        item.validate()
        return item


@dataclass
class ShareAssignment:
    domain: str
    share: str
    tag: str
    read_only: bool = False
    auto_mount: bool = False
    drive: str | None = None
    mountpoint: str | None = None

    def validate(self) -> None:
        if not self.domain.strip():
            raise ValidationError("Assignment domain must not be empty.")
        if not self.share.strip():
            raise ValidationError("Assignment share must not be empty.")
        if not TAG_RE.match(self.tag):
            raise ValidationError(
                "Guest tag must start with a letter or digit and contain "
                "letters, digits, '.', '_' or '-'."
            )
        if self.drive is not None and not re.match(r"^[A-Za-z]$", self.drive):
            raise ValidationError("Drive letter must be one letter A-Z.")
        if self.mountpoint is not None and not self.mountpoint.startswith("/"):
            raise ValidationError("Linux mount point must be an absolute path.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "share": self.share,
            "tag": self.tag,
            "read_only": self.read_only,
            "auto_mount": self.auto_mount,
            "drive": self.drive,
            "mountpoint": self.mountpoint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ShareAssignment":
        item = cls(
            domain=str(data.get("domain", "")),
            share=str(data.get("share", "")),
            tag=str(data.get("tag", "")),
            read_only=bool(data.get("read_only", False)),
            auto_mount=bool(data.get("auto_mount", False)),
            drive=(
                str(data["drive"]).upper()
                if data.get("drive")
                else None
            ),
            mountpoint=(
                str(data["mountpoint"])
                if data.get("mountpoint")
                else None
            ),
        )
        item.validate()
        return item


class ShareStore:
    """Persistent catalog and assignments in one JSON file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(
            path
            or os.environ.get("VEMU_SHARE_CONFIG")
            or DEFAULT_CONFIG
        ).expanduser()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "shares": [], "assignments": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(
                f"Cannot read share catalog {self.path}: {exc}"
            ) from exc
        if int(data.get("version", 0)) != 1:
            raise ValidationError(
                f"Unsupported share catalog version in {self.path}."
            )
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def load(self) -> list[ShareDefinition]:
        return [
            ShareDefinition.from_dict(item)
            for item in self._load().get("shares", [])
        ]

    def assignments(self) -> list[ShareAssignment]:
        return [
            ShareAssignment.from_dict(item)
            for item in self._load().get("assignments", [])
        ]

    def list(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.load()]

    def get(self, name: str) -> ShareDefinition:
        for item in self.load():
            if item.name == name:
                return item
        raise NotFound(f"Share '{name}' is not in the catalog.")

    def add(self, share: ShareDefinition) -> dict[str, Any]:
        share.validate()
        data = self._load()
        items = [
            ShareDefinition.from_dict(item)
            for item in data.get("shares", [])
        ]
        if any(item.name == share.name for item in items):
            raise Conflict(f"Share '{share.name}' already exists.")
        items.append(share)
        data["shares"] = [item.to_dict() for item in items]
        self._save(data)
        return share.to_dict()

    def update(self, share: ShareDefinition) -> dict[str, Any]:
        share.validate()
        data = self._load()
        items = [
            ShareDefinition.from_dict(item)
            for item in data.get("shares", [])
        ]
        for index, item in enumerate(items):
            if item.name == share.name:
                assignments = [
                    ShareAssignment.from_dict(entry)
                    for entry in data.get("assignments", [])
                ]
                attached = [
                    entry
                    for entry in assignments
                    if entry.share == share.name
                ]
                if attached and (
                    item.tag != share.tag
                    or item.read_only != share.read_only
                ):
                    raise Conflict(
                        f"Share '{share.name}' is attached to a VM. Detach it "
                        "before changing its guest tag or read-only mode."
                    )
                items[index] = share
                data["shares"] = [entry.to_dict() for entry in items]
                self._save(data)
                return share.to_dict()
        raise NotFound(f"Share '{share.name}' is not in the catalog.")

    def remove(self, name: str) -> dict[str, Any]:
        data = self._load()
        items = [
            ShareDefinition.from_dict(item)
            for item in data.get("shares", [])
        ]
        found = next((item for item in items if item.name == name), None)
        if found is None:
            raise NotFound(f"Share '{name}' is not in the catalog.")
        items = [item for item in items if item.name != name]
        assignments = [
            ShareAssignment.from_dict(item)
            for item in data.get("assignments", [])
            if str(item.get("share", "")) != name
        ]
        data["shares"] = [item.to_dict() for item in items]
        data["assignments"] = [item.to_dict() for item in assignments]
        self._save(data)
        return found.to_dict()

    def rename_domain(self, old: str, new: str) -> int:
        """Move every share assignment from one domain name to another."""
        if old == new:
            return 0
        data = self._load()
        items = [
            ShareAssignment.from_dict(item)
            for item in data.get("assignments", [])
        ]
        changed = 0
        for item in items:
            if item.domain == old:
                item.domain = new
                changed += 1
        if changed:
            data["assignments"] = [item.to_dict() for item in items]
            self._save(data)
        return changed

    def purge_domain(self, domain: str) -> int:
        """Remove every share assignment for a deleted domain."""
        data = self._load()
        items = [
            ShareAssignment.from_dict(item)
            for item in data.get("assignments", [])
        ]
        kept = [item for item in items if item.domain != domain]
        removed = len(items) - len(kept)
        if removed:
            data["assignments"] = [item.to_dict() for item in kept]
            self._save(data)
        return removed

    def assignments_for(self, domain: str) -> list[ShareAssignment]:
        return [
            item for item in self.assignments() if item.domain == domain
        ]

    def attach(self, assignment: ShareAssignment) -> dict[str, Any]:
        assignment.validate()
        data = self._load()
        items = [
            ShareAssignment.from_dict(item)
            for item in data.get("assignments", [])
        ]
        if any(
            item.domain == assignment.domain
            and item.share == assignment.share
            for item in items
        ):
            raise Conflict(
                f"Share '{assignment.share}' is already attached to "
                f"'{assignment.domain}'."
            )
        if any(
            item.domain == assignment.domain and item.tag == assignment.tag
            for item in items
        ):
            raise Conflict(
                f"Tag '{assignment.tag}' is already used on "
                f"'{assignment.domain}'."
            )
        items.append(assignment)
        data["assignments"] = [item.to_dict() for item in items]
        self._save(data)
        return assignment.to_dict()

    def detach(self, domain: str, share: str) -> dict[str, Any]:
        data = self._load()
        items = [
            ShareAssignment.from_dict(item)
            for item in data.get("assignments", [])
        ]
        found = next(
            (
                item
                for item in items
                if item.domain == domain and item.share == share
            ),
            None,
        )
        if found is None:
            raise NotFound(
                f"Share '{share}' is not attached to '{domain}'."
            )
        items = [
            item
            for item in items
            if not (item.domain == domain and item.share == share)
        ]
        data["assignments"] = [item.to_dict() for item in items]
        self._save(data)
        return found.to_dict()

    def update_assignment(self, assignment: ShareAssignment) -> dict[str, Any]:
        assignment.validate()
        data = self._load()
        items = [
            ShareAssignment.from_dict(item)
            for item in data.get("assignments", [])
        ]
        for index, item in enumerate(items):
            if (
                item.domain == assignment.domain
                and item.share == assignment.share
            ):
                items[index] = assignment
                data["assignments"] = [entry.to_dict() for entry in items]
                self._save(data)
                return assignment.to_dict()
        raise NotFound(
            f"Share '{assignment.share}' is not attached to "
            f"'{assignment.domain}'."
        )
