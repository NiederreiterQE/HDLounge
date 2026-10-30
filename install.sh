#!/usr/bin/env bash
# Install HDLounge: the launcher app + its GNOME Shell extension.
# Reworked for Ubuntu / APT by NiederreiterQE.
set -euo pipefail

# Vars
# -------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC="$REPO_ROOT/assets/app"
# set -u would stop dead in a shell that never exported it.
USER="${USER:-$(id -un)}"
UUID="hdlounge@harmonicdesign.ca"
HWDB_FILE="61-nvidia-shield-remote.hwdb"
SHIELD_REMOTE="0955:7217"
GSCONNECT_UUID="gsconnect@andyholmes.github.io"
GSCONNECT_APT="gnome-shell-extension-gsconnect"
UBUNTU_DOCK_UUID="ubuntu-dock@ubuntu.com"
CHROME_DEB_URL="https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"

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

# System paths. Overridable only so the installer can be tested without root.
GDM_CONF="${HDLOUNGE_GDM_CONF:-/etc/gdm3/custom.conf}"
HWDB_DIR="${HDLOUNGE_HWDB_DIR:-/etc/udev/hwdb.d}"
INPUT_DEVICES="${HDLOUNGE_INPUT_DEVICES:-/proc/bus/input/devices}"

# Set to 1 once sudo has been obtained (see "Sudo" below).
SUDO_OK=0

# Helpers
# -------------------------------------------------------
say() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$1"; }

# ask VAR PROMPT -- VAR=1/y/yes answers yes, VAR=0/n/no answers no,
# otherwise ask on the terminal (default: no).
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

# names_of FIELD ENTRY... -- one field of each "a|b|c" entry, comma-separated.
names_of() {
  local field="$1" entry out=""
  shift
  for entry in "$@"; do
    out+="${out:+, }$(cut -d'|' -f"$field" <<<"$entry")"
  done
  echo "${out:-none}"
}

# True if ufw exists, sudo is available and the firewall is active.
ufw_active() {
  command -v ufw >/dev/null 2>&1 &&
    [ "$SUDO_OK" = 1 ] &&
    sudo -n ufw status 2>/dev/null | grep -q "Status: active"
}

# Confirmation
# -------------------------------------------------------
if [ "${HDLOUNGE_YES:-0}" != "1" ] && [ -t 0 ]; then
  cat <<'WARN'
This installs HDLounge as the desktop for THIS machine. It will:

  * install a GNOME Shell extension and enable it
  * enable KIOSK MODE -- hides the top panel and the Activities overview
  * switch off the Ubuntu Dock, if it is active
  * autostart the launcher on login
  * disable the hot corner and GNOME's welcome dialog

Then it asks some questions, all optional: Bluetooth TV remote, sleep and
the lock screen, automatic login, SSH, a phone as a remote (GSConnect),
and website tiles for streaming services (with Google Chrome for DRM).

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

# Dependencies (Ubuntu / APT)
# -------------------------------------------------------
APT_PACKAGES=(
  python3-gi
  python3-gi-cairo
  gir1.2-gtk-4.0
  gir1.2-adw-1
  libglib2.0-bin
  wireplumber
  pulseaudio-utils
  bluez
  curl
)

missing=()

check_deps() {
  missing=()
  for cmd in python3 gnome-shell glib-compile-schemas wpctl pactl bluetoothctl curl; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  python3 -c '
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
' 2>/dev/null || missing+=("Python GTK4 / libadwaita")
}

say "Checking dependencies"
check_deps

if [ ${#missing[@]} -gt 0 ]; then
  warn "Missing: ${missing[*]}"
  if command -v apt-get >/dev/null 2>&1 &&
     ask HDLOUNGE_APT "Install missing dependencies with APT (needs your password)?"; then
    sudo apt-get update || warn "apt-get update failed; trying to install anyway."
    sudo apt-get install -y "${APT_PACKAGES[@]}" || warn "apt-get install failed."
    check_deps
  fi
  if [ ${#missing[@]} -gt 0 ]; then
    echo "   Still missing: ${missing[*]}"
    echo "   Install with:"
    echo "   sudo apt-get install ${APT_PACKAGES[*]}"
    exit 1
  fi
fi

# Ensure extension is compatible with the running shell version
shell_major="$(gnome-shell --version 2>/dev/null | grep -oE '[0-9]+' | head -n1)" || shell_major=""
if [ -n "$shell_major" ] &&
   ! python3 - "$REPO_ROOT/shell-extension/$UUID/metadata.json" "$shell_major" <<'PY'
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
  echo "   (sudo apt-get install gir1.2-manette-0.2)"
fi

# A launcher already running
# -------------------------------------------------------
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
  host="$(PYTHONPATH="$APP_SRC" python3 - "${HDLOUNGE_TV_HOST:-}" 3<&0 4>&1 >&2 <<'TV'
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
WANT_CHROME=0
WANT_REBOOT=0
WEBSITES=()       # "name|address" of each website tile to add

BLANK_MINUTES="${HDLOUNGE_BLANK_MINUTES:-30}"
case "$BLANK_MINUTES" in
  ''|*[!0-9]*) BLANK_MINUTES=30 ;;
esac

# Pairing needs someone holding the remote, so it happens now, not later.
if [ -t 0 ] && ask HDLOUNGE_PAIR "Pair a Bluetooth TV remote now? (Also picks up one that is already paired.)"; then
  pair_remote
fi

# A TV remote's OK and Menu buttons report HID usages that map to keycodes.
# Also stops the remote's power button from suspending the machine.
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

# Most PCs have no HDMI-CEC, so without this the TV stays lit on a blank screen.
EXISTING_TV="$(configured_tv)" || EXISTING_TV=""
if [ -n "$EXISTING_TV" ] && [ -z "${HDLOUNGE_TV_HOST:-}" ]; then
  # Paired already; pairing again would put a second prompt on the TV.
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
# pairs with the TV and then never touches it again.
if [ -n "$TV_HOST" ] && [ "$BLANK_MINUTES" -eq 0 ]; then
  warn "Nothing blanks the screen, so nothing will ever switch the TV off."
  echo "   Re-run with HDLOUNGE_BLANK_MINUTES set to the number of idle minutes"
  echo "   you want."
fi

ask HDLOUNGE_AUTOLOGIN "Log in automatically at startup, straight into HDLounge?" && WANT_AUTOLOGIN=1
ask HDLOUNGE_SSH "Turn on SSH, so this box can be fixed from another computer?" && WANT_SSH=1
ask HDLOUNGE_GSCONNECT "Install GSConnect, so a phone can be the keyboard and remote?" && WANT_GSCONNECT=1

# Website tiles -- mainly for streaming services
mapfile -t WEB_PRESETS < <(PYTHONPATH="$APP_SRC" python3 -c '
from hdlounge.webapps import PRESETS
for name, url in PRESETS:
    print(f"{name}|{url}")
' 2>/dev/null)

if [ ${#WEB_PRESETS[@]} -gt 0 ] &&
   ask HDLOUNGE_WEBSITES "Add website tiles for streaming services ($(names_of 1 "${WEB_PRESETS[@]}"))?"; then
  for site in "${WEB_PRESETS[@]}"; do
    site_name="${site%%|*}"
    # HDLOUNGE_WEB_NETFLIX, HDLOUNGE_WEB_PRIMEVIDEO, ...
    site_key="$(tr -cd '[:alnum:]' <<<"$site_name" | tr '[:lower:]' '[:upper:]')"
    ask "HDLOUNGE_WEB_$site_key" "Add $site_name?" && WEBSITES+=("$site")
  done

  if [ ${#WEBSITES[@]} -gt 0 ]; then
    # Asked of the launcher's own code, so whatever it can find counts.
    browser_kind="$(PYTHONPATH="$APP_SRC" python3 -c '
from hdlounge import webapps
print("drm" if webapps.browser(drm=True) else "plain" if webapps.browser() else "none")
' 2>/dev/null)" || browser_kind="none"

    if [ "$browser_kind" != drm ]; then
      # Netflix and friends need Widevine, which Google Chrome ships.
      if [ "$(dpkg --print-architecture 2>/dev/null)" = amd64 ] &&
         ask HDLOUNGE_CHROME "Install Google Chrome (.deb from Google), so Netflix and the like can play?"; then
        WANT_CHROME=1
      elif [ "$browser_kind" = plain ]; then
        warn "Without Google Chrome the pages load, but Netflix and the like will not play."
      else
        warn "There is no supported browser for website tiles; skipping them."
        WEBSITES=()
      fi
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
  "Google Chrome" "$(yes_no "$WANT_CHROME")" \
  "Website tiles" "$(names_of 1 "${WEBSITES[@]}")" \
  "Restart at the end" "$(yes_no "$WANT_REBOOT")"

if [ "$WANT_AUTOLOGIN" = 1 ]; then
  echo
  warn "Automatic login leaves the login keyring locked, so Chrome and other"
  echo "   apps will ask for your password to unlock it. To stop that, open"
  echo "   Passwords and Keys (org.gnome.seahorse.Application), right-click"
  echo "   Login, choose Change Password and leave the new one blank. Saved"
  echo "   passwords are then stored unencrypted on this machine."
fi

# Sudo
# -------------------------------------------------------
# Asked for once, here, and kept fresh -- so a password prompt never sits
# waiting half an hour into the downloads.
if [ "$WANT_REMOTE" = 1 ] || [ "$WANT_AUTOLOGIN" = 1 ] || [ "$WANT_SSH" = 1 ] ||
   [ "$WANT_GSCONNECT" = 1 ] || [ "$WANT_CHROME" = 1 ]; then
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
cp -r "$APP_SRC/hdlounge" "$APP_DIR/"
cp "$APP_SRC/hdlounge-launcher" "$APP_DIR/"
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
cp -r "$REPO_ROOT/shell-extension/$UUID/." "$EXT_DIR/"
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
WantedBy=graphical-session.target
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

# The Ubuntu Dock would stay on screen in kiosk mode.
if gnome-extensions list --enabled 2>/dev/null | grep -qx "$UBUNTU_DOCK_UUID"; then
  if gnome-extensions disable "$UBUNTU_DOCK_UUID" 2>/dev/null; then
    record ubuntu-dock disabled-by-hdlounge
    echo "   Ubuntu Dock switched off (uninstall.sh switches it back on)."
  else
    warn "Could not switch off the Ubuntu Dock; do it with:"
    echo "   gnome-extensions disable $UBUNTU_DOCK_UUID"
  fi
fi

# Bluetooth remote
# -------------------------------------------------------
hwdb_reload() {
  sudo systemd-hwdb update &&
    sudo udevadm trigger --subsystem-match=input --action=change
}

if [ "$WANT_REMOTE" = 1 ]; then
  if [ "$SUDO_OK" != 1 ]; then
    warn "Skipped the remote key mapping (needs sudo). As root:"
    echo "   cp $REPO_ROOT/$HWDB_FILE $HWDB_DIR/ && systemd-hwdb update"
  elif [ ! -f "$REPO_ROOT/$HWDB_FILE" ]; then
    warn "$HWDB_FILE is not next to this script; skipping the remote key mapping."
  else
    say "Installing the remote key mapping"
    mapped=0
    if sudo install -D -m 0644 "$REPO_ROOT/$HWDB_FILE" "$HWDB_DIR/$HWDB_FILE"; then
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

# SSH
# -------------------------------------------------------
# Ubuntu 22.10+ starts sshd through ssh.socket; older releases use ssh.service.
# Record only the units that this script actually turned on, for uninstall.sh.
ssh_installed() {
  dpkg-query -W -f='${db:Status-Status}' openssh-server 2>/dev/null | grep -qx installed
}

if [ "$WANT_SSH" = 1 ]; then
  if [ "$SUDO_OK" != 1 ]; then
    warn "Skipped SSH (needs sudo). Install openssh-server, then: sudo systemctl enable --now ssh"
  else
    say "Turning on SSH"

    # Capture the state before installing openssh-server: the package may
    # switch units on by itself.
    was_socket="$(systemctl is-enabled ssh.socket 2>/dev/null || true)"
    was_service="$(systemctl is-enabled ssh.service 2>/dev/null || true)"

    if ! ssh_installed; then
      sudo apt-get install -y openssh-server || warn "Could not install openssh-server. SSH remains disabled."
    fi

    if ssh_installed; then
      if systemctl cat ssh.socket >/dev/null 2>&1; then
        ssh_unit="ssh.socket"
      else
        ssh_unit="ssh.service"
      fi

      if sudo systemctl enable --now "$ssh_unit"; then
        if [ "$(systemctl is-enabled ssh.socket 2>/dev/null || true)" = enabled ] &&
           [ "$was_socket" != enabled ]; then
          record ssh_socket enabled-by-hdlounge
        fi
        if [ "$(systemctl is-enabled ssh.service 2>/dev/null || true)" = enabled ] &&
           [ "$was_service" != enabled ]; then
          record ssh enabled-by-hdlounge
        fi

        address="$(ip -4 -o addr show scope global 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4; exit}')" || address=""
        echo "   From another computer:  ssh $USER@${HOSTNAME:-$(uname -n)}${address:+   (or ssh $USER@$address)}"
        echo "   Tip: copy a key over with ssh-copy-id and use that instead of a password."

        if ufw_active && ! sudo -n ufw status 2>/dev/null | grep -Eq 'OpenSSH|^22(/tcp)?[[:space:]]'; then
          warn "The firewall (ufw) does not allow SSH. Open it with:"
          echo "   sudo ufw allow OpenSSH"
        fi
      else
        warn "Could not start the SSH service."
      fi
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
  local status
  status="$(sudo -n ufw status 2>/dev/null)" || return 1
  grep -Eq '^1714:1764/tcp[[:space:]]+ALLOW' <<<"$status" &&
    grep -Eq '^1714:1764/udp[[:space:]]+ALLOW' <<<"$status"
}

# First choice: the Ubuntu package. Fallback: the build from extensions.gnome.org.
install_gsconnect() {
  local major url zip

  if [ "$SUDO_OK" = 1 ] && sudo apt-get install -y "$GSCONNECT_APT"; then
    return 0
  fi

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
    if ufw_active && ! kdeconnect_port_open; then
      warn "The firewall (ufw) may block GSConnect. Open its ports with:"
      echo "   sudo ufw allow 1714:1764/tcp"
      echo "   sudo ufw allow 1714:1764/udp"
    fi
  else
    WANT_GSCONNECT=0
    warn "Could not install GSConnect; get it from extensions.gnome.org later."
  fi
fi

# Google Chrome (for DRM video in the website tiles)
# -------------------------------------------------------
install_chrome() {
  local deb
  deb="$(mktemp --suffix=.deb)"
  chmod 644 "$deb"
  if curl -fsSL --max-time 600 -o "$deb" "$CHROME_DEB_URL" &&
     sudo apt-get install -y "$deb"; then
    rm -f "$deb"
    return 0
  fi
  rm -f "$deb"
  return 1
}

if [ "$WANT_CHROME" = 1 ]; then
  if [ "$SUDO_OK" != 1 ]; then
    warn "Skipped Google Chrome (needs sudo). Install it from google.com/chrome."
  else
    say "Installing Google Chrome (this can take a while)"
    if install_chrome; then
      echo "   Installed."
    else
      warn "Could not install Google Chrome; the website tiles will not play DRM video."
    fi
  fi
fi

# Website tiles
# -------------------------------------------------------
add_websites() {
  PYTHONPATH="$APP_DIR" \
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
