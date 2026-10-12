#!/bin/bash
# OMEN Hub installer. Run once from a real terminal:
#   sudo ~/omen-hub/install.sh
# Installs a root-owned copy in /usr/local/lib/omen-hub (the daemon never
# runs code from your home), the omen-hubd service, the launcher and the
# menu entry. It changes NO setting: profiles, fans and hooks stay exactly
# as they are until you change them from the app.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Esegui con sudo: sudo $0" >&2; exit 1; }

SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
LIB=/usr/local/lib/omen-hub
OWNER="${SUDO_USER:-$(stat -c %U "$SRC")}"
HOOKS=(keyboard-fix.sh ec-standby-flag.sh free-vram-on-suspend.sh)

python3 -c 'import PyQt6.QtWidgets' 2>/dev/null || echo "ATTENZIONE: python-pyqt6 non installato (serve alla GUI)."

echo "→ copia in $LIB"
install -d -m 0755 "$LIB" "$LIB/payload/hooks"
rm -rf "$LIB/omen_hub.new"
cp -r "$SRC/omen_hub" "$LIB/omen_hub.new"
find "$LIB/omen_hub.new" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$LIB/omen_hub.new"
chmod -R u=rwX,go=rX "$LIB/omen_hub.new"
rm -rf "$LIB/omen_hub.old"
[[ -d "$LIB/omen_hub" ]] && mv "$LIB/omen_hub" "$LIB/omen_hub.old"
mv "$LIB/omen_hub.new" "$LIB/omen_hub"
rm -rf "$LIB/omen_hub.old"
install -m 0755 "$SRC/omen-hubd" "$SRC/omen-hub" "$LIB/"
install -m 0644 "$SRC/README.md" "$LIB/" 2>/dev/null || true

# Snapshot of the live sleep hooks: what "Installa hook" restores after a
# removal. Also copied into the repo so the sources live under git.
echo "→ copia degli hook di sospensione attuali"
for h in "${HOOKS[@]}"; do
  if [[ -f /usr/lib/systemd/system-sleep/$h ]]; then
    install -m 0644 "/usr/lib/systemd/system-sleep/$h" "$LIB/payload/hooks/$h"
    install -m 0644 -o "$OWNER" -g "$OWNER" "/usr/lib/systemd/system-sleep/$h" "$SRC/payload/hooks/$h"
  elif [[ -f "$SRC/payload/hooks/$h" ]]; then
    install -m 0644 "$SRC/payload/hooks/$h" "$LIB/payload/hooks/$h"
  fi
done

install -d -m 0755 /etc/omen-hub
[[ -f /etc/omen-hub/hubd.json ]] || echo '{"admin_group": "wheel"}' > /etc/omen-hub/hubd.json

echo "→ servizio omen-hubd"
install -m 0644 "$SRC/systemd/omen-hubd.service" /etc/systemd/system/omen-hubd.service

echo "→ gruppo OMEN (omen-hub.target + omen-hub-user.target)"
install -m 0644 "$SRC/systemd/omen-hub.target" /etc/systemd/system/omen-hub.target
install -m 0644 "$SRC/systemd/omen-hub-user.target" /etc/systemd/user/omen-hub-user.target
/usr/bin/python3 -I "$LIB/omen-hubd" --setup-group | sed 's/^/   drop-in /'
systemctl daemon-reload
systemctl enable omen-hubd.service >/dev/null
systemctl restart omen-hubd.service
# Activating the targets starts nothing (no Wants=): it only arms PartOf, so
# that stopping/restarting the group reaches the members already running.
systemctl enable --now omen-hub.target >/dev/null 2>&1
systemctl --global enable omen-hub-user.target >/dev/null 2>&1
if [[ -n "${SUDO_USER:-}" ]]; then
  U_ENV=(XDG_RUNTIME_DIR="/run/user/$(id -u "$SUDO_USER")"
         DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "$SUDO_USER")/bus")
  sudo -u "$SUDO_USER" "${U_ENV[@]}" systemctl --user daemon-reload 2>/dev/null || true
  sudo -u "$SUDO_USER" "${U_ENV[@]}" systemctl --user start omen-hub-user.target 2>/dev/null || true
fi

ln -sf "$LIB/omen-hub" /usr/local/bin/omen-hub
install -D -m 0644 "$SRC/share/omen-hub.desktop" /usr/local/share/applications/omen-hub.desktop
# Icon, in the theme (menu, taskbar) and next to the code (window fallback).
# A local share/icons/omen-hub-<size>.png set wins (not in git: the one used
# here is HP's OMEN logo); otherwise the generic share/omen-hub.svg.
rm -rf "$LIB/share"
if compgen -G "$SRC/share/icons/omen-hub-*.png" >/dev/null; then
  rm -f /usr/local/share/icons/hicolor/scalable/apps/omen-hub.svg
  for png in "$SRC"/share/icons/omen-hub-*.png; do
    s="${png##*-}"; s="${s%.png}"
    install -D -m 0644 "$png" "/usr/local/share/icons/hicolor/${s}x${s}/apps/omen-hub.png"
    install -D -m 0644 "$png" "$LIB/share/icons/$(basename "$png")"
  done
else
  rm -f /usr/local/share/icons/hicolor/*x*/apps/omen-hub.png
  install -D -m 0644 "$SRC/share/omen-hub.svg" /usr/local/share/icons/hicolor/scalable/apps/omen-hub.svg
  install -D -m 0644 "$SRC/share/omen-hub.svg" "$LIB/share/omen-hub.svg"
fi
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f /usr/local/share/icons/hicolor 2>/dev/null || true
# The desktop file carries X-KDE-Shortcuts=DOS (the OMEN key, KEY_MSDOS from
# omen_rgb_keyboard): kglobalaccel registers it when the app database is rebuilt.
if [[ -n "${SUDO_USER:-}" ]] && command -v kbuildsycoca6 >/dev/null; then
  sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$SUDO_USER")" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "$SUDO_USER")/bus" \
    kbuildsycoca6 >/dev/null 2>&1 || true
fi

for i in $(seq 20); do [[ -S /run/omen-hub/hub.sock ]] && break; sleep 0.2; done
if [[ -S /run/omen-hub/hub.sock ]]; then
  echo "OK: omen-hubd attivo. Avvia l'app dal menu (OMEN Hub) o con: omen-hub"
else
  echo "omen-hubd non è partito: journalctl -u omen-hubd -n 30" >&2
  exit 1
fi
