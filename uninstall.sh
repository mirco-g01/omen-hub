#!/bin/bash
# Removes OMEN Hub. Keeps /etc/omen-hub (profiles) and /var/lib/omen-hub
# (backups of every file the hub rewrote, and hooks you removed from the app).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Esegui con sudo: sudo $0" >&2; exit 1; }
systemctl disable --now omen-hubd.service 2>/dev/null || true
# The group goes first, without stopping its members: only the binding is removed.
systemctl disable omen-hub.target 2>/dev/null || true
systemctl --global disable omen-hub-user.target 2>/dev/null || true
[[ -x /usr/local/lib/omen-hub/omen-hubd ]] && /usr/bin/python3 -I /usr/local/lib/omen-hub/omen-hubd --remove-group >/dev/null || true
rm -f /etc/systemd/system/omen-hub.target /etc/systemd/user/omen-hub-user.target
systemctl --global disable omen-hub-notify.path omen-hub-notify.service 2>/dev/null || true
if [[ -n "${SUDO_USER:-}" ]]; then
  sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$SUDO_USER")" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "$SUDO_USER")/bus" \
    systemctl --user disable --now omen-hub-luce.service 2>/dev/null || true
fi
rm -f /etc/systemd/user/omen-hub-luce.service
rm -f /etc/systemd/user/omen-hub-notify.path /etc/systemd/user/omen-hub-notify.service \
      /etc/pacman.d/hooks/95-omen-hub-gpufix.hook
rm -f /etc/systemd/system/omen-hubd.service /usr/local/bin/omen-hub \
      /usr/local/share/applications/omen-hub.desktop \
      /usr/local/share/icons/hicolor/scalable/apps/omen-hub.svg \
      /usr/local/share/icons/hicolor/*x*/apps/omen-hub.png
rm -rf /usr/local/lib/omen-hub
systemctl daemon-reload
if compgen -G "/var/lib/omen-hub/disabled-hooks/*" >/dev/null; then
  echo "ATTENZIONE: hook rimossi dall'app e ancora parcheggiati:"
  ls /var/lib/omen-hub/disabled-hooks/
  echo "Per rimetterli: install -m 0755 /var/lib/omen-hub/disabled-hooks/<nome> /usr/lib/systemd/system-sleep/"
fi
echo "OMEN Hub rimosso. Restano /etc/omen-hub e /var/lib/omen-hub (backup)."
