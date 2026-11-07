# v-EMU UEFI firmware

Custom firmware for **newly created** EFI VMs (x64, aarch64, riscv64):

- v-EMU boot logo and tagline **Desktop Virtualization**;
- boot manager title **v-EMU Boot Manager**;
- default resolution **1280×800** (higher modes are available);
- new x64 UEFI VMs have **Secure Boot enabled by default**, with a dual-CA
  2011+2023 VARS template and a wizard checkbox for unsigned ISOs;
- TPM 2.0 enabled for x64, aarch64 and riscv64 (the VM boots with or without a
  TPM; RISC-V with a TPM has booted a distro, but guest visibility should be
  verified);
- EDK II/OVMF BSD-2-Clause-Patent license and original copyrights preserved.

VMs created before the firmware update keep their existing firmware/NVRAM.
VMs already pointing at the custom firmware under `/usr/share/vemu/firmware`
receive fixes when `setup.sh` is run again; no VM recreation is required.

## Version

Current platform/firmware version: **1.0**, stored in `firmware/VERSION` and
installed as `/usr/share/vemu/firmware/VERSION`.

This version is **independent** of the v-EMU application release (`0.x.y`).
Bump it only when the firmware, EDK2 pin, patch, logo or Secure Boot CA change;
application releases do not require a firmware rebuild. New x86_64 VMs use the
same platform version in SMBIOS.

**Release rule:** do not run `build.sh`, change `VERSION` or edit `SHA256SUMS`
for a normal application release. Verify `sha256sum -c SHA256SUMS` and reuse
the existing firmware. Later releases stay focused on code fixes and roadmap
features instead of repeating a firmware build.

## Source and licenses

- EDK II: `https://github.com/tianocore/edk2`, commit `5a52bd1`.
- Built from `ArmVirtPkg/ArmVirtQemu.dsc`, `OvmfPkg/OvmfPkgX64.dsc` and
  `OvmfPkg/RiscVVirt/RiscVVirtQemu.dsc`.
- License: BSD-2-Clause-Patent. OpenSSL (CryptoPkg): Apache-2.0.
- See `LICENSE`, `NOTICE` and `licenses/`.
- The TianoCore logo is not included. The logo in `boot-logo.bmp` is original
  v-EMU artwork.
- Do not remove copyright/license/disclaimer notices; only visible branding
  may be replaced.

## Artifacts

```text
x64/OVMF_CODE.fd              3.65 MB  (raw, used directly)
x64/OVMF_VARS.fd              528 KB   (raw template)
x64/OVMF_CODE.secboot.fd      3.65 MB  (SMM + Secure Boot)
x64/OVMF_VARS.secboot.fd      528 KB   (dual-CA 2011+2023)
aarch64/QEMU_EFI.fd           3 MB
aarch64/QEMU_VARS.fd          768 KB
riscv64/RISCV_VIRT_CODE.fd    8 MB
riscv64/RISCV_VIRT_VARS.fd    768 KB
```

The `.xz` files are the distribution copies used by the repository.
`setup.sh` extracts them into `/usr/share/vemu/firmware` and creates pflash
images:

- aarch64: code and vars 64 MiB (zero padded);
- riscv64: code and vars 32 MiB (zero padded);
- x64: raw files used directly.

## Rebuild (maintainer)

Requirements: `git`, `gcc`, `aarch64-linux-gnu-gcc`,
`riscv64-linux-gnu-gcc`, `nasm`, `iasl`, `python3`, `xz`, and a **clean** EDK2
source tree at commit `5a52bd1` with submodules initialized.

```bash
EDK2_DIR=$HOME/edk2 ./firmware/build.sh
```

The script creates a temporary copy, applies `patches/vemu-firmware.patch`,
copies `boot-logo.bmp` to `MdeModulePkg/Logo/Logo.bmp`, builds the normal and
x64 Secure Boot variants (`SECURE_BOOT_ENABLE=TRUE`, `SMM_REQUIRE=TRUE`), and
updates the artifacts. The original EDK2 tree is not modified. The dual-CA
Secure Boot VARS image is a separately packaged artifact; the build script does
not overwrite it.

The patch makes these changes:

- `ArmVirtPkg/ArmVirtQemu.dsc`, `OvmfPkg/OvmfPkgX64.dsc`: boot timeout 3
  seconds so the logo is visible.
- `OvmfPkg/QemuRamfbDxe/QemuRamfb.c`: adds 1280×800, 1366×768, 1440×900 and
  1920×1080 modes; default 1280×800; `opt/ramfb/mode` fw_cfg override kept.
- `MdeModulePkg/Logo/Logo.bmp`: v-EMU logo, centered on the canvas.
- `BootManagerStrings.uni` / `FrontPageStrings.uni`: v-EMU strings.
- `OvmfPkg/RiscVVirt/.../PlatformBm.c`: v-EMU version banner.
- `ArmVirtPkg/Library/QemuVirtMemInfoLib/QemuVirtMemInfoLib.c`: restores the
  peripheral MMIO mapping `0x08000000–0x10000000` (Device) so PEI can reach
  TPM TIS at `0x0C000000`. Without this patch, aarch64 with
  `TPM2_ENABLE=TRUE` aborts in `Tcg2ConfigPei` with FAR `0xC000000`.

All three architectures use `-D TPM2_CONFIG_ENABLE=TRUE`; riscv64 also uses
`-D TPM2_ENABLE=TRUE` (firmware-level TPM support; a VM with a TPM has booted,
but guest visibility should be verified).

## Integration

v-EMU uses this firmware for new EFI domains when the files exist.
`VEMU_FIRMWARE_DIR` can override the default `/usr/share/vemu/firmware`.

New x64 UEFI domains use the `.secboot` pair and add
`<loader secure='yes'>` plus `<smm state='on'/>`. The user can disable this
with `--no-secure-boot` or the wizard checkbox. A missing secure-boot pair
falls back to normal firmware with a warning; missing custom firmware falls
back to libvirt's default firmware.

Install with:

```bash
./setup.sh --standard   # x64 firmware only
./setup.sh --full       # x64 + aarch64 + riscv64 firmware
```

`setup.sh` only extracts and pads artifacts; it does not build firmware on the
user's machine. Re-running it is idempotent.

## Verification

- Logo displays at 1280×800, centered, on all three architectures.
- Boot manager shows **v-EMU**.
- TPM 2.0 is present in firmware (x64/aarch64 verified; riscv64 is
  firmware-level and supports boot-to-firmware). **Boot to firmware** opens the
  menu with or without a TPM.
- NVMe and USB ISO boot paths still work.
- Existing VMs and the libvirt fallback are unchanged.
- RISC-V: firmware boots to the UEFI Shell and boot-to-firmware opens v-EMU
  Setup with a TPM. **OpenSUSE Tumbleweed RISC-V and Ubuntu-RISCV have
  booted**, slower than aarch64; VMs with a TPM have booted, but guest
  visibility should be verified.
- `./setup.sh --precheck` checks the host/firmware; `--standard`/`--full`
  verify installed files and pflash sizes (`--standard` checks x64 only).
