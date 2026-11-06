"""Best-effort host resource scheduling profiles.

Profiles change libvirt CPU/IO weights only. They do not set CPU quotas and do
not guarantee lower temperatures, power draw or host load; the VM can still use
idle host cores. The wording in the UI and docs must keep that distinction.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import ValidationError

DEFAULT_PROFILE = "standard"

PROFILES: dict[str, dict[str, Any]] = {
    "standard": {
        "label": "Standard",
        "cpu_shares": 512,
        "io_weight": 500,
        "description": (
            "Prefer desktop and host work. The VM still uses idle CPU cores."
        ),
    },
    "high": {
        "label": "High",
        "cpu_shares": 1024,
        "io_weight": 750,
        "description": (
            "Balanced: normal CPU/IO weight for interactive lab work."
        ),
    },
    "ultra": {
        "label": "Ultra",
        "cpu_shares": 2048,
        "io_weight": 1000,
        "description": (
            "Prefer the VM over host work. Host applications may feel slower "
            "during heavy guest load."
        ),
    },
}


def normalize_profile(name: str) -> str:
    key = str(name or "").strip().lower()
    if key not in PROFILES:
        raise ValidationError(
            "Resource profile must be one of: "
            + ", ".join(PROFILES)
            + "."
        )
    return key


def config_path() -> Path:
    override = os.environ.get("VEMU_RUNTIME_CONFIG")
    if override:
        return Path(override).expanduser()
    if os.geteuid() == 0:
        return Path("/var/lib/vemu/runtime.json")
    base = Path(
        os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    )
    return base / "vemu" / "runtime.json"


def load_profile() -> str:
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_PROFILE
    key = str(data.get("resource_profile", DEFAULT_PROFILE)).lower()
    return key if key in PROFILES else DEFAULT_PROFILE


def save_profile(name: str) -> str:
    key = normalize_profile(name)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"resource_profile": key}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return key


def profile_payload(name: str | None = None) -> dict[str, Any]:
    key = load_profile() if name is None else normalize_profile(name)
    spec = PROFILES[key]
    return {
        "profile": key,
        "label": spec["label"],
        "cpu_shares": spec["cpu_shares"],
        "io_weight": spec["io_weight"],
        "description": spec["description"],
        "best_effort": True,
        "profiles": [
            {
                "profile": item_key,
                "label": item["label"],
                "description": item["description"],
            }
            for item_key, item in PROFILES.items()
        ],
    }
