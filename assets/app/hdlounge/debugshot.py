"""Env-gated self-screenshot, for testing in a headless nested session.

GNOME 50 refuses org.gnome.Shell.Screenshot for untrusted callers, so when
HDLOUNGE_SCREENSHOT is set the app renders its own window to a PNG instead.
Not used in normal operation.
"""

from __future__ import annotations

import os
from typing import Callable

from gi.repository import Gdk, Gio, GLib, Gtk

DEBUG = bool(os.environ.get("HDLOUNGE_DEBUG_KEYS"))


def trace(message: str) -> None:
    """Emit a test-visible trace line when key debugging is on."""
    if DEBUG:
        print(f"TRACE {message}")


def _focus_label(window: Gtk.Window) -> str:
    focus = window.get_focus()
    if focus is None:
        return "None"
    app_id = getattr(focus, "app_id", None)
    return f"{type(focus).__name__}({app_id})" if app_id else type(focus).__name__


def maybe_trace_keys(window: Gtk.Window) -> None:
    """HDLOUNGE_DEBUG_KEYS=1: log every key the toplevel sees, plus focus state."""
    if not os.environ.get("HDLOUNGE_DEBUG_KEYS"):
        return

    def report(tag: str) -> Callable[..., bool]:
        def handler(
            _c: Gtk.EventControllerKey,
            keyval: int,
            keycode: int,
            state: Gdk.ModifierType,
        ) -> bool:
            name = Gdk.keyval_name(keyval) or keyval
            print(
                f"KEY[{tag}] {name} code={keycode} state={int(state)} "
                f"active={window.is_active()} focus={_focus_label(window)}"
            )
            return False

        return handler

    for phase, tag in (
        (Gtk.PropagationPhase.CAPTURE, "capture"),
        (Gtk.PropagationPhase.BUBBLE, "bubble"),
    ):
        c = Gtk.EventControllerKey()
        c.set_propagation_phase(phase)
        c.connect("key-pressed", report(tag))
        window.add_controller(c)

    def state(*_a: object) -> None:
        print(f"STATE active={window.is_active()} focus={_focus_label(window)}")

    window.connect("notify::is-active", state)
    GLib.timeout_add_seconds(3, lambda: (state(), GLib.SOURCE_CONTINUE)[1])


NAV_IFACE = """
<node>
  <interface name="ca.harmonicdesign.HDLounge.Debug">
    <method name="Nav">
      <arg type="s" direction="in" name="action"/>
    </method>
  </interface>
</node>"""


def maybe_export_nav(window: Gtk.Window) -> None:
    """HDLOUNGE_DEBUG_KEYS=1: take controller actions over D-Bus.

    A virtual game controller cannot be plugged in without root, so the tests
    drive the window's controller path (HDLoungeWindow._on_pad_nav) from here.
    """
    if not DEBUG:
        return

    node = Gio.DBusNodeInfo.new_for_xml(NAV_IFACE)
    window._debug_nav_node = node  # keep the interface info alive

    def on_call(
        _conn: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _iface: str,
        _method: str,
        params: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        window._on_pad_nav(params.unpack()[0])
        invocation.return_value(None)

    def on_bus(conn: Gio.DBusConnection, _name: str) -> None:
        conn.register_object(
            "/ca/harmonicdesign/HDLounge/Debug", node.interfaces[0], on_call, None, None
        )

    Gio.bus_own_name(
        Gio.BusType.SESSION,
        "ca.harmonicdesign.HDLounge.Debug",
        Gio.BusNameOwnerFlags.NONE,
        on_bus,
        None,
        None,
    )


def maybe_arm(window: Gtk.Window) -> None:
    path = os.environ.get("HDLOUNGE_SCREENSHOT")
    if not path:
        return
    delay = int(os.environ.get("HDLOUNGE_SCREENSHOT_DELAY", "5"))
    if os.environ.get("HDLOUNGE_SCREENSHOT_QS"):
        GLib.timeout_add_seconds(max(1, delay - 2), _open_qs, window)
    GLib.timeout_add_seconds(delay, _capture, window, path)


def _open_qs(window: Gtk.Window) -> bool:
    window.show_quick_settings()
    return GLib.SOURCE_REMOVE


def _capture(window: Gtk.Window, path: str) -> bool:
    try:
        width, height = window.get_width(), window.get_height()
        if width <= 0 or height <= 0:
            print("hdlounge: window not realised yet, cannot screenshot")
            return GLib.SOURCE_REMOVE

        paintable = Gtk.WidgetPaintable.new(window)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is None:
            print("hdlounge: empty render node")
            return GLib.SOURCE_REMOVE

        renderer = window.get_native().get_renderer()
        texture = renderer.render_texture(node, None)
        texture.save_to_png(path)
        print(f"hdlounge: wrote screenshot {path} ({width}x{height})")
    except Exception as exc:  # debug helper: never crash the app
        print(f"hdlounge: screenshot failed: {exc}")

    if os.environ.get("HDLOUNGE_SCREENSHOT_QUIT"):
        app = window.get_application()
        if app is not None:
            app.quit()
    return GLib.SOURCE_REMOVE
