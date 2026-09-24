"""Quick settings: volume, audio output, network/Bluetooth status, power."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from . import system
from .appinfo import lookup
from .keynav import RowNavigator

if TYPE_CHECKING:
    from .audio import Audio
    from .bridge import ShellBridge
    from .tvpower import TvPower

# Written by GSConnect once its service has run for the first time.
GSCONNECT_PREFS = "org.gnome.Shell.Extensions.GSConnect.Preferences.desktop"

# Shut Down, Restart and Log Out want a second press within this long. On a
# remote they are one stray Down and OK away from Change Background.
CONFIRM_SECONDS = 4


class StatusRow(Gtk.Box):
    """Read-only icon + title + value line."""

    def __init__(self, icon_name: str, title: str) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.add_css_class("qs_status_row")

        self._icon = Gtk.Image.new_from_icon_name(icon_name)
        self._icon.set_pixel_size(20)
        self.append(self._icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.set_hexpand(True)
        label = Gtk.Label(label=title, xalign=0.0)
        label.add_css_class("qs_status_title")
        text.append(label)
        self._value = Gtk.Label(label="", xalign=0.0)
        self._value.add_css_class("qs_status_value")
        self._value.set_ellipsize(3)
        text.append(self._value)
        self.append(text)

    def update(self, icon_name: str, value: str) -> None:
        self._icon.set_from_icon_name(icon_name)
        self._value.set_text(value)


class QuickSettings(Gtk.Box):
    """Slide-in panel. Everything here is reachable with arrows + OK."""

    __gsignals__ = {
        "close-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "background-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        audio: Audio,
        network: system.Network,
        bluetooth: system.Bluetooth,
        bridge: ShellBridge,
        tvpower: TvPower,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.audio = audio
        self.network = network
        self.bluetooth = bluetooth
        self.bridge = bridge
        self.tvpower = tvpower

        self.add_css_class("quick_settings")
        self.set_size_request(400, -1)

        # Rows are recomputed on every keypress: audio outputs come and go.
        self._nav = RowNavigator(self._rows)
        self._output_buttons: list[Gtk.Widget] = []

        title = Gtk.Label(label="Quick Settings", xalign=0.0)
        title.add_css_class("qs_title")
        self.append(title)

        # Scrolls, because it cannot know its own height: a graphics card
        # adds an audio output per HDMI and DisplayPort connector, and TV
        # set-up adds a row per TV found. The viewport follows D-pad focus.
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(content)
        self.append(scroller)

        content.append(self._build_volume())
        content.append(self._build_outputs())
        content.append(Gtk.Separator())
        content.append(self._build_status())
        content.append(Gtk.Separator())
        content.append(self._build_tv())
        content.append(Gtk.Separator())
        content.append(self._build_appearance())
        content.append(Gtk.Separator())
        content.append(self._build_power())
        content.append(self._build_credit())

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

        audio.connect("changed", lambda *_a: self._sync_volume())
        audio.connect("sinks-changed", lambda *_a: self._sync_outputs())
        network.connect("changed", lambda *_a: self._sync_status())
        bluetooth.connect("changed", lambda *_a: self._sync_status())
        tvpower.connect("state-changed", lambda *_a: self._sync_tv())

    # Volume
    # -------------------------------------------------------

    def _build_volume(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._vol_icon = Gtk.Image.new_from_icon_name("audio-volume-high-symbolic")
        self._vol_icon.set_pixel_size(20)
        header.append(self._vol_icon)
        label = Gtk.Label(label="Volume", xalign=0.0)
        label.set_hexpand(True)
        label.add_css_class("qs_section")
        header.append(label)
        self._vol_value = Gtk.Label(label="")
        self._vol_value.add_css_class("qs_status_value")
        header.append(self._vol_value)
        box.append(header)

        self._scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.0, 100.0, 5.0
        )
        self._scale.set_draw_value(False)
        self._scale.set_hexpand(True)
        self._scale_handler = self._scale.connect("value-changed", self._on_scale)
        box.append(self._scale)

        self._mute = Gtk.ToggleButton(label="Mute")
        self._mute_handler = self._mute.connect("toggled", self._on_mute)
        box.append(self._mute)

        self._sync_volume()
        return box

    def _on_scale(self, scale: Gtk.Scale) -> None:
        self.audio.set_volume(scale.get_value() / 100.0)

    def _on_mute(self, _button: Gtk.ToggleButton) -> None:
        self.audio.toggle_mute()

    def _sync_volume(self) -> None:
        self._scale.handler_block(self._scale_handler)
        self._scale.set_value(self.audio.volume * 100.0)
        self._scale.handler_unblock(self._scale_handler)

        self._mute.handler_block(self._mute_handler)
        self._mute.set_active(self.audio.muted)
        self._mute.handler_unblock(self._mute_handler)

        self._vol_icon.set_from_icon_name(self.audio.icon_name)
        self._vol_value.set_text(self.audio.label)

    # Outputs
    # -------------------------------------------------------

    def _build_outputs(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label = Gtk.Label(label="Output", xalign=0.0)
        label.add_css_class("qs_section")
        box.append(label)
        self._outputs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self._outputs)
        self._sync_outputs()
        return box

    def _sync_outputs(self) -> None:
        child = self._outputs.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._outputs.remove(child)
            child = nxt
        self._output_buttons = []

        group = None
        for sink in self.audio.sinks:
            button = Gtk.CheckButton(label=sink["description"])
            button.set_group(group)
            group = group or button
            button.set_active(sink["default"])
            button.connect(
                "toggled",
                lambda b, name=sink["name"]: b.get_active()
                and self.audio.set_default_sink(name),
            )
            self._outputs.append(button)
            self._output_buttons.append(button)

        if not self.audio.sinks:
            empty = Gtk.Label(label="No audio outputs found", xalign=0.0)
            empty.add_css_class("qs_status_value")
            self._outputs.append(empty)

    # Status
    # -------------------------------------------------------

    def _build_status(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._net_row = StatusRow("network-offline-symbolic", "Network")
        self._bt_row = StatusRow("bluetooth-disabled-symbolic", "Bluetooth")
        box.append(self._net_row)
        box.append(self._bt_row)

        buttons = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True
        )
        self._settings_buttons = []
        for label, panel in (
            ("Wi-Fi settings", "wifi"),
            ("Bluetooth settings", "bluetooth"),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, p=panel: self._open_settings(p))
            buttons.append(button)
            self._settings_buttons.append(button)
        box.append(buttons)

        # Kiosk mode hides GSConnect's indicator along with the top bar, so its
        # pairing window lives here. Shown only once GSConnect is set up.
        self._phone_button = Gtk.Button(label="Phone Remote")
        self._phone_button.connect("clicked", lambda _b: self._open_phone_remote())
        self._phone_button.set_visible(False)
        box.append(self._phone_button)

        self._sync_status()
        return box

    def _open_phone_remote(self) -> None:
        info = lookup(GSCONNECT_PREFS)
        if info is None:
            return
        try:
            info.launch([], None)
        except GLib.Error as exc:
            print(f"hdlounge: could not open GSConnect ({exc.message})")
            return
        # Same reason as Settings: it may already be open behind the launcher.
        self.bridge.raise_when_ready(GSCONNECT_PREFS)
        self.emit("close-requested")

    def _open_settings(self, panel: str) -> None:
        # Via the bridge, not a bare spawn: Settings is single-instance, so a
        # second invocation of an already-running (and Home-minimised) copy
        # switches its page without ever coming to the front.
        self.bridge.open_external(
            "org.gnome.Settings.desktop", ["gnome-control-center", panel]
        )
        self.emit("close-requested")

    def _sync_status(self) -> None:
        self._net_row.update(self.network.icon_name, self.network.label)
        self._bt_row.update(self.bluetooth.icon_name, self.bluetooth.label)

    # The tv
    # -------------------------------------------------------

    def _build_tv(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label = Gtk.Label(label="TV", xalign=0.0)
        label.add_css_class("qs_section")
        box.append(label)

        self._tv_row = StatusRow("video-display-symbolic", "TV power")
        box.append(self._tv_row)

        self._tv_auto = Gtk.ToggleButton(label="With the screen")
        self._tv_auto_handler = self._tv_auto.connect("toggled", self._on_tv_auto)
        box.append(self._tv_auto)

        buttons = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True
        )
        self._tv_on = Gtk.Button(label="TV On")
        self._tv_on.connect("clicked", lambda *_a: self.tvpower.turn_on())
        self._tv_off = Gtk.Button(label="TV Off")
        self._tv_off.connect("clicked", lambda *_a: self.tvpower.turn_off())
        buttons.append(self._tv_on)
        buttons.append(self._tv_off)
        box.append(buttons)
        self._tv_button_row = buttons
        self._tv_buttons = [self._tv_on, self._tv_off]

        # Set-up happens in place rather than in a dialog: discovery replaces
        # this button with one button per TV found, and choosing one pairs.
        self._tv_setup = Gtk.Button(label="Set Up TV\u2026")
        self._tv_setup.connect("clicked", lambda *_a: self._discover_tvs())
        self._tv_forget = Gtk.Button(label="Forget TV")
        self._tv_forget.connect("clicked", lambda *_a: self.tvpower.forget())
        setup_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True
        )
        setup_row.append(self._tv_setup)
        setup_row.append(self._tv_forget)
        box.append(setup_row)
        self._tv_busy = False  # looking for, or pairing with, a TV

        self._tv_found = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self._tv_found)
        self._tv_found_buttons: list[Gtk.Widget] = []

        self._sync_tv()
        return box

    def _on_tv_auto(self, button: Gtk.ToggleButton) -> None:
        self.tvpower.set_enabled(button.get_active())

    def _sync_tv(self) -> None:
        power = self.tvpower
        if power.status:
            value = power.status
        elif not power.configured:
            value = "Not set up"
        elif power.enabled:
            value = f"{power.host} \u2014 off with the screen"
        else:
            value = f"{power.host} \u2014 manual"
        self._tv_row.update("video-display-symbolic", value)

        self._tv_auto.handler_block(self._tv_auto_handler)
        self._tv_auto.set_active(power.enabled)
        self._tv_auto.handler_unblock(self._tv_auto_handler)

        # Hidden rather than greyed out until a TV has been paired: the panel
        # does not scroll, and controls that cannot do anything yet are just
        # height taken from the sections below.
        self._tv_auto.set_visible(power.configured)
        self._tv_button_row.set_visible(power.configured)
        self._tv_forget.set_visible(power.configured)
        self._tv_setup.set_label(
            "Change TV\u2026" if power.configured else "Set Up TV\u2026"
        )

    def _clear_found(self) -> None:
        child = self._tv_found.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._tv_found.remove(child)
            child = nxt
        self._tv_found_buttons = []

    def _set_tv_busy(self, busy: bool) -> None:
        # One search or pairing at a time: a second press would put a
        # second accept prompt on the TV.
        self._tv_busy = busy
        self._tv_setup.set_sensitive(not busy)
        self._tv_forget.set_sensitive(not busy)

    def _discover_tvs(self) -> None:
        if self._tv_busy:
            return
        self._clear_found()
        self._set_tv_busy(True)
        self._tv_row.update("video-display-symbolic", "Looking for a TV\u2026")

        def found(tvs: list[tuple[str, str]]) -> None:
            self._set_tv_busy(False)
            if not tvs:
                self._tv_row.update(
                    "video-display-symbolic",
                    "No TV answered. Is it switched on, on this network?",
                )
                return
            for host, name in tvs:
                button = Gtk.Button(label=f"{name} ({host})" if name else host)
                button.connect("clicked", lambda _b, h=host: self._pair_tv(h))
                self._tv_found.append(button)
                self._tv_found_buttons.append(button)
            self._tv_row.update("video-display-symbolic", "Which one is it?")
            # A remote has nothing to aim at otherwise.
            self._tv_found_buttons[0].grab_focus()

        self.tvpower.discover(found)

    def _pair_tv(self, host: str) -> None:
        if self._tv_busy:
            return
        self._clear_found()
        self._set_tv_busy(True)
        # The result arrives through state-changed, which redraws the row.
        self.tvpower.pair(host, lambda _ok, _message: self._set_tv_busy(False))
        self._tv_row.update(
            "video-display-symbolic", "Press OK on the TV to allow HDLounge"
        )

    # Appearance
    # -------------------------------------------------------

    def _build_appearance(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label = Gtk.Label(label="Appearance", xalign=0.0)
        label.add_css_class("qs_section")
        box.append(label)

        button = Gtk.Button(label="Change Background\u2026")
        button.connect("clicked", lambda *_a: self.emit("background-requested"))
        box.append(button)
        self._background_button = button
        return box

    # Power
    # -------------------------------------------------------

    def _build_power(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label = Gtk.Label(label="Power", xalign=0.0)
        label.add_css_class("qs_section")
        box.append(label)

        grid = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True
        )
        actions = (
            ("Shut Down", "system-shutdown-symbolic", system.power_off, True),
            ("Restart", "system-reboot-symbolic", system.reboot, False),
            ("Log Out", "system-log-out-symbolic", system.log_out, False),
        )
        self._power_buttons = []
        self._armed: tuple | None = None  # (button, label, text, timer)
        for text, icon, handler, destructive in actions:
            button = Gtk.Button()
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            image = Gtk.Image.new_from_icon_name(icon)
            image.set_pixel_size(20)
            content.append(image)
            label = Gtk.Label(label=text)
            content.append(label)
            button.set_child(content)
            if destructive:
                button.add_css_class("destructive-action")
            button.connect(
                "clicked",
                lambda b, l=label, t=text, fn=handler: self._press_power(b, l, t, fn),
            )
            # Moving off the button is changing one's mind.
            focus = Gtk.EventControllerFocus()
            focus.connect("leave", lambda _c, b=button: self._disarm_power(b))
            button.add_controller(focus)
            grid.append(button)
            self._power_buttons.append(button)
        box.append(grid)
        return box

    def _press_power(
        self,
        button: Gtk.Button,
        label: Gtk.Label,
        text: str,
        handler: Callable[[], None],
    ) -> None:
        if self._armed is not None and self._armed[0] is button:
            self._disarm_power()
            handler()
            return
        self._disarm_power()

        def expired() -> bool:
            self._armed = (button, label, text, 0)
            self._disarm_power()
            return GLib.SOURCE_REMOVE

        label.set_label("Press again")
        self._armed = (
            button,
            label,
            text,
            GLib.timeout_add_seconds(CONFIRM_SECONDS, expired),
        )

    def _disarm_power(self, only: Gtk.Button | None = None) -> None:
        if self._armed is None or (only is not None and self._armed[0] is not only):
            return
        _button, label, text, timer = self._armed
        self._armed = None
        if timer:
            GLib.source_remove(timer)
        label.set_label(text)

    # Credit
    # -------------------------------------------------------

    def _build_credit(self) -> Gtk.Widget:
        label = Gtk.Label()
        label.set_markup(
            "HDLounge created by "
            '<a href="https://harmonicdesign.ca">Harmonic Design</a>'
        )
        label.add_css_class("qs_credit")
        label.set_justify(Gtk.Justification.CENTER)
        label.set_halign(Gtk.Align.CENTER)
        label.set_margin_top(6)
        # Focusable so the link is reachable with a D-pad, like everything else.
        label.set_focusable(True)
        label.connect("activate-link", self._on_credit_link)
        self._credit = label
        return label

    def _on_credit_link(self, _label: Gtk.Label, uri: str) -> bool:
        Gtk.UriLauncher.new(uri=uri).launch(None, None, None, None)
        # A browser that is already running behind the launcher will not come
        # forward by itself, the same way Settings does not.
        browser = Gio.AppInfo.get_default_for_uri_scheme("https")
        if browser is not None:
            self.bridge.raise_when_ready(browser.get_id())
        self.emit("close-requested")
        return True  # handled; do not also run GTK's default

    # Keys
    # -------------------------------------------------------

    def _rows(self) -> list[list[Gtk.Widget]]:
        """Vertical stack of controls; inner lists are side-by-side."""
        rows: list[list[Gtk.Widget]] = [[self._scale], [self._mute]]
        rows += [[b] for b in self._output_buttons]
        rows.append(list(self._settings_buttons))
        rows.append([self._phone_button])
        rows.append([self._tv_auto])
        rows.append(list(self._tv_buttons))
        rows.append([self._tv_setup, self._tv_forget])
        rows += [[b] for b in self._tv_found_buttons]
        rows.append([self._background_button])
        rows.append(list(self._power_buttons))
        rows.append([self._credit])
        return rows

    def focus_first(self) -> bool:
        return self._nav.focus_first()

    def _on_key_pressed(
        self,
        _c: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        return self.handle_key(keyval)

    def handle_key(self, keyval: int) -> bool:
        if keyval in (
            Gdk.KEY_Escape,
            Gdk.KEY_BackSpace,
            Gdk.keyval_from_name("XF86Back"),
        ):
            self.emit("close-requested")
            return True
        # GTK4 will not walk this panel with a D-pad on its own.
        return self._nav.handle(keyval)

    def refresh(self) -> None:
        self._disarm_power()
        self._phone_button.set_visible(lookup(GSCONNECT_PREFS) is not None)
        self._sync_tv()
        self.audio.refresh()
        self.network.refresh()
        self.bluetooth.refresh()
