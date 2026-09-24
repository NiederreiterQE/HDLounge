"""Power actions, network status and Bluetooth status."""

from __future__ import annotations

import os

from gi.repository import Gio, GLib, GObject

from . import proc

LOGIN1 = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_MANAGER = "org.freedesktop.login1.Manager"


# Power
# -------------------------------------------------------


def _login1_call(method: str) -> None:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        bus.call(
            LOGIN1,
            LOGIN1_PATH,
            LOGIN1_MANAGER,
            method,
            GLib.Variant("(b)", (False,)),  # interactive=False
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
            None,
        )
    except GLib.Error as exc:
        print(f"hdlounge: logind {method} failed: {exc.message}")


def power_off() -> None:
    _login1_call("PowerOff")


def reboot() -> None:
    _login1_call("Reboot")


def suspend() -> None:
    _login1_call("Suspend")


def log_out() -> None:
    proc.fire(["gnome-session-quit", "--logout", "--no-prompt"])


# Network
# -------------------------------------------------------


class Network(GObject.Object):
    """Coarse NetworkManager status: enough for an at-a-glance indicator."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self) -> None:
        super().__init__()
        self.state = "unknown"  # connected / disconnected / unknown
        self.primary = ""  # name of the active connection
        self.kind = ""  # wifi / ethernet / ...
        self.refresh()

    def refresh(self) -> None:
        proc.run(
            ["nmcli", "-t", "-f", "STATE,CONNECTIVITY", "general"], self._on_general
        )

    def _on_general(self, stdout: str | None) -> None:
        if stdout is None:
            self._update("unavailable", "", "")
            return
        first = (stdout.strip().splitlines() or [""])[0]
        state = first.split(":")[0] if first else "unknown"

        def on_active(active: str | None) -> None:
            name, kind = "", ""
            for line in (active or "").strip().splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[1] in (
                    "802-11-wireless",
                    "802-3-ethernet",
                ):
                    name = parts[0]
                    kind = "wifi" if parts[1] == "802-11-wireless" else "ethernet"
                    break
            self._update(state, name, kind)

        proc.run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
            on_active,
        )

    def _update(self, state: str, primary: str, kind: str) -> None:
        if (state, primary, kind) != (self.state, self.primary, self.kind):
            self.state, self.primary, self.kind = state, primary, kind
            self.emit("changed")

    @property
    def label(self) -> str:
        if self.state == "unavailable":
            return "NetworkManager unavailable"
        if self.primary:
            return self.primary
        return "Not connected"

    @property
    def icon_name(self) -> str:
        if self.state != "connected":
            return "network-offline-symbolic"
        if self.kind == "ethernet":
            return "network-wired-symbolic"
        return "network-wireless-signal-excellent-symbolic"


# Bluetooth
# -------------------------------------------------------

# At or below this, a remote or controller gets a warning on the home screen.
# One that goes flat mid-film leaves a TV box with nothing to drive it.
LOW_BATTERY_PERCENT = 15


def parse_bluez(
    objects: dict,
) -> tuple[bool, bool, list[str], list[tuple[str, int, str]]]:
    """Reduce BlueZ's GetManagedObjects to what the launcher shows.

    Returns (adapter present, adapter powered, names of connected devices,
    [(name, battery percent, icon name)] for the connected ones that report a
    battery). BlueZ exposes the battery of HID devices -- remotes, controllers
    -- as org.bluez.Battery1, and the icon says what kind of device it is.
    """
    present = powered = False
    devices: list[str] = []
    batteries: list[tuple[str, int, str]] = []
    for interfaces in objects.values():
        adapter = interfaces.get("org.bluez.Adapter1")
        if adapter is not None:
            present = True
            powered = powered or bool(adapter.get("Powered", False))
        device = interfaces.get("org.bluez.Device1")
        if device is None or not device.get("Connected", False):
            continue
        name = device.get("Alias") or device.get("Address", "device")
        devices.append(name)
        battery = interfaces.get("org.bluez.Battery1")
        if battery is not None and "Percentage" in battery:
            batteries.append((name, int(battery["Percentage"]), device.get("Icon", "")))
    return present, powered, sorted(devices), sorted(batteries)


class Bluetooth(GObject.Object):
    """Adapter power state, connected devices and their batteries, from BlueZ."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self) -> None:
        super().__init__()
        self.present = False
        self.powered = False
        self.devices: list[str] = []  # aliases of connected devices
        self.batteries: list[tuple[str, int, str]] = []  # (alias, %, icon)
        self.refresh()

    def refresh(self) -> None:
        fake = os.environ.get("HDLOUNGE_FAKE_BATTERY", "")
        if fake.isdigit():
            # Testing only: a connected remote at this charge, no hardware.
            self._update(
                True,
                True,
                ["SHIELD Remote"],
                [("SHIELD Remote", int(fake), "input-keyboard")],
            )
            return

        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            self._update(False, False, [], [])
            return

        bus.call(
            "org.bluez",
            "/",
            "org.freedesktop.DBus.ObjectManager",
            "GetManagedObjects",
            None,
            GLib.VariantType("(a{oa{sa{sv}}})"),
            Gio.DBusCallFlags.NONE,
            3000,
            None,
            self._on_objects,
            None,
        )

    def _on_objects(
        self, bus: Gio.DBusConnection, result: Gio.AsyncResult, _data: object
    ) -> None:
        try:
            objects = bus.call_finish(result).unpack()[0]
        except GLib.Error:
            self._update(False, False, [], [])
            return
        self._update(*parse_bluez(objects))

    def _update(
        self,
        present: bool,
        powered: bool,
        devices: list[str],
        batteries: list[tuple[str, int, str]],
    ) -> None:
        state = (present, powered, devices, batteries)
        if state != (self.present, self.powered, self.devices, self.batteries):
            self.present, self.powered, self.devices, self.batteries = state
            self.emit("changed")

    @property
    def label(self) -> str:
        if not self.present:
            return "No adapter"
        if not self.powered:
            return "Off"
        if self.devices:
            charge = {name: percent for name, percent, _icon in self.batteries}
            return ", ".join(
                f"{name} ({charge[name]}%)" if name in charge else name
                for name in self.devices
            )
        return "On, nothing connected"

    @property
    def low_batteries(self) -> list[tuple[str, int]]:
        """Connected input devices (remotes, controllers) running low."""
        return [
            (name, percent)
            for name, percent, icon in self.batteries
            if icon.startswith("input-") and percent <= LOW_BATTERY_PERCENT
        ]

    @property
    def icon_name(self) -> str:
        if not self.present or not self.powered:
            return "bluetooth-disabled-symbolic"
        return "bluetooth-active-symbolic"
