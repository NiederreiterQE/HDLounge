"""Switching the TV off with the screen, and on again when it wakes."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable

from gi.repository import GLib, GObject

from . import webos
from .blank import BlankWatcher

if TYPE_CHECKING:
    from .config import Config

# The screen blanking is not always the end of it: a remote nudged on the sofa
# wakes it straight back up. Waiting a few seconds before cutting the TV's
# power keeps that from becoming a visible off/on cycle of the panel.
SETTLE_SECONDS = 5

# A press that wakes the screen also reaches the Power binding. Ignore the
# button for this long after the screen comes back, so one press is one
# action.
WAKE_GRACE_SECONDS = 3

# "TV off" and "TV on" are worth a glance, not a permanent place in the panel:
# after this long the TV row goes back to saying which TV and in what mode.
# Problems stay until something else happens.
STATUS_SECONDS = 10


class TvPower(GObject.Object):
    """Policy: what to do with the TV when the screen blanks and unblanks.

    Built once, for the life of the launcher. It watches the screen only when
    there is a TV configured and switched on, so an install that never
    answered the TV question costs one D-Bus subscription that never happens.
    """

    __gsignals__ = {
        # Something changed that the quick settings panel should redraw for.
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.status = ""  # last thing that happened, for the panel
        self._watcher: BlankWatcher | None = None
        self._pending = 0  # the settle timer, while one is running
        self._tv_off = False  # did *we* turn it off?
        self._off_in_flight = False  # a turnOff is connecting right now
        self._wake_after_off = False  # ... and the screen came back meanwhile
        self._woke_at = 0  # when the screen last came back
        self._status_timer = 0
        self._dry_run = os.environ.get("HDLOUNGE_TV_DRYRUN", "") == "1"
        self._sync_watcher()

    # Settings
    # -------------------------------------------------------

    @property
    def configured(self) -> bool:
        tv = self.config.tv
        return bool(tv["host"] and tv["key"])

    @property
    def enabled(self) -> bool:
        return bool(self.config.tv["enabled"]) and self.configured

    @property
    def host(self) -> str:
        return self.config.tv["host"]

    def set_enabled(self, enabled: bool) -> None:
        self.config.set_tv(enabled=bool(enabled))
        self._sync_watcher()
        self._report("")

    def forget(self) -> None:
        self.config.set_tv(enabled=False, host="", mac="", key="", secure=False)
        self._tv_off = False
        self._sync_watcher()
        self._report("")

    def _sync_watcher(self) -> None:
        if self.enabled and self._watcher is None:
            self._watcher = BlankWatcher()
            self._watcher.connect("blanked", lambda *_a: self._on_blanked())
            self._watcher.connect("unblanked", lambda *_a: self._on_unblanked())
        elif not self.enabled and self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
            self._cancel_pending()

    def _report(self, status: str, passing: bool = False) -> None:
        """Say what happened. A `passing` status clears itself shortly."""
        if self._status_timer:
            GLib.source_remove(self._status_timer)
            self._status_timer = 0
        self.status = status
        if passing and status:

            def clear() -> bool:
                self._status_timer = 0
                self.status = ""
                self.emit("state-changed")
                return GLib.SOURCE_REMOVE

            self._status_timer = GLib.timeout_add_seconds(STATUS_SECONDS, clear)
        self.emit("state-changed")

    def _remember_port(self, secure: bool | None) -> None:
        # Which port answered, so the next connection asks it first instead
        # of waiting on the one this set has closed.
        if secure is not None and bool(self.config.tv.get("secure")) != secure:
            self.config.set_tv(secure=secure)

    # Blanking
    # -------------------------------------------------------

    def _on_blanked(self) -> None:
        self._cancel_pending()

        def settled() -> bool:
            self._pending = 0
            self.turn_off()
            return GLib.SOURCE_REMOVE

        self._pending = GLib.timeout_add_seconds(SETTLE_SECONDS, settled)

    def _on_unblanked(self) -> None:
        self._woke_at = GLib.get_monotonic_time()
        # Woken inside the settle window: the TV was never touched.
        if self._pending:
            self._cancel_pending()
            return
        # Woken while the off is still on its way: it will land regardless,
        # so bring the TV back as soon as it has.
        if self._off_in_flight:
            self._wake_after_off = True
            return
        if self._tv_off:
            self.turn_on()

    def _cancel_pending(self) -> None:
        if self._pending:
            GLib.source_remove(self._pending)
            self._pending = 0

    # Actions
    # -------------------------------------------------------

    def toggle(self) -> None:
        """The remote's Power button: off if the TV is on, on if it is not.

        The TV is asked rather than guessed at, because the last thing to
        switch it off may well have been its own remote -- and a set in
        standby, or one that does not answer at all, gets a wake packet,
        which is harmless if it turns out to be on after all.

        Pressing it to wake a blanked screen already turns the TV on, by the
        route below -- so a press that lands in the moment after an unblank
        is that same press arriving twice, and switching the TV straight back
        off is not what anyone meant by it.
        """
        if not self.configured:
            return
        since_wake = GLib.get_monotonic_time() - self._woke_at
        if self._woke_at and since_wake < WAKE_GRACE_SECONDS * 1000000:
            return
        if self._dry_run:
            self.turn_on() if self._tv_off else self.turn_off()
            return
        self._switch_off(only_if_on=True)

    def turn_off(self) -> None:
        """Power the TV down. Used by the blank watcher and by the panel."""
        if not self.configured:
            return
        tv = self.config.tv
        if self._dry_run:
            self._tv_off = True
            self._report(f"would turn {tv['host']} off")
            print(f"hdlounge: HDLOUNGE_TV_DRYRUN, not turning {tv['host']} off")
            return
        self._switch_off(only_if_on=False)

    def _switch_off(self, only_if_on: bool) -> None:
        tv = self.config.tv
        self._off_in_flight = True
        self._wake_after_off = False

        def done(outcome: str, message: str, secure: bool | None) -> None:
            self._off_in_flight = False
            self._remember_port(secure)
            if outcome == webos.OFF:
                self._tv_off = True
                self._report(message, passing=True)
                if self._wake_after_off:
                    self.turn_on()
                return
            if only_if_on and outcome in (webos.IN_STANDBY, webos.UNREACHABLE):
                # Power pressed at a TV that is not on.
                self.turn_on()
                return
            # A failure says nothing new about _tv_off: if an earlier off
            # worked, the TV is likely unreachable precisely because it is
            # off, and the next wake still has to bring it back.
            if outcome == webos.FAILED:
                self._report(f"The TV did not switch off: {message}")
            else:
                self._report(f"Could not reach the TV: {message}")

        webos.turn_off(
            tv["host"],
            tv["key"],
            done,
            prefer_secure=bool(tv.get("secure")),
            only_if_on=only_if_on,
            connect_timeout=(
                webos.QUICK_CONNECT_TIMEOUT if only_if_on else webos.CONNECT_TIMEOUT
            ),
        )

    def turn_on(self) -> None:
        """Wake the TV. Harmless if it is already on."""
        tv = self.config.tv
        if not tv["mac"]:
            self._report("No MAC address, so the TV cannot be woken")
            return
        self._tv_off = False
        if self._dry_run:
            self._report(f"would wake {tv['mac']}")
            print(f"hdlounge: HDLOUNGE_TV_DRYRUN, not waking {tv['mac']}")
            return

        webos.wake(
            tv["mac"],
            tv["host"],
            lambda ok, message: self._report(
                message if ok else f"Could not wake the TV: {message}", passing=ok
            ),
        )

    # Pairing
    # -------------------------------------------------------

    def discover(self, on_done: Callable[[list[tuple[str, str]]], None]) -> None:
        """Look for TVs on the network. on_done([(host, name)])."""
        webos.discover(on_done)

    def pair(self, host: str, on_done: Callable[[bool, str], None]) -> None:
        """Pair with the TV at host and remember it. on_done(ok, message).

        The TV shows an accept prompt the first time, so this can sit waiting
        on someone with the TV's own remote for the best part of a minute.
        """

        def paired(ok: bool, key_or_error: str, mac: str) -> None:
            if not ok:
                self._report(key_or_error)
                on_done(False, key_or_error)
                return
            self.config.set_tv(
                enabled=True, host=host, mac=mac, key=key_or_error, secure=False
            )
            self._tv_off = False
            self._sync_watcher()
            if not mac:
                # Pairing worked, so off will work; on will not until the MAC
                # is known. Worth saying now rather than at 11pm.
                self._report("Paired, but no MAC address: the TV cannot be woken")
                on_done(True, "Paired, but the TV's MAC address was not found")
                return
            self._report(f"Paired with {host}", passing=True)
            on_done(True, f"Paired with {host}")

        self._report(f"Pairing with {host}…")
        webos.pair(host, paired)
