"""Game controllers drive the launcher the way a remote does.

A Wayland client cannot synthesise key events, not even for itself, so rather
than pretending to be a keyboard this turns controller input into named
actions -- "up", "ok", "back", "home" -- and the window routes them to
whatever has focus (HDLoungeWindow.dispatch_nav).

libmanette reads controllers straight from evdev, so it hears them whichever
window has focus. That is what lets holding Guide get back to Home from inside
a game, and it is why the window ignores every other action unless the
launcher itself is focused.
"""

from __future__ import annotations

from typing import Callable

import gi
from gi.repository import GLib, GObject

from .debugshot import trace

try:
    gi.require_version("Manette", "0.2")
    from gi.repository import Manette
except (ImportError, ValueError):
    Manette = None

# Linux input codes (linux/input-event-codes.h). libmanette reports buttons
# and axes in these terms once it has a mapping for the controller.
BTN_SOUTH = 0x130  # A / Cross
BTN_EAST = 0x131  # B / Circle
BTN_NORTH = 0x133  # Y / Triangle
BTN_WEST = 0x134  # X / Square
BTN_START = 0x13B
BTN_MODE = 0x13C  # Guide / Home / PS
BTN_DPAD_UP = 0x220
BTN_DPAD_DOWN = 0x221
BTN_DPAD_LEFT = 0x222
BTN_DPAD_RIGHT = 0x223
ABS_X = 0x00
ABS_Y = 0x01
ABS_HAT0X = 0x10
ABS_HAT0Y = 0x11

BUTTON_ACTIONS = {
    BTN_SOUTH: "ok",
    BTN_EAST: "back",
    BTN_NORTH: "options",
    BTN_WEST: "options",
    BTN_START: "menu",
}
DPAD_BUTTONS = {
    BTN_DPAD_UP: "up",
    BTN_DPAD_DOWN: "down",
    BTN_DPAD_LEFT: "left",
    BTN_DPAD_RIGHT: "right",
}
# Axis -> (direction for a negative value, direction for a positive one).
# evdev Y grows downwards, so negative is up.
AXIS_DIRECTIONS = {
    ABS_X: ("left", "right"),
    ABS_Y: ("up", "down"),
    ABS_HAT0X: ("left", "right"),
    ABS_HAT0Y: ("up", "down"),
}

# Guide only means Home when held. A tap is left alone, because Steam uses it
# to open its overlay in the middle of a game.
GUIDE_HOLD_MS = 600
# A held direction repeats, like a held arrow key.
REPEAT_DELAY_MS = 400
REPEAT_INTERVAL_MS = 120
# The stick counts as pushed past STICK_PRESS and as let go under
# STICK_RELEASE. The gap stops a stick resting near the threshold from
# chattering.
STICK_PRESS = 0.6
STICK_RELEASE = 0.3


class NavMapper:
    """Raw input from one controller -> navigation actions.

    No GObject and no clock of its own: timers go through `schedule` and
    `cancel`, so this can be exercised without hardware or a main loop.
    """

    def __init__(
        self,
        emit: Callable[[str], None],
        schedule: Callable[[int, Callable[[], bool]], int] | None = None,
        cancel: Callable[[int], None] | None = None,
    ) -> None:
        self._emit = emit
        self._schedule = schedule or GLib.timeout_add
        self._cancel = cancel or GLib.source_remove
        self._held: tuple | None = None  # input currently holding a direction
        self._repeat = 0
        self._guide = 0
        self._axes: dict[int, int] = {}  # stick axis -> -1 / 0 / 1

    # Input
    # -------------------------------------------------------

    def button(self, code: int, pressed: bool) -> None:
        if code == BTN_MODE:
            if pressed and not self._guide:
                self._guide = self._schedule(GUIDE_HOLD_MS, self._on_guide_held)
            elif not pressed:
                self._cancel_guide()
            return
        if code in DPAD_BUTTONS:
            source = ("button", code)
            if pressed:
                self._press(DPAD_BUTTONS[code], source)
            else:
                self._release(source)
            return
        action = BUTTON_ACTIONS.get(code)
        if action is not None and pressed:
            self._emit(action)

    def stick(self, axis: int, value: float) -> None:
        if axis not in (ABS_X, ABS_Y):
            return
        latched = self._axes.get(axis, 0)
        if value >= STICK_PRESS:
            state = 1
        elif value <= -STICK_PRESS:
            state = -1
        elif latched and abs(value) > STICK_RELEASE and (value > 0) == (latched > 0):
            state = latched  # between the thresholds: still held
        else:
            state = 0
        if state == latched:
            return
        self._axes[axis] = state
        self._set_axis(("axis", axis), axis, state)

    def hat(self, axis: int, value: int) -> None:
        if axis not in (ABS_HAT0X, ABS_HAT0Y):
            return
        self._set_axis(("hat", axis), axis, (value > 0) - (value < 0))

    def reset(self) -> None:
        """Forget everything held, e.g. when the controller disconnects."""
        self._stop_repeat()
        self._cancel_guide()
        self._held = None
        self._axes.clear()

    def release_axes(self) -> None:
        """Let go of stick and hat directions, e.g. while nobody is listening
        to them -- a direction held then would otherwise repeat forever."""
        if self._held is not None and self._held[0] in ("axis", "hat"):
            self._stop_repeat()
            self._held = None
        self._axes.clear()

    # Internals
    # -------------------------------------------------------

    def _set_axis(self, source: tuple, axis: int, state: int) -> None:
        if state == 0:
            self._release(source)
        else:
            negative, positive = AXIS_DIRECTIONS[axis]
            self._press(positive if state > 0 else negative, source)

    def _press(self, direction: str, source: tuple) -> None:
        self._stop_repeat()
        self._held = source
        self._emit(direction)
        self._repeat = self._schedule(
            REPEAT_DELAY_MS, lambda: self._on_repeat(direction)
        )

    def _release(self, source: tuple) -> None:
        # Letting go of the stick must not stop a D-pad direction still held.
        if self._held == source:
            self._stop_repeat()
            self._held = None

    def _on_repeat(self, direction: str) -> bool:
        self._emit(direction)
        self._repeat = self._schedule(
            REPEAT_INTERVAL_MS, lambda: self._on_repeat(direction)
        )
        return GLib.SOURCE_REMOVE  # replaced by the timer just scheduled

    def _stop_repeat(self) -> None:
        if self._repeat:
            self._cancel(self._repeat)
            self._repeat = 0

    def _on_guide_held(self) -> bool:
        self._guide = 0
        self._emit("home")
        return GLib.SOURCE_REMOVE

    def _cancel_guide(self) -> None:
        if self._guide:
            self._cancel(self._guide)
            self._guide = 0


class Gamepads(GObject.Object):
    """Every connected controller, reported as navigation actions."""

    __gsignals__ = {"nav": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self) -> None:
        super().__init__()
        self._devices: dict = {}  # device -> (NavMapper, handler ids)
        self._monitor = None
        # While a game has focus its sticks move hundreds of times a second,
        # and every event would cross into Python only to be ignored. Buttons
        # stay connected: holding Guide has to reach Home from anywhere.
        self._active = True
        if Manette is None:
            print("hdlounge: libmanette is not installed; game controllers are off")
            return

        self._monitor = Manette.Monitor()
        self._monitor.connect("device-connected", lambda _m, device: self._add(device))
        self._monitor.connect(
            "device-disconnected", lambda _m, device: self._remove(device)
        )
        devices = self._monitor.iterate()
        while True:
            ok, device = devices.next()
            if not ok:
                break
            self._add(device)

    def _add(self, device: Manette.Device) -> None:
        if device in self._devices:
            return
        mapper = NavMapper(lambda action: self.emit("nav", action))
        handlers = [
            device.connect("button-press-event", self._on_button, mapper, True),
            device.connect("button-release-event", self._on_button, mapper, False),
        ]
        axes = [
            device.connect("absolute-axis-event", self._on_absolute, mapper),
            device.connect("hat-axis-event", self._on_hat, mapper),
        ]
        if not self._active:
            for handler in axes:
                device.handler_block(handler)
        self._devices[device] = (mapper, handlers + axes)
        trace(f"PAD connected {device.get_name()}")

    def set_active(self, active: bool) -> None:
        """Listen to sticks and hats only while the launcher is in front."""
        if active == self._active:
            return
        self._active = active
        for device, (mapper, handlers) in self._devices.items():
            for handler in handlers[2:]:
                if active:
                    device.handler_unblock(handler)
                else:
                    device.handler_block(handler)
            if not active:
                mapper.release_axes()

    def _remove(self, device: Manette.Device) -> None:
        entry = self._devices.pop(device, None)
        if entry is None:
            return
        mapper, handlers = entry
        mapper.reset()
        for handler in handlers:
            device.disconnect(handler)
        trace(f"PAD disconnected {device.get_name()}")

    @staticmethod
    def _on_button(
        _device: Manette.Device, event: Manette.Event, mapper: NavMapper, pressed: bool
    ) -> None:
        ok, button = event.get_button()
        if ok:
            mapper.button(button, pressed)

    @staticmethod
    def _on_absolute(
        _device: Manette.Device, event: Manette.Event, mapper: NavMapper
    ) -> None:
        ok, axis, value = event.get_absolute()
        if ok:
            mapper.stick(axis, value)

    @staticmethod
    def _on_hat(
        _device: Manette.Device, event: Manette.Event, mapper: NavMapper
    ) -> None:
        ok, axis, value = event.get_hat()
        if ok:
            mapper.hat(axis, value)
