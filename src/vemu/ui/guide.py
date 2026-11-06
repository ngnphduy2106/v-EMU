"""In-app user guide rendered from Markdown."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .markdown import render_markdown
from .theme import DARK, LIGHT, Tokens
from .components import PageHeader
from .tokens import SPACE, TYPE


FALLBACK_MARKDOWN = r"""
# v-EMU guide

## Quick start

1. Create a storage location under **Storage**.
2. Create a network under **Networks**, then press **Apply**.
3. Import a golden image or create a VM from an ISO.
4. Use **Templates** to clone linked labs.
5. Use the inspector mini console for a quick status view.

## New VM from ISO

Use **Virtual machines → New VM** or `vemu create`. Windows Server 2025
installs unattended with SATA/e1000e/VGA and a generated answer USB. The
optional **Driver ISO** is Windows-only, starts empty, and is not mounted when
left blank.

## Windows templates

Run Sysprep **inside Windows** manually:

```text
C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown
```

Then **Seal as template** and confirm the Sysprep checkbox. v-EMU does not run
Sysprep. Sealing without it is allowed, but clones keep the same SID and
machine identity. Clones of a sysprepped image receive a per-clone OOBE answer
USB and boot to an active Administrator session.

## Networks

**Networks** manages NAT/isolated profiles, DHCP reservations, port forwards,
apply/restart and VM NIC attach. Port forwards warn when the guest IP has no
reservation or active lease.

## Templates and Packer

**Templates** imports Packer/qcow2 golden images, exports templates and clones
labs.

## IaC

`examples/lab.yaml` plus `vemu up`, `vemu status`, `vemu down`.
`vemu lab export/import` packages a portable lab.

## cloud-init

Linux clones can receive a NoCloud seed with user, SSH key, packages and
static network config: `vemu clone ... --cloud-init`.

## Shared folders

Attach a **Shared folders** catalog entry to one VM, then fully stop and start
it. Windows needs WinFsp and `viofs`. Linux needs host `virtiofsd` and guest
virtiofs support; vEMU mounts at `/mnt/vemu/<tag>` when QGA allows
`guest-exec`, otherwise mount manually:

```bash
sudo mkdir -p /mnt/share
sudo mount -t virtiofs share-tag /mnt/share
```

The tag is the catalog **Guest tag**. Shares are read/write unless marked
read-only, so they can replace clipboard file exchange when a guest blocks
`guest-exec`.

## Console and clipboard

The native console toolbar starts hidden; press `F9` or `Ctrl+Shift+T` to show
it. With it hidden, `Ctrl+Alt+Insert` sends Ctrl+Alt+Del, `Ctrl+Alt+F` toggles
fullscreen, `Ctrl+Alt+Print` saves a screenshot, and `Ctrl+Alt+V` pastes the
host clipboard into the guest.

Clipboard is optional. Windows needs QEMU guest agent, a logged-in user and the
`guest-exec`/`guest-file-*` RPCs. Linux needs Wayland, `wl-clipboard`,
`guest-get-osinfo` and `guest-exec`; RHEL/Alma/Rocky, some SUSE images and
Fedora 45 restrict `guest-exec` in `/etc/sysconfig/qemu-ga`. Allow it there and
restart `qemu-guest-agent` if you accept the wider command surface; v-EMU does
not change guest packages or agent policy. If the guest must keep the block,
use a VirtioFS share instead. Fedora 45, Alma 10.2, Debian 13, Leap 16.1 and
SLES 16 all needed guest-side changes when tested (agent policy, `wl-clipboard`
or a Wayland session).

## Limits

Static cloud-init IP requires `--define`; multi-NIC port-forward return routing
may need guest policy routing; packaging is deferred. Linux clipboard needs
Wayland, `wl-clipboard` and QGA `guest-exec`; X11 is out of scope.
"""


def _guide_css(tokens: Tokens) -> str:
    return f"""
    body {{
        color: {tokens.text};
        background-color: {tokens.bg};
        font-family: "Inter Variable", "Inter", sans-serif;
        font-size: {TYPE.body}px;
    }}
    h1 {{
        font-size: {TYPE.title}px;
        font-weight: 700;
        margin: 4px 0 12px 0;
        color: {tokens.text};
    }}
    h2 {{
        font-size: {TYPE.hero}px;
        font-weight: 700;
        margin: {SPACE.xl}px 0 {SPACE.sm}px 0;
        padding-bottom: 4px;
        border-bottom: 1px solid {tokens.border};
        color: {tokens.text};
    }}
    h3 {{
        font-size: {TYPE.body_lg}px;
        font-weight: 700;
        margin: {SPACE.lg}px 0 {SPACE.xs}px 0;
        color: {tokens.text};
    }}
    h4, h5, h6 {{
        font-size: {TYPE.body}px;
        font-weight: 700;
        margin: {SPACE.md}px 0 {SPACE.xs}px 0;
        color: {tokens.text};
    }}
    p {{ margin: 6px 0; }}
    a {{ color: {tokens.accent}; text-decoration: none; }}
    code {{
        color: {tokens.accent};
        background-color: {tokens.surface};
        font-family: "JetBrains Mono", monospace;
    }}
    pre {{
        color: {tokens.text};
        background-color: {tokens.surface};
        border: 1px solid {tokens.border};
        padding: 9px 11px;
        margin: 8px 0;
        white-space: pre-wrap;
    }}
    pre code {{ color: {tokens.text}; background-color: transparent; }}
    blockquote {{
        color: {tokens.muted};
        background-color: {tokens.surface};
        border-left: 3px solid {tokens.accent};
        padding: 6px 10px;
        margin: 8px 0;
    }}
    table {{
        border-collapse: collapse;
        margin: 8px 0;
        width: 100%;
    }}
    th, td {{
        border: 1px solid {tokens.border};
        padding: 5px 7px;
        vertical-align: top;
    }}
    th {{
        background-color: {tokens.surface};
        color: {tokens.text};
        font-weight: 700;
    }}
    hr {{
        border: none;
        border-top: 1px solid {tokens.border};
        margin: 14px 0;
    }}
    ul, ol {{ margin: 5px 0 5px 22px; }}
    li {{ margin: 2px 0; }}
    """


def guide_markdown() -> str:
    path = (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "USER-GUIDE.md"
    )
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_MARKDOWN


class GuidePage(QWidget):
    def __init__(self, parent=None, *, dark: bool = False) -> None:
        super().__init__(parent)
        self.dark = dark
        self._markdown = guide_markdown()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)

        layout.addWidget(
            PageHeader(
                "Guide",
                "Workflows, shortcuts and known limitations. "
                "Ctrl + mouse wheel zooms the text.",
            )
        )

        self.browser = QTextBrowser()
        self.browser.setObjectName("GuideBrowser")
        self.browser.setOpenExternalLinks(True)
        self.browser.setLineWrapMode(
            QTextBrowser.LineWrapMode.WidgetWidth
        )
        # Wrapped text and wide tables never need sideways scrolling; a
        # horizontal scrollbar otherwise appears after Ctrl+wheel zoom and
        # stays visible after zooming back out.
        self.browser.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        layout.addWidget(self.browser, 1)
        self._render()

    def _render(self) -> None:
        tokens = DARK if self.dark else LIGHT
        self.browser.document().setDefaultStyleSheet(_guide_css(tokens))
        self.browser.setHtml(render_markdown(self._markdown))

    def set_theme(self, dark: bool) -> None:
        if dark == self.dark:
            return
        self.dark = dark
        self._render()
