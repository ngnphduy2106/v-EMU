"""Short, page-specific help shown from F1 or the top-bar help button."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTextBrowser

from .dialogs import VemuDialog, dialog_layout

HELP_TOPICS: dict[str, tuple[str, str]] = {
    "Virtual machines": (
        "Virtual machines",
        "<p>Create a VM with <b>New VM</b> (<code>Ctrl+N</code>) from an "
        "installer ISO. The table shows OS, state, managed mode and disks.</p>"
        "<p>Select a VM to start, stop, open its console, take snapshots, "
        "edit hardware or clone it. External/read-only VMs must be adopted "
        "or migrated before editing.</p>"
        "<p><b>Ctrl+R</b> refreshes the current view; <b>Ctrl+I</b> toggles "
        "the details inspector.</p>",
    ),
    "Templates": (
        "Templates",
        "<p>Seal a stopped VM as an immutable base, or import a golden image/"
        "OVA. Clones are linked overlays and do not copy the base.</p>"
        "<p>Use <b>Edit metadata</b> to fix guest OS family or Sysprep "
        "metadata. <b>Ctrl+N</b> starts an import.</p>",
    ),
    "Networks": (
        "Networks",
        "<p>Create NAT, isolated or bridge profiles, then press <b>Apply</b> "
        "to reconcile libvirt/nft. Use <b>Ctrl+N</b> to add a network.</p>"
        "<p><b>LAN bridge…</b> registers an existing host bridge or creates "
        "a new one through NetworkManager. Creating a bridge briefly "
        "interrupts host networking; v-EMU snapshots the old uplink profile "
        "and rolls back on failure.</p>"
        "<p>Port forwards need a guest IP inside the profile subnet; restart "
        "the network after changing profiles.</p>",
    ),
    "Storage": (
        "Storage",
        "<p>Register folders for lab disks, templates and ISOs. A <code>labs"
        "</code> location is required before creating VMs. <b>Ctrl+N</b> adds "
        "a location.</p>"
        "<p>Use inspect/repair to fix labels or ownership, and garbage "
        "collection to remove unreferenced images.</p>",
    ),
    "Shared folders": (
        "Shared folders",
        "<p>Register host folders once, then attach them per VM. VirtioFS "
        "shares need a stopped VM for attach/detach. <b>Ctrl+N</b> adds a "
        "folder.</p>",
    ),
    "Labs": (
        "Labs",
        "<p>A lab is a <code>lab.yaml</code> file describing VMs and "
        "networks. Load it here to bring the whole lab up or down. "
        "<b>Ctrl+R</b> refreshes status.</p>",
    ),
    "Guide": (
        "Guide",
        "<p>In-app workflow reference: install, VM creation, templates, "
        "networks, storage, troubleshooting and known limitations. "
        "<b>F1</b> opens help for the current page.</p>",
    ),
    "Doctor": (
        "Doctor",
        "<p>Checks host prerequisites: KVM, libvirt, QEMU, firmware, guest "
        "tools and storage/network health. <b>Ctrl+R</b> runs the checks "
        "again.</p>",
    ),
    "Settings": (
        "Settings",
        "<p>Appearance, panels, refresh, new-VM defaults, resource"
        " scheduling, boot media and cross-architecture guests.</p>"
        "<p>Resource profiles are best-effort CPU/IO weights; they do not cap "
        "temperature or power. TCG is software emulation and should be used "
        "only for short tests.</p>",
    ),
}


class ContextHelpDialog(VemuDialog):
    """Modeless-looking modal help panel for the current page."""

    def __init__(self, topic: str, parent=None) -> None:
        super().__init__(parent, title=f"Help · {topic}")
        self.setMinimumSize(520, 360)
        layout = dialog_layout(self)
        title = QLabel(HELP_TOPICS.get(topic, (topic, ""))[0])
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        _, body = HELP_TOPICS.get(
            topic,
            (
                topic,
                "<p>No page-specific help is available yet.</p>",
            ),
        )
        browser = QTextBrowser()
        browser.setObjectName("GuideBrowser")
        browser.setOpenExternalLinks(True)
        browser.setHtml(body)
        layout.addWidget(browser, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("PrimaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
