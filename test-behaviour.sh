#!/usr/bin/env bash
# End-to-end behaviour test for the HDLounge shell bridge, inside an isolated
# headless nested GNOME Shell. Verifies the launch/focus/fullscreen/home/
# close/kill contract that the launcher depends on.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UUID="hdlounge@harmonicdesign.ca"
SANDBOX="/tmp/hdlounge-behaviour"
OUT="${HDLOUNGE_OUT:-$SRC/.test/behaviour.log}"
TEST_APP="${HDLOUNGE_TEST_APP:-org.gnome.Calculator.desktop}"

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
cp -r "$SRC/test-support/hdloungeinject@test" "$XDG_DATA_HOME/gnome-shell/extensions/"
glib-compile-schemas "$XDG_DATA_HOME/gnome-shell/extensions/$UUID/schemas"

WEB_TILE=""
if flatpak info org.chromium.Chromium >/dev/null 2>&1; then
  WEB_TILE="hdlounge-web-example.desktop"
  WEB_URL="https://www.example.com/"
  WEB_CLASS="$(PYTHONPATH="$SRC/app" python3 -c "import sys; from hdlounge.webapps import wm_class; print(wm_class(sys.argv[1]))" "$WEB_URL")"
  mkdir -p "$XDG_DATA_HOME/applications"
  cat > "$XDG_DATA_HOME/applications/$WEB_TILE" <<ENTRY
[Desktop Entry]
Type=Application
Name=Example
Exec=flatpak run org.chromium.Chromium --ozone-platform=wayland --user-data-dir=/tmp/hdlounge-behaviour-browser --no-first-run --no-default-browser-check "--app=$WEB_URL"
Icon=web-browser
StartupWMClass=$WEB_CLASS
ENTRY
fi

export HDLOUNGE_APP="$SRC/app/hdlounge-launcher"
export NESTED=hdlounge-behaviour
export TEST_APP OUT WEB_TILE

dbus-run-session -- bash -euc '
exec > "$OUT" 2>&1

inject_focus() { gdbus call --session --dest ca.harmonicdesign.HDLounge.Inject \
        --object-path /ca/harmonicdesign/HDLounge/Inject \
        --method ca.harmonicdesign.HDLounge.Inject.FocusInfo 2>&1; }

bus() { gdbus call --session --dest ca.harmonicdesign.HDLounge.Shell \
        --object-path /ca/harmonicdesign/HDLounge/Shell \
        --method "ca.harmonicdesign.HDLounge.Shell.$@" 2>&1; }

PASS=0; FAIL=0
check() { # check <name> <expected-substring> <actual>
  if printf "%s" "$3" | grep -qF "$2"; then
    echo "  PASS  $1"; PASS=$((PASS+1))
  else
    echo "  FAIL  $1"; echo "        want substring: $2"; echo "        got: $3"; FAIL=$((FAIL+1))
  fi
}

dconf write /org/gnome/shell/disable-user-extensions false
dconf write /org/gnome/shell/enabled-extensions "[\"hdlounge@harmonicdesign.ca\", \"hdloungeinject@test\"]"
dconf write /org/gnome/shell/disable-extension-version-validation true
dconf write /org/gnome/desktop/interface/enable-animations false

gnome-shell --headless --virtual-monitor 1600x900 --wayland-display="$NESTED" \
  > /tmp/hdlounge-behaviour-shell.log 2>&1 &

for _ in $(seq 1 60); do
  [ -S "$XDG_RUNTIME_DIR/$NESTED" ] && break
  sleep 0.5
done
sleep 5

WAYLAND_DISPLAY="$NESTED" GDK_BACKEND=wayland PYTHONUNBUFFERED=1 \
  "$HDLOUNGE_APP" > /tmp/hdlounge-behaviour-app.log 2>&1 &
sleep 5

echo "### 1. launcher registered and visible to the shell"
check "launcher appears in ListRunning" "ca.harmonicdesign.HDLounge" "$(bus ListRunning)"

echo
echo "### 2. launching an app (expect: launched, fullscreen)"
R="$(bus LaunchOrFocus \"$TEST_APP\" true)"
check "first launch reports launched" "launched" "$R"
sleep 6
W="$(bus ListWindows)"
echo "    windows: $W"
check "test app has a window" "$TEST_APP" "$W"
FS="$(printf "%s" "$W" | grep -o "$TEST_APP[^)]*" || true)"
echo "    entry: $FS"
case "$FS" in *"true, false"*) echo "  PASS  fullscreen=true minimized=false"; PASS=$((PASS+1));;
              *) echo "  FAIL  expected fullscreen=true minimized=false, got: $FS"; FAIL=$((FAIL+1));; esac

echo
echo "### 3. no duplicates (expect: focused, still one window)"
R2="$(bus LaunchOrFocus \"$TEST_APP\" true)"
check "second launch reports focused" "focused" "$R2"
sleep 2
COUNT="$(bus ListWindows | grep -o "$TEST_APP" | wc -l)"
if [ "$COUNT" = "1" ]; then echo "  PASS  still exactly 1 window"; PASS=$((PASS+1));
else echo "  FAIL  expected 1 window, got $COUNT"; FAIL=$((FAIL+1)); fi

echo
echo "### 4. Home minimises other apps but not the launcher"
bus ShowHome > /dev/null
sleep 3
W3="$(bus ListWindows)"
echo "    windows: $W3"
TESTENT="$(printf "%s" "$W3" | grep -o "$TEST_APP[^)]*" || true)"
case "$TESTENT" in *"true)"*|*", true"*) echo "  PASS  test app minimized"; PASS=$((PASS+1));;
              *) echo "  FAIL  test app not minimized: $TESTENT"; FAIL=$((FAIL+1));; esac
LAUNCHENT="$(printf "%s" "$W3" | grep -o "HDLounge., [a-z]*, [a-z]*" || true)"
case "$LAUNCHENT" in *", false"*) echo "  PASS  launcher NOT minimized"; PASS=$((PASS+1));;
              *) echo "  FAIL  launcher was minimized: $LAUNCHENT"; FAIL=$((FAIL+1));; esac

echo
echo "### 4b. re-selecting the app after Home restores and focuses it"
R3="$(bus LaunchOrFocus \"$TEST_APP\" true)"
check "re-launch reports focused" "focused" "$R3"
sleep 4
W4="$(bus ListWindows)"
echo "    windows: $W4"
ENT="$(printf "%s" "$W4" | grep -o "$TEST_APP[^)]*" || true)"
case "$ENT" in
  *", false"*) echo "  PASS  app un-minimised after re-select"; PASS=$((PASS+1));;
  *) echo "  FAIL  app still minimised after re-select: $ENT"; FAIL=$((FAIL+1));;
esac
FW="$(inject_focus)"
case "$FW" in
  *Calculator*) echo "  PASS  compositor focus is the app"; PASS=$((PASS+1));;
  *) echo "  FAIL  app did not take keyboard focus: $FW"; FAIL=$((FAIL+1));;
esac

echo
echo "### 4c. FocusApp raises a Home-minimised app (quick settings path)"
# Quick settings and the Software button spawn their app themselves and then
# ask the compositor to raise it. A second GApplication activation does not
# un-minimise, so this is the call that has to.
bus ShowHome > /dev/null
sleep 3
R4="$(bus FocusApp \"$TEST_APP\")"
check "FocusApp reports it focused something" "true" "$R4"
sleep 3
W5="$(bus ListWindows)"
echo "    windows: $W5"
ENT2="$(printf "%s" "$W5" | grep -o "$TEST_APP[^)]*" || true)"
case "$ENT2" in
  *", false"*) echo "  PASS  app un-minimised by FocusApp"; PASS=$((PASS+1));;
  *) echo "  FAIL  app still minimised after FocusApp: $ENT2"; FAIL=$((FAIL+1));;
esac
FW2="$(inject_focus)"
case "$FW2" in
  *Calculator*) echo "  PASS  compositor focus is the app"; PASS=$((PASS+1));;
  *) echo "  FAIL  app did not take keyboard focus: $FW2"; FAIL=$((FAIL+1));;
esac

echo
echo "### 5. graceful close"
bus CloseApp \"$TEST_APP\" > /dev/null
sleep 4
check "app gone after CloseApp" "false" "$(bus ListWindows | grep -c "$TEST_APP" | sed "s/^0$/false/")"

echo
echo "### 6. force kill"
bus LaunchOrFocus \"$TEST_APP\" true > /dev/null
sleep 6
bus KillApp \"$TEST_APP\" > /dev/null
sleep 4
GONE="$(bus ListWindows | grep -c "$TEST_APP" || true)"
if [ "$GONE" = "0" ]; then echo "  PASS  app gone after KillApp"; PASS=$((PASS+1));
else echo "  FAIL  app survived KillApp"; FAIL=$((FAIL+1)); fi

echo
echo "### 7. a website tile is an app of its own, not its browser"
if [ -z "$WEB_TILE" ]; then
  echo "  SKIP  Chromium (Flatpak) is not installed"
else
  R5="$(bus LaunchOrFocus \"$WEB_TILE\" true)"
  check "website tile launches" "launched" "$R5"
  sleep 25
  RUN="$(bus ListRunning)"
  echo "    running: $RUN"
  check "ListRunning reports the tile as a running app" "$WEB_TILE" "$RUN"
  case "$RUN" in
    *org.chromium.Chromium*) echo "  FAIL  the browser is listed for a website window"; FAIL=$((FAIL+1));;
    *) echo "  PASS  the browser is not listed for website windows"; PASS=$((PASS+1));;
  esac
  WENT="$(bus ListWindows | grep -o "$WEB_TILE[^)]*" || true)"
  echo "    entry: $WENT"
  case "$WENT" in *"true, false"*) echo "  PASS  website tile opened fullscreen"; PASS=$((PASS+1));;
                  *) echo "  FAIL  website tile not fullscreen: $WENT"; FAIL=$((FAIL+1));; esac
  R6="$(bus LaunchOrFocus \"$WEB_TILE\" true)"
  check "second launch focuses instead of duplicating" "focused" "$R6"
  sleep 3
  WCOUNT="$(bus ListWindows | grep -o "$WEB_TILE" | wc -l)"
  if [ "$WCOUNT" = "1" ]; then echo "  PASS  still exactly 1 website window"; PASS=$((PASS+1));
  else echo "  FAIL  expected 1 website window, got $WCOUNT"; FAIL=$((FAIL+1)); fi
  bus ShowHome > /dev/null
  sleep 3
  check "FocusApp raises the website tile after Home" "true" "$(bus FocusApp \"$WEB_TILE\")"
  bus CloseApp \"$WEB_TILE\" > /dev/null
  sleep 6
  WGONE="$(bus ListWindows | grep -c "$WEB_TILE" || true)"
  if [ "$WGONE" = "0" ]; then echo "  PASS  website tile gone after CloseApp"; PASS=$((PASS+1));
  else echo "  FAIL  website window survived CloseApp"; FAIL=$((FAIL+1)); fi
fi

echo
echo "=================================="
echo "  PASSED: $PASS   FAILED: $FAIL"
echo "=================================="

pkill -f "$HDLOUNGE_APP" 2>/dev/null || true
sleep 1
pkill -f "gnome-shell --headless" 2>/dev/null || true
exit 0
'
