"""Noticing when GNOME blanks the screen, and when it comes back."""

from __future__ import annotations

from gi.repository import Gio, GLib, GObject

SCREENSAVER = "org.gnome.ScreenSaver"
SCREENSAVER_PATH = "/org/gnome/ScreenSaver"

DISPLAY_CONFIG = "org.gnome.Mutter.DisplayConfig"
DISPLAY_CONFIG_PATH = "/org/gnome/Mutter/DisplayConfig"
PROPERTIES = "org.freedesktop.DBus.Properties"


class BlankWatcher(GObject.Object):
    """Emits `blanked` when the screen goes dark and `unblanked` when it returns.

    Two sources feed one piece of state. The screensaver is the one that
    matters; the monitor's power-save mode is a cheap second opinion for a
    session where the shield is disabled outright. Repeats collapse, so a
    screen that reports itself blank twice only turns the TV off once.
    """

    __gsignals__ = {
        "blanked": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "unblanked": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self._blank = False
        self._subscriptions: list[int] = []
        self._bus: Gio.DBusConnection | None = None

        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            print(
                f"hdlounge: no session bus, so the screen cannot be watched ({exc.message})"
            )
            return

        self._subscriptions.append(
            self._bus.signal_subscribe(
                None,
                SCREENSAVER,
                "ActiveChanged",
                SCREENSAVER_PATH,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_active_changed,
            )
        )
        self._subscriptions.append(
            self._bus.signal_subscribe(
                None,
                PROPERTIES,
                "PropertiesChanged",
                DISPLAY_CONFIG_PATH,
                DISPLAY_CONFIG,
                Gio.DBusSignalFlags.NONE,
                self._on_properties,
            )
        )

    def stop(self) -> None:
        for subscription in self._subscriptions:
            if self._bus is not None:
                self._bus.signal_unsubscribe(subscription)
        self._subscriptions = []

    @property
    def blanked(self) -> bool:
        return self._blank

    # Sources
    # -------------------------------------------------------

    def _on_active_changed(
        self,
        _bus: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _iface: str,
        _signal: str,
        params: GLib.Variant,
    ) -> None:
        self._set_blank(bool(params.unpack()[0]))

    def _on_properties(
        self,
        _bus: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _iface: str,
        _signal: str,
        params: GLib.Variant,
    ) -> None:
        changed = params.unpack()[1]
        if "PowerSaveMode" not in changed:
            return
        mode = changed["PowerSaveMode"]
        if mode < 0:  # -1: the driver cannot say, so ignore it
            return
        self._set_blank(mode != 0)

    def _set_blank(self, value: bool) -> None:
        if value == self._blank:
            return
        self._blank = value
        self.emit("blanked" if value else "unblanked")
