#!/bin/sh
# Install the v-EMU desktop entry and icon for the current user.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}

install -Dm644 \
  "$ROOT/src/vemu/ui/assets/org.vemu.VEMU.svg" \
  "$DATA/icons/hicolor/scalable/apps/org.vemu.VEMU.svg"
EXEC_LINE=${VEMU_EXEC:-"vemu ui"}
TRYEXEC_LINE=${VEMU_TRYEXEC:-${EXEC_LINE%% *}}
TMP_DESKTOP=$(mktemp)
trap 'rm -f "$TMP_DESKTOP"' EXIT
sed -e "s|^Exec=.*|Exec=$EXEC_LINE|" \
    -e "s|^TryExec=.*|TryExec=$TRYEXEC_LINE|" \
  "$ROOT/packaging/org.vemu.VEMU.desktop" >"$TMP_DESKTOP"
install -Dm644 \
  "$TMP_DESKTOP" \
  "$DATA/applications/org.vemu.VEMU.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DATA/applications" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$DATA/icons/hicolor" >/dev/null 2>&1 || true
fi
for cache in kbuildsycoca6 kbuildsycoca5; do
  if command -v "$cache" >/dev/null 2>&1; then
    "$cache" >/dev/null 2>&1 || true
  fi
done

echo "Installed v-EMU desktop entry and icon under $DATA"
