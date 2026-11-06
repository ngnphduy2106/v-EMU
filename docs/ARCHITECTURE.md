# v-EMU architecture (0.1.0)

This document describes the implemented v0.1.0 design. It is a maintenance
reference, not a feature wish list. Items that were removed or are deliberately
out of scope are listed at the end.

## 1. Process model

```text
vemu / vemu-ui / API clients
          │  JSON over a Unix socket
          ▼
        vemud  (root, systemd service)
          │
          ▼
      service.py
          │
          ▼
   LibvirtBackend  ── libvirt ── QEMU/KVM
          │
          ├── qemu-img / qemu-nbd / virt-customize / xorriso
          ├── storage registry and chain metadata
          ├── libvirt networks and nftables forwarding
          └── guest agent / virtiofs helpers
```

- `vemu`, `vemu-ui` and lab tools are thin clients. They do not mutate VM
  storage directly when the daemon is available.
- `vemud` runs as root and owns privileged operations: libvirt XML, qcow2
  chains, ownership/SELinux labels, network backends and guest provisioning.
- `--direct` / `--no-system` modes exist for development and tests; they use
  `LibvirtBackend` in-process and have the same storage rules.
- One UI instance runs per user. A second `vemu ui` sends `show` to the
  existing instance through a per-user `QLocalServer` socket and exits.

Socket: `/run/vemu/vemud.sock` (mode `0660`, owner `root:vemu`).

## 2. Core modules

| Module | Responsibility |
|---|---|
| `cli.py` | Command surface (`create`, `clone`, `lab`, `config`, `doctor`, etc.) |
| `client.py` | JSON-RPC Unix-socket client used by CLI/UI |
| `daemon.py` | Root service, RPC dispatch, event monitor, reboot auto-eject |
| `service.py` | Transactional operations shared by CLI, UI and daemon |
| `backend.py` | libvirt/QEMU adapter; domain lifecycle, media, stats, snapshots |
| `vmconfig.py` | Domain XML construction/mutation and feature parsing |
| `chain.py` | qcow2 v2 chain metadata, overlays, verification and garbage |
| `storage.py` | Registered storage roles, ownership, labels and repair |
| `network.py` | Network profiles, NAT/isolated/bridge definitions and nft |
| `hostbridge.py` | Host LAN bridge creation/rollback through NetworkManager |
| `template.py` | Template metadata, sealing and link-clone inheritance |
| `lab.py` | `lab.yaml` loader and up/down/status/reset/destroy orchestration |
| `cloudinit.py`, `identity.py` | NoCloud seed and Linux clone identity reset |
| `provision.py` | Windows unattend/OOBE media and Linux cloud-init presets |
| `ova.py` | OVA/OVF import planning and extraction |
| `guest_clipboard.py` | Windows WTS/PowerShell and Linux Wayland clipboard bridge |
| `guest_virtiofs.py`, `shares.py` | VirtioFS catalog, attach/detach and guest mount |
| `performance.py` | Offline Linux guest performance/sleep tuning precheck/apply |
| `drift.py` | XML baseline and external-change detection |
| `diagnose.py` | Host prerequisite checks used by `vemu doctor` |
| `ui/` | PySide6 desktop shell, dialogs, console and first-run wizard |

## 3. VM lifecycle

1. `vm_create_from_iso` validates ISO/destination/network and builds a
   `DomainSpec` from `vmconfig.py`.
2. Firmware is VGA-safe by default for x86_64; Windows Server 2025 and Windows
   11 presets supply Secure Boot/TPM defaults where applicable.
3. A qcow2 chain is created in the destination; the installer ISO and optional
   Windows answer media are attached.
4. `LibvirtBackend.define()` defines the domain; `start()` applies safe XML
   updates:
   - ensure the noVNC websocket exists;
   - disable guest suspend where configured;
   - promote the installed disk when it already contains an OS;
   - eject installer media when starting an installed VM;
   - apply TCG/NVMe compatibility updates.
5. Stops use libvirt ACPI shutdown; force-off uses `destroy`. The UI runs long
   actions in worker threads and calls `wait=False` for requests where
   appropriate.
6. Snapshots are offline qcow2 layers. A revert plus a new snapshot creates a
   branch; the UI shows the tree and active layer.

## 4. Storage model

```text
labs/       VM disks and per-VM chain directories
templates/  sealed bases, read-only
isos/       installer and driver images
```

- Every managed VM has `chain.json` (v2) with disk/layer metadata.
- Linked clones create qcow2 overlays; template bases are never copied.
- Deletion only removes files referenced by v-EMU metadata. Unreferenced files
  are handled by Storage → Clean garbage / `vemu gc`.
- Ownership, mode, SELinux labels and AppArmor paths are applied through the
  daemon; `Storage → Fix labels and access` can repair them.
- Registry: `/etc/vemu/storage.json`. Runtime data: `/var/lib/vemu/`.

## 5. Networking

- NAT, isolated and LAN-bridge profiles are stored in
  `/etc/vemu/networks.json` and defined in libvirt.
- NAT uses libvirt DHCP/DNS plus nftables forwarding rules.
- Isolated profiles can provide DHCP/DNS without uplink access.
- Bridge profiles use an existing host bridge or create one through
  NetworkManager with a backup/rollback path.
- Port forwards require NAT and a guest IP inside the subnet. The UI warns when
  there is no lease/reservation.
- A VM can have multiple NICs; models are selected by the preset and editable
  in Hardware.

## 6. Console

- Each VM console is a Qt window containing a loopback-only HTTP server and a
  QWebEngine noVNC client connected to QEMU's VNC websocket.
- The native Qt toolbar starts hidden (`F9` / `Ctrl+Shift+T`).
- Console hotkeys: `Ctrl+Alt+Insert` (Ctrl+Alt+Del), `Ctrl+Alt+F` (fullscreen),
  `Ctrl+Alt+Print` (screenshot), `Ctrl+Alt+V` (paste host clipboard).
- The console Power menu deliberately exposes only **Start** and **Force off**;
  ACPI shutdown/reboot is performed from the VM context menu or inside the
  guest. This avoids hangs before the guest agent is available.
- Clipboard is optional:
  - Windows: QGA + active user session + WTS/PowerShell helper;
  - Linux: Wayland + `wl-clipboard` + QGA `guest-exec`;
  - QEMU VNC clipboard has no guest-OS peer and is not a substitute.
- When a guest blocks `guest-exec`, clipboard sync is unavailable; an attached
  VirtioFS share is the supported two-way file-exchange path.
- Dialogs are non-modal; the main window, network map and console can stay open
  together. Destructive confirmations (`ConfirmDialog` and destructive
  `QMessageBox` prompts) remain modal.

## 7. Templates, labs and provisioning

- **Seal**: records template metadata and optionally validates a manual
  Sysprep; automatic Sysprep is not performed.
- **Clone**: creates linked overlays and inherits OS family, firmware, TPM and
  video metadata without copying NVRAM/TPM state.
- **lab.yaml**: declarative networks/VMs; `vemu up|status|down`, reset and
  destroy are orchestrated by `lab.py`.
- **Cloud-init**: NoCloud seed for Linux clones (user, SSH key, packages,
  static network).
- **Windows unattend**: Windows Server 2025 only; builds a small FAT answer
  media. Other Windows images use the interactive installer.
- **Import/adopt/migrate**: OVA/qcow2 import, read-only/partial adopt, full
  chain migration with preflight and XML backup.

## 8. Security and trust boundaries

- `vemud` is the privileged trust boundary. It uses polkit for client
  authorization and a `vemu` group-restricted socket.
- The daemon does not open a TCP listener. VNC/noVNC is loopback-only.
- External/read-only domains are never mutated by v-EMU.
- XML drift baselines live under `/var/lib/vemu/baselines`.
- Firmware artifacts are verified against SHA256SUMS; x86_64 Secure Boot is
  enabled by default for new UEFI VMs.
- User-provided paths are validated against allowed storage roots and symlink
  escapes in `pathpolicy.py`.

## 9. Explicitly removed / out of scope

These are product decisions, not missing implementation details:

- no remote thick client, multi-host/cluster, HA or live migration;
- no web UI or multi-user server mode;
- no 3D acceleration, GPU passthrough, VirtGL or macOS guests;
- no live snapshots; snapshots are offline and chain-based;
- no X11 guest clipboard support;
- no VNC clipboard as a guest clipboard peer;
- no automatic Sysprep or automatic post-creation video-model changes;
- QXL is a manual compatibility option, not the default;
- no general-purpose replacement for Proxmox/oVirt/Incus.

## 10. Tests and evidence

Unit tests cover service, chain, network, storage, import, migration, UI
dialogs, console and CLI behavior. Historical host evidence and release
investigations are maintained separately from this document; they may describe
removed features and are not part of the current contract.
