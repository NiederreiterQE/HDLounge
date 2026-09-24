#!/usr/bin/env bash
# Dry-run install.sh and uninstall.sh with every system command stubbed out.
# Nothing is written outside a temporary HOME: no sudo, no Bluetooth, no
# network, no real dconf, no systemd.
set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${HDLOUNGE_OUT:-$SRC/.test/install.log}"
T="$(mktemp -d /tmp/hdlounge-install-test.XXXXXX)"
mkdir -p "$(dirname "$OUT")" "$T/home" "$T/bin" "$T/etc/gdm" "$T/hwdb" "$T/bt"
exec > "$OUT" 2>&1

export HOME="$T/home"
export XDG_CONFIG_HOME="$HOME/.config" XDG_DATA_HOME="$HOME/.local/share"
export XDG_CACHE_HOME="$HOME/.cache" XDG_STATE_HOME="$HOME/.local/state"
export GSETTINGS_BACKEND=memory          # never the real dconf
export HDLOUNGE_GDM_CONF="$T/etc/gdm/custom.conf"
export HDLOUNGE_HWDB_DIR="$T/hwdb"
export HDLOUNGE_INPUT_DEVICES="$T/input-devices"
export STUB_LOG="$T/commands.log" BT="$T/bt"
export PATH="$T/bin:$PATH"

cat > "$HDLOUNGE_GDM_CONF" <<'CONF'
# GDM configuration storage

[daemon]

[security]

[debug]
# Uncomment the line below to turn on debugging
#Enable=true
CONF

cat > "$HDLOUNGE_INPUT_DEVICES" <<'DEVICES'
I: Bus=0003 Vendor=3434 Product=d028 Version=0110
N: Name="Some Keyboard"
U: Uniq=

I: Bus=0005 Vendor=1d5a Product=c081 Version=0001
N: Name="Living Room Remote"
P: Phys=8c:f8:c5:56:a5:f3
U: Uniq=aa:bb:cc:dd:ee:ff
H: Handlers=kbd event20
DEVICES

stub() { cat > "$T/bin/$1"; chmod +x "$T/bin/$1"; }

stub sudo <<'EOF'
#!/usr/bin/env bash
case "$1" in
  -n) exit 1 ;;                                   # never a cached ticket
  -v) echo "PROMPT" >> "$STUB_LOG"; exit "${SUDO_V_EXIT:-0}" ;;
esac
echo "sudo $*" >> "$STUB_LOG"
exec "$@"
EOF

stub sleep <<'EOF'
#!/usr/bin/env bash
whole="${1%.*}"
if [ "${whole:-0}" -ge 5 ] 2>/dev/null; then exec /usr/bin/sleep 0.3; fi
exec /usr/bin/sleep 0.05
EOF

stub bluetoothctl <<'EOF'
#!/usr/bin/env bash
echo "bluetoothctl $*" >> "$STUB_LOG"
[ "$1" = "--timeout" ] && shift 2
case "$1" in
  scan) exec /usr/bin/sleep 1 ;;
  devices)
    printf 'Device 11:22:33:44:55:66 Kitchen Speaker\n'
    printf 'Device 00:11:22:33:44:55 00-11-22-33-44-55\n'
    printf 'Device AA:BB:CC:DD:EE:FF Living Room Remote\n' ;;
  pair|connect|trust) touch "$BT/$1-$2" ;;
  info)
    [ -e "$BT/pair-$2" ] && echo "	Paired: yes" || echo "	Paired: no"
    [ -e "$BT/connect-$2" ] && echo "	Connected: yes" || echo "	Connected: no" ;;
esac
exit 0
EOF

stub systemctl <<'EOF'
#!/usr/bin/env bash
echo "systemctl $*" >> "$STUB_LOG"
case "$*" in
  # HDLOUNGE_ACTIVE_EXIT=0 pretends a launcher is already running.
  *is-active*hdlounge*) exit "${HDLOUNGE_ACTIVE_EXIT:-3}" ;;
  *is-active*) exit 3 ;;
  "is-enabled sshd") echo disabled; exit 1 ;;
esac
exit 0
EOF

for cmd in systemd-hwdb udevadm gsettings gnome-extensions dnf; do
  stub "$cmd" <<'EOF'
#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$STUB_LOG"
exit 0
EOF
done

stub firewall-cmd <<'EOF'
#!/usr/bin/env bash
echo "firewall-cmd $*" >> "$STUB_LOG"
case "$1" in
  --state|--query-service=ssh) exit 0 ;;
  --list-ports) echo "1025-65535/tcp 1025-65535/udp"; exit 0 ;;
esac
exit 1
EOF

stub curl <<'EOF'
#!/usr/bin/env bash
echo "curl $*" >> "$STUB_LOG"
for arg in "$@"; do
  case "$arg" in
    *extension-info*) echo '{"download_url": "/download-extension/gsconnect.zip?version_tag=1"}'; exit 0 ;;
  esac
done
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then echo zip > "$2"; exit 0; fi
  shift
done
exit 0
EOF

stub flatpak <<'EOF'
#!/usr/bin/env bash
echo "flatpak $*" >> "$STUB_LOG"
exports="${XDG_DATA_HOME:-$HOME/.local/share}/flatpak/exports/share/applications"
case "$1" in
  info) [ -f "$exports/$2.desktop" ]; exit ;;
  install)
    id="${!#}"
    mkdir -p "$exports"
    printf '[Desktop Entry]\nType=Application\nName=%s\nExec=true\n' "$id" > "$exports/$id.desktop" ;;
esac
exit 0
EOF

# Steam is already on the machine.
mkdir -p "$XDG_DATA_HOME/flatpak/exports/share/applications"
printf '[Desktop Entry]\nType=Application\nName=Steam\nExec=true\n' \
  > "$XDG_DATA_HOME/flatpak/exports/share/applications/com.valvesoftware.Steam.desktop"

# Checks
# -------------------------------------------------------

PASS=0; FAIL=0
t() { # t "name" command...
  local name="$1"; shift
  if "$@"; then echo "  PASS  $name"; PASS=$((PASS+1));
  else echo "  FAIL  $name"; FAIL=$((FAIL+1)); fi
}
not() { ! "$@"; }
clean() { sed 's/\x1b\[[0-9;]*m//g; s/\r//g' "$1"; }
pinned() { # pinned APP_ID [CONFIG_HOME]
  python3 -c "import json, sys; ids = [a['id'] for a in json.load(open(sys.argv[1]))['apps']]; sys.exit(sys.argv[2] not in ids)" \
    "${2:-$XDG_CONFIG_HOME}/hdlounge/config.json" "$1" 2>/dev/null
}
tv_host() { # tv_host [CONFIG_HOME] -- the TV address the installer stored
  python3 -c "import json, sys; print(json.load(open(sys.argv[1])).get('tv', {}).get('host', ''))" \
    "${1:-$XDG_CONFIG_HOME}/hdlounge/config.json" 2>/dev/null
}
line_of() { clean "$1" | grep -n -- "$2" | cut -d: -f1 | "$3" -1; }
asked() { # asked TRANSCRIPT "prompt" -- how many times it was put to the user.
  grep -o "?? $2" "$1" | wc -l
}

STATE="$XDG_CONFIG_HOME/hdlounge/installer-state"

echo "### 1. interactive install, every option"

printf 'y\ny\n\n2\ny\nn\ny\n\ny\ny\ny\ny\ny\nn\ny\ny\ny\nn\nn\ny\nn\ny\n' |
  python3 -c 'import pty, sys; pty.spawn(sys.argv[1:])' bash "$SRC/install.sh" \
  > "$T/transcript" 2>&1
clean "$T/transcript" > "$T/transcript.txt"

LAST_Q="$(line_of "$T/transcript" '??' tail)"
FIRST_WORK="$(line_of "$T/transcript" '==> Installing launcher' head)"
t "every question comes before any work (last ?? line $LAST_Q, work from $FIRST_WORK)" \
  test -n "$LAST_Q" -a -n "$FIRST_WORK" -a "${LAST_Q:-0}" -lt "${FIRST_WORK:-0}"
t "the password is asked for exactly once" test "$(grep -c '^PROMPT$' "$STUB_LOG")" = 1
t "the summary names the paired remote" grep -Eq "Paired remote +Living Room Remote" "$T/transcript.txt"
t "the key mapping question names the remote just paired" grep -q "SHIELD remote and Living Room Remote" "$T/transcript.txt"
t "the keyring consequence of autologin is spelled out" grep -q "login keyring locked" "$T/transcript.txt"
t "TV power over IP is offered" grep -q "Switch an LG TV off over the network" "$T/transcript.txt"
t "  once, not again after the sleep question" \
  test "$(asked "$T/transcript.txt" "Switch an LG TV off")" = 1
t "  and sleep is asked about exactly once" \
  test "$(asked "$T/transcript.txt" "Never sleep")" = 1
t "  declining it looks for no TV" not grep -q "Looking for a TV" "$T/transcript.txt"
t "  and stores no TV address" test -z "$(tv_host)"
t "  the plan says so" grep -Eq "TV power over IP +no" "$T/transcript.txt"
t "nameless scan results are not offered" not grep -q '00-11-22-33-44-55' "$T/transcript.txt"

t "remote paired, trusted and connected" \
  bash -c "grep -q 'pair AA:BB:CC:DD:EE:FF' '$STUB_LOG' && grep -q 'trust AA:BB:CC:DD:EE:FF' '$STUB_LOG' && grep -q 'connect AA:BB:CC:DD:EE:FF' '$STUB_LOG'"
t "SHIELD keymap installed" test -f "$T/hwdb/61-nvidia-shield-remote.hwdb"
t "  its Power button drives the TV, not the machine" \
  grep -qx ' KEYBOARD_KEY_c0030=prog1' "$T/hwdb/61-nvidia-shield-remote.hwdb"
REMOTE_HWDB="$T/hwdb/61-hdlounge-remote-1d5a-c081.hwdb"
t "a keymap was written for the paired remote" test -f "$REMOTE_HWDB"
t "  matched on its own Bluetooth IDs" grep -qx 'evdev:input:b0005v1D5ApC081\*' "$REMOTE_HWDB"
t "  OK -> Enter and Menu -> Menu" \
  bash -c "grep -qx ' KEYBOARD_KEY_c0041=enter' '$REMOTE_HWDB' && grep -qx ' KEYBOARD_KEY_c0040=compose' '$REMOTE_HWDB'"
t "  its Power button drives the TV, not the machine" \
  bash -c "grep -qx ' KEYBOARD_KEY_c0030=prog1' '$REMOTE_HWDB' && grep -qx ' KEYBOARD_KEY_10081=prog1' '$REMOTE_HWDB'"
t "  and nothing is disabled outright any more" not grep -q 'reserved' "$REMOTE_HWDB"
t "hwdb reloaded once for both" test "$(grep -c '^systemd-hwdb update' "$STUB_LOG")" = 1

t "screen blanks after 30 minutes" grep -q "gsettings set org.gnome.desktop.session idle-delay 1800" "$STUB_LOG"
t "  through the screensaver's own idle activation" grep -q "gsettings reset org.gnome.desktop.screensaver idle-activation-enabled" "$STUB_LOG"
t "  but it never locks" grep -q "gsettings set org.gnome.desktop.screensaver lock-enabled false" "$STUB_LOG"
t "  and never suspends" grep -q "sleep-inactive-ac-type nothing" "$STUB_LOG"

t "autologin keys sit right under [daemon]" \
  test "$(sed -n '/^\[daemon\]$/{n;p;n;p}' "$HDLOUNGE_GDM_CONF" | tr '\n' '|')" = "AutomaticLoginEnable=True|AutomaticLogin=$USER|"
t "  the file's comments survive" grep -q "^# Uncomment the line below" "$HDLOUNGE_GDM_CONF"
t "  the original is backed up" test -f "$HDLOUNGE_GDM_CONF.hdlounge-bak"
t "  recorded for uninstall" grep -qx "autologin=$USER" "$STATE"

t "sshd enabled and started" grep -q "systemctl enable --now sshd" "$STUB_LOG"
t "  recorded as ours to undo" grep -qx "sshd=enabled-by-hdlounge" "$STATE"

t "GSConnect downloaded and installed" grep -q "gnome-extensions install --force" "$STUB_LOG"
t "  from the build for this shell version" grep -q "extension-info/?uuid=gsconnect@andyholmes.github.io&shell_version=[0-9]" "$STUB_LOG"

INSTALLS="$(grep '^flatpak install' "$STUB_LOG")"
t "apps downloaded in order: Kodi, then Chrome" \
  test "$(printf '%s\n' "$INSTALLS" | awk '{print $NF}' | tr '\n' ' ')" = "tv.kodi.Kodi com.google.Chrome "
t "an app already installed is not downloaded again" not grep -q 'install.*com.valvesoftware.Steam' "$STUB_LOG"
t "Steam, already installed, is pinned" pinned com.valvesoftware.Steam.desktop
t "Kodi is pinned once downloaded" pinned tv.kodi.Kodi.desktop
t "Chrome is installed but not pinned" not pinned com.google.Chrome.desktop
APPS_DIR="$XDG_DATA_HOME/applications"
t "website tiles written for the chosen sites only" \
  bash -c "[ -f '$APPS_DIR/hdlounge-web-netflix.desktop' ] && [ -f '$APPS_DIR/hdlounge-web-crave.desktop' ] && [ ! -e '$APPS_DIR/hdlounge-web-prime-video.desktop' ]"
t "  they open in Chrome, on Wayland" \
  grep -qx 'Exec=flatpak run com.google.Chrome --ozone-platform=wayland "--app=https://www.netflix.com/"' "$APPS_DIR/hdlounge-web-netflix.desktop"
t "  Netflix is pinned" pinned hdlounge-web-netflix.desktop
t "  Crave is pinned" pinned hdlounge-web-crave.desktop
t "no launcher to pause, so none is touched" \
  not grep -Eq 'systemctl --user (stop|start|restart) hdlounge' "$STUB_LOG"
t "restarts at the very end" test "$(grep '^systemctl' "$STUB_LOG" | tail -1)" = "systemctl reboot"

echo
echo "### 2. automatic login again: the GDM edit is idempotent"
cp "$HDLOUNGE_GDM_CONF" "$T/gdm-first"
STUB_LOG="$T/commands2.log" HDLOUNGE_YES=1 HDLOUNGE_AUTOLOGIN=1 HDLOUNGE_REMOTE=0 HDLOUNGE_NOSLEEP=0 \
  HDLOUNGE_SSH=0 HDLOUNGE_GSCONNECT=0 HDLOUNGE_INSTALL_APPS=0 HDLOUNGE_REBOOT=0 \
  bash "$SRC/install.sh" </dev/null > "$T/second.txt" 2>&1
t "second run leaves custom.conf byte-for-byte the same" cmp -s "$T/gdm-first" "$HDLOUNGE_GDM_CONF"

echo
echo "### 3. unattended, no sudo: system steps are skipped, not attempted"
STUB_LOG="$T/commands3.log" SUDO_V_EXIT=1 HDLOUNGE_YES=1 HDLOUNGE_REMOTE=1 HDLOUNGE_AUTOLOGIN=1 \
  HDLOUNGE_SSH=1 HDLOUNGE_NOSLEEP=0 HDLOUNGE_GSCONNECT=0 HDLOUNGE_INSTALL_APPS=0 \
  HDLOUNGE_WEBSITES=1 HDLOUNGE_WEB_CBCGEM=1 HDLOUNGE_REBOOT=0 \
  bash "$SRC/install.sh" </dev/null > "$T/third.txt" 2>&1
clean "$T/third.txt" > "$T/third.clean"
t "completes without a terminal" grep -q "HDLounge is installed" "$T/third.clean"
t "no pairing without someone to hold the remote" not grep -q 'pairing mode' "$T/third.clean"
t "keymap skipped, with the command to run" grep -q "Skipped the remote key mapping (needs sudo)" "$T/third.clean"
t "autologin skipped, with what to add" grep -q "Skipped automatic login (needs sudo)" "$T/third.clean"
t "SSH skipped" grep -q "Skipped SSH (needs sudo)" "$T/third.clean"
t "nothing ran under sudo" not grep -q '^sudo ' "$T/commands3.log"
t "HDLOUNGE_WEB_CBCGEM adds the CBC Gem tile, with no apps chosen" pinned hdlounge-web-cbc-gem.desktop
t "  in the Chrome already there, not a second download" not grep -q "flatpak install" "$T/commands3.log"

echo
echo "### 4. uninstall undoes exactly what was recorded"
STUB_LOG="$T/commands-uninstall.log" bash "$SRC/uninstall.sh" </dev/null > "$T/uninstall.txt" 2>&1
t "autologin keys removed" not grep -q '^AutomaticLogin' "$HDLOUNGE_GDM_CONF"
t "  the rest of custom.conf intact" \
  bash -c "grep -q '^# Uncomment the line below' '$HDLOUNGE_GDM_CONF' && grep -qx '\[daemon\]' '$HDLOUNGE_GDM_CONF' && grep -qx '\[security\]' '$HDLOUNGE_GDM_CONF'"
t "both keymaps removed" test -z "$(ls "$T/hwdb")"
t "sshd disabled but not stopped (this may be an SSH session)" \
  bash -c "grep -qx 'systemctl disable sshd' '$T/commands-uninstall.log' && ! grep -q 'disable --now sshd' '$T/commands-uninstall.log'"
t "password asked once" test "$(grep -c '^PROMPT$' "$T/commands-uninstall.log")" = 1
t "installer state forgotten" test ! -e "$STATE"

echo
echo "### 5. saying no to TV apps still offers website tiles"
H5="$T/home5"
mkdir -p "$H5"

printf 'y\nn\nn\nn\nn\nn\nn\nn\nn\ny\ny\nn\nn\nn\nn\nn\n' |
  env HOME="$H5" XDG_CONFIG_HOME="$H5/.config" XDG_DATA_HOME="$H5/.local/share" \
    XDG_CACHE_HOME="$H5/.cache" XDG_STATE_HOME="$H5/.local/state" STUB_LOG="$T/commands5.log" \
    python3 -c 'import pty, sys; pty.spawn(sys.argv[1:])' bash "$SRC/install.sh" > "$T/fifth" 2>&1
clean "$T/fifth" > "$T/fifth.txt"
t "pairing is offered on its own" grep -q "Pair a Bluetooth TV remote now?" "$T/fifth.txt"
t "no app questions once TV apps are declined" not grep -q "Install Kodi" "$T/fifth.txt"
t "website tiles are still offered" grep -q "Add website tiles for streaming services" "$T/fifth.txt"
t "  Chrome is downloaded for them" grep -q "flatpak install --user -y flathub com.google.Chrome" "$T/commands5.log"
t "  and nothing else is" test "$(grep -c '^flatpak install' "$T/commands5.log")" = 1
t "  the Netflix tile is written and pinned" pinned hdlounge-web-netflix.desktop "$H5/.config"
t "no password asked when nothing needs one" test "$(cat "$T/commands5.log" 2>/dev/null | grep -c '^PROMPT$')" = 0
t "declining the TV leaves the config alone" test -z "$(tv_host "$H5/.config")"

echo
echo "### 6. a re-run leaves the paired TV alone and pauses the launcher"
H6="$T/home6"
mkdir -p "$H6/.config/hdlounge"
cat > "$H6/.config/hdlounge/config.json" <<'CONF'
{"apps": [],
 "tv": {"enabled": true, "host": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff",
        "key": "abc123", "secure": false}}
CONF

printf 'y\nn\nn\ny\n\nn\nn\nn\nn\nn\nn\n' |
  env HOME="$H6" XDG_CONFIG_HOME="$H6/.config" XDG_DATA_HOME="$H6/.local/share" \
    XDG_CACHE_HOME="$H6/.cache" XDG_STATE_HOME="$H6/.local/state" \
    STUB_LOG="$T/commands6.log" HDLOUNGE_ACTIVE_EXIT=0 \
    python3 -c 'import pty, sys; pty.spawn(sys.argv[1:])' bash "$SRC/install.sh" \
    > "$T/sixth" 2>&1
clean "$T/sixth" > "$T/sixth.txt"
t "the TV already set up is not asked about again" \
  test "$(asked "$T/sixth.txt" "Switch an LG TV off")" = 0
t "  it is named as left alone" grep -q "The TV at 10.0.0.5 is already set up" "$T/sixth.txt"
t "  and kept in the plan" grep -Eq "TV power over IP +10\.0\.0\.5" "$T/sixth.txt"
t "  with its pairing key untouched" grep -q abc123 "$H6/.config/hdlounge/config.json"
t "sleep is asked about once, not a second time for the TV's sake" \
  test "$(asked "$T/sixth.txt" "Never sleep")" = 1
t "  and that one question says why the TV needs it" grep -q "The TV needs this" "$T/sixth.txt"
STOP_AT="$(grep -n 'systemctl --user stop hdlounge' "$T/commands6.log" | head -1 | cut -d: -f1)"
ENABLE_AT="$(grep -n 'systemctl --user enable hdlounge' "$T/commands6.log" | head -1 | cut -d: -f1)"
t "the running launcher is stopped, so it cannot cover the terminal" test -n "$STOP_AT"
t "  before any of the work (stopped at line $STOP_AT, service installed at $ENABLE_AT)" \
  test -n "$ENABLE_AT" -a "${STOP_AT:-0}" -lt "${ENABLE_AT:-0}"
t "  and never restarted partway through" \
  not grep -q 'systemctl --user restart hdlounge' "$T/commands6.log"
t "  started again only once the last word is on screen" \
  test "$(grep '^systemctl --user' "$T/commands6.log" | tail -1)" = "systemctl --user start hdlounge.service"
t "  which the closing text says out loud" grep -q "Starting the launcher again" "$T/sixth.txt"

echo
echo "=================================="
echo "  PASSED: $PASS   FAILED: $FAIL"
echo "=================================="
if [ "$FAIL" -gt 0 ] || [ -n "${HDLOUNGE_KEEP:-}" ]; then
  echo "  (kept $T for inspection)"
else
  rm -rf "$T"
fi
exit 0
