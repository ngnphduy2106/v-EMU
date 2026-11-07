# v-EMU User Guide

> Lab-first desktop virtualization on QEMU/KVM: local, single-user, and built
> around reusable templates, linked clones and declarative labs.
>
> This in-app guide covers the app and CLI workflows for the current release.
> It is intentionally self-contained and does not fetch anything from the
> network.

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

## 2. First use

If first-run setup was skipped, create a `labs` location under **Storage** and
add or apply a network under **Networks** first.

| Step | Where |
|---|---|
| Lab storage | **Storage → Add location** (role: labs) |
| Network | **Networks** → add/apply a profile |
| First VM | **Virtual machines → New VM** |
| Console | Select the VM → **Console** |

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
- TCG (ARM/RISC-V on an x86_64 host) is opt-in in Settings and slow.

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
**Hardware → Video** when you want QXL or virtio-gpu. ACPI S3 remains disabled
for managed x86 VMs, so Windows does not enter S3 sleep.

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
Lab up/down reports group identical warnings per VM and list the affected VMs once.

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

Use **Import…** on the VM page for OVA/disk images, or adopt/migrate a VM that
already exists in libvirt. Choose how far to go, per VM:

| Option | Command | Disks | Unlocks |
|---|---|---|---|
| External / read-only | none (default) | untouched | inspect, console, XML backup |
| Partial | `vemu adopt --domain NAME --mode partial --yes` | stay in place | start/stop, hardware, ISO/NIC |
| Full | `vemu migrate --domain NAME --mode full` | copied to a v-EMU chain | snapshots, clone/seal |

External domains are read-only by default. Partial adoption requires the VM to
be stopped and writes only the XML; roll back with the saved XML if needed.
Full migration keeps the domain UUID, NVRAM, TPM and Secure Boot loader, copies
the disks into `labs/<name>/`, and keeps the old disks. Run
`vemu migrate --domain NAME --mode full --dry-run` first.

Before any migration: shut the VM down, back up guest data and BitLocker/LUKS
keys, note MAC/IP/hostdevs, and keep the XML backup until the VM passes checks.
**XML drift** shows external edits and can re-pin the baseline.

### 4.5 Snapshots

Snapshots are offline by default and create a new chain layer. The UI shows the
active layer, parent and size; revert/delete only when the VM is stopped.
Multi-disk VMs snapshot every disk as one unit. Live snapshots are not part of
the current scope.

## 5. Networking

```text
             ┌─────────────┐
LAN router ──│ host bridge │── VM (reachable from LAN)
             └─────────────┘

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
lists leases and reservations.

## 6. Console and clipboard

Each VM opens its own noVNC window (graphics or serial). The inspector mini
console is a screenshot preview. USB passthrough and the firmware/BIOS menu are
in the VM context menu. If a USB device was unplugged, vEMU marks it **Missing**
so you can detach it; starting a VM removes missing USB entries and shows a
warning instead of blocking the start.

The native toolbar starts hidden so the guest fills the window; press `F9` or
`Ctrl+Shift+T` to show it. The console **Power** menu keeps only **Start** and
**Force off**; use the VM context menu for ACPI shutdown/reboot, or shut down
inside the guest.

**Clipboard requirements:** optional; vEMU does not install guest packages or
change the guest-agent policy.

- **Windows:** QEMU guest agent running, an interactive user logged in, and
  the agent allowing `guest-get-osinfo`, `guest-get-users`, `guest-exec`,
  `guest-exec-status` and `guest-file-*`. Clipboard is unavailable at the
  sign-in screen.
- **Linux:** Wayland desktop session (X11 is not supported), a logged-in user,
  `wl-clipboard` installed, and the agent allowing `guest-get-osinfo`,
  `guest-exec` and `guest-exec-status` (`guest-get-users` is used when
  available). RHEL/Alma/Rocky, some SUSE images and Fedora 45 block
  `guest-exec` through `QEMU_GA_ARGS`/`FILTER_RPC_ARGS`; allow it and restart
  `qemu-guest-agent`.

Tested guests (2026-11-07), out of the box — every one needed a guest-side
change:

| Guest | Missing requirement |
|---|---|
| Fedora 45 | `guest-exec` blocked; Wayland and `wl-clipboard` OK |
| AlmaLinux 10.2 | `guest-exec` blocked; `wl-clipboard` missing |
| Debian 13 | `wl-clipboard` missing; X11 session |
| openSUSE Leap 16.1 | `guest-exec` blocked; `wl-clipboard` missing; X11 session |
| SLES 16 | `wl-clipboard` missing; no user Wayland session |

To fix them, allow `guest-exec`/`guest-exec-status` in the guest agent policy,
install `wl-clipboard`, and log in to Wayland. vEMU does not make these changes.

Use `Ctrl+Alt+V` to send the host clipboard on demand, or **View → Clipboard
sync (guest ↔ host)** for automatic sync.

On Fedora 45, the packaged `/etc/sysconfig/qemu-ga` blocks `guest-exec` and
`guest-file-*` by default. Remove `guest-exec` and `guest-exec-status` from
`QEMU_GA_ARGS` (or comment out the line) and restart the agent:

```bash
sudo systemctl restart qemu-guest-agent
```

Tested on Fedora 45 Workstation: blocked by default; after this change both
directions worked. Enabling `guest-exec` widens the guest agent's command
surface. When the policy cannot be changed, use a VirtioFS share instead; it
needs no guest agent and provides two-way file exchange.

## 7. Keyboard shortcuts

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

## 8. Storage and shared folders

```text
labs/           VM disks + chains
templates/      immutable bases
isos/           installers and virtio-win
```

Storage → **Add location** registers a path and role. **Fix labels and access**
repairs ownership/SELinux; **Manage files…** copies ISOs and deletes only
unreferenced files. **Clean garbage…** removes orphan chain directories after
a VM delete. Templates are read-only from Storage; manage them in Templates.

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

When the QEMU guest agent allows `guest-exec`, vEMU mounts Linux shares
automatically at `/mnt/vemu/<tag>`. If the guest blocks `guest-exec` or has no
agent, mount the **Guest tag** manually (the tag, not the share name; tags are
case-sensitive):

```bash
sudo mkdir -p /mnt/share
sudo mount -t virtiofs share-tag /mnt/share
```

Check with `findmnt -t virtiofs`. Add `share-tag /mnt/share virtiofs
defaults,nofail 0 0` to `/etc/fstab` to persist it. The share is read/write
unless marked read-only, so it works as a two-way replacement when clipboard
sync is blocked. Verified with a Fedora 45 guest.

## 9. Settings

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

## 10. Day-2 and troubleshooting

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
Wayland, `wl-clipboard` and QGA `guest-exec` (X11 is out of scope), so guests
that block `guest-exec` must use a VirtioFS share; 125%
does not fit a 1440×900 display; v-EMU is
single-host/local-only. Use one display and Linux workspaces rather than
multi-monitor. v-EMU does not manage containers or replace Docker/Podman/
Kubernetes tooling.

## 11. CLI cheat sheet

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
