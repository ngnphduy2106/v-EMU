# Graphics and guest sleep

vEMU's New VM wizard uses **VGA** for every x86_64 VM type. VGA is the safest
lifecycle choice: it is available in every installer, needs no guest driver and
resumes reliably. The trade-off is a slower noVNC console than QXL or
virtio-gpu.

There is no single video model that is best for every guest. Choose manually in
the VM's **Hardware** editor according to the table below.

## Quick choice

| Guest | Recommended | Why |
|---|---|---|
| Windows, best 2D console | **QXL, 128 MB** | Best tested Windows 2D response with the QXL driver |
| Windows, zero setup | **VGA, 64 MB** | Works with the built-in Basic Display Adapter |
| Linux, best console | **virtio-gpu** | Best 2D performance and resolution; requires disabling guest s2idle |
| Linux, no guest changes | **VGA, 64 MB** | Safe fallback on every tested distro |
| Linux, middle ground | **QXL, 128 MB** | Good on Fedora/Debian/Alma; not universal |
| ARM64 / RISC-V | virtio or ramfb | VGA is not the normal display device on these machines |

Clones keep the video model stored in their template. The wizard default
applies to new VMs created from an ISO. vEMU does not change the video model
automatically after creation; use the Hardware editor if the template model is
not what you want.

## Changing the video model

1. Stop the VM completely (not a guest reboot).
2. Open **Hardware** and change **Video**.
3. For VGA or QXL, choose **Video memory** (64, 128 or 256 MB; 64 MB is enough
   for 1280×800, 128 MB is the recommended Windows QXL value).
4. Start the VM and check the console and guest resolution.

A full stop/start is required because the video device is part of the domain
hardware.

## Windows

### QXL (best 2D experience)

- Select **QXL** and **128 MB** video memory.
- Install the QXL display driver (`qxldod`) from the virtio-win driver ISO in
  the guest. Without it Windows falls back to the Basic Display Adapter.
- QXL boots at 1024×768. Set the desired mode once in Windows display
  settings (for example a higher mode offered by the QXL driver); Windows
  remembers it across reboots. XML `<resolution>` is only a boot preference,
  not a replacement for the guest setting.
- vEMU keeps ACPI S3 disabled for managed x86 VMs, so Windows normally does
  not enter sleep. If S3 is enabled outside vEMU, QXL was the only tested
  model that resumed with a usable display (after a key press).

### virtio-gpu

- Select **virtio** and install the virtio-win WDDM driver (`viogpudo`).
- This gives the best 2D performance and 1280×800 or higher, but resume from
  sleep is not reliable: the guest can come back with the display output
  inactive.
- Use virtio when console performance matters more than sleep. vEMU's default
  S3 block means Windows will not sleep in normal use.

### VGA

- Select **VGA** and **64 MB**.
- Uses the Microsoft Basic Display Adapter; no driver ISO is needed.
- Windows reports that standby is disabled by the graphics component, so it
  will not sleep. This is the safest lifecycle option, at the cost of console
  quality.

## Linux

### virtio-gpu (best console, requires a guest setting)

virtio-gpu gives the best noVNC experience. However, vEMU disables ACPI S3 but
**cannot disable Linux s2idle**: that is a kernel state inside the guest. A
virtio-gpu guest resumed from s2idle can return with a dead scanout while the
VM itself is still running.

If you want to use virtio-gpu, disable guest sleep in the guest after
installation:

```bash
sudo mkdir -p /etc/systemd/sleep.conf.d
sudo tee /etc/systemd/sleep.conf.d/90-vemu-no-sleep.conf >/dev/null <<'EOF'
[Sleep]
AllowSuspend=no
AllowHibernation=no
AllowSuspendThenHibernate=no
AllowHybridSleep=no
EOF
sudo systemctl kill -s HUP systemd-logind
```

Verify:

```bash
busctl get-property org.freedesktop.login1 /org/freedesktop/login1 \
  org.freedesktop.login1.Manager CanSuspend
```

The result should be `b false`. The Sleep action then does nothing instead of
entering the broken resume path. Remove the file and reload `systemd-logind`
to re-enable sleep.

Notes:

- This uses systemd. For a non-systemd guest, use the distro's own method to
  disable suspend.
- `SuspendState=freeze` is not a workaround: freeze is the same s2idle state.
- ACPI S3 is disabled by vEMU; do not re-enable it for virtio-gpu without
  testing the guest's resume behaviour.

### QXL

- Select **QXL** and **128 MB**.
- Linux has the `qxl` kernel driver built in; no extra package is normally
  needed.
- QXL worked on Fedora, Debian and AlmaLinux in testing, but not on every
  distro. openSUSE Leap 16.1 returned from sleep with a corrupted frame in
  testing. Use VGA if QXL fails on your distro.
- QXL may boot at 1024×768. Set the desired mode once in the desktop's
  display settings; Linux desktops normally remember it for later boots.

### VGA

- Select **VGA** and **64 MB**.
- The safe fallback. Works without guest drivers, resumes reliably and is the
  wizard default.
- The console is slower and less smooth than QXL or virtio-gpu.

## Sleep behaviour summary

| Video | Linux | Windows |
|---|---|---|
| VGA | Sleep/wake works in testing | Standby is disabled by Windows graphics; no sleep |
| QXL | Works on Fedora/Debian/Alma; Leap 16.1 failed | S3 resumes with a key press; set the final mode once in Windows, it persists |
| virtio-gpu | Best console; guest s2idle must be disabled | Best console; S3 resume loses the display |

## Test checklist

After changing the video model:

1. Stop and start the VM.
2. Open **Console** and confirm the login screen or desktop.
3. If using sleep, suspend and wake the guest once.
4. Confirm the console returns, the keyboard/pointer work, and the guest agent
   is still reachable.
5. Keep a snapshot before changing production labs.

## Limits

- vEMU cannot control Linux s2idle through the domain XML or firmware. S3 can
  be blocked, but the guest kernel still offers s2idle.
- QXL is a legacy paravirtual device. It is supported by vEMU 0.1.x but is not
  the default because it is not universal. Its 1024×768 boot mode is not a
  permanent limit: change it once in the guest and the setting persists.
- The wizard default, VGA, is chosen for lifecycle safety, not for console
  performance. Power users should choose manually.


## Performance tuning wizard

Right-click a Linux VM and choose **Performance tuning…**. The wizard runs an
offline precheck while the VM is shut down, then applies the changes only if
the guest passes:

- Linux guest with systemd;
- not an immutable image (Silverblue/CoreOS/MicroOS etc.);
- VM completely stopped;
- supported guest distribution (tested: Fedora, Debian, AlmaLinux, openSUSE
  Leap and SUSE Linux Enterprise Server 16; other systemd distributions are
  shown as untested);
- readable guest disk.

When precheck passes, the wizard:

- switches the domain XML video model to **virtio-gpu**;
- disables guest suspend and hibernate with a systemd drop-in;
- masks the systemd sleep targets;
- adds `vm.swappiness`/dirty-ratio tuning and a small boot-time tuning unit.

The privileged disk and XML changes are performed by the vEMU service as root;
the wizard does not store or transmit a sudo password. If the vEMU service is
not available, the action is disabled.

Guest power tuning is best-effort. `tuned` is detected but is not required;
Debian and AlmaLinux did not include it in testing. CPU frequency governors are
not exposed by these KVM guests, so "tuning" should not be expected to raise
CPU clock. Keep a snapshot or backup before changing production labs; there is
no automatic rollback in this version.
