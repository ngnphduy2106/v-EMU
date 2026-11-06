#!/usr/bin/env bash
# Build the v-EMU UEFI firmware (x64, aarch64, riscv64).
#
# Usage:
#   EDK2_DIR=$HOME/edk2 ./firmware/build.sh
#
# Requirements: EDK2 source with submodules, aarch64/riscv64 cross GCC,
# nasm, iasl, python3, xz, rsync. The source tree is copied to a temporary
# directory, patched and built; the original tree is never modified.
set -euo pipefail

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
EDK2_DIR=${EDK2_DIR:-$HOME/edk2}
OUT=${OUT:-$HERE}
WORK=${WORK:-$(mktemp -d "${TMPDIR:-/tmp}/vemu-fw.XXXXXX")}
KEEP_WORK=${VEMU_KEEP_WORK:-0}

cleanup() {
    if [[ "$KEEP_WORK" != "1" ]]; then
        rm -rf "$WORK"
    else
        echo "Build tree kept at $WORK"
    fi
}
trap cleanup EXIT

for tool in git tar xz aarch64-linux-gnu-gcc riscv64-linux-gnu-gcc nasm iasl; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "Missing build tool: $tool" >&2
        exit 1
    }
done

if [[ -n "$(git -C "$EDK2_DIR" status --porcelain 2>/dev/null)" ]]; then
    echo "EDK2_DIR must be a clean checkout at the pinned commit." >&2
    echo "Use a clean worktree instead of a patched tree." >&2
    exit 1
fi

echo "Copying EDK2 source..."
mkdir -p "$WORK/edk2"
tar -C "$EDK2_DIR" --exclude=Build --exclude=.git -cf - . \
    | tar -C "$WORK/edk2" -xf -
(cd "$WORK/edk2" && git apply --unsafe-paths "$HERE/patches/vemu-firmware.patch")
cp "$HERE/boot-logo.bmp" "$WORK/edk2/MdeModulePkg/Logo/Logo.bmp"

cd "$WORK/edk2"
export WORKSPACE=$PWD
export EDK_TOOLS_PATH=$PWD/BaseTools
export GCC_AARCH64_PREFIX=aarch64-linux-gnu-
export GCC_RISCV64_PREFIX=riscv64-linux-gnu-
# shellcheck disable=SC1091
set +u
source edksetup.sh
set -u
make -C BaseTools -j "$(nproc)" >/dev/null

echo "Building x64 OVMF..."
build -a X64 -t GCC -b RELEASE -p OvmfPkg/OvmfPkgX64.dsc \
    -D FD_SIZE_4MB -D TPM2_ENABLE=TRUE \
    -D TPM2_CONFIG_ENABLE=TRUE -n "$(nproc)" >/dev/null

mkdir -p "$WORK/std"
cp "$WORK/edk2/Build/OvmfX64/RELEASE_GCC/FV/OVMF_CODE.fd" \
    "$WORK/edk2/Build/OvmfX64/RELEASE_GCC/FV/OVMF_VARS.fd" "$WORK/std/"

# The matching OVMF_VARS.secboot.fd is a dual-CA (2011+2023) template
# packaged separately; EDK II only produces an empty VARS image.
echo "Building x64 OVMF Secure Boot..."
build -a X64 -t GCC -b RELEASE -p OvmfPkg/OvmfPkgX64.dsc \
    -D FD_SIZE_4MB -D TPM2_ENABLE=TRUE \
    -D TPM2_CONFIG_ENABLE=TRUE -D SECURE_BOOT_ENABLE=TRUE \
    -D SMM_REQUIRE=TRUE -n "$(nproc)" >/dev/null

cp "$WORK/edk2/Build/OvmfX64/RELEASE_GCC/FV/OVMF_CODE.fd" \
    "$WORK/OVMF_CODE.secboot.fd"

echo "Building aarch64 ArmVirtQemu..."
build -a AARCH64 -t GCC -b RELEASE -p ArmVirtPkg/ArmVirtQemu.dsc \
    -D FD_SIZE_IN_MB=3 -D TPM2_ENABLE=TRUE \
    -D TPM2_CONFIG_ENABLE=TRUE -n "$(nproc)" >/dev/null

echo "Building riscv64 RiscVVirtQemu..."
build -a RISCV64 -t GCC -b RELEASE -p OvmfPkg/RiscVVirt/RiscVVirtQemu.dsc \
    -D TPM2_ENABLE=TRUE \
    -D TPM2_CONFIG_ENABLE=TRUE -n "$(nproc)" >/dev/null

install_artifact() {
    local source=$1 target=$2
    install -Dm644 "$source" "$OUT/$target"
    xz -9 -k -f "$OUT/$target"
}

install_artifact "$WORK/std/OVMF_CODE.fd" x64/OVMF_CODE.fd
install_artifact "$WORK/std/OVMF_VARS.fd" x64/OVMF_VARS.fd
install_artifact "$WORK/OVMF_CODE.secboot.fd" x64/OVMF_CODE.secboot.fd
install_artifact "$WORK/edk2/Build/ArmVirtQemu-AArch64/RELEASE_GCC/FV/QEMU_EFI.fd" aarch64/QEMU_EFI.fd
install_artifact "$WORK/edk2/Build/ArmVirtQemu-AArch64/RELEASE_GCC/FV/QEMU_VARS.fd" aarch64/QEMU_VARS.fd
install_artifact "$WORK/edk2/Build/RiscVVirtQemu/RELEASE_GCC/FV/RISCV_VIRT_CODE.fd" riscv64/RISCV_VIRT_CODE.fd
install_artifact "$WORK/edk2/Build/RiscVVirtQemu/RELEASE_GCC/FV/RISCV_VIRT_VARS.fd" riscv64/RISCV_VIRT_VARS.fd

(
    cd "$OUT"
    sha256sum x64/*.fd.xz aarch64/*.fd.xz riscv64/*.fd.xz > SHA256SUMS
    if [[ -f boot-logo.bmp && -f patches/vemu-firmware.patch ]]; then
        sha256sum boot-logo.bmp patches/vemu-firmware.patch >> SHA256SUMS
    fi
)
echo "Firmware updated in $OUT"
