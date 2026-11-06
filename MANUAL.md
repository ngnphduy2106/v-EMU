# v-EMU Manual

> Lab-first desktop virtualization on QEMU/KVM: local, single-user, and built
> around reusable templates, linked clones and declarative labs.
>
> This manual is the complete user-facing reference. The **Guide** page inside
> v-EMU is a concise self-contained reference; this file adds installation,
> host support, security and troubleshooting. It does not require network
> access after the repository is cloned.

## 0. Install and host support

### Requirements

- 64-bit Linux with KVM enabled: Intel VT-x (`vmx`) or AMD-V (`svm`) and
  `/dev/kvm`. v-EMU refuses to install without working KVM.
- Python 3.11+; the installer creates a local venv.
- `sudo` for host packages, the daemon, groups and firmware.
- A Wayland desktop session for the UI (KDE Plasma, GNOME and COSMIC tested).
- Free space for lab disks: at least 20 GiB plus the VM images you plan to use.

### Install from source

```bash
git clone https://github.com/withcoresystem/v-EMU vEMU
cd vEMU
./setup.sh --precheck
./setup.sh --standard
```

`--precheck` is read-only. `--standard` installs x86_64 host packages, v-EMU,
`vemud`, the desktop entry and x64 firmware. `--full` additionally installs
optional aarch64/riscv64 TCG packages where the distribution provides them.
Release `.run` installers (`vemu-<version>-x86_64.run`) wrap the same logic;
verify the adjacent `.sha256` before running one. The installer keeps the
extracted source it runs from in `~/.local/share/vemu/source` (override with
`VEMU_SOURCE_DIR`); `--uninstall` removes it. Build and release steps are in
[`docs/RELEASING.md`](docs/RELEASING.md).

After the first install, log out and back in so the new `vemu`, `libvirt` and
`kvm` group membership applies.

### Upgrade and uninstall

- Upgrade by re-running `./setup.sh --standard` (or a newer `.run`); packages,
  the venv and the service are reused, and VMs/templates are untouched.
- `./setup.sh --uninstall` removes the service, package, firmware, venv and
  desktop entry. VM disks, templates and `/etc/vemu` are kept.
- `--uninstall --purge` also removes `/etc/vemu`, user settings and logs.

### Host support

| Tier | Hosts |
|---|---|
| Official | Fedora 44; Rocky Linux 10.2; RHEL 10.2 (`--standard` + GNOME UI); openSUSE Leap 16.1; Tumbleweed snapshot 20261028; Debian 13 |
| Best effort | Debian 12 |
| As-is | Ubuntu/Kubuntu 26.04, Slowroll, Fedora 45+, SLES/SLED, older RHEL/Rocky/Alma, Arch/Kali and unlisted distributions |

Host support only covers installing/running v-EMU; guest OS support is wider.
The host must provide KVM. Cross-architecture TCG is optional and slow.

### First checks

```bash
vemu --version
vemu doctor
vemu storage list
vemu network list
```

Register a lab location and a NAT network before creating a VM:

```bash
vemu storage add --path /srv/vemu/labs --role labs
vemu storage add --path /srv/vemu/templates --role templates
vemu storage add --path /srv/vemu/isos --role isos
vemu network add --name lab-nat --mode nat
vemu network apply
```

The same operations are available under **Storage** and **Networks**. Use
`vemu doctor` (or the Doctor page) if storage ownership/labels need repair.

---

## 1. What v-EMU is for

```text
        ISO / OVA / qcow2
                │
                ▼
   ┌─────────────────────────┐
   │  VM (one lab machine)   │
   └────────────┬────────────┘
                │ Seal
                ▼
   ┌─────────────────────────┐
   │ Template (immutable)    │
   └────────────┬────────────┘
                │ Clone (linked overlay)
                ▼
   ┌─────────────────────────┐
   │ Lab: node1, node2, ...  │
   └─────────────────────────┘
```

v-EMU is a good fit for:

- one Linux host, one user, local VMs;
- repeatable training/study labs;
- Windows/Linux templates with linked clones;
- `lab.yaml` labs that can be brought up or destroyed together;
- NAT, isolated and LAN-bridge networks.

It is not a cloud, cluster, multi-tenant platform or remote image registry.
Battle-tested VM start/stop/delete behaviour is deliberately not re-documented
here; use the buttons or `vemu start|shutdown|delete`.

## 2. Quick start

```text
setup.sh ──► first-run wizard ──► storage + NAT ──► New VM ──► console
```

| Step | Command / UI |
|---|---|
| Check KVM | `./setup.sh --precheck` |
| Install x86_64 host | `./setup.sh --standard` |
| Add ARM/RISC-V TCG | `./setup.sh --full` |
| Open the app | `vemu ui` or the desktop entry |
| First run | create a `labs` storage role and a NAT network |
| First VM | **Virtual machines → New VM** |

Only one v-EMU UI runs per user; launching it again raises the existing window.
Most dialogs are non-modal, so the main window, Network map and consoles can
stay open together; destructive confirmations stay modal.

The New VM wizard has four pages: **name/OS**, **hardware**, **storage/network**
and **summary**. It remembers choices when going back. The ISO name fills the
OS preset; every field remains editable.

Useful defaults:

- UEFI x86_64: **Secure Boot on**; uncheck it for unsigned ISOs.
- Linux: UEFI, virtio disk/NIC, 64 GB disk.
- Windows 11: UEFI + TPM 2.0; Windows Server presets use BIOS.
- **Driver ISO** is Windows-only, starts empty and accepts any ISO path; leave
  it blank to skip the driver CD.
- Only Windows Server 2025 has an unattended answer file.
- TCG (ARM/RISC-V on an x86_64 host) is opt-in and slow; see
  **Support, versions and limits**.

Minimum CLI example:

```bash
vemu create --name lab-1 --iso /home/ISO/linux.iso \
  --dest /home/VM/vemu-labs --os linux --start
vemu console lab-1
```

**Everyday controls:** start/shutdown/force-off, console, snapshots, hardware,
networks, USB and delete are in the VM table and context menu. Deleting a VM
asks whether to remove its managed disks; snapshots and running state are
handled by the normal libvirt rules. A paused or suspended guest still exposes
**Console**; opening it wakes the guest first (ACPI wake for a suspended guest,
resume for a paused one). If a wake fails, v-EMU reports why before showing the
console.

New x86_64 VMs created by the wizard use VGA video by default. VGA works in
every installer and resumes reliably; it is the safe lifecycle choice, but the
console is slower than QXL or virtio-gpu. Clones inherit their template's video
model. vEMU does not change the video model automatically after creation; use
the Hardware editor when you want QXL or virtio-gpu. ACPI S3 remains disabled
for managed x86 VMs, so Windows does not enter S3 sleep. For Windows QXL, Linux
virtio-gpu with guest s2idle disabled, and manual video choices, see
[`GRAPHICS.md`](GRAPHICS.md).

## 3. The v-EMU model

```text
Storage roles                 Network profiles
labs      ── VM disks         NAT       ── outbound internet
templates ── base images      isolated  ── lab-only segment
isos      ── installers       bridge    ── LAN reachability

VM ── current disk chain
 └─ base + overlay layers (v2 chain)
Template ── immutable base
Lab ── lab.yaml + N clones + networks
```

| Object | Meaning | Main screen |
|---|---|---|
| VM | One running/stopped libvirt domain | Virtual machines |
| Template | Immutable base for linked clones | Templates |
| Clone | Small overlay on a template base | Templates / Clone |
| Lab | Declarative group of VMs and networks | Labs |
| Storage | Registered `labs`, `templates` or `isos` roots | Storage |
| Network profile | NAT/isolated/bridge switch definition | Networks |

## 4. Lab-first workflows

### 4.1 Seal → clone

```text
Windows: run Sysprep inside guest ──► shut down ──► Seal
Linux:   shut down ───────────────────────────────► Seal
                                                        │
                                        ┌───────────────┴───────────────┐
                                        ▼                               ▼
                                  linked clone 1                  linked clone N
```

Windows must be prepared manually:

```text
C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown
```

Then use **Seal as template**. v-EMU does not run Sysprep. Sealing without it
is allowed but clones keep the same SID/machine identity. Windows templates
created by the wizard use the local administrator password `Vemu!Lab2026` by
default; change it for anything beyond a disposable lab. Windows seals require
the BitLocker/FDE confirmation: v-EMU does not copy TPM state; keep recovery
keys or disable/suspend BitLocker before sealing.

```bash
vemu seal --domain win-lab-1 --dest /home/VM/vemu-templates \
  --name win2025-base --os-family windows --sysprepped --accept-bitlocker

vemu clone --template /home/VM/vemu-templates/win2025-base \
  --dest /home/VM/vemu-labs --count 3 --name 'win-{{n}}'
```

Clone behaviour:

| Template | Clone gets |
|---|---|
| Linux | offline hostname/machine-id/SSH-key reset, or cloud-init seed |
| Windows + Sysprep | optional per-clone OOBE answer USB |
| Any TPM template | fresh TPM state and fresh NVRAM |
| All | linked overlay; base stays immutable |

Cloud-init Linux example:

```bash
vemu clone --template /home/VM/vemu-templates/fedora-cloud \
  --dest /home/VM/vemu-labs --count 3 --name 'node-{{n}}' \
  --network nat --cloud-init --ci-user lab \
  --ci-ssh-key ~/.ssh/id_ed25519.pub --ci-package vim
```

Immutable images (CoreOS, Silverblue, MicroOS, Flatcar, NixOS, Ubuntu Core)
skip offline identity reset; use Ignition/cloud-init for first boot.

### 4.2 `lab.yaml`

`lab.yaml` is the shareable unit. Make one, then bring the whole lab up/down:

```yaml
version: 1
lab: rhcsa-practice
dest: /home/VM/vemu-labs/rhcsa-practice
templates_root: /home/VM/vemu-templates

networks:
  mgmt: { mode: nat, subnet: auto }
  server: { mode: isolated, subnet: 192.168.50.0/24 }

vms:
  node1:
    template: fedora-cloud
    cpus: 2
    memory: 2G
    networks: [mgmt, server]
    ip: { server: 192.168.50.11 }
    cloud_init:
      user: lab
      ssh_key: ~/.ssh/id_ed25519.pub
      packages: [vim, tcpdump]
  node2:
    template: fedora-cloud
    cpus: 1
    memory: 1G
    networks: [server]
    ip: { server: 192.168.50.12 }
```

```bash
vemu init lab.yaml                 # starter file
vemu up lab.yaml                   # create networks/VMs, start
vemu status lab.yaml
vemu down lab.yaml                 # graceful shutdown
vemu down lab.yaml --force
vemu lab reset --lab lab.yaml --vm node1 --force
vemu lab destroy --lab lab.yaml --force
```

`up` is idempotent for existing chains/domains. Lab reset recreates selected
VMs from their templates; destroy removes lab VMs/chains/networks but keeps
templates and `lab.yaml`. Both actions ask for confirmation in the UI.

### 4.3 Portable lab bundles

```text
lab.yaml + manifest + (optional templates) ──► .tar.gz ──► another host
```

```bash
vemu lab export --lab lab.yaml --dest ./lab.tar.gz
vemu lab export --lab lab.yaml --dest ./lab-full.tar.gz --with-templates
vemu lab import --bundle ./lab-full.tar.gz --dest /home/VM/lab-import
vemu up /home/VM/lab-import/lab.yaml
```

Without `--with-templates`, only references are included; provide the templates
separately.

### 4.4 Import / adopt / migrate

```text
Already in libvirt or Cockpit?
├─ keep external, inspect only ──► Adopt read-only
├─ manage XML, keep disks ───────► Adopt partial
└─ build a v-EMU chain ──────────► Migrate full

OVA/appliance ────────────────────► Import OVA
qcow2/raw/VMDK ───────────────────► Import disk
```

Use **Import…** on the VM page for OVA/appliance or qcow2/raw/VMDK images, or
adopt/migrate a VM that already exists in libvirt. You can stop at any level:

| Option | Command | XML | Disks | Unlocks |
|---|---|---|---|---|
| External / read-only | none (default) | untouched | untouched | inspect, console, XML backup, rescue |
| Partial | `vemu adopt --domain NAME --mode partial --yes` | vEMU manages | stay in place | start/stop, hardware, ISO/NIC, drift view |
| Full | `vemu migrate --domain NAME --mode full` | vEMU manages | copied into a v-EMU chain | snapshots, clone/seal, chain tools |

Every domain without v-EMU metadata is **External** and read-only: v-EMU does
not change its XML, disks or power state. **Rescue start** is a separate,
confirmed override for a stuck external VM; it does not change the mode.

#### Read-only: look before touching

1. `vemu list` shows all domains, including External ones.
2. Open the VM to inspect configuration and, if it is already running, the
   console. You can back up the XML.
3. Nothing is written. No rollback is needed.

This is the right first step on a production host: confirm the UI, storage
layout and console work before changing anything.

#### Partial: manage the XML, keep the disks

1. Shut the VM down (Partial adoption requires a stopped domain).
2. Back up the guest data and note MAC/IP, host devices and USB passthrough.
3. Adopt it and keep an XML backup in your own directory:

   ```bash
   vemu adopt --domain NAME --mode partial --yes \
     --backup-dir ~/vemu-backups
   ```

4. Start the VM from vEMU and verify network, console and disk behaviour.
   Hardware edits now go through vEMU; external XML edits appear as **drift**.
5. v-EMU snapshots and clones stay unavailable until Full migration.

Rollback: shut the VM down, then redefine the backup XML:

```bash
virsh -c qemu:///system define ~/vemu-backups/NAME/<stamp>-adopt.xml
```

#### Full: move the disks into a v-EMU chain

Full migration copies the current disk state into `labs/<name>/` as a base plus
overlay and a `chain.json`. It repoints the same domain and keeps its UUID,
NVRAM, TPM and Secure Boot loader.

1. Shut the VM down. Back up the guest data and BitLocker/LUKS recovery keys.
2. Run the read-only preflight. It measures every disk, adds 15% headroom and
   stops if the labs location is too small:

   ```bash
   vemu migrate --domain NAME --mode full --dry-run
   ```

3. Run the migration. `--accept-snapshot-loss` is required when libvirt
   snapshots exist; the current state is flattened into the new base.

   ```bash
   vemu migrate --domain NAME --mode full \
     --accept-snapshot-loss --yes --backup-dir ~/vemu-backups --start
   ```

   `--import-snapshots` imports external snapshot chains instead of flattening
   them. Multi-disk VMs are migrated as one set. `--new-firmware` switches to
   v-EMU firmware; by default the existing firmware and variables are kept.

4. Boot the VM, then check disks, NIC/IP, console and guest services. Migration
   changes storage, not the guest identity; Windows still needs Sysprep before
   sealing a template.
5. The old disks and snapshots are **kept**. Remove them manually only after
   the migrated VM has been verified.

Rollback: shut the migrated VM down and redefine its XML backup:

```bash
virsh -c qemu:///system define ~/vemu-backups/NAME/<UTC>.xml
```

The old disks are still intact, so the domain boots as before. The unused v-EMU
chain directory can be removed later with **Storage → Clean garbage**.

#### Before any migration

- Stop the VM; never migrate a running domain.
- Suspend BitLocker or keep the recovery key, and have LUKS passphrases ready.
- Record MAC addresses, IP reservations, host devices, USB redirection and
  shared disks that live outside the VM definition.
- Full migration needs free space for measured disk size + 15%.
- Use `--dry-run` first, and keep the XML/backup directory until the VM has
  booted and passed your checks.

**XML drift** shows external edits and can re-pin the baseline; v-EMU does not
silently overwrite an externally changed domain.

### 4.5 Snapshots

Snapshots are offline by default and create a new chain layer. The UI shows the
active layer, parent and size; revert/delete only when the VM is stopped.
Multi-disk VMs snapshot every disk as one unit. Live snapshots are not part of
the current scope.

## 5. Networking

```text
             ┌────────────┐
LAN router ──│ host bridge │── VM (reachable from LAN)
             └────────────┘

host ── NAT switch ── VM ──► internet
host ── isolated switch ── VM/VM only
```

| Mode | Host IP | DHCP/DNS | Typical use |
|---|---|---|---|
| NAT | yes | v-EMU/libvirt | internet labs |
| Isolated | optional | v-EMU if enabled | exam/offline labs |
| Bridge | LAN’s DHCP | LAN’s DHCP/DNS | VMs reachable from LAN |

**LAN bridge…** uses an existing host bridge (no host changes) or creates one
on an unused Ethernet port. Creating a bridge briefly interrupts host
networking; v-EMU backs up the old NetworkManager profile and rolls back if
activation fails. Use **Apply** after editing profiles. **Restart network** is
blocked while attached VMs run; `--force` drops their connectivity.

Port forwards require NAT and a guest IP inside the subnet; v-EMU warns when
the guest has no lease/reservation. `vemu network leases --network <profile>`
lists both.

**Console:** each VM opens its own noVNC window (graphics or serial). The
inspector mini console is a screenshot preview. Clipboard sync is optional;
see **Clipboard (host ↔ guest)** below for the guest requirements.
USB passthrough and boot-to-firmware/BIOS menu are in the VM context menu.
The native toolbar starts hidden; press `F9` or `Ctrl+Shift+T` to show it.
Console shortcuts: `Ctrl+Alt+Insert` sends Ctrl+Alt+Del, `Ctrl+Alt+F` toggles
fullscreen, `Ctrl+Alt+Print` saves a screenshot, and `Ctrl+Alt+V` pastes the
host clipboard into the guest. The console **Power** menu keeps only **Start**
and **Force off**; use the VM context menu for ACPI shutdown/reboot, or shut
down inside the guest.

#### Keyboard shortcuts

| Area | Shortcut | Action |
|---|---|---|
| App | `Ctrl+1` … `Ctrl+6` | Labs, Templates, Virtual machines, Networks, Storage, Shared folders |
| App | `Ctrl+7` / `Ctrl+8` | Guide / Doctor |
| App | `Ctrl+,` | Settings |
| App | `Ctrl+N` | Add item on the current page |
| App | `Ctrl+R` | Refresh the current page |
| App | `Ctrl+B` / `Ctrl+I` | Toggle sidebar / inspector |
| App | `Ctrl+Shift+M` | Network map |
| App | `Ctrl+Shift+K` | CLI console |
| App | `F1` | Help for the current page |
| Console | `F9` or `Ctrl+Shift+T` | Show/hide the toolbar |
| Console | `Ctrl+Alt+Insert` | Send Ctrl+Alt+Del |
| Console | `Ctrl+Alt+F` | Toggle fullscreen |
| Console | `Ctrl+Alt+Print` | Save a screenshot |
| Console | `Ctrl+Alt+V` | Paste the host clipboard |
| Console | `Esc` | Leave fullscreen |
| Serial | `Ctrl+Shift+C` / `Ctrl+Shift+V` | Copy / paste |
| Serial | `Ctrl+Insert` / `Shift+Insert` | Copy / paste |

#### Clipboard (host ↔ guest)

vEMU provides the channel; the guest must provide the clipboard helper. vEMU
does not install guest packages or relax the guest-agent policy. No host-side
clipboard utility (`wl-clipboard`, `xclip`, etc.) is required; Qt handles the
host clipboard.

| Guest | Requirements |
|---|---|
| Windows | QEMU guest agent running, an interactive user logged in, and the agent allowing `guest-get-osinfo`, `guest-get-users`, `guest-exec`, `guest-exec-status` and `guest-file-*`. The virtio-win defaults normally allow these; a hardened agent policy must permit them. Clipboard is unavailable at the sign-in screen. |
| Linux | Wayland desktop session (X11 is not supported), a logged-in user, `wl-clipboard` (`wl-copy` and `wl-paste`) installed, and the agent allowing `guest-get-osinfo`, `guest-exec` and `guest-exec-status` (`guest-get-users` is used when available). |

Use **View → Clipboard sync (guest ↔ host)** for automatic sync, or
`Ctrl+Alt+V` to send the host clipboard on demand.

##### Tested Linux guests (2026-11-07)

Out of the box, none of the tested Linux guests had all three requirements
met. This is guest configuration, not a vEMU switch; vEMU deliberately does not
change guest packages, the desktop session or the agent policy.

| Guest | Agent policy | `wl-clipboard` | Session in test | Result |
|---|---|---|---|---|
| Fedora 45 Workstation | `QEMU_GA_ARGS` blocks `guest-exec` | installed | Wayland | Blocked by policy |
| AlmaLinux 10.2 | `FILTER_RPC_ARGS` allowlist excludes `guest-exec` | missing | Wayland | Blocked by policy and missing tool |
| Debian 13 | `guest-exec` allowed | missing | X11 | Needs tool and a Wayland session |
| openSUSE Leap 16.1 | `FILTER_RPC_ARGS` blocks `guest-exec` | missing | X11 | Blocked by policy and missing tool |
| SLES 16 | `guest-exec` allowed | missing | no user Wayland session | Needs tool and a graphical login |

To make clipboard work, a guest administrator must do all of the following:

1. allow `guest-exec` and `guest-exec-status` in the guest agent policy
   (`/etc/sysconfig/qemu-ga` on Fedora/Alma/RHEL; `/usr/etc/sysconfig/qemu-ga`
   or `/etc/sysconfig/qemu-ga` on SUSE) and restart `qemu-guest-agent`;
2. install `wl-clipboard` (`sudo dnf install wl-clipboard`,
   `sudo apt install wl-clipboard`, or `sudo zypper install wl-clipboard`);
3. log in to a **Wayland** desktop session; X11 is not supported.

If any of these cannot be changed, attach a **VirtioFS** share instead; it
provides two-way file exchange without the guest agent (see below).

Fedora 45 ships `qemu-guest-agent` with an explicit block list. On a Fedora 45
Workstation guest, `/etc/sysconfig/qemu-ga` contains:

```text
QEMU_GA_ARGS=--block-rpcs=guest-file-open,guest-file-close,guest-file-read,guest-file-write,guest-file-seek,guest-file-flush,guest-exec,guest-exec-status
```

While that line is active, clipboard sync cannot work and vEMU reports that the
guest blocks `guest-exec`. This is a guest package default, not a vEMU
limitation. RHEL/Alma/Rocky and some SUSE images use a similar policy through
`FILTER_RPC_ARGS`.

To enable clipboard, a guest administrator must allow `guest-exec` and
`guest-exec-status`: remove those two entries from `QEMU_GA_ARGS` (or comment
out the whole line), then restart the agent:

```bash
sudo systemctl restart qemu-guest-agent
```

Enabling `guest-exec` increases the guest agent's command surface; make that
change only if you accept the trade-off. If the guest is at the sign-in screen,
log in first; vEMU reports the missing session and retries after login.

Verified on Fedora 45 Workstation (2026-11-07): with `guest-exec` blocked,
vEMU reports the policy error; after allowing it, host → guest and guest → host
both worked through `wl-copy`/`wl-paste`. When the policy cannot be changed,
use an attached **VirtioFS** share for two-way file exchange instead (see
below); it does not depend on the guest agent.

| Symptom | What to check |
|---|---|
| “No active Windows user session” | Log in to the Windows guest; clipboard is unavailable at the sign-in screen. |
| “guest blocks guest-exec” | Add `guest-exec` and `guest-exec-status` to the guest QGA policy and restart `qemu-guest-agent`. |
| “Install wl-clipboard” | Install `wl-clipboard` in the guest. |
| “requires an active Wayland session” | Log in to a Wayland desktop session; X11 is unsupported. |
| Nothing arrives and no error appears | Confirm the guest helper and agent policy; the VNC channel alone has no guest clipboard peer. |
| Guest blocks `guest-exec` | Clipboard is unavailable by design; attach a VirtioFS share for two-way file exchange. |

## 6. Storage and shared folders

```text
labs/           VM disks + chains
templates/      immutable bases
isos/           installers and virtio-win
```

Storage → **Add location** registers a path and role. **Fix labels and access**
repairs ownership/SELinux; **Manage files…** copies ISOs and deletes only
unreferenced files. **Clean garbage…** removes orphan chain directories after
a VM delete. Templates are read-only from Storage; manage them in Templates.
Deletion is reference-checked; unreferenced files are removed only through
Manage files or Clean garbage.

```text
Shared folders (catalog)
        │ attach to one VM
        ▼
VirtioFS share ──► /mnt/vemu/<tag> (Linux)
                └─► Z: (Windows + WinFsp/viofs)
```

The catalog never mounts anything globally. Attach per VM, then fully stop and
start the VM. Windows needs WinFsp and the `viofs` driver inside the guest;
Linux needs `virtiofsd` on the host and a guest with virtiofs support. Sealing a
template removes shares; clones do not inherit them.

### Mounting a VirtioFS share in a Linux guest

vEMU attaches the device at the VM level; the guest mounts it by **tag**. When
the QEMU guest agent is available and allows `guest-exec`, vEMU creates the
mountpoint and mounts it automatically at `/mnt/vemu/<tag>`.

If the agent is missing or blocks `guest-exec`, mount it manually inside the
guest. `<tag>` is the **Guest tag** value from the share catalog, not the share
name or description; tags are case-sensitive:

```bash
sudo mkdir -p /mnt/share
sudo mount -t virtiofs <tag> /mnt/share
```

For example, a catalog entry with guest tag `share-tag`:

```bash
sudo mkdir -p /mnt/share
sudo mount -t virtiofs share-tag /mnt/share
```

Verify the mount with `findmnt -t virtiofs` or `mount | grep virtiofs`. To make
it persistent, use the tag in `/etc/fstab` after confirming the tag is stable:

```text
share-tag /mnt/share virtiofs defaults,nofail 0 0
```

VirtioFS is read/write unless the catalog entry is marked read-only, so it is a
two-way alternative to clipboard copy/paste when a guest blocks the agent.
Verified with a Fedora 45 guest; other Linux guests need virtiofs kernel support
and the host needs `virtiofsd`.

## 7. Settings

| Setting | Effect |
|---|---|
| Theme | Dark by default on first run; user light/dark choice is remembered |
| UI scale | 100 / 125 / 150 / 200%; restart required |
| Panels | Sidebar, inspector, mini console |
| Refresh | VM status interval, minimum 30 s |
| Resource profile | Standard/High/Ultra CPU/IO weights; best-effort, not a quota |
| Boot order | Installed disk before installer media; auto-eject after reboot |
| Cross-architecture | Enables ARM/RISC-V TCG; off by default |

Scale guidance for the 1280×720 logical minimum:

- 100%: desktop FullHD or smaller screens;
- 125%: FullHD laptop, needs at least 1600×900;
- 150%: 2K+;
- 200%: 4K.

`VEMU_UI_SCALE` overrides the saved value for tests/CI. Changing theme or scale
never changes VM definitions.

## 8. Day-2 and troubleshooting

| Symptom | First check |
|---|---|
| VM will not boot | `vemu doctor`; firmware/ISO boot order; Secure Boot |
| No network | profile **Apply**; NAT bridge; guest IP/lease |
| Low disk space | Storage → Manage files/Clean garbage; `vemu gc` |
| Console black | try graphics/serial; guest GPU driver; `VEMU_WEBENGINE_GPU` |
| RAM stat looks wrong | guest balloon driver; `vemu stats --domain <vm>` |
| Clone identity conflict | Sysprep Windows; cloud-init/offline reset Linux |
| Secure Boot refusal | signed shim/kernel; unsigned ISO needs Secure Boot off |

```bash
vemu doctor
vemu doctor --repair --dry-run
vemu stats --domain lab-1
vemu verify --chain <vm-chain-dir>
vemu network leases --network lab-nat
vemu list
```

Known limits: TCG is slow; live snapshots and 3D/GL are not planned for
0.1/0.2; Windows 3D on virtio-gpu is not supported; Linux clipboard needs
Wayland, `wl-clipboard` and QGA `guest-exec` (X11 is out of scope); 125% does
not fit a 1440×900 display; v-EMU is single-host/local-only. Use one display
and Linux workspaces rather than multi-monitor.

## 9. CLI cheat sheet

| Task | Command |
|---|---|
| Create | `vemu create --name lab-1 --iso <iso> --dest <labs> --os linux` |
| List | `vemu list` |
| Start/stop | `vemu start lab-1` · `vemu shutdown lab-1` |
| Console | `vemu console lab-1` · `vemu console --serial lab-1` |
| Stats | `vemu stats --domain lab-1` |
| Seal/clone | `vemu seal ...` · `vemu clone ...` |
| Template import | `vemu template import --from <qcow2> --dest <templates>` |
| Lab | `vemu up/status/down lab.yaml` |
| Lab bundle | `vemu lab export/import ...` |
| Network | `vemu network list` · `vemu network leases --network <profile>` |
| Storage | `vemu storage list` · `vemu storage inspect --path <path>` |
| Health | `vemu doctor` |
| Garbage | `vemu gc <labs>` |

## 10. Security and trust boundaries

- `vemud` runs as root and owns libvirt, storage, ownership, labels and network
  reconciliation. The UI reaches it through a local Unix socket restricted to
  the `vemu` group. Do not run the whole UI as root.
- VM isolation is provided by QEMU/KVM/libvirt. v-EMU is not a sandbox against
  root or other host tools: `virsh`, Cockpit or virt-manager can still modify
  the same domains.
- **External/read-only** prevents v-EMU from mutating a domain; it does not
  prevent other tools from doing so.
- Storage deletion is conservative. Only unreferenced files and orphan chain
  directories can be deleted through Storage; templates and chain bases are
  protected by reference checks.
- Secure Boot: new x64 UEFI VMs use v-EMU firmware with CA 2011 + CA 2023.
  Unsigned ISOs need Secure Boot disabled. The firmware itself is not signed;
  it is platform firmware, not a guest binary.
- Lab defaults (including the documented Windows administrator password) are
  for disposable labs. Change passwords and keys for any environment that
  matters.
- Blueprints and lab files may contain secrets. v-EMU warns before sharing;
  do not publish blueprints containing passwords, tokens or private keys.
- Report vulnerabilities through the private process in `SECURITY.md`; do not
  open a public issue.
- v-EMU is provided **as is**, without warranty. You are responsible for
  backups, guest licences and data; see [`DISCLAIMER.md`](DISCLAIMER.md).

## 11. Support, versions and limits

- The project is maintainer-led; there is no SLA. Issues on **as-is** hosts may
  be closed as out of scope.
- Releases are source-first, with an optional `.run` installer built from the
  same tag. The latest tagged release and `main` are the supported versions.
- The in-app **Guide** is concise and self-contained. Use this manual for
  install, technical and troubleshooting details.
- Limits: single host, one user, no cluster/HA/live migration, no web console,
  no 3D/GPU passthrough, no macOS guests, offline snapshots only, no container
  orchestration.
- Wayland is the supported session; X11 clipboard/integration is out of scope.
- The UI targets one display; use Linux workspaces instead of multi-monitor.
- UI scale 125% is supported on FullHD; the 1280×720 logical minimum means 125%
  does not fit a 1440×900 display.
- TCG cross-architecture guests are functional but slow; RISC-V is slower than
  ARM64.
