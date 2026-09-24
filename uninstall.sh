#!/usr/bin/env bash
# Remove HDLounge and restore stock GNOME behaviour.
set -euo pipefail

UUID="hdlounge@harmonicdesign.ca"
PREFIX="${PREFIX:-$HOME/.local}"
HWDB_FILE="61-nvidia-shield-remote.hwdb"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hdlounge"
STATE_FILE="$CONFIG_DIR/installer-state"

# System paths. Overridable only so this can be tested without root.
GDM_CONF="${HDLOUNGE_GDM_CONF:-/etc/gdm/custom.conf}"
HWDB_DIR="${HDLOUNGE_HWDB_DIR:-/etc/udev/hwdb.d}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$1"; }

# state KEY -- what install.sh recorded changing outside $HOME, if anything.
state() { grep -m1 "^$1=" "$STATE_FILE" 2>/dev/null | cut -d= -f2- || true; }

say "Stopping and disabling the service"
systemctl --user disable --now hdlounge.service 2>/dev/null || true
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/hdlounge.service"
systemctl --user daemon-reload 2>/dev/null || true

say "Disabling extension"
gnome-extensions disable "$UUID" 2>/dev/null || true
python3 - "$UUID" <<'PY' 2>/dev/null || true
import sys
from gi.repository import Gio
uuid = sys.argv[1]
settings = Gio.Settings.new("org.gnome.shell")
enabled = [e for e in settings.get_strv("enabled-extensions") if e != uuid]
settings.set_strv("enabled-extensions", enabled)
Gio.Settings.sync()
PY

say "Removing files"
rm -rf "$HOME/.local/share/gnome-shell/extensions/$UUID"
rm -rf "$PREFIX/share/hdlounge"
rm -f  "$PREFIX/bin/hdlounge"
rm -f  "$PREFIX/share/applications/ca.harmonicdesign.HDLounge.desktop"
rm -f  "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/ca.harmonicdesign.HDLounge.desktop"
# Website tiles are HDLounge's own launcher entries; left behind they would sit in
# GNOME's app grid as bare browser windows.
rm -f  "${XDG_DATA_HOME:-$HOME/.local/share}"/applications/hdlounge-web-*.desktop
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/hdlounge-websites"

say "Restoring GNOME settings"
gsettings reset org.gnome.desktop.interface enable-hot-corners || true
gsettings reset org.gnome.desktop.session idle-delay || true
gsettings reset org.gnome.desktop.screensaver lock-enabled || true
gsettings reset org.gnome.desktop.screensaver idle-activation-enabled || true
gsettings reset org.gnome.desktop.lockdown disable-lock-screen || true
gsettings reset org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type || true
gsettings reset org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type || true
gsettings reset org.gnome.shell welcome-dialog-last-shown-version || true

# System changes
# -------------------------------------------------------

# Everything outside $HOME needs a password
hwdb_files=()
for f in "$HWDB_DIR/$HWDB_FILE" "$HWDB_DIR"/61-hdlounge-remote-*.hwdb; do
  [ -f "$f" ] && hwdb_files+=("$f")
done
AUTOLOGIN_USER="$(state autologin)"
SSHD_STATE="$(state sshd)"

if [ ${#hwdb_files[@]} -gt 0 ] || [ -n "$AUTOLOGIN_USER" ] ||
   [ "$SSHD_STATE" = "enabled-by-hdlounge" ]; then
  say "Undoing the system changes the installer made (needs your password)"
  if ! command -v sudo >/dev/null 2>&1 || ! { sudo -n true 2>/dev/null || sudo -v; }; then
    warn "Could not get sudo. Undo these as root:"
    if [ ${#hwdb_files[@]} -gt 0 ]; then
      echo "   rm ${hwdb_files[*]} && systemd-hwdb update"
    fi
    if [ -n "$AUTOLOGIN_USER" ]; then
      echo "   remove the AutomaticLogin lines from [daemon] in $GDM_CONF"
    fi
    if [ "$SSHD_STATE" = "enabled-by-hdlounge" ]; then
      echo "   systemctl disable sshd"
    fi
  else
    if [ ${#hwdb_files[@]} -gt 0 ]; then
      if sudo rm -f "${hwdb_files[@]}" &&
         sudo systemd-hwdb update &&
         sudo udevadm trigger --subsystem-match=input --action=change; then
        echo "   Remote key mappings removed; the buttons go back to their stock keycodes."
      else
        warn "Could not remove ${hwdb_files[*]}"
      fi
    fi

    if [ -n "$AUTOLOGIN_USER" ]; then
      if sudo python3 - "$GDM_CONF" "$AUTOLOGIN_USER" <<'GDM'
import os
import sys

path, user = sys.argv[1], sys.argv[2]
if not os.path.exists(path):
    sys.exit(0)
with open(path, encoding="utf-8") as fh:
    lines = fh.read().splitlines()

keys = ("AutomaticLoginEnable", "AutomaticLogin")
section, current = None, None
for line in lines:
    s = line.strip()
    if s.startswith("[") and s.endswith("]"):
        section = s[1:-1].strip()
    elif section == "daemon" and s.split("=", 1)[0].strip() == "AutomaticLogin":
        current = s.split("=", 1)[1].strip()

# Someone has pointed it at another user since; that is not ours to undo.
if current not in (None, user):
    print(f"   Automatic login now belongs to {current}; left alone.")
    sys.exit(0)

out, section = [], None
for line in lines:
    s = line.strip()
    if s.startswith("[") and s.endswith("]"):
        section = s[1:-1].strip()
    elif section == "daemon" and "=" in s and s.split("=", 1)[0].strip() in keys:
        continue
    out.append(line)

tmp = path + ".hdlounge-tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
os.chmod(tmp, 0o644)
os.replace(tmp, path)
print("   Automatic login turned off.")
GDM
      then
        :
      else
        warn "Could not update $GDM_CONF; turn off automatic login in Settings > Users."
      fi
    fi

    if [ "$SSHD_STATE" = "enabled-by-hdlounge" ]; then
      # Disable, but do not stop: this may well be running over SSH.
      if sudo systemctl disable sshd 2>/dev/null; then
        echo "   SSH will not start at the next boot (it keeps running until then)."
      else
        warn "Could not disable sshd."
      fi
    fi
  fi
fi

rm -f "$STATE_FILE"

echo
echo "  Removed. Config kept at ~/.config/hdlounge/ -- delete it by hand if you want."
echo "  A paired TV is left switched on, and its pairing key stays in that file."
echo "  Log out and back in to fully unload the extension."
echo
