"""User-facing strings.

The product is English-only. Keeping strings in one module ensures consistent
terminology across the CLI, UI and future daemon.
"""

from __future__ import annotations

APP_NAME = "v-EMU"
APP_TAGLINE = "Desktop Virtualization for QEMU/KVM"

NAV_LABS = "Labs"
NAV_TEMPLATES = "Templates"
NAV_VMS = "Virtual machines"
NAV_NETWORKS = "Networks"
NAV_STORAGE = "Storage"
NAV_SHARES = "Shared folders"
NAV_GUIDE = "Guide"
NAV_DOCTOR = "Doctor"
NAV_SETTINGS = "Settings"

PAGE_VMS_TITLE = "Virtual machines"
PAGE_VMS_SUBTITLE = "Laboratory workspace"
ACTION_SEAL = "Seal as template"
ACTION_CLONE = "Clone"
ACTION_REFRESH = "Refresh"
ACTION_START = "Start"
ACTION_RESUME = "Resume"
ACTION_SHUTDOWN = "Shutdown"
ACTION_FORCE_OFF = "Force off"
ACTION_RESTART = "Restart"
ACTION_CONSOLE = "Console"
ACTION_PERFORMANCE = "Performance tuning…"
ACTION_SNAPSHOT = "Snapshots"
ACTION_MORE = "More"
ACTION_COPY_NAME = "Copy name"
ACTION_RENAME = "Rename VM…"
ACTION_MIGRATE = "Adopt / Migrate to v-EMU…"
ACTION_COPY_IP = "Copy IP"
ACTION_DELETE = "Delete VM…"

CONFIRM_FORCE_OFF = (
    "Force off {name}? Unsaved data in the guest may be lost."
)

KPI_TOTAL = "Total VMs"
KPI_RUNNING = "Running"
KPI_STOPPED = "Stopped"
KPI_DISKS = "Disks"

TABLE_NAME = "Name"
TABLE_OS = "OS"
TABLE_STATE = "State"
TABLE_MANAGED = "Managed by"
TABLE_DISKS = "Disks"
TABLE_AUTOSTART = "Autostart"
TABLE_UUID = "UUID"

INSPECTOR_TITLE = "Details"
INSPECTOR_SELECT = "Select a VM to inspect it."
INSPECTOR_STATE = "State"
INSPECTOR_UUID = "UUID"
INSPECTOR_DISKS = "Disks"

EMPTY_TITLE = "No virtual machines yet"
EMPTY_BODY = (
    "Install a guest with virt-manager, then seal it as a template "
    "and clone it here."
)

SEARCH_PLACEHOLDER = "Search VMs…"
THEME_TOGGLE = "Toggle theme"
STATUS_READY = "Ready"
STATUS_DOMAINS = "{count} domain(s)"

M0_NOTE = "M0 shell — read-only view of libvirt domains."
