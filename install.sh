#!/usr/bin/env bash
# Install HDLounge: the launcher app + its GNOME Shell extension.
set -euo pipefail

# Vars
# -------------------------------------------------------

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# set -u would stop dead in a shell that never exported it.
USER="${USER:-$(id -un)}"
UUID="hdlounge@harmonicdesign.ca"
HWDB_FILE="61-nvidia-shield-remote.hwdb"
SHIELD_REMOTE="0955:7217"
GSCONNECT_UUID="gsconnect@andyholmes.github.io"

# Offered one at a time, then installed together:
# "flatpak id|name|HDLOUNGE_APP_<key>|pin or nopin|what it is".
FLATPAK_APPS=(
  "tv.kodi.Kodi|Kodi|KODI|pin|media centre for local and network video"
  "rocks.shy.VacuumTube|VacuumTube|VACUUMTUBE|pin|YouTube's TV interface, built for a remote"
  "com.valvesoftware.Steam|Steam|STEAM|pin|games, with Big Picture mode"
  "net.retrodeck.retrodeck|RetroDeck|RETRODECK|pin|retro gaming platform"
)
# In case chrome not installed and user wants website tiles
CHROME_APP="com.google.Chrome|Google Chrome|CHROME|nopin|for the website tiles"

PREFIX="${PREFIX:-$HOME/.local}"
APP_DIR="$PREFIX/share/hdlounge"
BIN_DIR="$PREFIX/bin"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
DESKTOP_DIR="$PREFIX/share/applications"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hdlounge"
# What this script changed outside $HOME, so uninstall.sh undoes it
STATE_FILE="$CONFIG_DIR/installer-state"
# Freshly exported Flatpak desktop files are only visible to GLib if the exports directory is on XDG_DATA_DIRS
FLATPAK_DATA_DIRS="${XDG_DATA_HOME:-$HOME/.local/share}/flatpak/exports/share:/var/lib/flatpak/exports/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
# System paths. Overridable only so the installer can be tested without root.
GDM_CONF="${HDLOUNGE_GDM_CONF:-/etc/gdm/custom.conf}"
HWDB_DIR="${HDLOUNGE_HWDB_DIR:-/etc/udev/hwdb.d}"
INPUT_DEVICES="${HDLOUNGE_INPUT_DEVICES:-/proc/bus/input/devices}"

# Questions
# -------------------------------------------------------
say() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$1"; }

# The Questions
ask() {
  local var="$1" prompt="$2" reply
  case "${!var:-}" in
    1|y|Y|yes|YES) return 0 ;;
    0|n|N|no|NO)   return 1 ;;
  esac
  [ -t 0 ] || return 1
  printf '\033[1;35m??\033[0m %s [y/N] ' "$prompt"
  read -r reply
  case "$reply" in
    [yY]|[yY][eE][sS]) return 0 ;;
    *) return 1 ;;
  esac
}

# record KEY VALUE -- remember a system change for uninstall.sh.
record() {
  mkdir -p "$CONFIG_DIR"
  { grep -v "^$1=" "$STATE_FILE" 2>/dev/null || true; echo "$1=$2"; } > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}

# Confirmation
# -------------------------------------------------------
if [ "${HDLOUNGE_YES:-0}" != "1" ] && [ -t 0 ]; then
  cat <<'WARN'
This installs HDLounge as the desktop for THIS machine. It will:

  * install a GNOME Shell extension and enable it
  * enable KIOSK MODE -- hides the top panel and the Activities overview
  * autostart the launcher on login
  * disable the hot corner and GNOME's welcome dialog

Then it asks some questions, all optional: Bluetooth TV remote, sleep and
the lock screen, automatic login, SSH, a phone as a remote (GSConnect), 
TV apps from Flathub, and website tiles for streaming services.

Intended for a **dedicated TV box**, not a daily-driver desktop.
All of it is reversible with ./uninstall.sh, and kiosk mode can be turned
off at any time (see the README.md).

WARN
  printf 'Continue? [y/N] '
  read -r reply
  case "$reply" in
    [yY]|[yY][eE][sS]) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
  echo
fi

# Dependencies
# -------------------------------------------------------
DNF_PACKAGES=(python3-gobject gtk4 libadwaita wireplumber pulseaudio-utils curl)

check_deps() {
  missing=()
  for cmd in python3 gnome-shell glib-compile-schemas wpctl pactl curl; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  python3 -c '
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
' 2>/dev/null || missing+=("python3-gobject with GTK4 + libadwaita")
}

say "Checking dependencies"
check_deps
if [ ${#missing[@]} -gt 0 ]; then
  warn "Missing: ${missing[*]}"
  if command -v dnf >/dev/null 2>&1 &&
     ask HDLOUNGE_DNF "Install them now with dnf (needs your password)?"; then
    sudo dnf install -y "${DNF_PACKAGES[@]}" || true
    check_deps
  fi
  if [ ${#missing[@]} -gt 0 ]; then
    echo "   Install with:"
    echo "   sudo dnf install ${DNF_PACKAGES[*]}"
    exit 1
  fi
fi

# Ensure extension is compatible with the running shell version
shell_major="$(gnome-shell --version 2>/dev/null | grep -oE '[0-9]+' | head -n1)" || shell_major=""
if [ -n "$shell_major" ] &&
   ! python3 - "$SRC/shell-extension/$UUID/metadata.json" "$shell_major" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    supported = json.load(fh).get("shell-version", [])
sys.exit(0 if sys.argv[2] in supported else 1)
PY
then
  warn "This is GNOME Shell $shell_major, which the HDLounge extension has not been marked"
  echo "   as working with, so GNOME will refuse to load it and the launcher will get"
  echo "   no keyboard. To try it anyway, add \"$shell_major\" to shell-version in"
  echo "   shell-extension/$UUID/metadata.json and run this again."
fi

# Optional: without it everything works except game controllers.
if ! python3 -c 'import gi; gi.require_version("Manette", "0.2")' 2>/dev/null; then
  warn "libmanette is not installed, so game controllers will not function with HDLounge."
  echo "   (sudo dnf install libmanette)"
fi

# A launcher already running
LAUNCHER_PAUSED=0

resume_launcher() {
  [ "$LAUNCHER_PAUSED" = 1 ] || return 0
  LAUNCHER_PAUSED=0
  systemctl --user start hdlounge.service 2>/dev/null || true
}

cleanup() {
  if [ -n "${SUDO_KEEPALIVE:-}" ]; then
    kill "$SUDO_KEEPALIVE" 2>/dev/null || true
  fi
  resume_launcher
}
trap cleanup EXIT

if systemctl --user is-active --quiet hdlounge.service 2>/dev/null; then
  say "Stopping the launcher that is already running"
  systemctl --user stop hdlounge.service 2>/dev/null || true
  LAUNCHER_PAUSED=1
  echo "   It would cover this terminal; it starts again once everything else"
  echo "   is done."
elif pgrep -f "$APP_DIR/hdlounge-launcher" >/dev/null 2>&1; then
  warn "A launcher started by hand is running. Quit it with Ctrl+Q first, or it"
  echo "   will cover this terminal and write its own settings back over the"
  echo "   pins and the TV set up here."
fi

# Bluetooth helpers
# -------------------------------------------------------
PAIRED_MAC=""
PAIRED_NAME=""

# "MAC|name" for every device BlueZ knows about, minus the nameless ones
bt_devices() {
  bluetoothctl devices 2>/dev/null | while read -r kind mac name; do
    [ "$kind" = "Device" ] || continue
    [ -n "$name" ] && [ "$name" != "${mac//:/-}" ] || continue
    printf '%s|%s\n' "$mac" "$name"
  done
}

# bt_has MAC Paired|Connected
bt_has() {
  bluetoothctl info "$1" 2>/dev/null | grep -q "^[[:space:]]*$2: yes"
}

connect_remote() {
  local mac="$1" name="$2"
  say "Pairing $name"
  bt_has "$mac" Paired || bluetoothctl --timeout 30 pair "$mac" >/dev/null 2>&1 || true
  # Trusted is what lets the remote reconnect by itself after a restart.
  bluetoothctl trust "$mac" >/dev/null 2>&1 || true
  bt_has "$mac" Connected || bluetoothctl --timeout 15 connect "$mac" >/dev/null 2>&1 || true
  if bt_has "$mac" Paired && bt_has "$mac" Connected; then
    echo "   Paired, trusted and connected."
    PAIRED_MAC="$mac"
    PAIRED_NAME="$name"
    return 0
  fi
  warn "$name did not finish pairing."
  return 1
}

# Interactive: scan, let the user pick the remote, pair + trust + connect it.
pair_remote() {
  if ! command -v bluetoothctl >/dev/null 2>&1; then
    warn "bluetoothctl is not installed; pair the remote in Settings > Bluetooth."
    return 0
  fi
  if ! bluetoothctl power on >/dev/null 2>&1; then
    warn "Could not turn Bluetooth on -- is there an adapter? Skipping pairing."
    return 0
  fi

  local attempt choice entry i scan_pid devices=()
  for attempt in 1 2; do
    printf '   Put the remote in pairing mode, then press Enter. '
    read -r _
    echo "   Looking for it (20 seconds)..."
    # Keep discovering until the pairing is done: BlueZ forgets a device it
    # has only seen in a scan about half a minute after the scan stops.
    bluetoothctl --timeout 180 scan on </dev/null >/dev/null 2>&1 &
    scan_pid=$!
    sleep 20

    mapfile -t devices < <(bt_devices)
    if [ ${#devices[@]} -eq 0 ]; then
      warn "No Bluetooth devices found."
    else
      i=0
      for entry in "${devices[@]}"; do
        i=$((i + 1))
        printf '   %2d) %s%s\n' "$i" "${entry#*|}" \
          "$(bt_has "${entry%%|*}" Paired && echo '  (already paired)')"
      done
      printf '   Which one is the remote? [1-%d, Enter to skip] ' "$i"
      read -r choice
      if [ -z "$choice" ]; then
        kill "$scan_pid" 2>/dev/null || true
        return 0
      fi
      if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "$i" ]; then
        entry="${devices[choice - 1]}"
        if connect_remote "${entry%%|*}" "${entry#*|}"; then
          kill "$scan_pid" 2>/dev/null || true
          return 0
        fi
      else
        warn "That is not one of the numbers above."
      fi
    fi
    kill "$scan_pid" 2>/dev/null || true

    [ "$attempt" = 2 ] && break
    ask HDLOUNGE_PAIR_RETRY "Try again?" || break
  done
  warn "No remote paired; pair it later in Settings > Bluetooth and re-run this."
}

# "vendor:product" (lower-case hex) of the input device a Bluetooth address
# belongs to, read from the kernel's device list. Empty until it connects.
device_ids() {
  awk -v mac="$(printf '%s' "$1" | tr 'A-F' 'a-f')" '
    BEGIN { RS = "" }
    {
      n = split($0, line, "\n"); ids = ""; mine = 0
      for (i = 1; i <= n; i++) {
        if (line[i] ~ /^I:/) {
          v = line[i]; sub(/.*Vendor=/, "", v); sub(/ .*/, "", v)
          p = line[i]; sub(/.*Product=/, "", p); sub(/ .*/, "", p)
          ids = tolower(v) ":" tolower(p)
        }
        if (tolower(line[i]) == "u: uniq=" mac) mine = 1
      }
      if (mine && ids != "") { print ids; exit }
    }' "$INPUT_DEVICES" 2>/dev/null || true
}

# TV power
# Currently modern LG TVs only. 
# TODO: Figure out how to do with other major brands (Samsung, Sony, Vizio, etc.)
# -------------------------------------------------------
TV_HOST=""

configured_tv() {
  python3 - "$CONFIG_DIR/config.json" <<'CFG'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        tv = json.load(fh).get("tv") or {}
except (OSError, ValueError):
    tv = {}
print(tv["host"] if tv.get("enabled") and tv.get("host") and tv.get("key") else "")
CFG
}

# Interactive: find the TV, pair with it, write it into the launcher's config.
# (webOS shows an accept prompt)
pair_tv() {
  local host
  host="$(PYTHONPATH="$SRC/app" python3 - "${HDLOUNGE_TV_HOST:-}" 3<&0 4>&1 >&2 <<'TV'
import os
import sys

from gi.repository import GLib

from hdlounge import webos
from hdlounge.config import Config

answers = os.fdopen(3)
result = os.fdopen(4, "w")


def out(message):
    print(message, file=sys.stderr, flush=True)


wanted = sys.argv[1] if len(sys.argv) > 1 else ""
loop = GLib.MainLoop()


def pair(host):
    out(f"   Connecting to {host}. Press OK on the TV if it asks to allow HDLounge.")

    def paired(ok, key_or_error, mac):
        if not ok:
            out(f"   The TV did not pair: {key_or_error}")
            loop.quit()
            return
        # The MAC comes from the kernel's neighbour table, which is fresh
        # because we just connected -- but say so explicitly if it is not.
        mac = os.environ.get("HDLOUNGE_TV_MAC") or mac
        Config().set_tv(enabled=True, host=host, mac=mac, key=key_or_error)
        if not mac:
            out("   Paired, but no MAC address was found, so the TV cannot be")
            out("   woken again. Check it is on the same network as this box.")
        print(host, file=result, flush=True)    # the one line the shell reads
        loop.quit()

    webos.pair(host, paired)


def chosen(tvs):
    if not tvs:
        out("   No TV answered. It must be switched on and on this network.")
        loop.quit()
        return
    if len(tvs) == 1:
        pair(tvs[0][0])
        return
    for i, (host, name) in enumerate(tvs, 1):
        out(f"   {i:2d}) {name} ({host})")
    sys.stderr.write(f"   Which one is the TV? [1-{len(tvs)}, Enter to skip] ")
    sys.stderr.flush()
    reply = answers.readline().strip()
    if not reply.isdigit() or not 1 <= int(reply) <= len(tvs):
        loop.quit()
        return
    pair(tvs[int(reply) - 1][0])


if wanted:
    pair(wanted)
else:
    out("   Looking for a TV (a few seconds)...")
    webos.discover(chosen)

loop.run()
TV
)" || host=""

  TV_HOST="$host"
  if [ -n "$TV_HOST" ]; then
    echo "   Paired with $TV_HOST."
    warn "Turn on the TV's \"Mobile TV On\" setting (Quick Start+ on older sets),"
    echo "   or it will switch off and never come back on. On webOS it is under"
    echo "   Settings > General > Devices > TV Management. It is reliable over"
    echo "   Ethernet; over Wi-Fi it depends on the model."
  else
    warn "No TV paired; set one up later from the TV section of quick settings."
  fi
}

# Questions
# -------------------------------------------------------
echo
say "A few questions first."
echo

WANT_REMOTE=0
WANT_NOSLEEP=0
WANT_AUTOLOGIN=0
WANT_SSH=0
WANT_GSCONNECT=0
WANT_REBOOT=0
QUEUE=()          # apps to download
PIN_EXISTING=()   # apps already installed, to pin without downloading again
WEBSITES=()       # "name|address" of each website tile to add

BLANK_MINUTES="${HDLOUNGE_BLANK_MINUTES:-30}"
case "$BLANK_MINUTES" in
  ''|*[!0-9]*) BLANK_MINUTES=30 ;;
esac

HAVE_FLATPAK=0
command -v flatpak >/dev/null 2>&1 && HAVE_FLATPAK=1

# names_of FIELD ENTRY... -- one field of each "a|b|c" entry, comma-separated.
names_of() {
  local field="$1" entry out=""
  shift
  for entry in "$@"; do
    out+="${out:+, }$(cut -d'|' -f"$field" <<<"$entry")"
  done
  echo "${out:-none}"
}

# Pairing needs someone holding the remote, so it happens now, not later.
if [ -t 0 ] && ask HDLOUNGE_PAIR "Pair a Bluetooth TV remote now? (Also picks up one that is already paired.)"; then
  pair_remote
fi

# A TV remote's OK and Menu buttons report HID usages that map to keycodes
# Stop remote power button from suspending the machine since it's easy to accidentally press.
ask HDLOUNGE_REMOTE "Make a TV remote's OK, Menu and Power buttons work in every app? (A key mapping for the NVIDIA SHIELD remote${PAIRED_NAME:+ and $PAIRED_NAME}; Power switches the TV, never the PC.)" &&
  WANT_REMOTE=1

# ask_minutes -- how long before the screen blanks. Only asked in person, and
# only when HDLOUNGE_BLANK_MINUTES has not already said.
ask_minutes() {
  local reply
  [ -z "${HDLOUNGE_BLANK_MINUTES:-}" ] && [ -t 0 ] || return 0
  printf '\033[1;35m??\033[0m %s [%s] ' \
    "Blank the screen after how many idle minutes? (0 never blanks, which an OLED TV can burn in from)" \
    "$BLANK_MINUTES"
  read -r reply
  case "$reply" in
    ''|*[!0-9]*) ;;
    *) BLANK_MINUTES=$((10#$reply)) ;;
  esac
}

# Most PCs have no HDMI-CEC, so without this the TV stays lit on a blank
# screen. Pairing needs the TV's own remote, so it happens now, not later.
EXISTING_TV="$(configured_tv)" || EXISTING_TV=""
if [ -n "$EXISTING_TV" ] && [ -z "${HDLOUNGE_TV_HOST:-}" ]; then
  # Paired already, by an earlier run or from quick settings. Pairing again
  # would put a second "allow HDLounge?" prompt on the television for nothing.
  TV_HOST="$EXISTING_TV"
  say "The TV at $TV_HOST is already set up"
  echo "   (change or forget it under TV in quick settings)"
elif ask HDLOUNGE_TV_POWER "Switch an LG TV off over the network when the screen blanks, and on again when it wakes?"; then
  if [ -t 0 ] || [ -n "${HDLOUNGE_TV_HOST:-}" ]; then
    pair_tv
  else
    warn "Pairing needs someone at the TV; set it up later from the TV section"
    echo "   of quick settings."
  fi
fi

# Sleep, lock and blanking
if [ -n "$TV_HOST" ]; then
  sleep_q="Never sleep or lock?"
  [ "$BLANK_MINUTES" -gt 0 ] &&
    sleep_q="$sleep_q The screen still blanks when idle, and that is what switches the TV off."
elif [ "$BLANK_MINUTES" -gt 0 ]; then
  sleep_q="Never sleep or lock? The screen still blanks when idle, and any button wakes it."
else
  sleep_q="Never sleep, lock or blank the screen?"
fi

if ask HDLOUNGE_NOSLEEP "$sleep_q"; then
  WANT_NOSLEEP=1
  ask_minutes
elif [ -n "$TV_HOST" ]; then
  warn "GNOME may suspend this box when idle, and then nothing can switch the TV"
  echo "   back on. Turn suspend off in Settings > Power if that happens."
fi

# The screen going dark is the trigger, so a box told never to blank
# pairs with the TV and then never touch it again.
if [ -n "$TV_HOST" ] && [ "$BLANK_MINUTES" -eq 0 ]; then
  warn "Nothing blanks the screen, so nothing will ever switch the TV off."
  echo "   Re-run with HDLOUNGE_BLANK_MINUTES set to the number of idle minutes"
  echo "   you want."
fi

ask HDLOUNGE_AUTOLOGIN "Log in automatically at startup, straight into HDLounge?" && WANT_AUTOLOGIN=1
ask HDLOUNGE_SSH "Turn on SSH, so this box can be fixed from another computer?" && WANT_SSH=1
ask HDLOUNGE_GSCONNECT "Install GSConnect, so a phone can be the keyboard and remote?" && WANT_GSCONNECT=1

if ask HDLOUNGE_INSTALL_APPS "Add TV apps to the drawer ($(names_of 2 "${FLATPAK_APPS[@]}"))?"; then
  if [ "$HAVE_FLATPAK" != 1 ]; then
    warn "flatpak is not installed, so there are no apps to add. (sudo dnf install flatpak)"
  else
    for entry in "${FLATPAK_APPS[@]}"; do
      IFS='|' read -r app_id app_name app_key _ app_blurb <<<"$entry"
      # Per-app override, e.g. HDLOUNGE_APP_KODI=1.
      if flatpak info "$app_id" >/dev/null 2>&1; then
        ask "HDLOUNGE_APP_$app_key" "Add $app_name to the drawer? (It is already installed.)" &&
          PIN_EXISTING+=("$entry")
      else
        ask "HDLOUNGE_APP_$app_key" "Install $app_name -- $app_blurb?" && QUEUE+=("$entry")
      fi
    done
  fi
fi

# Website tiles - main;y for streaming services
mapfile -t WEB_PRESETS < <(PYTHONPATH="$SRC/app" python3 -c '
from hdlounge.webapps import PRESETS
for name, url in PRESETS:
    print(f"{name}|{url}")
' 2>/dev/null)
if [ ${#WEB_PRESETS[@]} -gt 0 ] &&
   ask HDLOUNGE_WEBSITES "Add website tiles for streaming services with no Linux app ($(names_of 1 "${WEB_PRESETS[@]}"))?"; then
  for site in "${WEB_PRESETS[@]}"; do
    site_name="${site%%|*}"
    # HDLOUNGE_WEB_NETFLIX, HDLOUNGE_WEB_PRIMEVIDEO, HDLOUNGE_WEB_DISNEY, HDLOUNGE_WEB_CRAVE, ...
    site_key="$(tr -cd '[:alnum:]' <<<"$site_name" | tr '[:lower:]' '[:upper:]')"
    ask "HDLOUNGE_WEB_$site_key" "Add $site_name?" && WEBSITES+=("$site")
  done
  if [ ${#WEBSITES[@]} -gt 0 ]; then
    # Asked of the launcher's own code, so a Chrome installed from Google's
    # RPM repository counts as well as the Flatpak.
    browser_kind="$(PYTHONPATH="$SRC/app" XDG_DATA_DIRS="$FLATPAK_DATA_DIRS" python3 -c '
from hdlounge import webapps
print("drm" if webapps.browser(drm=True) else "plain" if webapps.browser() else "none")
' 2>/dev/null)" || browser_kind="none"
    if [ "$browser_kind" = drm ]; then
      :
    elif [ "$HAVE_FLATPAK" = 1 ]; then
      echo "   Google Chrome will be installed to play them."
      QUEUE+=("$CHROME_APP")
    elif [ "$browser_kind" = plain ]; then
      warn "Without Google Chrome the pages load, but Netflix and the like will not play."
    else
      warn "There is no browser for website tiles and no flatpak to get one; skipping them."
      WEBSITES=()
    fi
  fi
fi

ask HDLOUNGE_REBOOT "Restart the computer when setup is done?" && WANT_REBOOT=1

# Summary
# -------------------------------------------------------
yes_no() {
  if [ "$1" = 1 ]; then echo "yes${2:+ ($2)}"; else echo "no"; fi
}

blank_note=""
[ "$BLANK_MINUTES" -gt 0 ] && blank_note="screen blanks after $BLANK_MINUTES min"

echo
say "Here is the plan"
printf '   %-22s %s\n' \
  "Paired remote" "${PAIRED_NAME:-none}" \
  "Remote key mapping" "$(yes_no "$WANT_REMOTE")" \
  "Never sleep or lock" "$(yes_no "$WANT_NOSLEEP" "$blank_note")" \
  "TV power over IP" "${TV_HOST:-no}" \
  "Automatic login" "$(yes_no "$WANT_AUTOLOGIN")" \
  "SSH" "$(yes_no "$WANT_SSH")" \
  "GSConnect" "$(yes_no "$WANT_GSCONNECT")" \
  "Apps to download" "$(names_of 2 "${QUEUE[@]}")" \
  "Apps to pin" "$(names_of 2 "${PIN_EXISTING[@]}")" \
  "Website tiles" "$(names_of 1 "${WEBSITES[@]}")" \
  "Restart at the end" "$(yes_no "$WANT_REBOOT")"

if [ "$WANT_AUTOLOGIN" = 1 ]; then
  echo
  warn "Automatic login leaves the login keyring locked, so Chrome and apps"
  echo "   like VacuumTube will ask for your password to unlock it. To stop"
  echo "   that, open Passwords and Keys (org.gnome.seahorse.Application),"
  echo "   right-click Login, choose Change Password and leave the new one"
  echo "   blank. Saved passwords are then stored unencrypted on this machine."
fi

# Sudo
# -------------------------------------------------------

# Asked for once, here, and kept fresh -- so a password prompt never sits
# waiting half an hour into the downloads.
SUDO_OK=0
if [ "$WANT_REMOTE" = 1 ] || [ "$WANT_AUTOLOGIN" = 1 ] || [ "$WANT_SSH" = 1 ]; then
  echo
  if ! command -v sudo >/dev/null 2>&1; then
    warn "sudo is not installed; skipping the steps that change system files."
  else
    say "Your password, once, for the steps that change system files"
    if sudo -n true 2>/dev/null || sudo -v; then
      SUDO_OK=1
      # Reaped by cleanup(), the EXIT trap set further up.
      (while kill -0 "$$" 2>/dev/null; do sudo -n true 2>/dev/null; sleep 50; done) &
      SUDO_KEEPALIVE=$!
    else
      warn "No sudo; skipping the steps that change system files."
    fi
  fi
fi

echo

# The app
# -------------------------------------------------------

say "Installing launcher to $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
cp -r "$SRC/app/hdlounge" "$APP_DIR/"
cp "$SRC/app/hdlounge-launcher" "$APP_DIR/"
chmod +x "$APP_DIR/hdlounge-launcher"
# Don't ship stale bytecode from the source tree.
find "$APP_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

ln -sf "$APP_DIR/hdlounge-launcher" "$BIN_DIR/hdlounge"

cat > "$DESKTOP_DIR/ca.harmonicdesign.HDLounge.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=HDLounge
Comment=Fullscreen TV launcher
Exec=$APP_DIR/hdlounge-launcher
Icon=video-display
Terminal=false
Categories=System;
StartupNotify=true
StartupWMClass=ca.harmonicdesign.HDLounge
DESKTOP

# The extension
# -------------------------------------------------------
say "Installing shell extension to $EXT_DIR"
rm -rf "$EXT_DIR"
mkdir -p "$EXT_DIR"
cp -r "$SRC/shell-extension/$UUID/." "$EXT_DIR/"
glib-compile-schemas "$EXT_DIR/schemas"

# Autostart
# -------------------------------------------------------
if [ "${HDLOUNGE_AUTOSTART:-1}" = "1" ]; then
  say "Installing systemd user service"
  mkdir -p "$SYSTEMD_DIR"
  cat > "$SYSTEMD_DIR/hdlounge.service" <<UNIT
[Unit]
Description=HDLounge launcher
Documentation=https://github.com/harm0nic/hdlounge
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStart=$APP_DIR/hdlounge-launcher
Restart=on-failure
RestartSec=2
Slice=session.slice

[Install]
WantedBy=gnome-session.target
UNIT

  # An older install may have left an XDG autostart entry
  rm -f "$AUTOSTART_DIR/ca.harmonicdesign.HDLounge.desktop"

  systemctl --user daemon-reload 2>/dev/null || true
  if systemctl --user enable hdlounge.service 2>/dev/null; then
    echo "   Enabled: starts automatically on login."
  else
    warn "Could not enable hdlounge.service; start it by hand with:"
    echo "   systemctl --user enable --now hdlounge.service"
  fi
else
  say "Skipping autostart (HDLOUNGE_AUTOSTART=0)"
fi

# Gnome bits
# -------------------------------------------------------

say "Applying GNOME settings"
gsettings set org.gnome.desktop.interface enable-hot-corners false || true
# GNOME Tour pops up on a fresh profile and steals keyboard focus from the
# launcher, which leaves the TV looking unresponsive.
gsettings set org.gnome.shell welcome-dialog-last-shown-version '99.0' || true

# Bluetooth remote
# -------------------------------------------------------
hwdb_reload() {
  sudo systemd-hwdb update &&
    sudo udevadm trigger --subsystem-match=input --action=change
}

if [ "$WANT_REMOTE" = 1 ]; then
  if [ "$SUDO_OK" != 1 ]; then
    warn "Skipped the remote key mapping (needs sudo). As root:"
    echo "   cp $SRC/$HWDB_FILE /etc/udev/hwdb.d/ && systemd-hwdb update"
  elif [ ! -f "$SRC/$HWDB_FILE" ]; then
    warn "$HWDB_FILE is not next to this script; skipping the remote key mapping."
  else
    say "Installing the remote key mapping"
    mapped=0
    if sudo install -D -m 0644 "$SRC/$HWDB_FILE" "$HWDB_DIR/$HWDB_FILE"; then
      mapped=1
      echo "   NVIDIA SHIELD remote: OK -> Enter, Menu -> Menu, Power -> TV on/off."
    fi

    # Most Android TV-style remotes send the same HID usages as the SHIELD
    if [ -n "$PAIRED_MAC" ]; then
      ids=""
      for _ in 1 2 3 4 5; do
        ids="$(device_ids "$PAIRED_MAC")"
        [ -n "$ids" ] && break
        sleep 1
      done
      if [ -z "$ids" ]; then
        warn "$PAIRED_NAME has not appeared as an input device, so only the SHIELD mapping applies."
      elif [ "$ids" != "$SHIELD_REMOTE" ]; then
        vendor="$(tr 'a-f' 'A-F' <<<"${ids%%:*}")"
        product="$(tr 'a-f' 'A-F' <<<"${ids##*:}")"
        remote_file="$HWDB_DIR/61-hdlounge-remote-${ids%%:*}-${ids##*:}.hwdb"
        if sudo tee "$remote_file" >/dev/null <<HWDB
# Written by the HDLounge installer for "$PAIRED_NAME" ($PAIRED_MAC).
#
# OK ("Menu Pick") -> Enter, Menu -> the PC Menu key, and Power -> KEY_PROG1,
# which nothing in GNOME acts on and the HDLounge extension binds to switch the
# television off and on.
evdev:input:b0005v${vendor}p${product}*
 KEYBOARD_KEY_c0041=enter
 KEYBOARD_KEY_c0040=compose
 KEYBOARD_KEY_c0030=prog1
 KEYBOARD_KEY_10081=prog1
HWDB
        then
          mapped=1
          echo "   $PAIRED_NAME: OK -> Enter, Menu -> Menu, Power -> TV on/off."
        fi
      fi
    fi

    if [ "$mapped" = 1 ] && hwdb_reload; then
      echo "   Press a button on the remote (or reconnect it) for this to take effect."
    else
      warn "Could not install the remote mapping; the launcher works without it."
    fi
  fi
fi

# Sleep and lock
# -------------------------------------------------------
if [ "$WANT_NOSLEEP" = 1 ]; then
  # The power plugin keys are what actually suspend the machine; they are
  # separate from the screensaver's.
  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing || true
  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type nothing || true
  gsettings set org.gnome.desktop.screensaver lock-enabled false || true
  gsettings set org.gnome.desktop.lockdown disable-lock-screen true || true

  if [ "$BLANK_MINUTES" -gt 0 ]; then
    say "Disabling sleep and the lock screen; the screen blanks after $BLANK_MINUTES idle minutes"
    gsettings set org.gnome.desktop.session idle-delay "$((BLANK_MINUTES * 60))" || true
    gsettings reset org.gnome.desktop.screensaver idle-activation-enabled || true
    echo "   The session never sleeps or locks; the screen blanks when idle."
  else
    say "Disabling sleep, screen blanking and the lock screen"
    # idle-delay 0 is "never blank".
    gsettings set org.gnome.desktop.session idle-delay 0 || true
    gsettings set org.gnome.desktop.screensaver idle-activation-enabled false || true
    echo "   The screen stays on and the session never locks."
  fi
fi

# Automatic login
# -------------------------------------------------------
# With the lock screen off, a restart or a power cut still stops at GDM
if [ "$WANT_AUTOLOGIN" = 1 ]; then
  if [ "$SUDO_OK" != 1 ]; then
    warn "Skipped automatic login (needs sudo). As root, add to [daemon] in $GDM_CONF:"
    echo "   AutomaticLoginEnable=True"
    echo "   AutomaticLogin=$USER"
  else
    say "Turning on automatic login for $USER"
    if sudo python3 - "$GDM_CONF" "$USER" <<'GDM'
import os
import shutil
import sys

path, user = sys.argv[1], sys.argv[2]
want = {"AutomaticLoginEnable": "True", "AutomaticLogin": user}

lines = []
if os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    backup = path + ".hdlounge-bak"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)

out, section, wrote = [], None, False
for line in lines:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        section = stripped[1:-1].strip()
        out.append(line)
        if section == "daemon" and not wrote:
            out += [f"{k}={v}" for k, v in want.items()]
            wrote = True
        continue
    key = stripped.split("=", 1)[0].strip()
    if section == "daemon" and "=" in stripped and key in want:
        continue        # replaced right under the section header
    out.append(line)

if not wrote:
    out += ["", "[daemon]"] + [f"{k}={v}" for k, v in want.items()]

os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".hdlounge-tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
os.chmod(tmp, 0o644)
os.replace(tmp, path)
GDM
    then
      record autologin "$USER"
      echo "   The next start goes straight to HDLounge."
    else
      warn "Could not update $GDM_CONF; turn on automatic login in Settings > Users."
    fi
  fi
fi

# Ssh
# -------------------------------------------------------
if [ "$WANT_SSH" = 1 ]; then
  if [ "$SUDO_OK" != 1 ]; then
    warn "Skipped SSH (needs sudo). As root: systemctl enable --now sshd"
  else
    say "Turning on SSH"
    was_enabled="$(systemctl is-enabled sshd 2>/dev/null || true)"
    if sudo systemctl enable --now sshd; then
      # Only undo on uninstall what this script did.
      [ "$was_enabled" = "enabled" ] || record sshd enabled-by-hdlounge
      address="$(ip -4 -o addr show scope global 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4; exit}')" || address=""
      echo "   From another computer:  ssh $USER@${HOSTNAME:-$(uname -n)}${address:+   (or ssh $USER@$address)}"
      echo "   Tip: copy a key over with ssh-copy-id and use that instead of a password."
      if command -v firewall-cmd >/dev/null 2>&1 &&
         firewall-cmd --state >/dev/null 2>&1 &&
         ! firewall-cmd --query-service=ssh >/dev/null 2>&1; then
        warn "The firewall does not allow SSH. Open it with:"
        echo "   sudo firewall-cmd --permanent --add-service=ssh && sudo firewall-cmd --reload"
      fi
    else
      warn "Could not start sshd; is openssh-server installed? (sudo dnf install openssh-server)"
    fi
  fi
fi

# Extensions
# -------------------------------------------------------
enable_extension() {
  python3 - "$1" <<'PY'
import sys
from gi.repository import Gio

uuid = sys.argv[1]
settings = Gio.Settings.new("org.gnome.shell")
enabled = list(settings.get_strv("enabled-extensions"))
if uuid not in enabled:
    enabled.append(uuid)
    settings.set_strv("enabled-extensions", enabled)
settings.set_boolean("disable-user-extensions", False)
Gio.Settings.sync()
PY
}

# GSConnect talks to the KDE Connect app on the phone on ports 1714-1764.
kdeconnect_port_open() {
  firewall-cmd --query-service=kdeconnect >/dev/null 2>&1 && return 0
  firewall-cmd --list-ports 2>/dev/null | tr ' ' '\n' | awk -F'[-/]' '
    $NF == "tcp" && ($1 == 1716 || (NF == 3 && $1 <= 1716 && $2 >= 1716)) { found = 1 }
    END { exit !found }'
}

install_gsconnect() {
  local major url zip
  major="$(gnome-shell --version | grep -oE '[0-9]+' | head -1)"
  url="$(curl -fsSL --max-time 30 \
           "https://extensions.gnome.org/extension-info/?uuid=$GSCONNECT_UUID&shell_version=$major" |
         python3 -c 'import json, sys; print(json.load(sys.stdin).get("download_url") or "")' \
         2>/dev/null)" || url=""
  if [ -z "$url" ]; then
    warn "No GSConnect release for GNOME Shell $major; skipping."
    return 1
  fi
  zip="$(mktemp --suffix=.zip)"
  if ! curl -fsSL --max-time 120 -o "$zip" "https://extensions.gnome.org$url" ||
     ! gnome-extensions install --force "$zip"; then
    rm -f "$zip"
    return 1
  fi
  rm -f "$zip"
}

if [ "$WANT_GSCONNECT" = 1 ]; then
  say "Installing GSConnect"
  if install_gsconnect && enable_extension "$GSCONNECT_UUID"; then
    echo "   Installed. It starts after the next login."
    if command -v firewall-cmd >/dev/null 2>&1 &&
       firewall-cmd --state >/dev/null 2>&1 && ! kdeconnect_port_open; then
      warn "The firewall blocks GSConnect. Open it with:"
      echo "   sudo firewall-cmd --permanent --add-service=kdeconnect && sudo firewall-cmd --reload"
    fi
  else
    WANT_GSCONNECT=0
    warn "Could not install GSConnect; get it from extensions.gnome.org later."
  fi
fi

# Apps
# Pin a .desktop id into the launcher's drawer
# -------------------------------------------------------

pin_app() {
  PYTHONPATH="$APP_DIR" XDG_DATA_DIRS="$FLATPAK_DATA_DIRS" \
  python3 - "$1" <<'PIN'
import sys
from hdlounge.config import Config

app_id = sys.argv[1]
config = Config()
if config.is_pinned(app_id):
    print("   Already in the drawer.")
else:
    config.pin(app_id)
    print("   Added to the drawer."
          if config.is_pinned(app_id)
          else "   Could not add it to the drawer; add it with the + button.")
PIN
}

for entry in "${PIN_EXISTING[@]}"; do
  IFS='|' read -r app_id app_name _ <<<"$entry"
  say "Adding $app_name to the drawer"
  pin_app "$app_id.desktop" || true
done

if [ ${#QUEUE[@]} -gt 0 ]; then
  flatpak remote-add --if-not-exists --user flathub \
    https://dl.flathub.org/repo/flathub.flatpakrepo >/dev/null 2>&1 || true
fi

# One install per app rather than a single transaction, so one failed
# download does not take the others down with it.
count=0
for entry in "${QUEUE[@]}"; do
  IFS='|' read -r app_id app_name _ app_pin _ <<<"$entry"
  count=$((count + 1))
  say "Installing $app_name from Flathub ($count of ${#QUEUE[@]}, this can take a while)"
  if flatpak install --user -y flathub "$app_id"; then
    if [ "$app_pin" = "pin" ]; then
      pin_app "$app_id.desktop" || true
    fi
  else
    warn "Could not install $app_name; skipping."
  fi
done

add_websites() {
  PYTHONPATH="$APP_DIR" XDG_DATA_DIRS="$FLATPAK_DATA_DIRS" \
  python3 - "$@" <<'WEB'
import sys

from gi.repository import GLib

from hdlounge import webapps
from hdlounge.config import Config

found = webapps.browser()
if found is None:
    print("   No browser to open them in; add them later from + > Add a Website.")
    sys.exit(1)
if not webapps.plays_drm(found[0]):
    print(f"   They open in {found[1]}: the pages load, but DRM video will not play.")

config = Config()
loop = GLib.MainLoop()
waiting = 0


def settled():
    global waiting
    waiting -= 1
    if waiting <= 0:
        loop.quit()


for site in sys.argv[1:]:
    name, url = site.split("|", 1)
    waiting += 1
    app_id = webapps.create(name, url, on_settled=settled)
    if app_id is None:
        waiting -= 1
        print(f"   Could not add {name}.")
        continue
    config.pin(app_id)
    print(f"   {name}: added to the drawer.")

# Each tile's icon comes from its own site.
if waiting > 0:
    GLib.timeout_add_seconds(30, loop.quit)
    loop.run()
WEB
}

if [ ${#WEBSITES[@]} -gt 0 ]; then
  say "Adding website tiles"
  add_websites "${WEBSITES[@]}" || true
fi

# Finish
# -------------------------------------------------------
say "Enabling the extension"
if enable_extension "$UUID"; then
  echo "   Added to enabled-extensions."
else
  warn "Could not write enabled-extensions; enable it by hand:"
  echo "   gnome-extensions enable $UUID"
fi

# Best effort for the current session too (works if the shell already knows it).
gnome-extensions enable "$UUID" 2>/dev/null || true

cat <<'DONE'

  HDLounge is installed.

  On Wayland a shell extension cannot load into a running session, so it
  starts at the next login. After that everything comes up on its own --
  extension enabled, launcher started.

  Verify with:
    gnome-extensions info hdlounge@harmonicdesign.ca   # expect State: ACTIVE
    systemctl --user status hdlounge.service           # expect active (running)

  Run the launcher by hand any time with:  hdlounge

  ---------------------------------------------------------------
  ESCAPE HATCH -- kiosk mode hides the top bar and the overview.
  If you need stock GNOME back:

    gsettings --schemadir ~/.local/share/gnome-shell/extensions/hdlounge@harmonicdesign.ca/schemas \
      set org.gnome.shell.extensions.hdlounge kiosk-mode false

  Or disable the extension entirely:

    gnome-extensions disable hdlounge@harmonicdesign.ca

  You can press ALT+F2 to open up a command runner, SSH in from another machine,
  or use GSConnect to run commands from a phone.

  ---------------------------------------------------------------

DONE

if [ "$WANT_GSCONNECT" = 1 ]; then
  echo "  Phone as a remote: install KDE Connect on the phone, then in HDLounge press"
  echo "  Menu and choose Phone Remote to pair it. Both must be on the same network."
  echo
fi

if [ "$WANT_REBOOT" = 1 ]; then
  for i in 10 9 8 7 6 5 4 3 2 1; do
    printf '\r  Restarting in %2d seconds -- Ctrl+C to stay.' "$i"
    sleep 1
  done
  echo
  systemctl reboot
elif [ "$LAUNCHER_PAUSED" = 1 ]; then
  echo "  Starting the launcher again -- its window will cover this terminal."
  echo
  resume_launcher
else
  echo "  Log out and back in (or restart) to start HDLounge."
  echo
fi