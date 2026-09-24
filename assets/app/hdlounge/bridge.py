"""Client for the HDLounge shell extension's D-Bus interface.

Everything that needs compositor privileges (enumerating, focusing,
fullscreening, minimising or killing windows) lives on the other side of this.
If the extension is not running, every call degrades to a no-op and
`available` goes False so the UI can say so instead of silently misbehaving.
"""

from __future__ import annotations

import os
from typing import Callable

from gi.repository import Gio, GLib, GObject

# A helper app we just spawned may take a moment to map its window, so keep
# asking the compositor to focus it for a few seconds before giving up.
FOCUS_CHASE_ATTEMPTS = 20
FOCUS_CHASE_INTERVAL_MS = 300

BUS_NAME = "ca.harmonicdesign.HDLounge.Shell"
OBJECT_PATH = "/ca/harmonicdesign/HDLounge/Shell"
INTERFACE = "ca.harmonicdesign.HDLounge.Shell"


class ShellBridge(GObject.Object):
    """Proxy onto the shell extension."""

    __gsignals__ = {
        # Emitted when the set of running apps, or the focused app, changes.
        "running-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # Emitted when the global Home key was pressed.
        "home-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # Emitted when the remote's Power button was pressed.
        "tv-power-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # Emitted when the extension appears or disappears.
        "availability-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._proxy: Gio.DBusProxy | None = None
        self._available = False
        self._connect()

    # Lifecycle
    # -------------------------------------------------------

    def _connect(self) -> None:
        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            None,
            BUS_NAME,
            OBJECT_PATH,
            INTERFACE,
            None,
            self._on_proxy_ready,
        )

    def _on_proxy_ready(
        self, _source: GObject.Object | None, result: Gio.AsyncResult
    ) -> None:
        try:
            self._proxy = Gio.DBusProxy.new_for_bus_finish(result)
        except GLib.Error as exc:
            print(f"hdlounge: shell extension unavailable ({exc.message})")
            self._set_available(False)
            return

        self._proxy.connect("g-signal", self._on_signal)
        self._proxy.connect("notify::g-name-owner", self._on_owner_changed)
        self._on_owner_changed()

    def _on_owner_changed(self, *_args: object) -> None:
        owned = bool(self._proxy and self._proxy.get_name_owner())
        self._set_available(owned)
        if owned:
            # Tell the extension which process owns the launcher window, so it
            # can identify it for Home / minimise-all without guessing wm_class.
            self.register_launcher()

    def _set_available(self, value: bool) -> None:
        if value == self._available:
            return
        self._available = value
        self.emit("availability-changed", value)

    def _on_signal(
        self,
        _proxy: Gio.DBusProxy,
        _sender: str,
        signal_name: str,
        _params: GLib.Variant,
    ) -> None:
        if signal_name == "RunningChanged":
            self.emit("running-changed")
        elif signal_name == "HomeRequested":
            self.emit("home-requested")
        elif signal_name == "TvPowerRequested":
            self.emit("tv-power-requested")

    @property
    def available(self) -> bool:
        return self._available

    # Calling
    # -------------------------------------------------------

    def _call(
        self,
        method: str,
        params: GLib.Variant | None = None,
        on_result: Callable[[GLib.Variant], None] | None = None,
    ) -> None:
        """Fire a method asynchronously; never block the UI on the compositor."""
        if not self._proxy or not self._available:
            return

        def done(proxy: Gio.DBusProxy, result: Gio.AsyncResult) -> None:
            try:
                value = proxy.call_finish(result)
            except GLib.Error as exc:
                print(f"hdlounge: {method} failed: {exc.message}")
                return
            if on_result is not None:
                on_result(value)

        self._proxy.call(method, params, Gio.DBusCallFlags.NONE, 3000, None, done)

    # Methods
    # -------------------------------------------------------

    def register_launcher(self) -> None:
        self._call("RegisterLauncher", GLib.Variant("(u)", (os.getpid(),)))

    def launch_or_focus(self, app_id: str, fullscreen: bool = True) -> None:
        """Focus the app if it is already running, otherwise launch it."""
        self._call("LaunchOrFocus", GLib.Variant("(sb)", (app_id, fullscreen)))

    def focus_app(
        self, app_id: str, on_result: Callable[[bool], None] | None = None
    ) -> None:
        """Raise and focus a running app. Reports False if it is not running."""
        self._call(
            "FocusApp",
            GLib.Variant("(s)", (app_id,)),
            (
                None
                if on_result is None
                else lambda value: on_result(bool(value.unpack()[0]))
            ),
        )

    def open_external(self, app_id: str, argv: list[str]) -> None:
        """Spawn a helper app and make sure it ends up in front.

        Settings and Software are single-instance GApplications: running the
        command a second time only sends an activation to the copy that is
        already there. After Home minimised that copy, the activation leaves
        it minimised in the background and the screen appears not to react.
        Spawning still does the useful work (it is what selects the Wi-Fi or
        Bluetooth page), so we spawn *and* have the compositor raise it.
        """
        from . import proc

        proc.fire(argv)
        self.raise_when_ready(app_id)

    def raise_when_ready(self, app_id: str) -> None:
        """Bring an app forward once it has a window. Never launches it."""
        self._chase_focus(app_id, FOCUS_CHASE_ATTEMPTS)

    def _chase_focus(self, app_id: str, attempts: int) -> None:
        if attempts <= 0 or not self._available:
            return

        def retry() -> bool:
            self._chase_focus(app_id, attempts - 1)
            return GLib.SOURCE_REMOVE

        def result(focused: bool) -> None:
            if not focused:  # not mapped yet -- it is still starting up
                GLib.timeout_add(FOCUS_CHASE_INTERVAL_MS, retry)

        self.focus_app(app_id, result)

    def close_app(self, app_id: str) -> None:
        self._call("CloseApp", GLib.Variant("(s)", (app_id,)))

    def kill_app(self, app_id: str) -> None:
        self._call("KillApp", GLib.Variant("(s)", (app_id,)))

    def show_home(self) -> None:
        self._call("ShowHome")

    def focus_launcher(self) -> None:
        """Ask the compositor to give us keyboard focus.

        A Wayland client cannot focus itself; without this the window maps
        but receives no key events at all.
        """
        self._call("FocusLauncher")

    def list_running(self, callback: Callable[[dict[str, dict]], None]) -> None:
        """Fetch running apps as {app_id: {"windows": int, "focused": bool}}."""
        if not self._proxy or not self._available:
            callback({})
            return

        def unpack(value: GLib.Variant) -> None:
            result = {}
            for app_id, name, windows, focused in value.unpack()[0]:
                result[app_id] = {"name": name, "windows": windows, "focused": focused}
            callback(result)

        self._call("ListRunning", None, unpack)
