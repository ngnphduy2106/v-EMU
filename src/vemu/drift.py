"""Canonical domain XML hashing and drift detection.

The baseline is stored in the v-EMU management metadata. Hashing ignores
metadata and sorts attributes/children so harmless XML ordering changes from
libvirt or Cockpit do not look like drift.
"""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

MANAGEMENT_NS = "https://vemu.local/management"
_VOLATILE_TAGS = {"metadata", "seclabel"}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _canonical(element: ET.Element) -> None:
    for child in list(element):
        if _local_name(child.tag) in _VOLATILE_TAGS:
            element.remove(child)
    element.attrib = dict(sorted(element.attrib.items()))
    if element.text is not None:
        element.text = element.text.strip() or None
    children = list(element)
    children.sort(
        key=lambda child: (
            child.tag,
            ET.tostring(child, encoding="unicode"),
        )
    )
    element[:] = children
    for child in children:
        _canonical(child)


def canonical_xml(xml: str) -> str:
    root = ET.fromstring(xml)
    _canonical(root)
    return ET.tostring(root, encoding="unicode")


def xml_hash(xml: str) -> str:
    return hashlib.sha256(canonical_xml(xml).encode("utf-8")).hexdigest()


def baseline(xml: str) -> str | None:
    root = ET.fromstring(xml)
    metadata = root.find(f".//{{{MANAGEMENT_NS}}}management")
    if metadata is None:
        return None
    return metadata.get("baseline") or None


def set_baseline(xml: str) -> str:
    root = ET.fromstring(xml)
    metadata = root.find(f".//{{{MANAGEMENT_NS}}}management")
    if metadata is None:
        return xml
    metadata.set("baseline", xml_hash(xml))
    return ET.tostring(root, encoding="unicode")


def status(xml: str) -> str:
    expected = baseline(xml)
    if not expected:
        return "unknown"
    return "clean" if expected == xml_hash(xml) else "drifted"


def _baseline_dir() -> Path:
    configured = os.environ.get("VEMU_BASELINE_DIR")
    if configured:
        return Path(configured).expanduser()
    backups = os.environ.get("VEMU_BACKUP_DIR")
    if backups:
        return Path(backups).expanduser() / "baselines"
    if os.geteuid() == 0:
        return Path("/var/lib/vemu/baselines")
    return Path.home() / ".local" / "state" / "vemu" / "baselines"


def _safe_domain(domain: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", domain)


def baseline_path(domain: str) -> Path:
    return _baseline_dir() / f"{_safe_domain(domain)}.xml"


def store_baseline(domain: str, xml: str) -> str | None:
    """Best-effort sidecar copy of the XML used for the current baseline."""
    path = baseline_path(domain)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".xml.tmp")
        tmp.write_text(xml, encoding="utf-8")
        os.replace(tmp, path)
        return str(path)
    except OSError:
        return None


def load_baseline(domain: str) -> str | None:
    try:
        return baseline_path(domain).read_text(encoding="utf-8")
    except OSError:
        return None


def _short(value: str, limit: int = 140) -> str:
    text = value.strip()
    if not text:
        return "∅"
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def diff(before_xml: str, after_xml: str, limit: int = 40) -> list[str]:
    """Return human-readable XML changes between a baseline and current XML."""
    try:
        before = ET.fromstring(before_xml)
        after = ET.fromstring(after_xml)
    except ET.ParseError:
        return ["The baseline or current XML could not be parsed."]
    _canonical(before)
    _canonical(after)
    changes: list[str] = []

    def walk(left: ET.Element, right: ET.Element, path: str) -> None:
        if left.tag != right.tag:
            changes.append(
                f"{path}: element {_local_name(left.tag)} -> "
                f"{_local_name(right.tag)}"
            )
            return
        for key in sorted(set(left.attrib) | set(right.attrib)):
            old = left.attrib.get(key)
            new = right.attrib.get(key)
            if old != new:
                changes.append(
                    f"{path}/@{key}: {old or '∅'} -> {new or '∅'}"
                )
        old_text = (left.text or "").strip()
        new_text = (right.text or "").strip()
        if old_text != new_text:
            changes.append(
                f"{path}: {_short(old_text)} -> {_short(new_text)}"
            )
        left_groups: dict[str, list[ET.Element]] = {}
        right_groups: dict[str, list[ET.Element]] = {}
        for child in list(left):
            left_groups.setdefault(child.tag, []).append(child)
        for child in list(right):
            right_groups.setdefault(child.tag, []).append(child)
        for tag in sorted(set(left_groups) | set(right_groups)):
            old_items = left_groups.get(tag, [])
            new_items = right_groups.get(tag, [])
            name = _local_name(tag)
            if len(old_items) != len(new_items):
                changes.append(
                    f"{path}/{name}: {len(old_items)} -> "
                    f"{len(new_items)} element(s)"
                )
            for index, (old_item, new_item) in enumerate(
                zip(old_items, new_items)
            ):
                label = name
                if len(old_items) > 1 or len(new_items) > 1:
                    label = f"{name}[{index + 1}]"
                walk(old_item, new_item, f"{path}/{label}")

    walk(before, after, _local_name(before.tag))
    if len(changes) > limit:
        extra = len(changes) - limit
        changes = changes[:limit] + [f"… {extra} more change(s)"]
    return changes


def details(xml: str) -> dict[str, str | None]:
    expected = baseline(xml)
    current = xml_hash(xml)
    if not expected:
        state = "unknown"
    elif expected == current:
        state = "clean"
    else:
        state = "drifted"
    return {
        "status": state,
        "baseline": expected,
        "current": current,
    }
