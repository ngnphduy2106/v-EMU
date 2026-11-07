#!/usr/bin/env bash
# v-EMU source setup for Fedora/RHEL-like and openSUSE/SUSE-like hosts.
#
# Modes: --precheck, --standard (x64), --full (x64 + aarch64/riscv64),
# --uninstall. The script intentionally builds a local virtual environment
# instead of producing an RPM, installs host packages, installs v-EMU in
# editable mode, links the CLI into ~/.local/bin, and deactivates the venv
# before it exits.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}
SYSTEM_PYTHON=${SYSTEM_PYTHON:-/usr/bin/python3}
if [[ ! -x "$SYSTEM_PYTHON" ]]; then
    SYSTEM_PYTHON=$(command -v python3)
fi
VENV="$ROOT/.venv"
INSTALL_SYSTEM=1
INSTALL_DESKTOP=1
INSTALL_DAEMON=0
CROSS_ARCH=0
MODE=""
MODE_SET=0
LEGACY=0
SYSTEM_EXPLICIT=0
DESKTOP_EXPLICIT=0
CROSS_FLAG=0
PRECHECK_INSTALL=0
PURGE=0
ASSUME_YES=0
DRY_RUN=0
DISTRO_FAMILY="unknown"

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case " ${ID:-} ${ID_LIKE:-} " in
        *opensuse*|*suse*|*sles*|*sled*) DISTRO_FAMILY="suse" ;;
        *fedora*|*rhel*|*centos*|*rocky*|*almalinux*) DISTRO_FAMILY="fedora" ;;
        *debian*|*ubuntu*) DISTRO_FAMILY="debian" ;;
    esac
fi

# Debian AppArmor's virt-aa-helper only trusts firmware below
# /usr/share/OVMF. Install the custom firmware there on Debian so UEFI
# domains can start without weakening the host profile.
FIRMWARE_DIR=/usr/share/vemu/firmware
if [[ $DISTRO_FAMILY == "debian" ]]; then
    FIRMWARE_DIR=/usr/share/OVMF/vemu
fi

# PySide6 is optional in several distro repositories. When it is absent we
# install it from pip; Debian 12 (bookworm) needs the known-good 6.10.2
# because 6.12.0 segfaults on shutdown in the Qt WebEngine stack. Maintainers
# can override the pip version with VEMU_PYSIDE6_VERSION.
PIP_PYSIDE6_SPEC=""
if [[ -n "${VEMU_PYSIDE6_VERSION:-}" ]]; then
    PIP_PYSIDE6_SPEC="PySide6==${VEMU_PYSIDE6_VERSION}"
elif [[ $DISTRO_FAMILY == "debian" && "${VERSION_ID:-}" == "12" ]]; then
    PIP_PYSIDE6_SPEC="PySide6==6.10.2"
fi

usage() {
    cat <<'EOF'
Usage: ./setup.sh <mode> [options]

Run as your normal user, not with sudo. The script requests the sudo password
once for host packages, groups and services.

Modes (choose one; no mode and no legacy flags means --standard):
  --precheck    Check CPU virtualization, /dev/kvm, host tools, TPM and
                firmware only. No installation, no sudo. Exits 1 when KVM is
                unavailable.
  --standard    Install everything for this x86_64 host: dependencies, v-EMU,
                daemon, desktop entry and x64 firmware (including Secure Boot).
  --full        Same as --standard plus aarch64/riscv64 TCG packages and
                firmware for all three architectures.
  --uninstall   Remove the v-EMU service, system package, firmware, venv,
                desktop entry and CLI links. Keeps VMs, storage, templates,
                /etc/vemu and host QEMU/libvirt packages. Add --purge to also
                remove /etc/vemu and user logs/settings.

Options:
  --no-system      Skip host package installation (advanced).
  --daemon         Install/start vemud (implied by --standard/--full).
  --desktop        Install the desktop entry and icon (default).
  --no-desktop     Do not install the desktop entry.
  --cross-arch     Legacy alias: install aarch64/riscv64 packages/firmware
                   when no mode is chosen. Prefer --full.
  --yes, -y        Do not ask for uninstall confirmation.
  --dry-run        With --uninstall, print actions without changing anything.
  -h, --help       Show this help.

The script creates .venv, installs v-EMU, and links vemu/vemu-ui/vemud into
~/.local/bin. --standard/--full also install the system daemon, create the vemu
group, install the polkit action, and verify the firmware/pflash it installs.

Supported host families: Fedora/RHEL/CentOS/Rocky/AlmaLinux (dnf),
openSUSE/SUSE-family (zypper) and Debian/Ubuntu (apt).
SLES/SLED are untested; use them as-is.

Hard requirements:
  - The host CPU must expose Intel VT-x (vmx) or AMD-V (svm) and /dev/kvm
    must be available. setup.sh refuses to install when KVM is unavailable;
    there is no TCG-only install mode. VT-d/IOMMU is only required for PCI
    passthrough, not for KVM. --precheck reports the same requirement without
    changing the host, and --uninstall never checks KVM.
  - Python 3.11 or newer. setup.sh uses $PYTHON (default python3) for the
    venv/UI and $SYSTEM_PYTHON (default /usr/bin/python3) for the daemon.
    Install 3.11+ and set both variables when the default is older, e.g.
    PYTHON=/usr/bin/python3.11 SYSTEM_PYTHON=/usr/bin/python3.11 ./setup.sh --standard

Install modes also enable IPv4 forwarding for NAT lab networks and persist it
in /etc/sysctl.d/90-vemu.conf.

Set VEMU_DEBUG=1 to trace each setup step. VEMU_ALLOW_ROOT=1 permits running
as root for containers/CI (not for normal desktop installs).
EOF
}

set_mode() {
    local value=$1
    if [[ $MODE_SET -eq 1 && $MODE != "$value" ]]; then
        echo "Choose only one of --precheck, --standard, --full or --uninstall." >&2
        exit 2
    fi
    MODE=$value
    MODE_SET=1
}

for arg in "$@"; do
    case "$arg" in
        --precheck) set_mode precheck ;;
        --standard) set_mode standard ;;
        --full) set_mode full ;;
        --uninstall) set_mode uninstall ;;
        --purge) PURGE=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --no-system)
            INSTALL_SYSTEM=0
            SYSTEM_EXPLICIT=1
            LEGACY=1
            ;;
        --daemon)
            INSTALL_DAEMON=1
            LEGACY=1
            ;;
        --desktop)
            INSTALL_DESKTOP=1
            DESKTOP_EXPLICIT=1
            LEGACY=1
            ;;
        --no-desktop)
            INSTALL_DESKTOP=0
            DESKTOP_EXPLICIT=1
            LEGACY=1
            ;;
        --cross-arch)
            CROSS_ARCH=1
            CROSS_FLAG=1
            LEGACY=1
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ $PURGE -eq 1 && $MODE != "uninstall" ]]; then
    echo "--purge is only valid with --uninstall." >&2
    exit 2
fi
if [[ $DRY_RUN -eq 1 && $MODE != "uninstall" ]]; then
    echo "--dry-run is only valid with --uninstall." >&2
    exit 2
fi
if [[ $MODE_SET -eq 0 ]]; then
    if [[ $LEGACY -eq 1 ]]; then
        MODE="legacy"
    else
        MODE="standard"
    fi
    MODE_SET=1
fi
case "$MODE" in
    full)
        [[ $SYSTEM_EXPLICIT -eq 1 ]] || INSTALL_SYSTEM=1
        [[ $DESKTOP_EXPLICIT -eq 1 ]] || INSTALL_DESKTOP=1
        INSTALL_DAEMON=1
        CROSS_ARCH=1
        ;;
    standard)
        if [[ $CROSS_FLAG -eq 1 ]]; then
            echo "--standard cannot be combined with --cross-arch; use --full." >&2
            exit 2
        fi
        [[ $SYSTEM_EXPLICIT -eq 1 ]] || INSTALL_SYSTEM=1
        [[ $DESKTOP_EXPLICIT -eq 1 ]] || INSTALL_DESKTOP=1
        INSTALL_DAEMON=1
        CROSS_ARCH=0
        ;;
    legacy|precheck|uninstall) : ;;
esac

if [[ "${VEMU_DEBUG:-}" == "1" ]]; then
    set -x
    PS4='+ [$(date +%H:%M:%S)] '
fi

if [[ ! -f "$ROOT/pyproject.toml" ]]; then
    echo "Run this script from the v-EMU source tree." >&2
    exit 1
fi

# Python version helpers for the 3.11+ requirement enforced below.
PYTHON_MIN_VERSION="3.11"

python_version_string() {
    "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' \
        2>/dev/null || true
}

python_version_supported() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1
}

check_python_requirement() {
    local exe=$1 role=$2 version
    if ! command -v "$exe" >/dev/null 2>&1; then
        echo "v-EMU setup: $role Python interpreter not found: $exe" >&2
        return 1
    fi
    if python_version_supported "$exe"; then
        return 0
    fi
    version=$(python_version_string "$exe")
    echo "v-EMU setup: $role interpreter '$exe' is Python ${version:-unknown}; Python $PYTHON_MIN_VERSION or newer is required." >&2
    return 1
}

python_requirement_hint() {
    cat >&2 <<'EOF'
Install Python 3.11 or newer (including its pip and development packages) and
select it for both roles. Example:
  PYTHON=/usr/bin/python3.11 SYSTEM_PYTHON=/usr/bin/python3.11 ./setup.sh --standard
Distributions that still default to an older python3 must provide the newer
interpreter first; setup.sh does not install an alternate Python itself.
EOF
}

if [[ $(id -u) -eq 0 ]]; then
    if [[ "${VEMU_ALLOW_ROOT:-}" == "1" ]]; then
        SUDO=()
    else
        echo "Do not run setup.sh with sudo/root." >&2
        echo "Run it as your normal user; the script invokes sudo for system steps." >&2
        if [[ -n "${SUDO_USER:-}" ]]; then
            echo "Try: ./setup.sh ${*}" >&2
        fi
        exit 1
    fi
else
    SUDO=(sudo)
fi

# pyproject.toml requires Python 3.11+. Check before requesting sudo or
# installing host packages so old hosts fail with a clear, actionable message
# instead of a pip editable-build error halfway through.
if [[ $MODE != "precheck" && $MODE != "uninstall" ]]; then
    if ! check_python_requirement "$PYTHON" "venv/UI"; then
        python_requirement_hint
        exit 1
    fi
    if [[ $INSTALL_DAEMON -eq 1 ]] && \
       ! check_python_requirement "$SYSTEM_PYTHON" "daemon"; then
        python_requirement_hint
        exit 1
    fi
fi

if [[ ${#SUDO[@]} -gt 0 && $MODE != "precheck" ]]; then
    if [[ $MODE == "uninstall" && $DRY_RUN -eq 1 ]]; then
        :
    else
        echo "Requesting sudo privileges for host packages and services..."
        if ! "${SUDO[@]}" -n true 2>/dev/null; then
            if [[ ! -t 0 ]]; then
                echo "sudo needs a password, but no terminal is available." >&2
                echo "Run the installer from a terminal or configure sudo for this user." >&2
                exit 1
            fi
            "${SUDO[@]}" -v
        fi
    fi
fi

echo "v-EMU setup mode: $MODE ($DISTRO_FAMILY)"

pkg_available() {
    # Check the RPM database first: dnf/zypper queries can block on metadata
    # refresh or a PackageKit lock even when the package is already installed.
    if command -v rpm >/dev/null 2>&1 && rpm -q "$1" >/dev/null 2>&1; then
        return 0
    fi
    case "$DISTRO_FAMILY" in
        # Never let a package query inherit the terminal: dnf can prompt to
        # import a GPG key for an unrelated repo, and with stdout/stderr
        # redirected that prompt is invisible (setup looks hung). Prefer the
        # local cache so the probe does not refresh metadata; fall back to a
        # non-interactive query only when the cache is incomplete.
        suse) zypper --non-interactive -q info "$1" </dev/null >/dev/null 2>&1 ;;
        fedora)
            dnf -q --cacheonly list --available "$1" </dev/null >/dev/null 2>&1 \
                || dnf -q list --available "$1" </dev/null >/dev/null 2>&1
            ;;
        debian)
            dpkg-query -W -f='${Status}' "$1" 2>/dev/null \
                | grep -q "install ok installed" && return 0
            apt-cache show "$1" >/dev/null 2>&1
            ;;
        *) return 1 ;;
    esac
}

add_optional_packages() {
    local pkg
    for pkg in "$@"; do
        if pkg_available "$pkg"; then
            packages+=("$pkg")
        fi
    done
}

enable_unit() {
    "${SUDO[@]}" systemctl enable --now "$1" >/dev/null || true
}

enable_virtualization() {
    if [[ $DISTRO_FAMILY == "debian" ]]; then
        # Debian package defaults to the monolithic libvirtd service. Do not
        # start the modular daemons here; they can compete for the same
        # sockets.
        enable_unit libvirtd
        return
    fi

    local modular_available=0 libvirtd_active=0 virtqemud_active=0
    local choice=${VEMU_LIBVIRT_STACK:-} running=0
    if systemctl cat virtqemud.service >/dev/null 2>&1; then
        modular_available=1
    fi
    if systemctl is-active --quiet libvirtd; then
        libvirtd_active=1
    fi
    if systemctl is-active --quiet virtqemud; then
        virtqemud_active=1
    fi
    if [[ -n "$choice" && "$choice" != "modular" && "$choice" != "libvirtd" ]]; then
        echo "Invalid VEMU_LIBVIRT_STACK='$choice' (use modular or libvirtd)." >&2
        return 1
    fi
    if [[ -z "$choice" ]]; then
        if (( modular_available )); then
            choice=modular
        else
            choice=libvirtd
        fi
        if (( modular_available && libvirtd_active && virtqemud_active )); then
            running=$(virsh -c qemu:///system list --state-running --name \
                2>/dev/null | sed '/^[[:space:]]*$/d' | wc -l)
            if (( running > 0 )); then
                echo "WARNING: libvirtd and virtqemud are both active, but" \
                    "$running VM(s) are running." >&2
                echo "v-EMU keeps the current libvirt stack to avoid stopping" \
                    "them." >&2
                echo "Fix it after shutting the VMs down:" >&2
                echo "  sudo systemctl disable --now libvirtd libvirtd.socket" \
                    "libvirtd-ro.socket libvirtd-admin.socket" >&2
                echo "  sudo systemctl enable --now virtqemud.socket" \
                    "virtqemud.service virtnodedevd.socket virtnodedevd.service" >&2
                echo "Then run ./setup.sh again (or set" \
                    "VEMU_LIBVIRT_STACK=modular)." >&2
                return 0
            fi
        fi
    fi

    local -a modular_units=(
        virtqemud.socket virtqemud.service
        virtnodedevd.socket virtnodedevd.service
        virtnetworkd.socket virtnetworkd.service
        virtstoraged.socket virtstoraged.service
        virtsecretd.socket virtsecretd.service
    )
    if [[ "$choice" == "modular" ]]; then
        if (( ! modular_available )); then
            echo "VEMU_LIBVIRT_STACK=modular but virtqemud is not installed." >&2
            return 1
        fi
        echo "Using modular libvirt daemons."
        "${SUDO[@]}" systemctl disable --now \
            libvirtd libvirtd.socket libvirtd-ro.socket libvirtd-admin.socket \
            >/dev/null 2>&1 || true
        local unit
        for unit in "${modular_units[@]}"; do
            enable_unit "$unit"
        done
    else
        echo "Using monolithic libvirtd."
        local unit
        for unit in "${modular_units[@]}"; do
            "${SUDO[@]}" systemctl disable --now "$unit" \
                >/dev/null 2>&1 || true
        done
        enable_unit libvirtd
    fi
}

enable_ip_forward() {
    # NAT lab networks need IPv4 forwarding. libvirt normally sets this when a
    # NAT network starts, but a fresh host with no networks yet would fail
    # vemu doctor. Persist the setting so it survives a reboot.
    local current="" changed=0
    if [[ ! -f /etc/sysctl.d/90-vemu.conf ]]; then
        printf 'net.ipv4.ip_forward = 1\n' | \
            "${SUDO[@]}" tee /etc/sysctl.d/90-vemu.conf >/dev/null
        changed=1
    fi
    if [[ -r /proc/sys/net/ipv4/ip_forward ]]; then
        current=$(< /proc/sys/net/ipv4/ip_forward)
    fi
    if [[ "$current" != "1" ]]; then
        changed=1
    fi
    if [[ $changed -eq 1 ]]; then
        echo "Enabling IPv4 forwarding for lab NAT networks..."
        if [[ -x /usr/lib/systemd/systemd-sysctl ]]; then
            "${SUDO[@]}" /usr/lib/systemd/systemd-sysctl \
                /etc/sysctl.d/90-vemu.conf >/dev/null 2>&1 || true
        fi
        if [[ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || true)" != "1" ]]; then
            if ! echo 1 | "${SUDO[@]}" tee /proc/sys/net/ipv4/ip_forward >/dev/null; then
                echo "Warning: could not enable IPv4 forwarding; NAT labs may not work." >&2
            fi
        fi
    fi
}

PRECHECK_ERRORS=0
PRECHECK_WARNINGS=0

precheck_ok() {
    printf '  [ ok ] %s\n' "$1"
}

precheck_warn() {
    printf '  [warn] %s\n' "$1"
    PRECHECK_WARNINGS=$((PRECHECK_WARNINGS + 1))
}

precheck_fail() {
    printf '  [fail] %s\n' "$1" >&2
    PRECHECK_ERRORS=$((PRECHECK_ERRORS + 1))
}

precheck_cmd() {
    local cmd=$1 label=${2:-$1} required=${3:-1} path=""
    if command -v "$cmd" >/dev/null 2>&1; then
        path=$(command -v "$cmd")
    elif [[ -x "/usr/sbin/$cmd" ]]; then
        path="/usr/sbin/$cmd"
    elif [[ -x "/sbin/$cmd" ]]; then
        path="/sbin/$cmd"
    elif [[ -x "/usr/local/sbin/$cmd" ]]; then
        path="/usr/local/sbin/$cmd"
    fi
    if [[ -n "$path" ]]; then
        precheck_ok "$label ($path)"
    elif [[ $required -eq 1 && $PRECHECK_INSTALL -eq 1 ]]; then
        precheck_warn "$label not found (will be installed)"
    elif [[ $required -eq 1 ]]; then
        precheck_fail "$label not found"
    else
        precheck_warn "$label not found (optional)"
    fi
}

VIRT_OK=0
VIRT_REASON=unknown
VIRT_DETAIL=""
VIRT_HINT=""
VIRT_ARCH="unknown"
VIRT_CPU_LABEL=""
VIRT_CPU_FLAGS=""
VIRT_ENV="unknown"

load_virtualization() {
    local output
    if ! output=$(PYTHONPATH="$ROOT/src" "$PYTHON" -m vemu.virtcheck \
                  --shell 2>&1); then
        VIRT_OK=0
        VIRT_REASON=error
        VIRT_DETAIL="virtualization check failed: $output"
        VIRT_HINT="Ensure python3 is installed and run from the v-EMU source tree."
        return 1
    fi
    eval "$output"
    return 0
}

precheck_virtualization() {
    if ! load_virtualization; then
        precheck_fail "$VIRT_DETAIL"
        return
    fi
    case "$VIRT_REASON" in
        ok)
            precheck_ok "$VIRT_DETAIL"
            ;;
        kvm-permission)
            precheck_warn "$VIRT_DETAIL. $VIRT_HINT"
            ;;
        kvm-missing)
            if [[ $PRECHECK_INSTALL -eq 1 ]]; then
                precheck_warn "$VIRT_DETAIL $VIRT_HINT The post-install check will retry after the KVM packages are installed."
            else
                precheck_fail "$VIRT_DETAIL $VIRT_HINT"
            fi
            ;;
        *)
            precheck_fail "$VIRT_DETAIL $VIRT_HINT"
            ;;
    esac
}

ensure_virtualization() {
    local stage=${1:-post}
    local module=""
    load_virtualization || {
        echo "Cannot verify host KVM support." >&2
        echo "  $VIRT_DETAIL" >&2
        exit 1
    }
    if [[ $VIRT_OK -eq 0 && $VIRT_REASON == "kvm-missing" ]]; then
        case " $VIRT_CPU_FLAGS " in
            *" vmx "*) module=kvm_intel ;;
            *" svm "*) module=kvm_amd ;;
        esac
        if [[ -n "$module" ]] && command -v modprobe >/dev/null 2>&1; then
            echo "KVM device is missing; trying to load $module..."
            "${SUDO[@]}" modprobe "$module" >/dev/null 2>&1 || true
            load_virtualization || true
        fi
    fi
    if [[ $VIRT_REASON == "kvm-missing" && $stage == "pre" ]]; then
        echo "Warning: $VIRT_DETAIL" >&2
        echo "         $VIRT_HINT" >&2
        echo "         Setup will recheck KVM before creating the venv." >&2
        return 0
    fi
    case "$VIRT_REASON" in
        ok)
            echo "KVM ready: $VIRT_DETAIL"
            ;;
        kvm-permission)
            # precheck_host reports the group/permission warning once.
            ;;
        *)
            echo "v-EMU requires working KVM hardware virtualization." >&2
            echo "  $VIRT_DETAIL" >&2
            if [[ -n "$VIRT_HINT" ]]; then
                echo "  $VIRT_HINT" >&2
            fi
            echo "Setup stopped before creating or starting the v-EMU venv/daemon." >&2
            exit 1
            ;;
    esac
}

precheck_host() {
    local qemu_x86="" libvirt_version="" major="" arch="" file="" fw_ok=1
    local python_version=""
    PRECHECK_ERRORS=0
    PRECHECK_WARNINGS=0

    echo "Prechecking host tools for v-EMU..."
    python_version=$(python_version_string "$PYTHON")
    if check_python_requirement "$PYTHON" "venv/UI" 2>/dev/null; then
        precheck_ok "Python $python_version ($PYTHON)"
    else
        precheck_fail "Python ${python_version:-unknown} at $PYTHON; Python $PYTHON_MIN_VERSION or newer is required"
    fi
    precheck_cmd qemu-img "qemu-img" 1
    precheck_cmd virsh "virsh" 1
    precheck_cmd nft "nft" 1
    precheck_cmd virt-customize "virt-customize" 0
    precheck_cmd swtpm "swtpm" 0
    if command -v virtiofsd >/dev/null 2>&1; then
        precheck_ok "virtiofsd ($(command -v virtiofsd))"
    elif [[ -x /usr/libexec/virtiofsd ]]; then
        precheck_ok "virtiofsd (/usr/libexec/virtiofsd)"
    else
        precheck_warn "virtiofsd not found (optional; VirtioFS shares disabled)"
    fi
    precheck_cmd xorriso "xorriso" 0
    precheck_cmd mkfs.vfat "mkfs.vfat" 0
    precheck_cmd mcopy "mcopy" 0
    precheck_cmd xz "xz" 1

    if command -v qemu-system-x86_64 >/dev/null 2>&1; then
        qemu_x86=$(command -v qemu-system-x86_64)
    elif [[ -x /usr/libexec/qemu-kvm ]]; then
        qemu_x86=/usr/libexec/qemu-kvm
    fi
    if [[ -n "$qemu_x86" ]]; then
        precheck_ok "x86_64 QEMU ($qemu_x86)"
    elif [[ $PRECHECK_INSTALL -eq 1 ]]; then
        precheck_warn "x86_64 QEMU not found (will be installed)"
    else
        precheck_fail "x86_64 QEMU not found (qemu-system-x86_64 or qemu-kvm)"
    fi

    precheck_virtualization

    libvirt_version=$(virsh --version 2>/dev/null || true)
    if [[ "$libvirt_version" =~ ^([0-9]+)\.([0-9]+) ]]; then
        major=${BASH_REMATCH[1]}
        if (( major < 9 )); then
            precheck_warn "libvirt $libvirt_version may ignore <tcg><tb-cache>; v-EMU falls back to the default cache"
        else
            precheck_ok "libvirt $libvirt_version"
        fi
    else
        precheck_warn "cannot determine libvirt version"
    fi

    if [[ -n "$qemu_x86" ]] && \
       "$qemu_x86" -device help 2>/dev/null | grep -q "tpm-crb"; then
        precheck_ok "x86_64 TPM CRB device"
    else
        precheck_warn "x86_64 QEMU has no tpm-crb; TPM 2.0 will be unavailable"
    fi

    if [[ $CROSS_ARCH -eq 1 ]]; then
        for arch in aarch64 riscv64; do
            if command -v "qemu-system-$arch" >/dev/null 2>&1; then
                precheck_ok "cross-arch QEMU ($arch)"
                if "qemu-system-$arch" -device help 2>/dev/null | \
                   grep -q "tpm-tis-device"; then
                    precheck_ok "$arch TPM TIS device"
                else
                    precheck_warn "$arch QEMU has no tpm-tis-device; TPM 2.0 will be unavailable"
                fi
                if "qemu-system-$arch" -device help 2>/dev/null | \
                   grep -q "ramfb"; then
                    precheck_ok "$arch ramfb device"
                else
                    precheck_warn "$arch QEMU has no ramfb; use virtio-gpu"
                fi
            elif [[ $PRECHECK_INSTALL -eq 1 ]]; then
                precheck_warn "$arch QEMU missing (will be installed)"
            else
                precheck_fail "$arch QEMU missing; install the package or use --standard"
            fi
        done
    else
        precheck_ok "cross-architecture TCG disabled (use --full for aarch64/riscv64)"
    fi

    if [[ -d "$ROOT/firmware" ]]; then
        if (cd "$ROOT/firmware" && sha256sum -c SHA256SUMS >/dev/null 2>&1); then
            precheck_ok "firmware artifacts and SHA256SUMS"
        else
            precheck_fail "firmware SHA256SUMS verification failed"
        fi
        local -a fw_files=(
            x64/OVMF_CODE.fd.xz x64/OVMF_VARS.fd.xz \
            x64/OVMF_CODE.secboot.fd.xz x64/OVMF_VARS.secboot.fd.xz
        )
        if [[ $CROSS_ARCH -eq 1 ]]; then
            fw_files+=(
                aarch64/QEMU_EFI.fd.xz aarch64/QEMU_VARS.fd.xz
                riscv64/RISCV_VIRT_CODE.fd.xz riscv64/RISCV_VIRT_VARS.fd.xz
            )
        fi
        if command -v xz >/dev/null 2>&1; then
            for file in "${fw_files[@]}"; do
                if [[ -f "$ROOT/firmware/$file" ]] && \
                   xz -t "$ROOT/firmware/$file" >/dev/null 2>&1; then
                    :
                else
                    precheck_fail "missing or corrupt $file"
                    fw_ok=0
                fi
            done
            if [[ $fw_ok -eq 1 ]]; then
                precheck_ok "firmware .xz archives decompress"
            fi
        elif [[ $PRECHECK_INSTALL -eq 1 ]]; then
            precheck_warn "xz missing; firmware archives will be verified after install"
        else
            precheck_fail "xz missing; cannot verify firmware archives"
        fi
    else
        precheck_fail "firmware/ directory is missing"
    fi

    if (( PRECHECK_ERRORS > 0 )); then
        echo "Precheck failed with $PRECHECK_ERRORS error(s); fix them before continuing." >&2
        exit 1
    fi
    if (( PRECHECK_WARNINGS > 0 )); then
        echo "Precheck passed with $PRECHECK_WARNINGS warning(s)."
    else
        echo "Precheck passed."
    fi
}

verify_firmware_install() {
    local cross=${1:-1}
    local src="$ROOT/firmware" dst="$FIRMWARE_DIR"
    local code size arch
    local -a files=(
        x64/OVMF_CODE.fd x64/OVMF_VARS.fd \
        x64/OVMF_CODE.secboot.fd x64/OVMF_VARS.secboot.fd
    )
    if [[ $cross -eq 1 ]]; then
        files+=(
            aarch64/QEMU_EFI.fd aarch64/QEMU_VARS.fd
            riscv64/RISCV_VIRT_CODE.fd riscv64/RISCV_VIRT_VARS.fd
        )
    fi
    for code in "${files[@]}"; do
        if ! xz -dc "$src/$code.xz" | cmp -s - "$dst/$code"; then
            echo "Firmware verification failed: $dst/$code differs from $code.xz" >&2
            return 1
        fi
    done
    if [[ -f "$src/VERSION" ]]; then
        if ! cmp -s "$src/VERSION" "$dst/VERSION"; then
            echo "Firmware verification failed: $dst/VERSION differs" >&2
            return 1
        fi
    fi
    if [[ $cross -eq 0 ]]; then
        echo "Firmware install verified (x64 raw)."
        return 0
    fi
    for code in aarch64/QEMU_EFI riscv64/RISCV_VIRT_CODE; do
        size=$(xz -dc "$src/$code.fd.xz" | wc -c)
        if ! xz -dc "$src/$code.fd.xz" | \
           cmp -s -n "$size" - "$dst/$code-pflash.raw"; then
            echo "Firmware verification failed: $dst/$code-pflash.raw is not padded from $code.fd" >&2
            return 1
        fi
    done
    for code in aarch64/QEMU_VARS riscv64/RISCV_VIRT_VARS; do
        arch=${code%%/*}
        size=$(xz -dc "$src/$code.fd.xz" | wc -c)
        if ! xz -dc "$src/$code.fd.xz" | \
           cmp -s -n "$size" - "$dst/$arch/vars-template.raw"; then
            echo "Firmware verification failed: $dst/$arch/vars-template.raw is not padded from $code.fd" >&2
            return 1
        fi
    done
    if [[ $(stat -c%s "$dst/aarch64/QEMU_EFI-pflash.raw") -ne 67108864 ]]; then
        echo "Firmware verification failed: aarch64 code pflash is not 64 MiB" >&2
        return 1
    fi
    if [[ $(stat -c%s "$dst/aarch64/vars-template.raw") -ne 67108864 ]]; then
        echo "Firmware verification failed: aarch64 vars pflash is not 64 MiB" >&2
        return 1
    fi
    if [[ $(stat -c%s "$dst/riscv64/RISCV_VIRT_CODE-pflash.raw") -ne 33554432 ]]; then
        echo "Firmware verification failed: riscv64 code pflash is not 32 MiB" >&2
        return 1
    fi
    if [[ $(stat -c%s "$dst/riscv64/vars-template.raw") -ne 33554432 ]]; then
        echo "Firmware verification failed: riscv64 vars pflash is not 32 MiB" >&2
        return 1
    fi
    echo "Firmware install verified (x64 raw, aarch64 64 MiB, riscv64 32 MiB)."
}

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '+'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

uninstall_vemu() {
    if [[ $DRY_RUN -eq 0 && $ASSUME_YES -eq 0 ]]; then
        if [[ ! -t 0 ]]; then
            echo "Refusing non-interactive uninstall without --yes." >&2
            exit 1
        fi
        read -r -p "Remove v-EMU (service, package, firmware, venv)? [y/N] " answer
        [[ $answer =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
    fi
    if [[ "${VIRTUAL_ENV:-}" == "$VENV" ]]; then
        deactivate 2>/dev/null || true
        echo "Note: run 'deactivate' in your current shell after this."
    fi
    echo "Removing v-EMU..."
    if command -v systemctl >/dev/null 2>&1; then
        run "${SUDO[@]}" systemctl disable --now vemud || true
    fi
    run "${SUDO[@]}" rm -f /etc/systemd/system/vemud.service
    if command -v systemctl >/dev/null 2>&1; then
        run "${SUDO[@]}" systemctl daemon-reload || true
    fi
    run "${SUDO[@]}" rm -f /usr/lib/sysusers.d/vemu.conf
    run "${SUDO[@]}" rm -f /usr/share/polkit-1/actions/org.vemu.policy
    run "${SUDO[@]}" rm -rf /usr/share/vemu
    if [[ "$FIRMWARE_DIR" != "/usr/share/vemu/firmware" ]]; then
        run "${SUDO[@]}" rm -rf "$FIRMWARE_DIR"
    fi

    local system_site dist data
    system_site=$("$SYSTEM_PYTHON" -c \
        'import sysconfig; print(sysconfig.get_paths()["purelib"])')
    run "${SUDO[@]}" rm -rf "$system_site/vemu" "$system_site/vemu.libs"
    for dist in "$system_site"/vemu-*.dist-info; do
        [[ -e "$dist" ]] || continue
        run "${SUDO[@]}" rm -rf "$dist"
    done

    run rm -f "$HOME/.local/bin/vemu" "$HOME/.local/bin/vemu-ui" \
        "$HOME/.local/bin/vemud"
    data=${XDG_DATA_HOME:-$HOME/.local/share}
    run rm -f "$data/applications/org.vemu.VEMU.desktop" \
        "$data/icons/hicolor/scalable/apps/org.vemu.VEMU.svg"
    if command -v update-desktop-database >/dev/null 2>&1; then
        run update-desktop-database "$data/applications" || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        run gtk-update-icon-cache -f -t "$data/icons/hicolor" || true
    fi

    if getent group vemu >/dev/null 2>&1; then
        run "${SUDO[@]}" gpasswd -d "$USER" vemu || true
        run "${SUDO[@]}" groupdel vemu || true
    fi
    if [[ -e "$ROOT/build" || -e "$ROOT/src/vemu.egg-info" ]]; then
        run "${SUDO[@]}" rm -rf "$ROOT/build" "$ROOT/src/vemu.egg-info" || true
    fi
    if [[ -e "$VENV" ]]; then
        if ! run rm -rf "$VENV" 2>/dev/null; then
            run "${SUDO[@]}" rm -rf "$VENV" || true
        fi
    fi
    if [[ -f "$ROOT/.vemu-run-source" ]]; then
        echo "Removing installer source at $ROOT..."
        ( cd / && run rm -rf "$ROOT" ) || true
    fi

    if [[ $PURGE -eq 1 ]]; then
        run "${SUDO[@]}" rm -rf /etc/vemu
        run rm -rf "$HOME/.local/state/vemu" "$HOME/.config/vemu" \
            "$HOME/.config/v-EMU"
    else
        echo "Kept /etc/vemu, VM images/templates and user logs/settings."
        echo "Use --uninstall --purge to remove /etc/vemu and user settings."
    fi
    echo "v-EMU removed. Host QEMU/libvirt packages and VM data were kept."
    echo "VMs that reference $FIRMWARE_DIR need v-EMU reinstalled"
    echo "or a different loader path before they can start again."
}

if [[ $MODE == "precheck" ]]; then
    precheck_host
    exit 0
fi

if [[ $MODE == "uninstall" ]]; then
    uninstall_vemu
    exit 0
fi

if [[ $INSTALL_SYSTEM -eq 1 && $MODE != "legacy" ]]; then
    echo "Pre-install precheck..."
    PRECHECK_INSTALL=1
    precheck_host
    PRECHECK_INSTALL=0
fi

if [[ $MODE == "full" && $INSTALL_SYSTEM -eq 1 ]]; then
    cross_missing=()
    if ! command -v qemu-system-aarch64 >/dev/null 2>&1; then
        if [[ $DISTRO_FAMILY == "suse" ]]; then
            pkg_available qemu-arm || \
                cross_missing+=("qemu-arm (aarch64)")
        elif [[ $DISTRO_FAMILY == "debian" ]]; then
            pkg_available qemu-system-arm || \
                cross_missing+=("qemu-system-arm (aarch64)")
        else
            pkg_available qemu-system-aarch64 || \
                cross_missing+=("qemu-system-aarch64 (aarch64)")
        fi
    fi
    if ! command -v qemu-system-riscv64 >/dev/null 2>&1; then
        if [[ $DISTRO_FAMILY == "suse" ]]; then
            pkg_available qemu-riscv64 || \
                cross_missing+=("qemu-riscv64 (riscv64)")
        elif [[ $DISTRO_FAMILY == "debian" ]]; then
            pkg_available qemu-system-misc || \
                cross_missing+=("qemu-system-misc (riscv64)")
        else
            pkg_available qemu-system-riscv || \
                cross_missing+=("qemu-system-riscv (riscv64)")
        fi
    fi
    if [[ ${#cross_missing[@]} -gt 0 ]]; then
        echo "Full install needs cross-arch QEMU, but these packages are not" >&2
        echo "available in the enabled repositories:" >&2
        for pkg in "${cross_missing[@]}"; do
            echo "  - $pkg" >&2
        done
        echo "Enable the required repo/install them manually, or use --standard." >&2
        exit 1
    fi
fi

if [[ $INSTALL_SYSTEM -eq 0 || $MODE == "legacy" ]]; then
    if [[ $MODE != "precheck" && $MODE != "uninstall" ]]; then
        ensure_virtualization pre
    fi
fi

if [[ $INSTALL_SYSTEM -eq 1 ]]; then
    packages=()
    if [[ "$DISTRO_FAMILY" == "debian" ]]; then
        if ! command -v apt-get >/dev/null 2>&1; then
            echo "apt-get was not found. Use --no-system on this host." >&2
            exit 1
        fi
        if ! apt-cache show qemu-system-x86 >/dev/null 2>&1; then
            echo "Refreshing apt package lists..."
            "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get update
        fi
    fi
    if [[ "$DISTRO_FAMILY" == "suse" ]]; then
        if ! command -v zypper >/dev/null 2>&1; then
            echo "zypper was not found. Use --no-system on this host." >&2
            exit 1
        fi
        PY_TAG=$("$PYTHON" -c \
            'import sys; print(f"python{sys.version_info[0]}{sys.version_info[1]}")')
        packages=(
            qemu-x86 qemu-tools qemu-img xz
            libvirt-daemon libvirt-client libvirt-daemon-qemu
            libvirt-daemon-config-network libvirt-daemon-driver-network
            libvirt-daemon-driver-storage nftables
            "${PY_TAG}"-pip "${PY_TAG}"-libvirt-python swtpm
        )
        # Optional on SLES/openSUSE builds that do not ship these packages.
        add_optional_packages \
            guestfs-tools xorriso dosfstools mtools virt-install \
            virtiofsd qemu-ovmf-x86_64 "${PY_TAG}"-devel \
            gcc pkgconf-pkg-config policycoreutils \
            policycoreutils-python-utils selinux-tools \
            libxkbcommon-x11-0 libgbm1 libnss3 mozilla-nss libgtk-3-0
        # PySide6 wheels link libgthread; SUSE splits it out of libglib into a
        # separate compatibility package, so the pip fallback needs it.
        add_optional_packages libgthread-2_0-0
        # PySide6 from the distribution is optional; pip is the fallback on
        # releases that do not package it for the current Python.
        if pkg_available "${PY_TAG}-pyside6"; then
            packages+=("${PY_TAG}-pyside6")
        fi
        add_optional_packages virt-viewer
        if [[ $CROSS_ARCH -eq 1 ]]; then
            add_optional_packages \
                qemu-arm qemu-riscv64 qemu-uefi-aarch64 qemu-uefi-riscv64
        fi
        missing=()
        for pkg in "${packages[@]}"; do
            rpm -q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
        done
        if [[ ${#missing[@]} -eq 0 ]]; then
            echo "Host packages already installed; skipping zypper."
        else
            echo "Installing host packages with zypper..."
            "${SUDO[@]}" zypper --non-interactive install "${missing[@]}"
        fi
    elif [[ "$DISTRO_FAMILY" == "fedora" ]]; then
        if ! command -v dnf >/dev/null 2>&1; then
            echo "dnf was not found. Use --no-system on this host." >&2
            exit 1
        fi
        packages=(
            qemu-kvm qemu-img xz libvirt libvirt-client libvirt-daemon-kvm
            libvirt-daemon-config-network libvirt-daemon-driver-network
            libvirt-daemon-driver-storage nftables
            python3 python3-pip python3-libvirt swtpm
            mesa-libgbm libxkbcommon-x11
            xcb-util-cursor xcb-util-image xcb-util-keysyms
            xcb-util-renderutil xcb-util-wm nss gtk3
        )
        # Optional; custom firmware is used for new VMs.
        add_optional_packages \
            guestfs-tools xorriso dosfstools mtools virt-install \
            virtiofsd edk2-ovmf virt-viewer libvirt-devel \
            python3-devel gcc pkgconf-pkg-config \
            policycoreutils policycoreutils-python-utils selinux-tools
        if [[ $CROSS_ARCH -eq 1 ]]; then
            add_optional_packages \
                qemu-system-aarch64 qemu-system-riscv edk2-aarch64 edk2-riscv64
        fi
        missing=()
        for pkg in "${packages[@]}"; do
            rpm -q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
        done
        if [[ ${#missing[@]} -eq 0 ]]; then
            echo "Host packages already installed; skipping dnf."
        else
            echo "Installing host packages with dnf..."
            "${SUDO[@]}" dnf install -y "${missing[@]}"
        fi
    elif [[ "$DISTRO_FAMILY" == "debian" ]]; then
        packages=(
            qemu-system-x86 qemu-utils xz-utils
            libvirt-daemon-system libvirt-clients
            libvirt-daemon-config-network libvirt-daemon-driver-qemu
            nftables
            python3 python3-pip python3-venv python3-libvirt swtpm
        )
        # Optional on minimal Debian installs; custom firmware is preferred.
        add_optional_packages \
            libguestfs-tools xorriso dosfstools mtools acl \
            virtiofsd ovmf swtpm-tools \
            policycoreutils selinux-utils setools semanage-utils \
            python3-pyside6.qtwidgets \
            python3-pyside6.qtwebenginewidgets \
            python3-pyside6.qtsvg \
            libgbm1 libxkbcommon-x11-0 libnss3 libgtk-3-0t64
        if [[ $CROSS_ARCH -eq 1 ]]; then
            add_optional_packages \
                qemu-system-arm qemu-system-misc \
                qemu-efi-aarch64 qemu-efi-riscv64
        fi
        missing=()
        for pkg in "${packages[@]}"; do
            dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null \
                | grep -q "install ok installed" || missing+=("$pkg")
        done
        if [[ ${#missing[@]} -eq 0 ]]; then
            echo "Host packages already installed; skipping apt."
        else
            echo "Installing host packages with apt..."
            "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive \
                apt-get install -y "${missing[@]}"
        fi
    else
        echo "Unsupported host distribution. Use --no-system after installing" >&2
        echo "the packages listed in docs/COMPATIBILITY.md." >&2
        exit 1
    fi
fi

if [[ $MODE != "precheck" && $MODE != "uninstall" ]]; then
    ensure_virtualization post
fi

precheck_host

if [[ ! -x "$VENV/bin/python" || ! -f "$VENV/bin/activate" ]]; then
    if [[ -d "$VENV" ]]; then
        if [[ ! -w "$VENV" ]]; then
            echo "$VENV is incomplete and not writable by $USER." >&2
            echo "Remove it with sudo, then rerun setup.sh." >&2
            exit 1
        fi
        echo "Removing incomplete $VENV..."
        rm -rf "$VENV"
    fi
    echo "Creating $VENV..."
    if "$PYTHON" -c 'import libvirt' >/dev/null 2>&1; then
        # Reuse the distro libvirt binding (and PySide6 when present)
        # instead of compiling libvirt-python from source.
        "$PYTHON" -m venv --system-site-packages "$VENV"
    else
        "$PYTHON" -m venv "$VENV"
    fi
    if [[ ! -f "$VENV/bin/activate" ]]; then
        echo "Virtual environment creation did not complete." >&2
        echo "Install the Python venv/ensurepip package and rerun." >&2
        exit 1
    fi
fi

# shellcheck disable=SC1091
. "$VENV/bin/activate"
VENV_ACTIVE=1
# The venv has its own pip; upgrading the host/user pip does not affect it.
# Suppress pip's network-only self-update notice instead of failing an
# offline install.
PIP_OPTS=(--disable-pip-version-check)
export PIP_DISABLE_PIP_VERSION_CHECK=1
cleanup_venv() {
    if [[ ${VENV_ACTIVE:-0} -eq 1 ]]; then
        deactivate 2>/dev/null || true
        VENV_ACTIVE=0
    fi
}
trap cleanup_venv EXIT
echo "Installing Python dependencies..."
if python -c 'import libvirt' >/dev/null 2>&1; then
    EXTRAS='[ui]'
else
    EXTRAS='[ui,libvirt]'
fi
if [[ -n "$PIP_PYSIDE6_SPEC" ]] || ! python -c 'import PySide6' >/dev/null 2>&1; then
    [[ -n "$PIP_PYSIDE6_SPEC" ]] || PIP_PYSIDE6_SPEC="PySide6"
    echo "PySide6 is not available from the distribution; installing $PIP_PYSIDE6_SPEC via pip..."
    python -m pip "${PIP_OPTS[@]}" install -e "$ROOT$EXTRAS" \
        "$PIP_PYSIDE6_SPEC"
else
    echo "Using the distribution PySide6 package."
    python -m pip "${PIP_OPTS[@]}" install -e "$ROOT$EXTRAS"
fi

mkdir -p "$HOME/.local/bin"
echo "Linking vemu/vemu-ui/vemud into $HOME/.local/bin..."
for tool in vemu vemu-ui vemud; do
    ln -sf "$VENV/bin/$tool" "$HOME/.local/bin/$tool"
done


install_firmware() {
    local src="$ROOT/firmware" dst="$FIRMWARE_DIR"
    local file dir
    local -a files=(
        x64/OVMF_CODE.fd x64/OVMF_VARS.fd \
        x64/OVMF_CODE.secboot.fd x64/OVMF_VARS.secboot.fd
    )
    local -a dirs=(x64)
    if [[ $CROSS_ARCH -eq 1 ]]; then
        files+=(
            aarch64/QEMU_EFI.fd aarch64/QEMU_VARS.fd
            riscv64/RISCV_VIRT_CODE.fd riscv64/RISCV_VIRT_VARS.fd
        )
        dirs+=(aarch64 riscv64)
    fi
    [[ -d "$src" ]] || return 0
    if ! command -v xz >/dev/null 2>&1; then
        echo "xz is required to install the v-EMU firmware." >&2
        return 1
    fi
    echo "Installing v-EMU firmware (${dirs[*]})..."
    "${SUDO[@]}" install -d -m 0755 "$dst"
    for dir in "${dirs[@]}"; do
        "${SUDO[@]}" install -d -m 0755 "$dst/$dir"
    done
    for file in "${files[@]}"; do
        if [[ -f "$src/$file.xz" ]]; then
            xz -dc "$src/$file.xz" | "${SUDO[@]}" tee "$dst/$file" >/dev/null
        elif [[ -f "$src/$file" ]]; then
            "${SUDO[@]}" install -m 0644 "$src/$file" "$dst/$file"
        else
            echo "Missing firmware artifact: $file" >&2
            return 1
        fi
        "${SUDO[@]}" chmod 0644 "$dst/$file"
    done
    if [[ $CROSS_ARCH -eq 1 ]]; then
        # aarch64 uses two 64 MiB pflash banks.
        "${SUDO[@]}" truncate -s 64M "$dst/aarch64/QEMU_EFI-pflash.raw"
        "${SUDO[@]}" dd if="$dst/aarch64/QEMU_EFI.fd" \
            of="$dst/aarch64/QEMU_EFI-pflash.raw" conv=notrunc status=none
        "${SUDO[@]}" truncate -s 64M "$dst/aarch64/vars-template.raw"
        "${SUDO[@]}" dd if="$dst/aarch64/QEMU_VARS.fd" \
            of="$dst/aarch64/vars-template.raw" conv=notrunc status=none
        # RISC-V virt uses two 32 MiB pflash banks.
        "${SUDO[@]}" truncate -s 32M "$dst/riscv64/RISCV_VIRT_CODE-pflash.raw"
        "${SUDO[@]}" dd if="$dst/riscv64/RISCV_VIRT_CODE.fd" \
            of="$dst/riscv64/RISCV_VIRT_CODE-pflash.raw" conv=notrunc status=none
        "${SUDO[@]}" truncate -s 32M "$dst/riscv64/vars-template.raw"
        "${SUDO[@]}" dd if="$dst/riscv64/RISCV_VIRT_VARS.fd" \
            of="$dst/riscv64/vars-template.raw" conv=notrunc status=none
        "${SUDO[@]}" chmod 0644 "$dst/aarch64/QEMU_EFI-pflash.raw" \
            "$dst/aarch64/vars-template.raw" \
            "$dst/riscv64/RISCV_VIRT_CODE-pflash.raw" \
            "$dst/riscv64/vars-template.raw"
    fi
    if [[ -f "$src/VERSION" ]]; then
        "${SUDO[@]}" install -m 0644 "$src/VERSION" "$dst/VERSION"
    fi
    for notice in LICENSE NOTICE README.md; do
        if [[ -f "$src/$notice" ]]; then
            "${SUDO[@]}" install -m 0644 "$src/$notice" "$dst/$notice"
        fi
    done
    if [[ -d "$src/licenses" ]]; then
        "${SUDO[@]}" install -d -m 0755 "$dst/licenses"
        for license in "$src"/licenses/*; do
            [[ -f "$license" ]] || continue
            "${SUDO[@]}" install -m 0644 "$license" "$dst/licenses/"
        done
    fi
}

if [[ $INSTALL_DAEMON -eq 1 ]]; then
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "--standard/--full require systemd (or use legacy --no-system)." >&2
        exit 1
    fi
    echo "Installing vemud system service..."
    install_firmware
    verify_firmware_install "$CROSS_ARCH"
    # The daemon must run from a system path. A systemd unit executing a
    # venv under /home is blocked by SELinux (init_t -> user_home_t), so
    # install a daemon-only copy into /usr/local and use system Python.
    SYSTEM_SITE=$("$SYSTEM_PYTHON" -c \
        'import sysconfig; print(sysconfig.get_paths()["purelib"])')
    PIP_FLAGS=(
        --disable-pip-version-check --no-deps --upgrade
        --target "$SYSTEM_SITE" --root-user-action=ignore
    )
    if "$SYSTEM_PYTHON" -m pip help install 2>/dev/null \
       | grep -q -- '--break-system-packages'; then
        PIP_FLAGS+=(--break-system-packages)
    fi
    "${SUDO[@]}" "$SYSTEM_PYTHON" -m pip install \
        "${PIP_FLAGS[@]}" "$ROOT"
    # The daemon install runs as root and leaves build artifacts in the source
    # tree. Remove them so a later .run upgrade can replace the tree.
    "${SUDO[@]}" rm -rf "$ROOT/build" "$ROOT/src/vemu.egg-info"
    "${SUDO[@]}" install -Dm644 \
        "$ROOT/packaging/vemu.sysusers" /usr/lib/sysusers.d/vemu.conf
    "${SUDO[@]}" systemd-sysusers
    "${SUDO[@]}" install -Dm644 \
        "$ROOT/packaging/org.vemu.policy" \
        /usr/share/polkit-1/actions/org.vemu.policy
    "${SUDO[@]}" install -d -m 0755 /etc/vemu
    SELINUX_CONTEXT=""
    if [[ $DISTRO_FAMILY == "fedora" && -e /sys/fs/selinux/enforce ]]; then
        # libvirt's virtqemud_t reads the client daemon's /proc/<pid>/stat.
        # The default systemd service type (unconfined_service_t) triggers a
        # permissive AVC and a setroubleshoot notification on every new
        # libvirt connection. Both contexts are unconfined; unconfined_t is
        # the type libvirt already allows. Replace this with a dedicated
        # vemud_t policy when it exists.
        SELINUX_CONTEXT="SELinuxContext=system_u:system_r:unconfined_t:s0"
    fi
    VIRT_AFTER="After=libvirtd.service virtqemud.socket virtnetworkd.service virtstoraged.service"
    VIRT_WANTS="Wants=virtqemud.socket virtnetworkd.service virtstoraged.service"
    if [[ $DISTRO_FAMILY == "debian" ]]; then
        VIRT_AFTER="After=libvirtd.service"
        VIRT_WANTS="Wants=libvirtd.service"
    fi
    QEMU_ENV_LINE="EnvironmentFile=-/etc/vemu/qemu.env"
    if [[ -n "${VEMU_QEMU_USER:-}" || -n "${VEMU_QEMU_GROUP:-}" ]]; then
        for qemu_value in "${VEMU_QEMU_USER:-}" "${VEMU_QEMU_GROUP:-}"; do
            if [[ -n "$qemu_value" && ! "$qemu_value" =~ ^[A-Za-z0-9._-]+$ ]]; then
                echo "Invalid QEMU user/group name: $qemu_value" >&2
                exit 1
            fi
        done
        TMP_ENV=$(mktemp)
        if [[ -n "${VEMU_QEMU_USER:-}" ]]; then
            printf 'VEMU_QEMU_USER=%s\n' "$VEMU_QEMU_USER" >>"$TMP_ENV"
        fi
        if [[ -n "${VEMU_QEMU_GROUP:-}" ]]; then
            printf 'VEMU_QEMU_GROUP=%s\n' "$VEMU_QEMU_GROUP" >>"$TMP_ENV"
        fi
        "${SUDO[@]}" install -Dm644 "$TMP_ENV" /etc/vemu/qemu.env
        rm -f "$TMP_ENV"
    fi
    TMP_SERVICE=$(mktemp)
    cat >"$TMP_SERVICE" <<EOF
[Unit]
Description=v-EMU daemon
Documentation=https://github.com/withcoresystem/v-EMU
$VIRT_AFTER
$VIRT_WANTS

[Service]
Type=simple
Group=vemu
$SELINUX_CONTEXT
$QEMU_ENV_LINE
ExecStart=$SYSTEM_PYTHON -m vemu.daemon --foreground
RuntimeDirectory=vemu
RuntimeDirectoryMode=0750
NoNewPrivileges=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_NETLINK
LockPersonality=yes
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
    "${SUDO[@]}" install -Dm644 "$TMP_SERVICE" \
        /etc/systemd/system/vemud.service
    rm -f "$TMP_SERVICE"
    "${SUDO[@]}" systemctl daemon-reload
    enable_virtualization
    "${SUDO[@]}" systemctl enable vemud >/dev/null || true
    "${SUDO[@]}" systemctl restart vemud
    if [[ $(id -u) -ne 0 ]]; then
        "${SUDO[@]}" usermod -aG vemu "$USER" >/dev/null || true
    fi
fi

if command -v systemctl >/dev/null 2>&1; then
    enable_virtualization
fi
enable_ip_forward

if [[ $(id -u) -ne 0 ]]; then
    "${SUDO[@]}" usermod -aG libvirt,kvm "$USER" >/dev/null || true
fi
if [[ $INSTALL_DESKTOP -eq 1 ]]; then
    echo "Installing desktop entry and icon..."
    VEMU_EXEC="$VENV/bin/vemu ui" \
        "$ROOT/packaging/install-desktop-user.sh"
fi

if [[ $INSTALL_DESKTOP -eq 1 ]]; then
    DESKTOP_NOTE="
  Application menu:
    v-EMU  (installed under ${XDG_DATA_HOME:-$HOME/.local/share}/applications)
"
else
    DESKTOP_NOTE=""
fi

cleanup_venv
trap - EXIT

cat <<EOF

v-EMU is installed (mode: $MODE).

  The Python virtualenv is not left active in this shell.
  Run the desktop app:
    vemu ui
${DESKTOP_NOTE}
  Run the CLI:
    vemu --help

  Daemon:
    systemctl status vemud     # installed by --standard/--full

  Manage the installation:
    ./setup.sh --precheck
    ./setup.sh --standard
    ./setup.sh --full
    ./setup.sh --uninstall

If this is your first install, a full reboot is recommended so group
membership, icon caches and libvirt sockets are fully refreshed.
Alternatively log out and back in, then run \`newgrp libvirt\` / \`newgrp vemu\`.
EOF
