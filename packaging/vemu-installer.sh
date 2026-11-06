#!/usr/bin/env bash
# Thin, user-facing installer wrapper for a source bundle or .run package.
#
# It deliberately does not install packages itself. The real logic lives in
# setup.sh; this script only provides welcome/license/precheck/setup/finish
# screens and a reboot recommendation.
set -euo pipefail

# Match the app's dark first-run look on GTK-based dialogs (zenity and the
# single-window installer). An explicit user GTK_THEME still wins.
export GTK_THEME=${GTK_THEME:-Adwaita:dark}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=""
for candidate in "$SCRIPT_DIR" "$SCRIPT_DIR/.." "$PWD"; do
    if [[ -f "$candidate/setup.sh" && -f "$candidate/LICENSE" ]]; then
        ROOT=$(CDPATH= cd -- "$candidate" && pwd)
        break
    fi
done
if [[ -z "$ROOT" ]]; then
    echo "Run this installer from the extracted v-EMU bundle." >&2
    exit 1
fi

MODE=standard
ASSUME=0
DRY_RUN=0
NO_UI=0
LOG="${TMPDIR:-/tmp}/vemu-install-$(date +%Y%m%dT%H%M%S).log"
GUI=""
KEEPALIVE_PID=""

cleanup() {
    if [[ -n "$KEEPALIVE_PID" ]]; then
        kill "$KEEPALIVE_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

usage() {
    cat <<'EOF'
Usage: ./vemu-installer.sh [--standard|--full] [--yes] [--dry-run]

  --standard   Install x86_64 host support (default)
  --full       Also request aarch64/riscv64 TCG packages where available
  --yes        Accept the license and skip confirmation prompts
  --no-ui      Use terminal prompts even when a desktop is available
  --dry-run    Show the steps without changing the host
EOF
}

for arg in "$@"; do
    case "$arg" in
        --standard) MODE=standard ;;
        --full) MODE=full ;;
        --yes|-y) ASSUME=1 ;;
        --no-ui) NO_UI=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ $(id -u) -eq 0 ]]; then
    echo "Run as your normal desktop user, not root." >&2
    exit 1
fi

if [[ $DRY_RUN -eq 1 ]]; then
    printf 'Dry run: v-EMU installer\n\nRoot: %s\nMode: %s\nLog: %s\n\nWould run setup.sh --precheck, then setup.sh --%s.\n' \
        "$ROOT" "$MODE" "$LOG" "$MODE"
    exit 0
fi

if [[ $NO_UI -eq 0 && "${VEMU_INSTALLER_NO_UI:-0}" != "1" ]]; then
    if [[ "${VEMU_INSTALLER_NO_GTK:-0}" != "1" ]] &&
       [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] &&
       command -v python3 >/dev/null 2>&1 &&
       python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' >/dev/null 2>&1; then
        args=(--root "$ROOT" --mode "$MODE")
        [[ $ASSUME -eq 1 ]] && args+=(--yes)
        exec python3 "$ROOT/packaging/vemu-installer-gtk.py" "${args[@]}"
    fi
    if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v zenity >/dev/null 2>&1; then
        GUI=zenity
    elif [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v kdialog >/dev/null 2>&1; then
        GUI=kdialog
    fi
fi

ui_info() {
    case "$GUI" in
        zenity) zenity --info --title="v-EMU" --width=620 --text="$1" ;;
        kdialog) kdialog --title "v-EMU" --msgbox "$1" ;;
        *) printf '\n%s\n' "$1" ;;
    esac
}

ui_error() {
    case "$GUI" in
        zenity) zenity --error --title="v-EMU" --width=620 --text="$1" ;;
        kdialog) kdialog --title "v-EMU" --error "$1" ;;
        *) printf '\nERROR: %s\n' "$1" >&2 ;;
    esac
}

ui_question() {
    case "$GUI" in
        zenity) zenity --question --title="v-EMU" --width=620 --text="$1" ;;
        kdialog) kdialog --title "v-EMU" --yesno "$1" ;;
        *) printf '\n%s [y/N] ' "$1"; read -r answer; [[ "$answer" =~ ^[Yy]$ ]] ;;
    esac
}

ui_text_file() {
    case "$GUI" in
        zenity) zenity --text-info --title="v-EMU" --filename="$1" --width=900 --height=600 ;;
        kdialog) kdialog --title "v-EMU" --textbox "$1" 900 600 ;;
        *) if [[ ${NO_UI:-0} -eq 1 || ! -t 0 ]]; then cat "$1"; else ${PAGER:-less} "$1"; fi ;;
    esac
}

ui_password() {
    case "$GUI" in
        zenity) zenity --password --title="v-EMU sudo password" ;;
        kdialog) kdialog --title "v-EMU sudo password" --password ;;
        *) return 1 ;;
    esac
}

show_log() {
    printf '\n--- %s ---\n' "$LOG"
    tail -n 40 "$LOG" 2>/dev/null || true
}

# License
if [[ $ASSUME -eq 1 ]]; then
    license_ok=1
else
    ui_text_file "$ROOT/LICENSE"
    if ui_question "Do you accept the GNU General Public License v3.0 or later?"; then
        license_ok=1
    else
        license_ok=0
    fi
fi
if [[ $license_ok -ne 1 ]]; then
    ui_info "Installation cancelled."
    exit 1
fi

# Precheck
echo "Running setup.sh --precheck ..." | tee "$LOG"
if ! "$ROOT/setup.sh" --precheck >>"$LOG" 2>&1; then
    ui_text_file "$LOG"
    ui_error "Precheck failed. Review the log, fix the host, then retry."
    exit 1
fi

if [[ $ASSUME -eq 0 ]]; then
    if ! ui_question "Continue with the $MODE installation?"; then
        ui_info "Installation cancelled."
        exit 1
    fi
fi

# Cache sudo credentials for GUI installs. setup.sh itself still asks for
# sudo only for the steps that need it.
if [[ -n "$GUI" ]]; then
    password=$(ui_password) || {
        ui_error "Sudo password is required."
        exit 1
    }
    if ! printf '%s\n' "$password" | sudo -S -v >/dev/null 2>&1; then
        ui_error "Sudo authentication failed."
        exit 1
    fi
    unset password
    # Keep the sudo timestamp fresh during a long package install.
    ( while sleep 60; do sudo -n -v >/dev/null 2>&1 || exit 0; done ) &
    KEEPALIVE_PID=$!
fi

ui_info "Installing v-EMU…"
if ! "$ROOT/setup.sh" "--$MODE" >>"$LOG" 2>&1; then
    ui_text_file "$LOG"
    ui_error "Installation failed. Review the log above."
    exit 1
fi

# Ensure the services needed after the first reboot are enabled. setup.sh
# normally does this, but verify explicitly so the finish screen is honest.
units=(vemud.service)
if [[ -f /etc/debian_version ]]; then
    units+=(libvirtd.service)
else
    units+=(virtqemud.service virtnetworkd.service virtstoraged.service
            virtsecretd.service virtnodedevd.service)
fi
for unit in virtlogd.socket virtlockd.socket; do
    if systemctl list-unit-files "$unit" 2>/dev/null | grep -q "$unit"; then
        units+=("$unit")
    fi
done
service_failures=()
for unit in "${units[@]}"; do
    if ! sudo systemctl is-enabled "$unit" >/dev/null 2>&1; then
        sudo systemctl enable "$unit" >>"$LOG" 2>&1 || service_failures+=("$unit not enabled")
    fi
    if ! sudo systemctl is-active "$unit" >/dev/null 2>&1; then
        # Socket units may only become active at boot; report but keep going.
        service_failures+=("$unit not active yet")
    fi
done

{
    echo "v-EMU is installed."
    echo
    echo "Log: $LOG"
    if [[ ${#service_failures[@]} -gt 0 ]]; then
        echo
        echo "Service checks:"
        printf '  - %s\n' "${service_failures[@]}"
    fi
    echo
    echo "Reboot recommended before first use."
} > "${LOG}.finish"
ui_text_file "${LOG}.finish"

if [[ $ASSUME -eq 0 ]] && ui_question "Reboot now?"; then
    sudo systemctl reboot
fi
