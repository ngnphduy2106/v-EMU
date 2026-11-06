# Daemon installation notes

For a source-tree install, use the top-level `./setup.sh` (see README). It
creates `.venv`, installs Python dependencies, links the CLI tools and can
install the user desktop entry with the absolute venv path. Use
`./setup.sh --daemon` to install a daemon-only copy under `/usr/local` and a
`vemud.service` unit. Running a systemd service directly from a venv inside
`/home` fails under SELinux with `203/EXEC` (`init_t` cannot execute
`user_home_t`), which is why the daemon copy uses system Python. The manual
system daemon steps below remain the reference for a permanent host daemon.

The daemon runs as root because qcow2 chain operations, `qemu-nbd`, libvirt
domain definitions and SELinux repair need privileged access. Access to the
Unix socket is restricted to the `vemu` group.

## Step-by-step installer wrapper

`packaging/vemu-installer.sh` is the user-facing entrypoint used by the
release `.run` package. It does not duplicate installation logic: it shows
welcome/license/precheck screens, calls `setup.sh --precheck` and
`setup.sh --standard`, verifies the service units, and recommends a reboot.

It prefers a single-window GTK3 UI (`vemu-installer-gtk.py`) when
PyGObject/GTK3 is importable and a desktop is available, then falls back to
`zenity`, `kdialog`, or terminal prompts. The GTK UI uses the v-EMU dark
design tokens and keeps the copy minimal. PySide6 is not required before
installation. `VEMU_INSTALLER_NO_GTK=1` forces the fallback path; `--full` is
available for advanced users but is not the default, and missing TCG packages
are not treated as an installation failure. A packaged `.run` must include
`setup.sh`, `LICENSE`, `src/`, `firmware/`, `packaging/` and the installer
wrapper.

Public build, clean-host test and release steps are in [`../docs/RELEASING.md`](../docs/RELEASING.md).

`packaging/build-run.sh [version] [output-dir]` builds a self-extracting
`vemu-<version>-x86_64.run` from a clean Git tree. It excludes development
notes and tests, and writes a `.sha256` next to the artifact. The `.run`
extracts to `~/.local/share/vemu/source` (or `VEMU_SOURCE_DIR`) so the editable
install and in-app guide remain valid after the wrapper exits. Build artifacts belong in `dist/` or a GitHub Release, not in the
source tree.

## Manual install (development)

```bash
sudo install -Dm644 packaging/vemu.sysusers /usr/lib/sysusers.d/vemu.conf
sudo systemd-sysusers
sudo install -Dm644 packaging/vemud.service /etc/systemd/system/vemud.service
sudo install -Dm644 packaging/org.vemu.policy /usr/share/polkit-1/actions/org.vemu.policy
sudo systemctl daemon-reload
sudo systemctl enable --now libvirtd virtnetworkd virtstoraged virtsecretd
sudo systemctl enable --now vemud
sudo usermod -aG vemu "$USER"
# log out and back in, then:
vemu daemon ping
vemu daemon status
```

Windows unattended installs also need `xorriso` (or `genisoimage`),
`dosfstools`, and `mtools` on the host. `virtio-win.iso` is optional and is
used to install qemu-guest-agent in the guest. Host/guest VirtioFS shares need
`virtiofsd` (`sudo dnf install virtiofsd`).

Cross-architecture guests (optional, TCG):

```bash
sudo dnf install qemu-system-aarch64 qemu-system-riscv \
  edk2-aarch64 edk2-riscv64
sudo systemctl restart libvirtd
```

After this, libvirt exposes `aarch64` and `riscv64` in capabilities and the New
VM wizard shows them. They run under TCG on x86_64, require UEFI firmware and
an architecture-matched ISO. Windows unattended install is x86_64-only.

## Desktop icon (Wayland)

Wayland compositors resolve the taskbar/dock icon through the application's
desktop file and icon theme, not only through the window icon. Install the
per-user files once:

```bash
./packaging/install-desktop-user.sh
```

This installs `org.vemu.VEMU.desktop` and the SVG icon under
`~/.local/share`. It may be necessary to log out and back in (or restart the
shell) for the compositor to refresh its icon cache.

Socket: `/run/vemu/vemud.sock`, mode `0660`, owner `root:vemu`.

Override for development:

```bash
vemu daemon serve --socket /tmp/vemu.sock
VEMU_SOCKET=/tmp/vemu.sock vemu daemon ping
```

## Privilege model

- The daemon is the only process that runs privileged operations.
- Users in the `vemu` group can call the socket.
- Every connection is checked with `SO_PEERCRED`: root, the daemon owner, or a
  member of the socket group (primary or supplementary) is allowed. Other
  peers receive `PermissionError` and are disconnected.
- `--polkit` enables `pkcheck` for mutating methods. The installed policy
  allows active local sessions; inactive/remote sessions need authentication.
  It is off by default because the socket group is already a strong gate.
- The CLI currently still has a direct path for development. Production
  packaging routes commands through the daemon.
