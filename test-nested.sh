#!/usr/bin/env bash
# Run HDLounge inside a headless nested GNOME Shell with isolated config, so
# testing cannot touch the host session's dconf, extensions or panel state.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UUID="hdlounge@harmonicdesign.ca"
SANDBOX="${HDLOUNGE_TEST_DIR:-/tmp/hdlounge-nested}"
SHOT="${HDLOUNGE_SHOT:-/tmp/hdlounge-shot.png}"
HOLD="${HDLOUNGE_HOLD:-18}"

rm -rf "$SANDBOX"
mkdir -p "$SANDBOX"/{config,data/gnome-shell/extensions,cache,state}

export XDG_CONFIG_HOME="$SANDBOX/config"
export XDG_DATA_HOME="$SANDBOX/data"
export XDG_CACHE_HOME="$SANDBOX/cache"
export XDG_STATE_HOME="$SANDBOX/state"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export XDG_DATA_DIRS="${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"

cp -r "$SRC/shell-extension/$UUID" "$XDG_DATA_HOME/gnome-shell/extensions/"
glib-compile-schemas "$XDG_DATA_HOME/gnome-shell/extensions/$UUID/schemas"

export HDLOUNGE_APP="$SRC/app/hdlounge-launcher"
export HDLOUNGE_SHOT="$SHOT"
export HDLOUNGE_HOLD="$HOLD"
export WAYLAND_DISPLAY_NESTED=hdlounge-test

exec dbus-run-session -- bash -euc '
  dconf write /org/gnome/shell/disable-user-extensions false
  dconf write /org/gnome/shell/enabled-extensions "[\"hdlounge@harmonicdesign.ca\"]"
  dconf write /org/gnome/shell/disable-extension-version-validation true
  dconf write /org/gnome/desktop/interface/enable-animations false

  gnome-shell --headless --virtual-monitor 1600x900 \
              --wayland-display="$WAYLAND_DISPLAY_NESTED" > /tmp/hdlounge-shell.log 2>&1 &

  for _ in $(seq 1 60); do
    [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY_NESTED" ] && break
    sleep 0.5
  done
  if [ ! -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY_NESTED" ]; then
    echo "FATAL: nested shell never created its wayland socket"; exit 1
  fi
  sleep 4

  echo "=== extension state ==="
  gnome-extensions info hdlounge@harmonicdesign.ca 2>&1 | sed "s/^/[ext] /" || true

  echo "=== bridge on bus? ==="
  gdbus introspect --session --dest ca.harmonicdesign.HDLounge.Shell \
    --object-path /ca/harmonicdesign/HDLounge/Shell --only-properties 2>&1 \
    | head -5 | sed "s/^/[bus] /" || true

  WAYLAND_DISPLAY="$WAYLAND_DISPLAY_NESTED" GDK_BACKEND=wayland \
  HDLOUNGE_SCREENSHOT="$HDLOUNGE_SHOT" HDLOUNGE_SCREENSHOT_DELAY=14 PYTHONUNBUFFERED=1 HDLOUNGE_SCREENSHOT_QS="${HDLOUNGE_QS:-}" \
    "$HDLOUNGE_APP" > /tmp/hdlounge-app.log 2>&1 &
  APP_PID=$!

  sleep "$HDLOUNGE_HOLD"

  echo "=== D-Bus smoke test: ListRunning ==="
  gdbus call --session --dest ca.harmonicdesign.HDLounge.Shell \
    --object-path /ca/harmonicdesign/HDLounge/Shell \
    --method ca.harmonicdesign.HDLounge.Shell.ListRunning 2>&1 | sed "s/^/[bus] /" || true

  kill $APP_PID 2>/dev/null || true
  sleep 1
  pkill -f "gnome-shell --headless" 2>/dev/null || true
  echo "=== done ==="
'
