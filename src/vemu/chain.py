"""qcow2 overlay chain manager.

A chain is a tree of qcow2 overlays on top of an immutable base image::

    base.qcow2  <-  disk-0001.qcow2  <-  disk-0002.qcow2
                                    \\-  disk-0003.qcow2

Only the active layer is attached to a VM. Snapshots create new layers;
reverting switches the active layer; deleting merges linear parents and
children. Branched layers can be deleted leaf-first but are not merged
automatically.

All operations expect the VM to be shut down. Files may be owned by ``qemu``
after libvirt has started a domain, so the production daemon runs them as root.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import (
    Conflict,
    ExternalCommandError,
    NotFound,
    ValidationError,
    VemuError,
)

CHAIN_FILE = "chain.json"
COMMIT_JOURNAL = "commit.json"
CHAIN_VERSION = 2
LEGACY_CHAIN_VERSION = 1
QEMU_IMG = "qemu-img"
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9_-]+$")


def find_commit_journals(root: str | Path) -> list[Path]:
    """Return pending commit journals under a chain directory or tree."""
    target = Path(root).expanduser().resolve()
    if target.is_file() and target.name == COMMIT_JOURNAL:
        return [target]
    if target.is_dir():
        return sorted(target.rglob(COMMIT_JOURNAL))
    return []



def find_orphan_layers(root: str | Path) -> list[Path]:
    """Return ``disk-*.qcow2`` files not referenced by a nearby chain.json.

    The scan is intentionally conservative: only files matching the managed
    layer naming pattern inside a directory that contains a valid chain file
    are considered. Template bases and arbitrary images are never returned.
    """
    target = Path(root).expanduser().resolve()
    if target.is_file() and target.name == CHAIN_FILE:
        chain_files = [target]
    elif target.is_dir():
        chain_files = sorted(target.rglob(CHAIN_FILE))
    else:
        return []
    orphans: list[Path] = []
    for chain_file in chain_files:
        try:
            data = json.loads(chain_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        referenced: set[Path] = set()
        if int(data.get("version", LEGACY_CHAIN_VERSION)) >= 2:
            for disk in (data.get("disks") or {}).values():
                if not isinstance(disk, dict):
                    continue
                base = disk.get("base")
                if base:
                    referenced.add(Path(str(base)).expanduser().resolve())
                for layer in disk.get("layers") or []:
                    path = layer.get("path") if isinstance(layer, dict) else None
                    if path:
                        referenced.add(Path(str(path)).expanduser().resolve())
        else:
            base = data.get("base")
            if base:
                referenced.add(Path(str(base)).expanduser().resolve())
            for layer in data.get("layers") or []:
                path = layer.get("path") if isinstance(layer, dict) else None
                if path:
                    referenced.add(Path(str(path)).expanduser().resolve())
        for candidate in chain_file.parent.glob("disk-*.qcow2"):
            if candidate.resolve() not in referenced:
                orphans.append(candidate)
    return sorted(set(orphans))


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _resolved(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _iter_disks(chain: Chain | MultiDiskChain) -> list[Chain]:
    if isinstance(chain, MultiDiskChain):
        return list(chain.disks.values())
    return [chain]


def _path_inside(path: str | Path, root: Path) -> bool:
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def _rewrite_path(path: str, old_root: Path, new_root: Path) -> str:
    try:
        relative = Path(path).relative_to(old_root)
    except ValueError:
        return path
    return str(new_root / relative)


@dataclass
class Layer:
    id: str
    path: str
    parent: str
    label: str = ""
    description: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Layer":
        return cls(
            id=str(data["id"]),
            path=str(data["path"]),
            parent=str(data["parent"]),
            label=str(data.get("label", "")),
            description=str(data.get("description", "")),
            created_at=str(data.get("created_at", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "parent": self.parent,
            "label": self.label,
            "description": self.description,
            "created_at": self.created_at,
        }


@dataclass
class Chain:
    version: int
    base: str
    layers: list[Layer]
    active: str | None
    target: str = ""
    bus: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Chain":
        version = int(data.get("version", 0))
        if version != LEGACY_CHAIN_VERSION:
            raise ValidationError(
                f"Unsupported disk chain version {version}; expected "
                f"{LEGACY_CHAIN_VERSION}."
            )
        return cls(
            version=version,
            base=str(data["base"]),
            layers=[Layer.from_dict(item) for item in data.get("layers", [])],
            active=data.get("active"),
            target=str(data.get("target", "")),
            bus=str(data.get("bus", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "base": self.base,
            "layers": [layer.to_dict() for layer in self.layers],
            "active": self.active,
        }

    def to_disk_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "base": self.base,
            "layers": [layer.to_dict() for layer in self.layers],
            "active": self.active,
        }
        if self.bus:
            data["bus"] = self.bus
        return data

    def layer(self, layer_id: str) -> Layer:
        for layer in self.layers:
            if layer.id == layer_id:
                return layer
        raise NotFound(f"Snapshot layer '{layer_id}' was not found.")

    def active_layer(self) -> Layer | None:
        if self.active is None:
            return None
        return self.layer(self.active)

    def children(self, layer_id: str) -> list[Layer]:
        return [layer for layer in self.layers if layer.parent == layer_id]

    def first_layer(self) -> Layer | None:
        for layer in self.layers:
            if layer.parent == "base":
                return layer
        return None

    def active_disk(self) -> str:
        layer = self.active_layer()
        if layer is None:
            return self.base
        return layer.path

    def validate(self) -> None:
        if not self.layers:
            raise ValidationError("Chain has no layers.")
        ids = [layer.id for layer in self.layers]
        if len(ids) != len(set(ids)):
            raise ValidationError("Chain contains duplicate layer IDs.")
        first = self.first_layer()
        if first is None:
            raise ValidationError("Chain has no layer directly on top of the base.")
        by_id = {layer.id: layer for layer in self.layers}
        for layer in self.layers:
            if layer.parent != "base" and layer.parent not in ids:
                raise ValidationError(
                    f"Layer '{layer.id}' references missing parent '{layer.parent}'."
                )
            seen: set[str] = set()
            current = layer
            while current.parent != "base":
                if current.id in seen:
                    raise ValidationError(
                        f"Chain contains a cycle at layer '{current.id}'."
                    )
                seen.add(current.id)
                current = by_id[current.parent]
        if self.active is not None and self.active not in ids:
            raise ValidationError(
                f"Active layer '{self.active}' does not exist in the chain."
            )


@dataclass
class SnapshotSet:
    """A layer shared by every disk in a multi-disk chain."""

    id: str
    label: str
    created_at: str
    layers: dict[str, Layer]
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        first = next(iter(self.layers.values()), None)
        return {
            "id": self.id,
            "path": first.path if first is not None else "",
            "parent": first.parent if first is not None else "",
            "label": self.label,
            "description": self.description,
            "created_at": self.created_at,
            "layers": {
                target: layer.to_dict()
                for target, layer in self.layers.items()
            },
        }

    @property
    def path(self) -> str:
        first = next(iter(self.layers.values()), None)
        return first.path if first is not None else ""

    @property
    def parent(self) -> str:
        first = next(iter(self.layers.values()), None)
        return first.parent if first is not None else ""


@dataclass
class MultiDiskChain:
    """Version 2 chain: one linear snapshot set per data disk."""

    version: int
    disks: dict[str, Chain]
    multi_disk: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MultiDiskChain":
        version = int(data.get("version", 0))
        if version != CHAIN_VERSION:
            raise ValidationError(
                f"Unsupported chain version {version}; expected {CHAIN_VERSION}."
            )
        raw = data.get("disks")
        if not isinstance(raw, dict) or not raw:
            raise ValidationError("Multi-disk chain has no 'disks' mapping.")
        disks: dict[str, Chain] = {}
        for target, item in raw.items():
            if not isinstance(item, dict):
                raise ValidationError(f"Invalid disk entry for '{target}'.")
            disk = Chain.from_dict({**item, "version": LEGACY_CHAIN_VERSION})
            disk.target = str(item.get("target", target))
            disk.bus = str(item.get("bus", ""))
            disks[str(target)] = disk
        chain = cls(version=version, disks=disks)
        chain.validate()
        return chain

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "disks": {
                target: disk.to_disk_dict()
                for target, disk in self.disks.items()
            },
        }

    def disk(self, target: str) -> Chain:
        try:
            return self.disks[target]
        except KeyError as exc:
            raise NotFound(f"Disk target '{target}' was not found.") from exc

    def _only_disk(self) -> Chain:
        if len(self.disks) != 1:
            raise ValidationError(
                "This operation needs a single-disk chain; "
                f"found {len(self.disks)} disks."
            )
        return next(iter(self.disks.values()))

    @property
    def active(self) -> str | None:
        return self._only_disk().active

    @property
    def layers(self) -> list[Layer]:
        return self._only_disk().layers

    @property
    def base(self) -> str:
        return self._only_disk().base

    def active_disk(self) -> str:
        return self._only_disk().active_disk()

    def layer(self, layer_id: str) -> Layer:
        return self._only_disk().layer(layer_id)

    def children(self, layer_id: str) -> list[Layer]:
        return self._only_disk().children(layer_id)

    def first_layer(self) -> Layer | None:
        return self._only_disk().first_layer()

    def disk_for_path(self, path: str | Path) -> Chain:
        resolved = _resolved(path)
        for disk in self.disks.values():
            if _resolved(disk.base) == resolved or any(
                _resolved(layer.path) == resolved for layer in disk.layers
            ):
                return disk
        raise NotFound(f"Disk path '{path}' is not part of this chain.")

    def active_set(self) -> str | None:
        active = {disk.active for disk in self.disks.values()}
        if len(active) == 1:
            return next(iter(active))
        return None

    def active_disks(self) -> dict[str, str]:
        return {
            target: disk.active_disk()
            for target, disk in self.disks.items()
        }

    def validate(self) -> None:
        if not self.disks:
            raise ValidationError("Multi-disk chain has no disks.")
        for target, disk in self.disks.items():
            if not _SAFE_TARGET.match(target):
                raise ValidationError(
                    f"Unsafe disk target for chain storage: '{target}'."
                )
            disk.validate()
        ids = [set(layer.id for layer in disk.layers) for disk in self.disks.values()]
        if any(item != ids[0] for item in ids[1:]):
            raise ValidationError(
                "Multi-disk chain layers are not aligned across disks."
            )
        active = {disk.active for disk in self.disks.values()}
        if len(active) != 1:
            raise ValidationError(
                "Multi-disk chain has different active layers across disks."
            )
        for layer_id in ids[0]:
            parents = {
                disk.layer(layer_id).parent for disk in self.disks.values()
            }
            if len(parents) != 1:
                raise ValidationError(
                    f"Layer '{layer_id}' has different parents across disks."
                )


class ChainManager:
    """Read and mutate a chain stored in a directory."""

    def __init__(self, chain_dir: str | Path, qemu_img: str = QEMU_IMG) -> None:
        self.chain_dir = Path(chain_dir).expanduser().resolve()
        self.chain_file = self.chain_dir / CHAIN_FILE
        self.qemu_img = qemu_img

    # ------------------------------------------------------------------ load/save

    def load(self) -> Chain | MultiDiskChain:
        if not self.chain_file.exists():
            raise NotFound(f"No chain found at {self.chain_file}.")
        data = json.loads(self.chain_file.read_text(encoding="utf-8"))
        version = int(data.get("version", 0))
        if version == LEGACY_CHAIN_VERSION:
            chain: Chain | MultiDiskChain = Chain.from_dict(data)
        elif version == CHAIN_VERSION:
            chain = MultiDiskChain.from_dict(data)
        else:
            raise ValidationError(
                f"Unsupported chain version {version}; expected "
                f"{LEGACY_CHAIN_VERSION} or {CHAIN_VERSION}."
            )
        chain.validate()
        return chain

    def save(self, chain: Chain | MultiDiskChain) -> None:
        chain.validate()
        self.chain_dir.mkdir(parents=True, exist_ok=True)
        if self.chain_file.exists():
            shutil.copy2(self.chain_file, self.chain_file.with_suffix(".json.bak"))
        tmp = self.chain_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(chain.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.chain_file)

    # ------------------------------------------------------------------ creation

    @classmethod
    def create(
        cls,
        chain_dir: str | Path,
        base_path: str | Path,
        label: str = "baseline",
        qemu_img: str = QEMU_IMG,
        *,
        target: str = "vda",
        bus: str = "",
    ) -> "ChainManager":
        """Create a v2 chain with one disk (the normal VM case)."""
        return cls.create_multi(
            chain_dir,
            [
                {
                    "target": target,
                    "base": str(base_path),
                    "label": label,
                    "bus": bus,
                }
            ],
            qemu_img=qemu_img,
        )

    @classmethod
    def create_multi(
        cls,
        chain_dir: str | Path,
        disks: list[dict[str, Any]],
        qemu_img: str = QEMU_IMG,
    ) -> "ChainManager":
        """Create a version 2 chain with one base and overlay per data disk.

        ``disks`` entries need ``target`` and ``base``; ``bus`` and ``label``
        are optional.
        """
        manager = cls(chain_dir, qemu_img=qemu_img)
        if manager.chain_file.exists():
            raise Conflict(f"A chain already exists at {manager.chain_file}.")
        if len(disks) < 1:
            raise ValidationError(
                "A chain needs at least one data disk."
            )
        manager.chain_dir.mkdir(parents=True, exist_ok=True)
        chain = MultiDiskChain(version=CHAIN_VERSION, disks={})
        created: list[Path] = []
        try:
            for spec in disks:
                target = str(spec.get("target", "")).strip()
                if not _SAFE_TARGET.match(target):
                    raise ValidationError(
                        f"Unsafe disk target for chain storage: '{target}'."
                    )
                if target in chain.disks:
                    raise ValidationError(
                        f"Duplicate disk target in chain: '{target}'."
                    )
                base = Path(str(spec["base"])).expanduser().resolve()
                if not base.is_file():
                    raise NotFound(f"Disk base image was not found: {base}")
                layer = Layer(
                    id="0001",
                    path=str(
                        manager.chain_dir / f"disk-{target}-0001.qcow2"
                    ),
                    parent="base",
                    label=str(spec.get("label", "baseline")),
                    created_at=_now(),
                )
                manager._create_overlay(base, Path(layer.path))
                created.append(Path(layer.path))
                chain.disks[target] = Chain(
                    version=LEGACY_CHAIN_VERSION,
                    base=str(base),
                    layers=[layer],
                    active=layer.id,
                    target=target,
                    bus=str(spec.get("bus", "")),
                )
            manager.save(chain)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        return manager

    @classmethod
    def import_multi(
        cls,
        chain_dir: str | Path,
        disks: list[dict[str, Any]],
        qemu_img: str = QEMU_IMG,
    ) -> "ChainManager":
        """Build a v2 chain from existing external or internal snapshots.

        Each disk entry needs ``target`` and ``layers``; ``base`` may be a
        source image or ``None`` with ``virtual_size`` for an empty base.
        A layer is ``{"source": path, "label": str, "parent": id,
        "source_kind": "external"|"internal"|"current", "active": bool}``.
        ``source=None`` creates an empty overlay on the parent, representing a
        snapshot whose state is the base image. External overlays use
        ``qemu-img convert -B``; internal states are extracted with
        ``qemu-img convert -l`` before being layered on the parent.
        """
        manager = cls(chain_dir, qemu_img=qemu_img)
        if manager.chain_file.exists():
            raise Conflict(f"A chain already exists at {manager.chain_file}.")
        if not disks:
            raise ValidationError("A chain needs at least one data disk.")
        counts = {len(item.get("layers") or []) for item in disks}
        if len(counts) != 1 or 0 in counts:
            raise ValidationError(
                "Imported snapshot layers must align across every disk."
            )
        manager.chain_dir.mkdir(parents=True, exist_ok=True)
        chain = MultiDiskChain(version=CHAIN_VERSION, disks={})
        created: list[Path] = []
        try:
            for spec in disks:
                target = str(spec.get("target", "")).strip()
                if not _SAFE_TARGET.match(target):
                    raise ValidationError(
                        f"Unsafe disk target for chain storage: '{target}'."
                    )
                if target in chain.disks:
                    raise ValidationError(
                        f"Duplicate disk target in chain: '{target}'."
                    )
                base_path = manager.chain_dir / (
                    "base.qcow2"
                    if len(disks) == 1
                    else f"base-{target}.qcow2"
                )
                base_source = spec.get("base")
                if base_source:
                    source_path = (
                        Path(str(base_source)).expanduser().resolve()
                    )
                    if not source_path.is_file():
                        raise NotFound(
                            f"Disk base image was not found: {source_path}"
                        )
                    manager._run(
                        "convert", "-O", "qcow2",
                        str(source_path), str(base_path),
                    )
                else:
                    virtual_size = int(spec.get("virtual_size") or 0)
                    if virtual_size <= 0:
                        raise ValidationError(
                            "An imported empty base needs a virtual size."
                        )
                    manager._run(
                        "create", "-f", "qcow2",
                        str(base_path), str(virtual_size),
                    )
                try:
                    os.chmod(base_path, 0o444)
                except OSError:
                    pass
                created.append(base_path)
                layers: list[Layer] = []
                layer_paths: dict[str, Path] = {}
                temp_paths: list[Path] = []
                active: str | None = None
                for index, item in enumerate(spec["layers"], start=1):
                    layer_id = f"{index:04d}"
                    layer_path = (
                        manager.chain_dir / f"disk-{target}-{layer_id}.qcow2"
                    )
                    parent_id = str(
                        item.get("parent")
                        or (
                            "base"
                            if index == 1
                            else f"{index - 1:04d}"
                        )
                    )
                    if parent_id == "base":
                        parent_path = base_path
                    else:
                        parent_path = layer_paths.get(parent_id)
                        if parent_path is None:
                            raise ValidationError(
                                f"Layer '{layer_id}' references unknown "
                                f"parent '{parent_id}'."
                            )
                    source = item.get("source")
                    kind = str(item.get("source_kind", "external"))
                    if source:
                        source_path = (
                            Path(str(source)).expanduser().resolve()
                        )
                        convert_source = source_path
                        if kind in {"internal", "current"}:
                            temp = (
                                manager.chain_dir
                                / f".import-{target}-{layer_id}.qcow2"
                            )
                            if kind == "internal":
                                snapshot = str(item.get("snapshot", ""))
                                if not snapshot:
                                    raise ValidationError(
                                        "Internal snapshot import needs a name."
                                    )
                                manager._run(
                                    "convert", "-l", snapshot,
                                    "-O", "qcow2",
                                    str(source_path), str(temp),
                                )
                            else:
                                manager._run(
                                    "convert", "-O", "qcow2",
                                    str(source_path), str(temp),
                                )
                            temp_paths.append(temp)
                            convert_source = temp
                        manager._run(
                            "convert", "-O", "qcow2",
                            "-B", str(parent_path), "-F", "qcow2",
                            str(convert_source), str(layer_path),
                        )
                        if convert_source in temp_paths:
                            convert_source.unlink(missing_ok=True)
                            temp_paths.remove(convert_source)
                    else:
                        manager._create_overlay(parent_path, layer_path)
                    created.append(layer_path)
                    layer_paths[layer_id] = layer_path
                    layers.append(
                        Layer(
                            id=layer_id,
                            path=str(layer_path),
                            parent=parent_id,
                            label=str(item.get("label", "")),
                            created_at=str(item.get("created_at", "")),
                        )
                    )
                    if item.get("active"):
                        active = layer_id
                if active is None:
                    active = layers[-1].id
                chain.disks[target] = Chain(
                    version=LEGACY_CHAIN_VERSION,
                    base=str(base_path),
                    layers=layers,
                    active=active,
                    target=target,
                    bus=str(spec.get("bus", "")),
                )
            manager.save(chain)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            for path in manager.chain_dir.glob(".import-*.qcow2"):
                path.unlink(missing_ok=True)
            try:
                manager.chain_dir.rmdir()
            except OSError:
                pass
            raise
        return manager

    def rename(self, new_dir: str | Path) -> "ChainManager":
        """Move the chain directory and rewrite all backing paths.

        Layer files must live inside the chain directory. A base image kept
        outside (for example a template base) is left in place. The operation
        is rolled back if metadata or qemu-img rebase fails.
        """
        new_path = Path(new_dir).expanduser().resolve()
        if new_path == self.chain_dir:
            return self
        if new_path.exists():
            raise Conflict(f"Chain destination already exists: {new_path}")
        if (self.chain_dir / COMMIT_JOURNAL).is_file():
            raise Conflict(
                "Chain has a pending snapshot commit; finish it before rename."
            )
        chain = self.load()
        old_json = self.chain_file.read_text(encoding="utf-8")
        disks = _iter_disks(chain)
        for disk in disks:
            for layer in disk.layers:
                if not _path_inside(layer.path, self.chain_dir):
                    raise ValidationError(
                        f"Layer is outside the chain directory: {layer.path}"
                    )
        try:
            os.rename(self.chain_dir, new_path)
        except OSError as exc:
            raise Conflict(
                f"Cannot rename chain directory {self.chain_dir}: {exc}"
            ) from exc
        new_manager = ChainManager(new_path, qemu_img=self.qemu_img)
        try:
            for disk in disks:
                disk.base = _rewrite_path(
                    disk.base, self.chain_dir, new_path
                )
                for layer in disk.layers:
                    layer.path = _rewrite_path(
                        layer.path, self.chain_dir, new_path
                    )
            new_manager.save(chain)
            self._rebase_all(new_manager, chain)
        except Exception as exc:
            try:
                new_manager.chain_file.write_text(old_json, encoding="utf-8")
                os.rename(new_path, self.chain_dir)
                old_manager = ChainManager(
                    self.chain_dir, qemu_img=self.qemu_img
                )
                self._rebase_all(old_manager, old_manager.load())
            except Exception as rollback_exc:
                raise VemuError(
                    "Chain rename failed and rollback also failed: "
                    f"{exc}; {rollback_exc}"
                ) from exc
            raise
        return new_manager

    @staticmethod
    def _rebase_all(manager: "ChainManager", chain: Chain | MultiDiskChain) -> None:
        for disk in _iter_disks(chain):
            for layer in disk.layers:
                parent = (
                    disk.base
                    if layer.parent == "base"
                    else disk.layer(layer.parent).path
                )
                manager._run(
                    "rebase",
                    "-u",
                    "-f", "qcow2",
                    "-F", "qcow2",
                    "-b", parent,
                    layer.path,
                )

    # ------------------------------------------------------------------ operations

    def snapshot(
        self, label: str = "", description: str = ""
    ) -> Layer | SnapshotSet:
        chain = self.load()
        if isinstance(chain, MultiDiskChain):
            return self._snapshot_multi(chain, label, description)
        parent_id = chain.active or "base"
        parent_path = (
            chain.base if parent_id == "base" else chain.layer(parent_id).path
        )
        layer_id = self._next_id(chain)
        layer = Layer(
            id=layer_id,
            path=str(self.chain_dir / f"disk-{layer_id}.qcow2"),
            parent=parent_id,
            label=label,
            description=description,
            created_at=_now(),
        )
        self._create_overlay(Path(parent_path), Path(layer.path))
        chain.layers.append(layer)
        chain.active = layer.id
        try:
            self.save(chain)
        except Exception:
            Path(layer.path).unlink(missing_ok=True)
            raise
        return layer

    def revert(self, layer_id: str) -> Layer | SnapshotSet:
        chain = self.load()
        if isinstance(chain, MultiDiskChain):
            return self._revert_multi(chain, layer_id)
        layer = chain.layer(layer_id)
        chain.active = layer.id
        self.save(chain)
        return layer

    def reset(self) -> Layer | SnapshotSet:
        """Return to the first layer (the clean clone on top of the template)."""
        chain = self.load()
        if isinstance(chain, MultiDiskChain):
            return self._reset_multi(chain)
        layer = chain.first_layer()
        if layer is None:
            raise NotFound("Chain has no baseline layer.")
        chain.active = layer.id
        self.save(chain)
        return layer

    def update_snapshot(
        self,
        layer_id: str,
        *,
        label: str | None = None,
        description: str | None = None,
    ) -> Layer | SnapshotSet:
        """Update user-facing metadata without touching disk overlays."""
        if label is None and description is None:
            raise ValidationError("No snapshot metadata was provided.")
        chain = self.load()
        if isinstance(chain, MultiDiskChain):
            layers: dict[str, Layer] = {}
            for target, disk in chain.disks.items():
                layer = disk.layer(layer_id)
                if label is not None:
                    layer.label = label
                if description is not None:
                    layer.description = description
                layers[target] = layer
            self.save(chain)
            first = next(iter(layers.values()))
            return SnapshotSet(
                id=first.id,
                label=first.label,
                description=first.description,
                created_at=first.created_at,
                layers=layers,
            )
        layer = chain.layer(layer_id)
        if label is not None:
            layer.label = label
        if description is not None:
            layer.description = description
        self.save(chain)
        return layer

    def delete(self, layer_id: str, force: bool = False) -> None:
        """Delete a snapshot layer.

        - Leaf layer: remove the file.
        - Middle layer: commit its child into it, remove the child, then rename
          the middle to the child, preserving the child's identity.
        - The first layer cannot be deleted while it has children (it would
          require merging into the read-only base).
        """
        chain = self.load()
        if isinstance(chain, MultiDiskChain):
            return self._delete_multi(chain, layer_id, force=force)
        return self._delete_single(chain, layer_id, force=force)

    def _delete_single(
        self,
        chain: Chain,
        layer_id: str,
        *,
        force: bool = False,
        save: Any = None,
    ) -> None:
        """Delete/commit one disk chain (v1 or the single disk of a v2 chain)."""
        persist = save or (lambda: self.save(chain))
        layer = chain.layer(layer_id)
        children = chain.children(layer.id)

        if len(chain.layers) == 1:
            raise Conflict("Cannot delete the only layer in the chain.")
        if len(children) > 1:
            raise Conflict(
                f"Layer '{layer.id}' has {len(children)} child snapshots. "
                "Delete the unwanted branches first; a branched layer cannot "
                "be merged automatically."
            )
        if layer.parent == "base" and children:
            raise Conflict(
                "Cannot delete the first layer while it has children. "
                "Consolidate or flatten the chain first."
            )
        if layer.id == chain.active and not children and not force:
            raise Conflict(
                "Deleting the active layer discards the current state. "
                "Use --force to confirm."
            )

        if not children:
            Path(layer.path).unlink(missing_ok=True)
            chain.layers.remove(layer)
            if chain.active == layer.id:
                chain.active = (
                    None if layer.parent == "base" else layer.parent
                )
            persist()
            return

        child = children[0]
        journal = {
            "version": 1,
            "operation": "commit",
            "layer": layer.id,
            "child": child.id,
            "layer_path": layer.path,
            "child_path": child.path,
            "active_before": chain.active,
            "started_at": _now(),
        }
        self._write_commit_journal(journal)
        self._run("commit", child.path)
        Path(child.path).unlink(missing_ok=True)
        os.replace(layer.path, child.path)
        self._finalize_commit(chain, layer, child)
        persist()
        self._clear_commit_journal()

    # ------------------------------------------------------------------ multi-disk

    def _snapshot_multi(
        self, chain: MultiDiskChain, label: str, description: str = ""
    ) -> SnapshotSet:
        layer_id = self._next_id_multi(chain)
        created: list[Path] = []
        layers: dict[str, Layer] = {}
        created_at = _now()
        try:
            for target, disk in chain.disks.items():
                parent_id = disk.active or "base"
                parent_path = (
                    disk.base
                    if parent_id == "base"
                    else disk.layer(parent_id).path
                )
                layer = Layer(
                    id=layer_id,
                    path=str(
                        self.chain_dir / f"disk-{target}-{layer_id}.qcow2"
                    ),
                    parent=parent_id,
                    label=label,
                    description=description,
                    created_at=created_at,
                )
                self._create_overlay(Path(parent_path), Path(layer.path))
                created.append(Path(layer.path))
                layers[target] = layer
            for target, disk in chain.disks.items():
                disk.layers.append(layers[target])
                disk.active = layer_id
            self.save(chain)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        return SnapshotSet(
            id=layer_id,
            label=label,
            description=description,
            created_at=created_at,
            layers=layers,
        )

    def _revert_multi(
        self, chain: MultiDiskChain, layer_id: str
    ) -> SnapshotSet:
        layers: dict[str, Layer] = {}
        for target, disk in chain.disks.items():
            layers[target] = disk.layer(layer_id)
        for disk in chain.disks.values():
            disk.active = layer_id
        self.save(chain)
        first = next(iter(layers.values()))
        return SnapshotSet(
            id=first.id,
            label=first.label,
            description=first.description,
            created_at=first.created_at,
            layers=layers,
        )

    def _reset_multi(self, chain: MultiDiskChain) -> SnapshotSet:
        first_ids = set()
        for disk in chain.disks.values():
            first = disk.first_layer()
            if first is None:
                raise NotFound("Chain has no baseline layer.")
            first_ids.add(first.id)
        if len(first_ids) != 1:
            raise ValidationError(
                "Multi-disk chain disks have different baseline layers."
            )
        return self._revert_multi(chain, next(iter(first_ids)))

    def _delete_multi(
        self, chain: MultiDiskChain, layer_id: str, *, force: bool
    ) -> None:
        if len(chain.disks) == 1:
            target, disk = next(iter(chain.disks.items()))
            self._delete_single(
                disk,
                layer_id,
                force=force,
                save=lambda: self.save(chain),
            )
            return
        children = {
            target: disk.children(layer_id)
            for target, disk in chain.disks.items()
        }
        if any(children.values()):
            if any(len(items) != 1 for items in children.values()):
                raise Conflict(
                    "A branched layer cannot be merged automatically. "
                    "Delete the unwanted leaf branches first."
                )
            if any(
                disk.layer(layer_id).parent == "base"
                for disk in chain.disks.values()
            ):
                raise Conflict(
                    "Cannot delete the first layer while it has children. "
                    "Consolidate or flatten the chain first."
                )
            self._delete_multi_middle(chain, layer_id)
            return
        for disk in chain.disks.values():
            if len(disk.layers) == 1:
                raise Conflict("Cannot delete the only layer in the chain.")
        active = chain.active_set()
        if active == layer_id and not force:
            raise Conflict(
                "Deleting the active layer discards the current state. "
                "Use --force to confirm."
            )
        paths: list[Path] = []
        for disk in chain.disks.values():
            layer = disk.layer(layer_id)
            paths.append(Path(layer.path))
            disk.layers.remove(layer)
            if disk.active == layer_id:
                disk.active = None if layer.parent == "base" else layer.parent
        # Persist metadata first; an orphan file is safer than a chain entry
        # pointing at a deleted file.
        self.save(chain)
        for path in paths:
            path.unlink(missing_ok=True)

    def _delete_multi_middle(
        self, chain: MultiDiskChain, layer_id: str
    ) -> None:
        entries: dict[str, tuple[Layer, Layer]] = {}
        for target, disk in chain.disks.items():
            layer = disk.layer(layer_id)
            children = disk.children(layer_id)
            if len(children) != 1:
                raise Conflict(
                    f"Layer '{layer_id}' is branched on disk '{target}'."
                )
            entries[target] = (layer, children[0])
        journal = {
            "version": 2,
            "operation": "commit_multi",
            "layer": layer_id,
            "child": next(iter(entries.values()))[1].id,
            "disks": {
                target: {
                    "layer_path": layer.path,
                    "child_path": child.path,
                    "active_before": chain.disks[target].active,
                }
                for target, (layer, child) in entries.items()
            },
            "started_at": _now(),
        }
        self._write_commit_journal(journal)
        for target, (layer, child) in entries.items():
            self._run("commit", child.path)
            Path(child.path).unlink(missing_ok=True)
            os.replace(layer.path, child.path)
        for target, (layer, child) in entries.items():
            self._finalize_commit(chain.disks[target], layer, child)
        self.save(chain)
        self._clear_commit_journal()

    def _next_id_multi(self, chain: MultiDiskChain) -> str:
        numeric = [
            int(layer.id)
            for disk in chain.disks.values()
            for layer in disk.layers
            if layer.id.isdigit()
        ]
        next_value = (max(numeric) + 1) if numeric else 1
        return f"{next_value:04d}"

    def add_disk(
        self,
        target: str,
        base: str | Path,
        *,
        bus: str = "",
        label: str = "added",
    ) -> Chain:
        """Add a data disk to a v2 chain, aligned to existing snapshots.

        The new disk gets one overlay per existing layer ID. Older snapshots
        therefore see the new disk at its empty/base state; they do not gain
        historical data that never existed.
        """
        chain = self.load()
        if not isinstance(chain, MultiDiskChain):
            raise Conflict("Adding a chain disk requires a v2 chain.")
        if target in chain.disks:
            raise Conflict(f"Disk target '{target}' is already in the chain.")
        if len(chain.disks) < 1:
            raise ValidationError("A chain needs at least one disk.")
        base_path = Path(base).expanduser().resolve()
        if not base_path.is_file():
            raise NotFound(f"Disk base image was not found: {base_path}")
        first = next(iter(chain.disks.values()))
        canonical = list(first.layers)
        created: list[Path] = []
        layers: list[Layer] = []
        parent_path = base_path
        try:
            for existing in canonical:
                path = self.chain_dir / f"disk-{target}-{existing.id}.qcow2"
                self._create_overlay(parent_path, path)
                created.append(path)
                layers.append(
                    Layer(
                        id=existing.id,
                        path=str(path),
                        parent=existing.parent,
                        label=existing.label or label,
                        created_at=existing.created_at or _now(),
                    )
                )
                parent_path = path
            new_chain = Chain(
                version=LEGACY_CHAIN_VERSION,
                base=str(base_path),
                layers=layers,
                active=chain.active_set(),
                target=target,
                bus=bus,
            )
            chain.disks[target] = new_chain
            chain.validate()
            self.save(chain)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            chain.disks.pop(target, None)
            raise
        return new_chain

    def remove_disk(self, target: str) -> Chain:
        """Remove one disk from a v2 chain, leaving other disks untouched."""
        chain = self.load()
        if not isinstance(chain, MultiDiskChain):
            raise Conflict("Removing a chain disk requires a v2 chain.")
        if len(chain.disks) < 2:
            raise Conflict("Cannot remove the only data disk from a chain.")
        disk = chain.disk(target)
        del chain.disks[target]
        chain.validate()
        self.save(chain)
        return disk

    def recover_commit(self) -> dict[str, Any] | None:
        """Finish a commit that was interrupted by a crash.

        The journal is written before ``qemu-img commit``. Re-running commit
        is safe because qemu-img copies the child's clusters over the parent
        again. If the rename already happened, only the chain metadata needs
        updating. Returns ``None`` when no journal exists.
        """
        journal_path = self.chain_dir / COMMIT_JOURNAL
        if not journal_path.is_file():
            return None
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            layer_id = str(journal["layer"])
            child_id = str(journal["child"])
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise ValidationError(
                f"Cannot read commit journal {journal_path}: {exc}"
            ) from exc
        if int(journal.get("version", 1)) == 2 and "disks" in journal:
            return self._recover_multi(journal_path, journal)
        loaded = self.load()
        if isinstance(loaded, MultiDiskChain):
            if len(loaded.disks) != 1:
                raise ValidationError(
                    "Commit recovery is not supported for multi-disk chains."
                )
            chain = loaded._only_disk()
            save = lambda: self.save(loaded)
        else:
            chain = loaded
            save = lambda: self.save(loaded)
        try:
            layer = chain.layer(layer_id)
            child = chain.layer(child_id)
        except NotFound:
            journal_path.unlink(missing_ok=True)
            return {
                "chain": str(self.chain_dir),
                "recovered": False,
                "already_complete": True,
            }
        layer_path = Path(layer.path)
        child_path = Path(child.path)
        if layer_path.exists() and child_path.exists():
            # The commit was interrupted before the child was removed. Re-run
            # it from the beginning; qemu-img commit is idempotent.
            self._run("commit", str(child_path))
            child_path.unlink(missing_ok=True)
            os.replace(layer_path, child_path)
            action = "commit"
        elif layer_path.exists() and not child_path.exists():
            # Commit finished, but the rename did not happen.
            os.replace(layer_path, child_path)
            action = "rename"
        elif child_path.exists() and not layer_path.exists():
            # Rename finished, but chain.json was not updated.
            action = "metadata"
        else:
            raise ValidationError(
                f"Cannot recover chain {self.chain_dir}: both "
                f"{layer_path.name} and {child_path.name} are missing."
            )
        self._finalize_commit(chain, layer, child)
        save()
        journal_path.unlink(missing_ok=True)
        return {
            "chain": str(self.chain_dir),
            "recovered": True,
            "action": action,
            "layer": layer_id,
            "child": child_id,
        }

    def _recover_multi(
        self, journal_path: Path, journal: dict[str, Any]
    ) -> dict[str, Any]:
        loaded = self.load()
        if not isinstance(loaded, MultiDiskChain):
            raise ValidationError(
                "Multi-disk commit journal does not match chain format."
            )
        layer_id = str(journal["layer"])
        child_id = str(journal.get("child", ""))
        entries = journal.get("disks")
        if not isinstance(entries, dict) or not entries:
            raise ValidationError("Multi-disk commit journal has no disks.")
        for target in entries:
            try:
                disk = loaded.disk(str(target))
            except NotFound as exc:
                raise ValidationError(
                    f"Commit journal references missing disk '{target}'."
                ) from exc
            try:
                layer = disk.layer(layer_id)
                child = disk.layer(child_id)
            except NotFound:
                # The metadata save may have completed before the journal was
                # cleared; the child is already the surviving layer.
                continue
            layer_path = Path(layer.path)
            child_path = Path(child.path)
            if layer_path.exists() and child_path.exists():
                self._run("commit", str(child_path))
                child_path.unlink(missing_ok=True)
                os.replace(layer_path, child_path)
            elif layer_path.exists() and not child_path.exists():
                os.replace(layer_path, child_path)
            elif not child_path.exists():
                raise ValidationError(
                    f"Cannot recover disk '{target}': both "
                    f"{layer_path.name} and {child_path.name} are missing."
                )
            self._finalize_commit(disk, layer, child)
        self.save(loaded)
        journal_path.unlink(missing_ok=True)
        return {
            "chain": str(self.chain_dir),
            "recovered": True,
            "action": "commit_multi",
            "layer": layer_id,
            "child": child_id,
        }

    def _write_commit_journal(self, journal: dict[str, Any]) -> None:
        path = self.chain_dir / COMMIT_JOURNAL
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(journal, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)

    def _clear_commit_journal(self) -> None:
        (self.chain_dir / COMMIT_JOURNAL).unlink(missing_ok=True)

    @staticmethod
    def _finalize_commit(
        chain: Chain, layer: Layer, child: Layer
    ) -> None:
        if any(item.id == layer.id for item in chain.layers):
            chain.layers.remove(layer)
        child.parent = layer.parent
        if chain.active == layer.id:
            chain.active = child.id

    # ------------------------------------------------------------------ inspection

    def info(self) -> dict[str, Any]:
        chain = self.load()
        pending = (self.chain_dir / COMMIT_JOURNAL).is_file()
        if isinstance(chain, MultiDiskChain):
            return self._info_multi(chain, pending=pending)
        layers = [self._layer_info(chain, layer) for layer in chain.layers]
        return {
            "base": chain.base,
            "active": chain.active,
            "active_disk": chain.active_disk(),
            "layers": layers,
            "pending_recovery": pending,
        }

    def _layer_info(self, chain: Chain, layer: Layer) -> dict[str, Any]:
        size = 0
        path = Path(layer.path)
        if path.exists():
            try:
                size = int(
                    json.loads(
                        self._run(
                            "info", "-U", "--output=json", str(path)
                        ).stdout
                    )["actual-size"]
                )
            except (ExternalCommandError, ValueError, KeyError):
                size = 0
        return {
            **layer.to_dict(),
            "actual_size": size,
            "active": layer.id == chain.active,
        }

    def _info_multi(
        self, chain: MultiDiskChain, *, pending: bool
    ) -> dict[str, Any]:
        disks: list[dict[str, Any]] = []
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for target in sorted(chain.disks):
            disk = chain.disks[target]
            layers = [self._layer_info(disk, layer) for layer in disk.layers]
            for layer in layers:
                layer_id = str(layer["id"])
                if layer_id not in merged:
                    merged[layer_id] = {
                        **layer,
                        "actual_size": 0,
                        "disks": [],
                    }
                    order.append(layer_id)
                merged[layer_id]["actual_size"] += int(
                    layer.get("actual_size") or 0
                )
                merged[layer_id]["disks"].append(target)
                merged[layer_id]["active"] = bool(
                    merged[layer_id].get("active") or layer.get("active")
                )
            disks.append(
                {
                    "target": target,
                    "bus": disk.bus,
                    "base": disk.base,
                    "active": disk.active,
                    "active_disk": disk.active_disk(),
                    "layers": layers,
                }
            )
        first = chain.disks[sorted(chain.disks)[0]]
        canonical = [merged[layer_id] for layer_id in order]
        return {
            "multi_disk": True,
            "version": chain.version,
            "disk_count": len(chain.disks),
            "disks": disks,
            "base": first.base,
            "active": chain.active_set(),
            "active_disk": first.active_disk(),
            "layers": canonical,
            "pending_recovery": pending,
        }

    def verify(self) -> list[str]:
        """Return a list of integrity issues. An empty list means healthy."""
        try:
            chain = self.load()
        except Exception as exc:  # noqa: BLE001 - report as an issue
            return [str(exc)]
        journal = self.chain_dir / COMMIT_JOURNAL
        issues: list[str] = []
        if journal.is_file():
            issues.append(
                f"Unfinished snapshot commit journal: {journal}. "
                "Run 'vemu doctor --repair' to finish it."
            )
        if isinstance(chain, MultiDiskChain):
            for target in sorted(chain.disks):
                issues.extend(
                    self._verify_single(
                        chain.disks[target], label=f"disk '{target}': "
                    )
                )
            return issues
        return issues + self._verify_single(chain)

    def _verify_single(
        self, chain: Chain, *, label: str = ""
    ) -> list[str]:
        issues: list[str] = []
        prefix = f"{label} " if label else ""
        if not Path(chain.base).is_file():
            issues.append(f"{prefix}Base image is missing: {chain.base}")
        for layer in chain.layers:
            path = Path(layer.path)
            if not path.is_file():
                issues.append(
                    f"{prefix}Layer file is missing: {layer.path}"
                )
                continue
            result = self._run("check", str(path), check=False)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                issues.append(
                    f"{prefix}qemu-img check failed for {layer.path}: {detail}"
                )

        active = chain.active_layer()
        if active is None:
            issues.append(f"{prefix}Chain has no active layer.")
            return issues
        result = self._run(
            "info",
            "-U",
            "--backing-chain",
            "--output=json",
            active.path,
            check=False,
        )
        if result.returncode != 0:
            issues.append(
                f"{prefix}Cannot read backing chain for {active.path}: "
                f"{result.stderr.strip()}"
            )
            return issues
        try:
            images = json.loads(result.stdout)
            expected_paths: list[str] = []
            current: Layer | None = active
            while current is not None:
                expected_paths.append(str(Path(current.path).resolve()))
                if current.parent == "base":
                    break
                current = chain.layer(current.parent)
            expected_paths.append(str(Path(chain.base).resolve()))
            if len(images) != len(expected_paths):
                issues.append(
                    f"{prefix}Backing chain has {len(images)} images; "
                    f"expected {len(expected_paths)}."
                )
            else:
                actual = [
                    str(Path(str(image.get("filename", ""))).resolve())
                    for image in images
                ]
                if actual != expected_paths:
                    issues.append(
                        f"{prefix}Backing chain does not match chain.json order."
                    )
        except (ValueError, TypeError):
            issues.append(
                f"{prefix}Could not parse qemu-img backing chain output."
            )
        return issues

    # ------------------------------------------------------------------ helpers

    def _next_id(self, chain: Chain) -> str:
        numeric = [int(layer.id) for layer in chain.layers if layer.id.isdigit()]
        next_value = (max(numeric) + 1) if numeric else 1
        return f"{next_value:04d}"

    def _create_overlay(self, parent: Path, dest: Path) -> None:
        if not parent.is_file():
            raise NotFound(f"Backing image was not found: {parent}")
        if dest.exists():
            raise Conflict(f"Layer file already exists: {dest}")
        self._run(
            "create",
            "-f", "qcow2",
            "-F", "qcow2",
            "-b", str(parent),
            str(dest),
        )

    def _run(
        self,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.qemu_img, *args]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise ExternalCommandError(
                " ".join(command),
                result.returncode,
                result.stderr,
            )
        return result
