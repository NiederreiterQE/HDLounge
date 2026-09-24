#!/usr/bin/env python3
"""Switching the TV with the screen, without a screen or a TV: feeds blank
and unblank into TvPower on a fake clock and checks what it sends.

The rules worth protecting are the awkward ones -- don't cut the power for a
remote nudged on the sofa, and never wake a TV somebody else switched off.

    python3 test-support/test_tv_power.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from hdlounge import tvpower  # noqa: E402


class Clock:
    """Stands in for GLib timeouts, like test_gamepad_mapping.py's."""

    def __init__(self):
        self.timers, self.next_id, self.now = {}, 1, 0

    def add(self, seconds, fn):
        timer = self.next_id
        self.next_id += 1
        self.timers[timer] = (self.now + seconds, fn)
        return timer

    def remove(self, timer):
        # GLib.source_remove on an id that already fired warns; a settle timer
        # cancelled twice would mean the state is wrong.
        assert timer in self.timers, f"cancelled a timer that already fired: {timer}"
        del self.timers[timer]

    def advance(self, seconds):
        self.now += seconds
        for timer, (when, fn) in sorted(self.timers.items()):
            if when <= self.now:
                del self.timers[timer]
                fn()


class FakeGLib:
    SOURCE_REMOVE = False

    def __init__(self, clock):
        self.clock = clock

    def timeout_add_seconds(self, seconds, fn):
        return self.clock.add(seconds, fn)

    def source_remove(self, timer):
        self.clock.remove(timer)

    def get_monotonic_time(self):
        return self.clock.now * 1000000      # GLib counts microseconds


class FakeWebos:
    """Records what would have gone out on the wire.

    `tv` is what the set is doing: "on", "standby" or "gone". `hold` keeps the
    reply to an off back until release() -- a connection still in flight.
    """

    OFF, IN_STANDBY, UNREACHABLE, FAILED = "off", "standby", "unreachable", "failed"
    CONNECT_TIMEOUT, QUICK_CONNECT_TIMEOUT = 10, 3

    def __init__(self):
        self.calls = []
        self.tv = "on"
        self.secure = False
        self.hold = False
        self.held = []

    def turn_off(self, host, key, on_done, prefer_secure=False,
                 only_if_on=False, connect_timeout=10):
        if self.tv == "gone":
            reply = (self.UNREACHABLE, "the TV did not answer", None)
        elif self.tv == "standby" and only_if_on:
            reply = (self.IN_STANDBY, "The TV is in standby", self.secure)
        else:
            self.calls.append(("off", host))
            self.tv = "gone"            # a set in standby stops listening
            reply = (self.OFF, "TV off", self.secure)
        if self.hold:
            self.held.append(lambda: on_done(*reply))
        else:
            on_done(*reply)

    def release(self):
        held, self.held = self.held, []
        for reply in held:
            reply()

    def wake(self, mac, host="", on_done=None):
        self.calls.append(("on", mac, host))
        self.tv = "on"
        if on_done:
            on_done(True, "TV on")


class FakeConfig:
    def __init__(self, **tv):
        self._tv = {"enabled": True, "host": "10.0.0.9",
                    "mac": "aa:bb:cc:dd:ee:ff", "key": "k"}
        self._tv.update(tv)

    @property
    def tv(self):
        return dict(self._tv)

    def set_tv(self, **fields):
        self._tv.update(fields)


class FakeWatcher:
    def connect(self, *_args):
        return 0

    def stop(self):
        pass


FAILURES = []


def check(name, got, want):
    if got == want:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}\n        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


def build(**tv):
    """A TvPower on a fake clock, with a fake TV on the other end."""
    clock, web = Clock(), FakeWebos()
    tvpower.GLib = FakeGLib(clock)
    tvpower.webos = web
    tvpower.BlankWatcher = FakeWatcher
    return tvpower.TvPower(FakeConfig(**tv)), clock, web


# The ordinary evening: the screen goes dark, the TV follows.
power, clock, web = build()
power._on_blanked()
check("nothing is sent during the settle window", web.calls, [])
clock.advance(tvpower.SETTLE_SECONDS)
check("the TV goes off once the screen has stayed dark",
      web.calls, [("off", "10.0.0.9")])
power._on_unblanked()
check("and comes back when the screen wakes",
      web.calls[-1], ("on", "aa:bb:cc:dd:ee:ff", "10.0.0.9"))

# A remote knocked on the sofa: the screen blanks and wakes seconds later.
# Cutting the TV's power here would be a visible off/on cycle of the panel.
power, clock, web = build()
power._on_blanked()
power._on_unblanked()
clock.advance(60)
check("a nudge inside the settle window touches nothing", web.calls, [])
check("  and leaves no timer behind", clock.timers, {})

# Somebody switched the TV off themselves and later woke the box. Sending a
# wake packet here would turn on a TV that was meant to stay off.
power, clock, web = build()
power._on_unblanked()
check("an unblank we did not cause sends nothing", web.calls, [])

power, clock, web = build()
power._on_blanked()
power._on_blanked()
clock.advance(60)
check("a repeated blank switches off once", web.calls, [("off", "10.0.0.9")])

power, clock, web = build()
for _ in range(2):
    power._on_blanked()
    clock.advance(tvpower.SETTLE_SECONDS)
    power._on_unblanked()
check("repeated evenings alternate cleanly",
      [call[0] for call in web.calls], ["off", "on", "off", "on"])

# Pairing can succeed without the MAC turning up. Off still works; on cannot,
# and saying so is better than a TV that never comes back.
power, clock, web = build(mac="")
power._on_blanked()
clock.advance(60)
power._on_unblanked()
check("without a MAC the TV still switches off",
      [call[0] for call in web.calls], ["off"])
check("  and says why it cannot be woken", "cannot be woken" in power.status, True)

power, clock, web = build(key="", host="")
power._on_blanked()
clock.advance(60)
check("an unpaired TV is never contacted", web.calls, [])
check("  and does not count as configured", power.configured, False)

power, clock, web = build(enabled=False)
check("a paired but switched-off TV does not watch the screen",
      power.enabled, False)

# The remote's Power button
# -------------------------------------------------------

power, clock, web = build()
power.toggle()
check("Power on a TV that is on switches it off",
      [call[0] for call in web.calls], ["off"])
power.toggle()
check("  and again switches it back on",
      [call[0] for call in web.calls], ["off", "on"])
power.toggle()
check("  and again, off", [call[0] for call in web.calls], ["off", "on", "off"])

# The press that wakes the screen is delivered to the key binding too. Acting
# on both would turn the TV on and straight back off.
power, clock, web = build()
power._on_blanked()
clock.advance(tvpower.SETTLE_SECONDS)
web.calls.clear()
power._on_unblanked()
check("waking the screen turns the TV on", [c[0] for c in web.calls], ["on"])
power.toggle()
check("  and the same press does not then switch it off",
      [c[0] for c in web.calls], ["on"])

clock.advance(tvpower.WAKE_GRACE_SECONDS + 1)
power.toggle()
check("  but a press a few seconds later does",
      [c[0] for c in web.calls], ["on", "off"])

power, clock, web = build(key="", host="")
power.toggle()
check("Power does nothing at all when no TV is set up", web.calls, [])

# Somebody switched the TV off with its own remote. HDLounge never knew, so a
# guess would say "on" and send an off to a set that is not listening.
power, clock, web = build()
web.tv = "gone"
power.toggle()
check("Power at a TV that does not answer wakes it",
      [c[0] for c in web.calls], ["on"])

power, clock, web = build()
web.tv = "standby"
power.toggle()
check("Power at a TV in standby wakes it rather than switching it off",
      [c[0] for c in web.calls], ["on"])

power, clock, web = build()
web.tv = "gone"
power._on_blanked()
clock.advance(tvpower.SETTLE_SECONDS)
check("an unanswered off from the blank watcher does not send a wake",
      web.calls, [])
check("  and says so", "Could not reach" in power.status, True)

# The off is still connecting when a button wakes the screen. It lands anyway,
# so the TV has to come back straight after, not stay dark until the next wake.
power, clock, web = build()
web.hold = True
power._on_blanked()
clock.advance(tvpower.SETTLE_SECONDS)
power._on_unblanked()
check("waking during an off that is in flight sends nothing yet",
      [c[0] for c in web.calls], ["off"])
web.release()
check("  and wakes the TV as soon as the off has landed",
      [c[0] for c in web.calls], ["off", "on"])

power, clock, web = build()
web.secure = True
power.turn_off()
check("the port that answered is remembered", power.config.tv["secure"], True)

power, clock, web = build()
power.turn_off()
check("a routine status is shown", power.status, "TV off")
clock.advance(tvpower.STATUS_SECONDS)
check("  then clears itself", power.status, "")

power, clock, web = build(mac="")
power.turn_on()
clock.advance(tvpower.STATUS_SECONDS * 2)
check("a problem stays on screen", "cannot be woken" in power.status, True)

os.environ["HDLOUNGE_TV_DRYRUN"] = "1"
power, clock, web = build()
power._on_blanked()
clock.advance(60)
check("HDLOUNGE_TV_DRYRUN sends nothing", web.calls, [])
check("  but reports what it would have done", "would turn" in power.status, True)
del os.environ["HDLOUNGE_TV_DRYRUN"]

print(f"\n  {'FAILED: ' + str(len(FAILURES)) if FAILURES else 'all passed'}")
sys.exit(1 if FAILURES else 0)
