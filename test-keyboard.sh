#!/usr/bin/env bash
# Diagnose/verify keyboard navigation by injecting synthetic key events into
# an isolated headless nested GNOME Shell.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UUID="hdlounge@harmonicdesign.ca"
INJECT="hdloungeinject@test"
SANDBOX="/tmp/hdlounge-keyboard"
OUT="${HDLOUNGE_OUT:-$SRC/.test/keyboard.log}"

mkdir -p "$(dirname "$OUT")"
rm -rf "$SANDBOX"
mkdir -p "$SANDBOX"/{config,data/gnome-shell/extensions,cache,state}

export XDG_CONFIG_HOME="$SANDBOX/config"
export XDG_DATA_HOME="$SANDBOX/data"
export XDG_CACHE_HOME="$SANDBOX/cache"
export XDG_STATE_HOME="$SANDBOX/state"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export XDG_DATA_DIRS="${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"

cp -r "$SRC/shell-extension/$UUID" "$XDG_DATA_HOME/gnome-shell/extensions/"
cp -r "$SRC/test-support/$INJECT"  "$XDG_DATA_HOME/gnome-shell/extensions/"
glib-compile-schemas "$XDG_DATA_HOME/gnome-shell/extensions/$UUID/schemas"

export HDLOUNGE_APP="$SRC/app/hdlounge-launcher"
export NESTED=hdlounge-keyboard
export OUT

dbus-run-session -- bash -euc '
exec > "$OUT" 2>&1

bus()  { gdbus call --session --dest ca.harmonicdesign.HDLounge.Shell \
         --object-path /ca/harmonicdesign/HDLounge/Shell \
         --method "ca.harmonicdesign.HDLounge.Shell.$@" 2>&1; }
inject() { gdbus call --session --dest ca.harmonicdesign.HDLounge.Inject \
         --object-path /ca/harmonicdesign/HDLounge/Inject \
         --method "ca.harmonicdesign.HDLounge.Inject.$@" 2>&1; }

dconf write /org/gnome/shell/disable-user-extensions false
dconf write /org/gnome/shell/enabled-extensions "[\"hdlounge@harmonicdesign.ca\", \"hdloungeinject@test\"]"
dconf write /org/gnome/shell/disable-extension-version-validation true
dconf write /org/gnome/desktop/interface/enable-animations false
dconf write /org/gnome/shell/welcome-dialog-last-shown-version "\"99.0\""

gnome-shell --headless --virtual-monitor 1600x900 --wayland-display="$NESTED" \
  > /tmp/hdlounge-keyboard-shell.log 2>&1 &

for _ in $(seq 1 60); do
  [ -S "$XDG_RUNTIME_DIR/$NESTED" ] && break
  sleep 0.5
done
sleep 5

WAYLAND_DISPLAY="$NESTED" GDK_BACKEND=wayland PYTHONUNBUFFERED=1 \
  HDLOUNGE_DEBUG_KEYS=1 "$HDLOUNGE_APP" > /tmp/hdlounge-keyboard-app.log 2>&1 &
sleep 6

PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; echo "        $2"; FAIL=$((FAIL+1)); }
trace() { grep -E "^KEY|^STATE|^TRACE" /tmp/hdlounge-keyboard-app.log; }
focused() { grep -E "^TRACE FOCUS" /tmp/hdlounge-keyboard-app.log | tail -1 | awk "{print \$3}"; }
count() { grep -c "$1" /tmp/hdlounge-keyboard-app.log || true; }

echo "### 1. the launcher holds compositor keyboard focus"
FI="$(inject FocusInfo)"
case "$FI" in *HDLounge*) ok "compositor focus is the launcher";;
              *) bad "no keyboard focus" "$FI";; esac

case "$(trace | grep -c "active=True")" in
  0) bad "window never became active" "$(trace | head -3)";;
  *) ok "GTK window is active";;
esac

echo
echo "### 2. arrow keys move focus between tiles"
BEFORE="$(focused)"
inject Key 65363 >/dev/null; sleep 1     # Right
MID="$(focused)"
inject Key 65363 >/dev/null; sleep 1     # Right
AFTER="$(focused)"
if [ -n "$BEFORE" ] && [ "$BEFORE" != "$MID" ] && [ "$MID" != "$AFTER" ]; then
  ok "focus advanced twice: $BEFORE -> $MID -> $AFTER"
else
  bad "arrow keys did not move focus" "before=$BEFORE mid=$MID after=$AFTER"
fi

echo
echo "### 3. long-press opens the options menu and does NOT launch"
WIN_BEFORE="$(bus ListWindows | grep -o "desktop" | wc -l)"
ACT_BEFORE="$(grep -c "TRACE ACTIVATE" /tmp/hdlounge-keyboard-app.log || true)"
inject KeyHold 65293 900 >/dev/null      # hold Return past the 550ms threshold
sleep 4
if trace | grep -q "TRACE MENU opened"; then
  ok "long-press opened the options menu"
else
  bad "long-press did not open the menu" "$(trace | tail -3)"
fi
WIN_AFTER="$(bus ListWindows | grep -o "desktop" | wc -l)"
if [ "$WIN_BEFORE" = "$WIN_AFTER" ]; then
  ok "long-press did not launch the app"
else
  bad "long-press launched something" "before=$WIN_BEFORE after=$WIN_AFTER"
fi
# Window count alone is satisfied by an app that was already running, so
# check the tile did not fire at all.
ACT_AFTER="$(grep -c "TRACE ACTIVATE" /tmp/hdlounge-keyboard-app.log || true)"
if [ "$ACT_BEFORE" = "$ACT_AFTER" ]; then
  ok "long-press did not activate the tile"
else
  bad "long-press activated the tile" "activations before=$ACT_BEFORE after=$ACT_AFTER"
fi

echo
echo "### 4. Escape closes the menu, short press then launches"
inject Key 65307 >/dev/null; sleep 1     # Escape
TARGET="$(focused)"
L_BEFORE="$(grep -c "TRACE LAUNCHING" /tmp/hdlounge-keyboard-app.log || true)"
inject Key 65293 >/dev/null              # Return
sleep 1
# The tile must say "starting" straight away -- a cold boot is a long silence
# otherwise, and the user presses OK again.
L_AFTER="$(grep -c "TRACE LAUNCHING" /tmp/hdlounge-keyboard-app.log || true)"
if [ "$L_AFTER" -gt "$L_BEFORE" ]; then
  ok "pressed tile showed launch feedback immediately"
else
  bad "no launch feedback on press" "$(trace | tail -3)"
fi
sleep 7
W="$(bus ListWindows)"
APPID="$(printf "%s" "$TARGET" | sed -E "s/.*\((.*)\)/\1/")"
if printf "%s" "$W" | grep -qF "$APPID"; then
  ok "short press launched the focused app ($APPID)"
else
  bad "focused app did not launch" "target=$APPID windows=$W"
fi
if printf "%s" "$W" | grep -oE "$APPID., .[^,]*, true" >/dev/null; then
  ok "launched app is fullscreen"
else
  bad "launched app not fullscreen" "$W"
fi
# ...and it must stop saying it once the app actually has a window.
if grep -q "TRACE LAUNCH-SETTLED $APPID" /tmp/hdlounge-keyboard-app.log; then
  ok "launch feedback cleared once the app was running"
else
  bad "tile still shows launch feedback" "$(trace | grep -E "LAUNCH" | tail -3)"
fi

echo
echo "### 5. Down opens tile options; Up reaches the action bar"
bus ShowHome >/dev/null; sleep 2
BEFORE_MENU="$(grep -c "TRACE MENU opened" /tmp/hdlounge-keyboard-app.log || true)"
inject Key 65364 >/dev/null; sleep 2      # Down
AFTER_MENU="$(grep -c "TRACE MENU opened" /tmp/hdlounge-keyboard-app.log || true)"
if [ "$AFTER_MENU" -gt "$BEFORE_MENU" ]; then ok "Down opened tile options"
else bad "Down did not open options" "menus before=$BEFORE_MENU after=$AFTER_MENU"; fi
inject Key 65307 >/dev/null; sleep 1      # Escape

A_BEFORE="$(grep -c "TRACE FOCUS action:" /tmp/hdlounge-keyboard-app.log || true)"
inject Key 65362 >/dev/null; sleep 2      # Up -> action bar
A_AFTER="$(grep -c "TRACE FOCUS action:" /tmp/hdlounge-keyboard-app.log || true)"
if [ "$A_AFTER" -gt "$A_BEFORE" ]; then
  ok "Up moved focus to the action bar ($(grep "TRACE FOCUS action:" /tmp/hdlounge-keyboard-app.log | tail -1 | cut -d: -f2))"
else
  bad "Up did not reach the action bar" "$(trace | tail -3)"
fi
# A remote has no hover: the focused button must name itself in the bubble.
if trace | grep "TRACE BUBBLE" | tail -1 | grep -q "BUBBLE Add App"; then
  ok "focus bubble names the focused action button (Add App)"
else
  bad "no focus bubble for the action bar" "$(trace | grep BUBBLE | tail -3)"
fi
inject Key 65363 >/dev/null; sleep 1      # Right within the action bar
if [ "$(grep -c "TRACE FOCUS action:" /tmp/hdlounge-keyboard-app.log)" -gt "$A_AFTER" ]; then
  ok "Left/Right moves within the action bar"
else
  bad "action bar does not navigate horizontally" "$(trace | tail -3)"
fi
if trace | grep "TRACE BUBBLE" | tail -1 | grep -q "BUBBLE Get More Apps"; then
  ok "focus bubble follows focus to the next button"
else
  bad "focus bubble did not follow focus" "$(trace | grep BUBBLE | tail -3)"
fi
inject Key 65364 >/dev/null; sleep 1      # Down -> back to the drawer
if trace | grep "TRACE BUBBLE" | tail -1 | grep -q "BUBBLE hidden"; then
  ok "focus bubble hides when focus leaves the action bar"
else
  bad "focus bubble stayed up after leaving the action bar" "$(trace | grep BUBBLE | tail -3)"
fi

echo
echo "### 6. remote-style hold (press/release pairs) is one hold, not taps"
bus ShowHome >/dev/null; sleep 2
inject Key 65364 >/dev/null; sleep 1; inject Key 65307 >/dev/null; sleep 1   # settle UI
WIN_B="$(bus ListWindows | grep -o "desktop" | wc -l)"
M_B="$(grep -c "TRACE MENU opened" /tmp/hdlounge-keyboard-app.log || true)"
inject KeyHoldRemote 65293 1000 >/dev/null
sleep 5
M_A="$(grep -c "TRACE MENU opened" /tmp/hdlounge-keyboard-app.log || true)"
WIN_A="$(bus ListWindows | grep -o "desktop" | wc -l)"
if [ "$M_A" -gt "$M_B" ]; then ok "remote-style hold opened the options menu"
else bad "remote-style hold did not register as a hold" "menus before=$M_B after=$M_A"; fi
if [ "$WIN_B" = "$WIN_A" ]; then ok "remote-style hold did not launch anything"
else bad "remote-style hold launched apps" "before=$WIN_B after=$WIN_A"; fi

echo
echo "### 7. quick settings is fully D-pad navigable"
bus ShowHome >/dev/null; sleep 2
N_B="$(grep -c "TRACE nav" /tmp/hdlounge-keyboard-app.log || true)"
inject Key 65383 >/dev/null; sleep 2       # Menu -> quick settings
N_OPEN="$(grep -c "TRACE nav" /tmp/hdlounge-keyboard-app.log || true)"
if [ "$N_OPEN" -gt "$N_B" ]; then
  ok "Menu key opened quick settings and focused a control"
else
  bad "Menu key did not open/focus quick settings" "$(trace | tail -3)"
fi

for _ in 1 2 3; do inject Key 65364 >/dev/null; sleep 1; done   # Down x3
NAVTAIL="$(grep "TRACE nav" /tmp/hdlounge-keyboard-app.log | tail -4)"
ROWS="$(printf "%s" "$NAVTAIL" | grep -oE "row=[0-9]+" | sort -u | wc -l)"
# Only the quick settings panel contains a Scale, so this cannot be satisfied
# by the tile options menu (which is all Buttons).
if [ "$ROWS" -ge 3 ] && printf "%s" "$NAVTAIL" | grep -q "nav Scale"; then
  ok "Down walks the quick settings panel ($ROWS rows, incl. the volume slider)"
else
  bad "quick settings did not navigate vertically" "$NAVTAIL"
fi

# Walk to the very bottom: the credit link must be reachable with a D-pad.
# More presses than there are rows on purpose -- Down at the bottom row is a
# no-op, and the row count varies with how many audio outputs exist and
# whether a TV is set up.
for _ in 1 2 3 4 5 6 7 8; do inject Key 65364 >/dev/null; sleep 0.6; done
if grep "TRACE nav" /tmp/hdlounge-keyboard-app.log | tail -3 | grep -q "nav Label"; then
  ok "D-pad reaches the credit link at the bottom"
else
  bad "credit link not reachable" "$(grep "TRACE nav" /tmp/hdlounge-keyboard-app.log | tail -3)"
fi
inject Key 65293 >/dev/null; sleep 2       # OK on the link must not crash
if grep -qE "Traceback|TypeError" /tmp/hdlounge-keyboard-app.log; then
  bad "activating the credit link raised" "$(grep -A3 Traceback /tmp/hdlounge-keyboard-app.log | tail -6)"
else
  ok "activating the credit link did not raise"
fi

inject Key 65307 >/dev/null; sleep 2       # Escape closes, focus returns
if grep -E "^TRACE FOCUS [a-z]" /tmp/hdlounge-keyboard-app.log | tail -1 | grep -q "desktop"; then
  ok "Escape closed the panel and returned focus to the drawer"
else
  bad "focus did not return to the drawer" "$(trace | tail -3)"
fi

echo
echo "### 8. cursor hides on idle, wakes on real movement, ignores crossings"
if grep -q "TRACE CURSOR hidden" /tmp/hdlounge-keyboard-app.log; then
  ok "cursor hid after idle"
else
  bad "cursor never hid" "$(trace | tail -3)"
fi

# Genuine pointer movement must bring it back.
SHOWN_B="$(grep -c "TRACE CURSOR shown" /tmp/hdlounge-keyboard-app.log || true)"
inject MoveTo 400 300 >/dev/null; sleep 1
inject MoveTo 700 500 >/dev/null; sleep 2
SHOWN_A="$(grep -c "TRACE CURSOR shown" /tmp/hdlounge-keyboard-app.log || true)"
if [ "$SHOWN_A" -gt "$SHOWN_B" ]; then
  ok "real pointer movement brings the cursor back"
else
  bad "cursor did not reappear on movement" "shown before=$SHOWN_B after=$SHOWN_A"
fi

# Let it idle out again, then confirm Home does not resurrect it.
sleep 12
if [ "$(grep -c "TRACE CURSOR hidden" /tmp/hdlounge-keyboard-app.log)" -ge 2 ]; then
  ok "cursor hid again after movement stopped"
else
  bad "cursor did not re-hide" "$(trace | tail -3)"
fi
SHOWN_C="$(grep -c "TRACE CURSOR shown" /tmp/hdlounge-keyboard-app.log || true)"
inject Key 65360 >/dev/null; sleep 2       # Home: minimises apps, pointer re-enters
bus ShowHome >/dev/null; sleep 3
SHOWN_D="$(grep -c "TRACE CURSOR shown" /tmp/hdlounge-keyboard-app.log || true)"
if [ "$SHOWN_C" = "$SHOWN_D" ]; then
  ok "cursor stayed hidden across Home (crossing ignored)"
else
  bad "cursor reappeared on Home" "shown before=$SHOWN_C after=$SHOWN_D"
fi

echo
echo "### 9. the remote OK button (evdev KEY_SELECT) drives the UI like Return"
# 0x1008ffa0: the keysym an Android TV remote (NVIDIA Shield and its clones)
# sends for OK. GTK activates a widget on Return/space but has no binding
# for this one, so every surface has to handle it deliberately.
OK=269025184

# (a) It must arrive at all. Clutter can only synthesise a keyval the keymap
#     can actually produce, so this doubles as proof the keysym is real.
# (b) ...and a short press must activate the focused tile. Done first and on
#     its own: activating raises the app, which takes keyboard focus away
#     from the launcher, so anything after it needs a fresh ShowHome.
bus ShowHome >/dev/null; sleep 3
inject Key 65307 >/dev/null; sleep 1
A_B="$(grep -c "TRACE ACTIVATE" /tmp/hdlounge-keyboard-app.log || true)"
inject Key $OK >/dev/null; sleep 3
A_A="$(grep -c "TRACE ACTIVATE" /tmp/hdlounge-keyboard-app.log || true)"
if trace | grep -q "SelectButton"; then
  ok "OK reaches the app as SelectButton"
else
  bad "OK never arrived at the app" "$(trace | tail -3)"
fi
if [ "$A_A" -gt "$A_B" ]; then
  ok "a short OK press activated the focused tile"
else
  bad "OK did not activate the tile" "activations before=$A_B after=$A_A"
fi

# (c) Holding it opens the options menu instead, and must not also activate:
#     the same invariant test 3 asserts for Return.
bus ShowHome >/dev/null; sleep 4
inject Key 65307 >/dev/null; sleep 1
M_B="$(grep -c "TRACE MENU opened" /tmp/hdlounge-keyboard-app.log || true)"
A_B="$(grep -c "TRACE ACTIVATE" /tmp/hdlounge-keyboard-app.log || true)"
inject KeyHold $OK 900 >/dev/null; sleep 4
M_A="$(grep -c "TRACE MENU opened" /tmp/hdlounge-keyboard-app.log || true)"
A_A="$(grep -c "TRACE ACTIVATE" /tmp/hdlounge-keyboard-app.log || true)"
if [ "$M_A" -gt "$M_B" ]; then ok "holding OK opened the options menu"
else bad "holding OK did not open the menu" "menus before=$M_B after=$M_A"; fi
if [ "$A_A" = "$A_B" ]; then ok "holding OK did not also activate the tile"
else bad "holding OK activated the tile too" "activations before=$A_B after=$A_A"; fi
inject Key 65307 >/dev/null; sleep 1

# (d) And it activates a plain Gtk.Button, which only works because the
#     navigator activates explicitly: the Settings action opens quick
#     settings, which focuses a control and so emits nav. Settings is the
#     last button in the row, so walk right to it.
bus ShowHome >/dev/null; sleep 4
inject Key 65362 >/dev/null; sleep 2      # Up -> action bar (Add App)
inject Key 65363 >/dev/null; sleep 1      # Right -> Get More Apps
inject Key 65363 >/dev/null; sleep 1      # Right -> Settings
N_B="$(grep -c "TRACE nav" /tmp/hdlounge-keyboard-app.log || true)"
inject Key $OK >/dev/null; sleep 2
N_A="$(grep -c "TRACE nav" /tmp/hdlounge-keyboard-app.log || true)"
if [ "$N_A" -gt "$N_B" ]; then
  ok "OK activated the focused action-bar button"
else
  bad "OK did not activate a Gtk.Button" "$(trace | tail -3)"
fi
inject Key 65307 >/dev/null; sleep 1

echo
echo "### 10. an options menu opened with Down takes the very next OK"
# Only a menu opened by *holding* OK may swallow OK until release (test 3).
# Opened with Down, nothing is held, and the first OK must act.
CFG="$XDG_CONFIG_HOME/hdlounge/config.json"
fs_of() {
  python3 -c "import json, sys; print(next((a.get(\"fullscreen\", True) for a in json.load(open(sys.argv[1]))[\"apps\"] if a[\"id\"] == sys.argv[2]), None))" "$CFG" "$1"
}
bus ShowHome >/dev/null; sleep 3
inject Key 65307 >/dev/null; sleep 1
inject Key 65363 >/dev/null; sleep 0.6; inject Key 65361 >/dev/null; sleep 1   # settle on the first tile
TILE="$(focused)"
FS_BEFORE="$(fs_of "$TILE")"
# The fullscreen toggle follows Switch to / Close / Force Kill for a running
# app, and Launch for one that is not.
if bus ListRunning | grep -qF "$TILE"; then STEPS=3; else STEPS=1; fi
inject Key 65364 >/dev/null; sleep 2      # Down -> options (first item focused)
for _ in $(seq "$STEPS"); do inject Key 65364 >/dev/null; sleep 0.6; done   # -> "Launch fullscreen"
inject Key 65293 >/dev/null; sleep 2      # OK, once
FS_AFTER="$(fs_of "$TILE")"
if [ -n "$FS_BEFORE" ] && [ "$FS_BEFORE" != "$FS_AFTER" ]; then
  ok "the first OK activated the item ($TILE fullscreen $FS_BEFORE -> $FS_AFTER)"
else
  bad "the first OK after Down was swallowed" "fullscreen before=$FS_BEFORE after=$FS_AFTER"
fi
inject Key 65364 >/dev/null; sleep 2      # and toggle it back
for _ in $(seq "$STEPS"); do inject Key 65364 >/dev/null; sleep 0.6; done
inject Key 65293 >/dev/null; sleep 2

echo
echo "### 11. an open menu never gets stranded behind the drawer"
# Up on the first item must stay in the menu: a key the popover does not
# consume is handed to the default focus movement, which used to walk out
# into the drawer and leave the menu open over a tile no longer selected.
bus ShowHome >/dev/null; sleep 3
inject Key 65307 >/dev/null; sleep 1
START="$(focused)"
inject Key 65364 >/dev/null; sleep 2      # Down -> options
inject Key 65362 >/dev/null; sleep 4      # Up on the first item, plus a STATE tick
case "$(grep "^STATE" /tmp/hdlounge-keyboard-app.log | tail -1)" in
  *focus=Button*) ok "Up on the first menu item stays in the menu";;
  *) bad "Up escaped the menu" "$(grep "^STATE" /tmp/hdlounge-keyboard-app.log | tail -1)";;
esac
# Right closes the menu and takes the selection one tile along, rather than
# moving the drawer behind a menu that stays up.
C_B="$(count "TRACE MENU closed")"
inject Key 65363 >/dev/null; sleep 2
if [ "$(count "TRACE MENU closed")" -gt "$C_B" ]; then
  ok "Right closed the open menu"
else
  bad "Right left the menu open" "$(trace | tail -3)"
fi
if [ "$(focused)" != "$START" ] && [ -n "$(focused)" ]; then
  ok "Right moved the selection on ($START -> $(focused))"
else
  bad "Right did not move the selection" "focused=$(focused)"
fi

echo
echo "### 12. a running app that is not pinned gets a tile, and OK switches to it"
# Not among the GNOME default favourites, so not pinned in this profile.
EXTRA=org.gnome.Characters.desktop
bus LaunchOrFocus \"$EXTRA\" false >/dev/null; sleep 6
bus ShowHome >/dev/null; sleep 4
if grep -q "TRACE TRANSIENT $EXTRA" /tmp/hdlounge-keyboard-app.log; then
  ok "unpinned $EXTRA got a tile"
else
  bad "no tile for the unpinned running app" "$(trace | grep TRANSIENT | tail -3)"
fi
for _ in 1 2 3 4 5 6 7 8 9 10; do inject Key 65363 >/dev/null; sleep 0.4; done   # Right to the end
sleep 1
if [ "$(focused)" = "$EXTRA" ]; then
  ok "Right reaches it past the pinned apps"
else
  bad "could not reach the unpinned tile" "focused=$(focused)"
fi
L_B="$(grep -c "TRACE LAUNCHING" /tmp/hdlounge-keyboard-app.log || true)"
inject Key 65293 >/dev/null; sleep 3
case "$(inject FocusInfo)" in
  *org.gnome.Characters*|*Characters*) ok "OK switched to it";;
  *) bad "OK did not switch to it" "$(inject FocusInfo)";;
esac
if [ "$(grep -c "TRACE LAUNCHING" /tmp/hdlounge-keyboard-app.log || true)" = "$L_B" ]; then
  ok "switching to it did not go through launch (no Starting...)"
else
  bad "switching to a running unpinned app showed launch feedback" "$(trace | grep LAUNCH | tail -2)"
fi
bus CloseApp \"$EXTRA\" >/dev/null; sleep 4
bus ShowHome >/dev/null; sleep 3
if grep -q "TRACE TRANSIENT-GONE $EXTRA" /tmp/hdlounge-keyboard-app.log; then
  ok "its tile went away when it closed"
else
  bad "tile stayed after the app closed" "$(trace | grep TRANSIENT | tail -3)"
fi

echo
echo "### 13. a game controller drives the same paths"
# No virtual controller without root: HDLOUNGE_DEBUG_KEYS exports the controller
# dispatcher on D-Bus, and the mapping itself is covered by
# test-support/test_gamepad_mapping.py.
nav() { gdbus call --session --dest ca.harmonicdesign.HDLounge.Debug \
          --object-path /ca/harmonicdesign/HDLounge/Debug \
          --method ca.harmonicdesign.HDLounge.Debug.Nav "\"$1\"" >/dev/null 2>&1; }
bus ShowHome >/dev/null; sleep 3
# Left then right: whichever end of the row focus is on, one of them moves.
F_MOVES="$(count "TRACE FOCUS [a-z]")"
nav left; sleep 1
nav right; sleep 1
if [ "$(count "TRACE PAD right")" -ge 1 ] && [ "$(count "TRACE FOCUS [a-z]")" -gt "$F_MOVES" ]; then
  ok "D-pad left/right moves between tiles"
else
  bad "D-pad did not move between tiles" "$(trace | grep -E "PAD|FOCUS" | tail -4)"
fi
nav up; sleep 1
if grep "TRACE FOCUS" /tmp/hdlounge-keyboard-app.log | tail -1 | grep -q "action:"; then
  ok "D-pad up reaches the action bar"
else
  bad "D-pad up did not reach the action bar" "$(trace | tail -2)"
fi
nav down; sleep 1
M_B="$(count "TRACE MENU opened")"
nav options; sleep 2
if [ "$(count "TRACE MENU opened")" -gt "$M_B" ]; then ok "Y opens tile options"
else bad "Y did not open tile options" "$(trace | tail -2)"; fi
N_B="$(count "TRACE nav Button")"
nav down; sleep 1
if [ "$(count "TRACE nav Button")" -gt "$N_B" ]; then ok "D-pad walks the options menu"
else bad "D-pad did not move within the menu" "$(trace | tail -2)"; fi
F_B="$(count "TRACE FOCUS [a-z]")"
nav back; sleep 2
if [ "$(count "TRACE FOCUS [a-z]")" -gt "$F_B" ]; then ok "bus closes the menu, focus back on the tile"
else bad "bus did not close the menu" "$(trace | tail -2)"; fi
S_B="$(count "TRACE nav Scale")"
nav menu; sleep 2
if [ "$(count "TRACE nav Scale")" -gt "$S_B" ]; then ok "Start opens quick settings"
else bad "Start did not open quick settings" "$(trace | tail -2)"; fi
nav back; sleep 2
if grep -E "^TRACE FOCUS [a-z]" /tmp/hdlounge-keyboard-app.log | tail -1 | grep -q "desktop"; then
  ok "bus closes quick settings"
else
  bad "bus did not close quick settings" "$(trace | tail -2)"
fi
A_B="$(count "TRACE ACTIVATE")"
nav ok; sleep 3
if [ "$(count "TRACE ACTIVATE")" -gt "$A_B" ]; then ok "A activates the focused tile"
else bad "A did not activate the tile" "$(trace | tail -2)"; fi
sleep 6
P_B="$(count "TRACE PAD right")"
nav right; sleep 1
if [ "$(count "TRACE PAD right")" = "$P_B" ]; then
  ok "controller navigation is ignored while an app has focus"
else
  bad "controller navigation reached the launcher behind an app" "$(trace | tail -2)"
fi
nav home; sleep 3
case "$(inject FocusInfo)" in
  *HDLounge*) ok "Home (held Guide) comes back from inside an app";;
  *) bad "Home did not return to the launcher" "$(inject FocusInfo)";;
esac

echo
echo "### 14. Add a Website makes a pinned tile; Remove Website deletes it"
WEB="$XDG_DATA_HOME/applications/hdlounge-web-netflix.desktop"
if ! flatpak info com.google.Chrome >/dev/null 2>&1 && ! flatpak info org.chromium.Chromium >/dev/null 2>&1; then
  echo "  SKIP  no Chrome or Chromium to host a website tile"
else
  bus ShowHome >/dev/null; sleep 3
  inject Key 97 >/dev/null; sleep 3          # a -> Add App
  inject Key 65364 >/dev/null; sleep 1       # Down -> "Add a Website..."
  inject Key 65293 >/dev/null; sleep 3       # OK
  if grep -q "TRACE PICKER websites" /tmp/hdlounge-keyboard-app.log; then
    ok "OK on Add a Website opens the website list"
  else
    bad "Add a Website did not open" "$(trace | tail -3)"
  fi
  inject Key 65293 >/dev/null; sleep 4       # OK on the first site, Netflix
  if [ -f "$WEB" ] && grep -q "TRACE FOCUS hdlounge-web-netflix.desktop" /tmp/hdlounge-keyboard-app.log; then
    ok "Netflix tile written, pinned and focused"
  else
    bad "no Netflix tile" "file=$(ls "$WEB" 2>&1) $(trace | grep -E "WEBAPP|FOCUS" | tail -3)"
  fi
  if grep -qx "StartupWMClass=chrome-www.netflix.com__-Default" "$WEB" 2>/dev/null; then
    ok "its window class matches what the browser names the window"
  else
    bad "unexpected window class" "$(grep StartupWMClass "$WEB" 2>&1)"
  fi
  inject Key 65364 >/dev/null; sleep 2       # Down -> its options
  for _ in 1 2 3 4; do inject Key 65364 >/dev/null; sleep 0.6; done   # to Remove Website
  inject Key 65293 >/dev/null; sleep 3
  if [ ! -f "$WEB" ] && grep -q "TRACE WEBAPP removed hdlounge-web-netflix.desktop" /tmp/hdlounge-keyboard-app.log; then
    ok "Remove Website deleted the tile and its entry"
  else
    bad "Remove Website did not remove it" "file=$(ls "$WEB" 2>&1) $(trace | tail -3)"
  fi
fi

echo "=================================="
echo "  PASSED: $PASS   FAILED: $FAIL"
echo "=================================="

echo
echo "--- key trace ---"
trace | head -25

pkill -f "$HDLOUNGE_APP" 2>/dev/null || true
sleep 1
pkill -f "gnome-shell --headless" 2>/dev/null || true
exit 0
'
