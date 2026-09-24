#!/usr/bin/env python3
"""Controller mapping, without a controller: feeds raw input into NavMapper
on a fake clock and checks the actions that come out.

    python3 test-support/test_gamepad_mapping.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from hdlounge import gamepad as g  # noqa: E402


class Clock:
    """Stands in for GLib timeouts. Cancelling a timer that already fired
    raises, as GLib.source_remove on a stale id would warn."""

    def __init__(self):
        self.now = 0
        self.timers = {}
        self.next_id = 1

    def schedule(self, ms, fn):
        timer = self.next_id
        self.next_id += 1
        self.timers[timer] = (self.now + ms, fn)
        return timer

    def cancel(self, timer):
        del self.timers[timer]

    def advance(self, ms):
        target = self.now + ms
        while True:
            due = sorted((when, t) for t, (when, _fn) in self.timers.items()
                         if when <= target)
            if not due:
                break
            when, timer = due[0]
            self.now = when
            _when, fn = self.timers.pop(timer)
            fn()
        self.now = target


def mapper():
    clock, actions = Clock(), []
    return g.NavMapper(actions.append, clock.schedule, clock.cancel), clock, actions


FAILURES = []


def check(name, got, want):
    if got == want:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}\n        got  {got}\n        want {want}")
        FAILURES.append(name)


# Face buttons act on press, once.
m, clock, out = mapper()
for code in (g.BTN_SOUTH, g.BTN_EAST, g.BTN_NORTH, g.BTN_WEST, g.BTN_START):
    m.button(code, True)
    clock.advance(1000)
    m.button(code, False)
check("A/B/Y/X/Start map to ok/back/options/options/menu",
      out, ["ok", "back", "options", "options", "menu"])

# D-pad: immediate, then repeats after the delay, stops on release.
m, clock, out = mapper()
m.button(g.BTN_DPAD_DOWN, True)
check("D-pad press moves once immediately", out, ["down"])
clock.advance(g.REPEAT_DELAY_MS - 1)
check("no repeat before the delay", out, ["down"])
clock.advance(1)
check("repeats once the delay passes", out, ["down", "down"])
clock.advance(g.REPEAT_INTERVAL_MS * 2)
check("keeps repeating at the interval", len(out), 4)
m.button(g.BTN_DPAD_DOWN, False)
clock.advance(5000)
check("release stops the repeat", len(out), 4)

# Guide: a tap does nothing (Steam's), a hold means Home, once.
m, clock, out = mapper()
m.button(g.BTN_MODE, True)
clock.advance(g.GUIDE_HOLD_MS - 100)
m.button(g.BTN_MODE, False)
clock.advance(5000)
check("tapping Guide does nothing", out, [])
m.button(g.BTN_MODE, True)
clock.advance(g.GUIDE_HOLD_MS)
m.button(g.BTN_MODE, False)
clock.advance(5000)
check("holding Guide goes Home once", out, ["home"])

# Stick: thresholds with hysteresis.
m, clock, out = mapper()
m.stick(g.ABS_X, 0.5)
check("stick under the press threshold does nothing", out, [])
m.stick(g.ABS_X, 0.7)
check("stick past the threshold moves", out, ["right"])
m.stick(g.ABS_X, 0.45)
clock.advance(g.REPEAT_DELAY_MS)
check("stick between thresholds stays held (repeats)", out, ["right", "right"])
m.stick(g.ABS_X, 0.1)
clock.advance(5000)
check("stick under the release threshold lets go", out, ["right", "right"])
m.stick(g.ABS_Y, -0.9)
check("stick up", out[-1], "up")
m.stick(g.ABS_Y, 0.9)
check("flipping the stick straight across moves the other way", out[-1], "down")

# Hat axes (controllers without a mapping).
m, clock, out = mapper()
m.hat(g.ABS_HAT0X, -1)
m.hat(g.ABS_HAT0X, 0)
m.hat(g.ABS_HAT0Y, 1)
m.hat(g.ABS_HAT0Y, 0)
clock.advance(5000)
check("hat left then down, no repeat after centring", out, ["left", "down"])

# Two inputs: releasing the stick must not stop a held D-pad.
m, clock, out = mapper()
m.stick(g.ABS_X, 0.9)
m.button(g.BTN_DPAD_UP, True)
m.stick(g.ABS_X, 0.0)
clock.advance(g.REPEAT_DELAY_MS)
check("releasing another input leaves the D-pad repeating", out, ["right", "up", "up"])

# Disconnect mid-hold: nothing fires afterwards, no stale timers.
m, clock, out = mapper()
m.button(g.BTN_DPAD_LEFT, True)
m.button(g.BTN_MODE, True)
m.reset()
clock.advance(5000)
check("reset cancels repeats and a pending Guide hold", out, ["left"])
check("reset leaves no timers behind", clock.timers, {})

# The launcher stops listening to sticks while a game has focus. A stick held
# at that moment would never report its release, so it must let go now.
m, clock, out = mapper()
m.stick(g.ABS_X, 0.9)
m.button(g.BTN_MODE, True)
m.release_axes()
clock.advance(g.GUIDE_HOLD_MS)
check("releasing axes stops a held stick's repeat but not a Guide hold",
      out, ["right", "home"])
m.stick(g.ABS_X, 0.9)
check("  and the stick moves again once it is listened to", out[-1], "right")

m, clock, out = mapper()
m.button(g.BTN_DPAD_UP, True)
m.release_axes()
clock.advance(g.REPEAT_DELAY_MS)
check("releasing axes leaves a held D-pad alone", out, ["up", "up"])

print(f"\n  {'FAILED: ' + str(len(FAILURES)) if FAILURES else 'all passed'}")
sys.exit(1 if FAILURES else 0)
