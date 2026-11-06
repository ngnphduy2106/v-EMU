"""Fixed, product-managed identity exposed to guests through SMBIOS.

This is intentionally not user-configurable. The identity belongs to the vEMU
product, managed by CORESYSTEM VN, and is applied only to new x86_64 domains
created by v-EMU. Existing domains and external/Cockpit VMs are left
untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone

MANUFACTURER = "CORESYSTEM VN"
PRODUCT = "vEMU"
BOARD_PRODUCT = "vEMU Virtual Board"
SKU = "VEMU-LAB"
FAMILY = "Virtual Lab"


def build_date() -> str:
    """Return the SMBIOS build date in the format QEMU expects."""
    return datetime.now(timezone.utc).strftime("%m/%d/%Y")


def serial(domain_uuid: str) -> str:
    """Return a compact, stable, per-domain serial number.

    The first eight hex characters of the UUID are enough to distinguish lab
    domains; the full UUID remains available in the domain XML.
    """
    compact = domain_uuid.replace("-", "").strip().upper()
    return f"VEMU-{compact[:8]}"


def smbios_groups(
    version: str, domain_uuid: str
) -> dict[str, list[tuple[str, str]]]:
    """Return the SMBIOS groups and entries for a v-EMU system."""
    return {
        "bios": [
            ("vendor", MANUFACTURER),
            ("version", f"vEMU {version}"),
            ("date", build_date()),
        ],
        "system": [
            ("manufacturer", MANUFACTURER),
            ("product", PRODUCT),
            ("version", version),
            ("serial", serial(domain_uuid)),
            ("sku", SKU),
            ("family", FAMILY),
        ],
        "baseBoard": [
            ("manufacturer", MANUFACTURER),
            ("product", BOARD_PRODUCT),
            ("version", version),
        ],
        "chassis": [
            ("manufacturer", MANUFACTURER),
            ("version", f"vEMU {version}"),
        ],
    }


def oem_strings(version: str) -> list[str]:
    """Return OEM strings that identify the product and its manager."""
    return [f"vEMU={version}", f"ManagedBy={MANUFACTURER}"]
