"""The HDLounge home screen: background, app drawer, quick settings."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable

from gi.repository import Adw, Gdk, GLib, Gio, GObject, Gtk

from . import webapps
from .audio import Audio
from .bubble import FocusBubble
from .debugshot import maybe_arm, maybe_export_nav, maybe_trace_keys, trace
from .drawer import ACTIVATE_KEYS, REPEAT_SETTLE_MS, AppDrawer, AppTile
from .gamepad import Gamepads
from .keynav import REMOTE_OK, RowNavigator
from .picker import AppPicker
from .quicksettings import QuickSettings
from .system import Bluetooth, Network

if TYPE_CHECKING:
    from .bridge import ShellBridge
    from .config import Config
    from .tvpower import TvPower

KEY_XF86_BACK = Gdk.keyval_from_name("XF86Back")
KEY_XF86_HOME = Gdk.keyval_from_name("XF86HomePage")
KEY_XF86_MUTE = Gdk.keyval_from_name("XF86AudioMute")
KEY_XF86_VOL_UP = Gdk.keyval_from_name("XF86AudioRaiseVolume")
KEY_XF86_VOL_DOWN = Gdk.keyval_from_name("XF86AudioLowerVolume")

RUNNING_POLL_SECONDS = 5
CURSOR_IDLE_SECONDS = 10
# The key hints are a reminder, not furniture: show them on arrival at the
# home screen, then get them out of the picture.
HINT_FADE_SECONDS = 3
# Below this, a "motion" event is a surface crossing rather than a real move.
POINTER_MOVE_EPSILON = 3.0

# Remote "menu"/"settings" style buttons, for opening quick settings.
MENU_KEYS = {
    Gdk.KEY_Menu,
    Gdk.keyval_from_name("XF86MenuKB"),
    Gdk.keyval_from_name("XF86MenuPB"),
    Gdk.keyval_from_name("XF86Tools"),
    Gdk.keyval_from_name("XF86Explorer"),
}

# Left and Right belong to the drawer even while a tile's options menu is
# open: the menu steps aside and the selection moves on.
DRAWER_STEPS = {
    Gdk.KEY_Left: -1,
    Gdk.KEY_KP_Left: -1,
    Gdk.KEY_Right: 1,
    Gdk.KEY_KP_Right: 1,
}
# Up and Down stop at the ends of that menu. A GtkPopover is a native surface
# of its own, so a key the menu does not consume is handed to GTK's default
# focus movement, which walks straight out into the window behind and strands
# the menu over a tile that is no longer the selected one.
MENU_TRAPPED_KEYS = {Gdk.KEY_Up, Gdk.KEY_KP_Up, Gdk.KEY_Down, Gdk.KEY_KP_Down}

# Controller actions (gamepad.py) and the keys they stand in for.
NAV_KEYS = {
    "up": Gdk.KEY_Up,
    "down": Gdk.KEY_Down,
    "left": Gdk.KEY_Left,
    "right": Gdk.KEY_Right,
    "ok": REMOTE_OK,
    "back": Gdk.KEY_Escape,
    "menu": Gdk.KEY_Menu,
}
NAV_DIRECTIONS = {
    "up": Gtk.DirectionType.UP,
    "down": Gtk.DirectionType.DOWN,
    "left": Gtk.DirectionType.LEFT,
    "right": Gtk.DirectionType.RIGHT,
}


def _icon(names: tuple[str, ...]) -> Gtk.Image:
    """An image for the first of `names` the icon theme actually has.

    Gio.ThemedIcon's own fallback list is searched theme by theme rather than
    name by name, so Adwaita's shapeless legacy "software install" icon wins
    over GNOME Software's own icon in hicolor. Ask for each name in turn
    instead, in our order of preference.
    """
    display = Gdk.Display.get_default()
    theme = Gtk.IconTheme.get_for_display(display) if display else None
    for name in names:
        if theme is None or theme.has_icon(name):
            return Gtk.Image.new_from_icon_name(name)
    return Gtk.Image.new_from_icon_name(names[-1])


def _blank_cursor() -> Gdk.Cursor | None:
    """A cursor that renders nothing, for the idle-hide behaviour."""
    cursor = Gdk.Cursor.new_from_name("none", None)
    if cursor is not None:
        return cursor
    # Some themes lack "none"; a 1x1 transparent texture always works.
    try:
        texture = Gdk.MemoryTexture.new(
            1, 1, Gdk.MemoryFormat.R8G8B8A8_PREMULTIPLIED, GLib.Bytes.new(bytes(4)), 4
        )
        return Gdk.Cursor.new_from_texture(texture, 0, 0)
    except Exception:
        return None


class HDLoungeWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        application: Adw.Application,
        config: Config,
        bridge: ShellBridge,
        tvpower: TvPower,
    ) -> None:
        super().__init__(application=application)
        self.config = config
        self.bridge = bridge
        self.tvpower = tvpower

        self.audio = Audio()
        self.network = Network()
        self.bluetooth = Bluetooth()

        self.set_title("HDLounge")
        self.set_default_size(1920, 1080)
        self.fullscreen()

        self._build_ui()
        # Start the hints' countdown when the window actually appears, not
        # when it is built: on a slow boot those are seconds apart.
        self.connect("map", lambda *_a: self._show_hints())
        self._connect_bridge()
        self._install_keys()
        self._install_cursor_hiding()
        self._open_menu = None
        # The open menu's key handler, so controller input can be routed into
        # it (see dispatch_nav).
        self._open_menu_keys = None
        self._menu_restores_focus = True

        self.bluetooth.connect("changed", lambda *_a: self._sync_battery_warning())
        self._sync_battery_warning()

        # Game controllers drive the launcher like a remote; see gamepad.py.
        self._pads = Gamepads()
        self._pads.connect("nav", lambda _p, action: self._on_pad_nav(action))
        self.connect("notify::is-active", lambda *_a: self._on_active_changed())

        # This window is the desktop. A stray Alt+F4 must not leave the TV
        # with nothing on screen -- Ctrl+Q is the deliberate way out.
        self.connect("close-request", lambda *_a: True)

        self._background_shown: tuple | None = None
        self.reload()
        GLib.timeout_add_seconds(RUNNING_POLL_SECONDS, self._poll)
        self._schedule_clock()
        GLib.idle_add(self._focus_drawer)
        self._focus_attempts = 0
        GLib.timeout_add(500, self._ensure_keyboard_focus)
        maybe_trace_keys(self)
        maybe_arm(self)
        maybe_export_nav(self)

    # UI
    # -------------------------------------------------------

    def _build_ui(self) -> None:
        self._overlay = Gtk.Overlay()
        self.set_content(self._overlay)

        self._background = Gtk.Picture()
        self._background.set_content_fit(Gtk.ContentFit.COVER)
        self._background.set_can_shrink(True)
        self._overlay.set_child(self._background)

        self._scrim = Gtk.Box()
        self._scrim.add_css_class("scrim")
        self._scrim.set_can_target(False)
        self._overlay.add_overlay(self._scrim)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._overlay.add_overlay(content)

        # Created before the top bar so its buttons can attach to it, but
        # stacked after the content so it draws on top.
        self._bubble = FocusBubble(self._overlay)
        content.append(self._build_topbar())

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        content.append(spacer)

        self._drawer = AppDrawer()
        self._drawer.connect("app-activated", self._on_app_activated)
        self._drawer.connect("app-menu", self._on_app_menu)
        # Up leaves the drawer for the action bar; Down opens the focused
        # tile's options, so a plain D-pad reaches everything.
        self._drawer.connect("nav-up", lambda *_a: self.focus_actions())
        self._drawer.connect("nav-down", lambda *_a: self._open_focused_menu())
        self._drawer.connect("extras-changed", lambda *_a: self._sync_empty())
        content.append(self._drawer)

        self._empty = Gtk.Label(label="No apps pinned yet -- press A to add one")
        self._empty.add_css_class("empty_hint")
        self._empty.set_visible(False)
        content.append(self._empty)

        spacer2 = Gtk.Box()
        spacer2.set_vexpand(True)
        content.append(spacer2)

        content.append(self._build_hints())
        self._overlay.add_overlay(self._bubble)

        # Quick settings slides in from the right.
        self._qs = QuickSettings(
            self.audio, self.network, self.bluetooth, self.bridge, self.tvpower
        )
        self._qs.connect("close-requested", lambda *_a: self.hide_quick_settings())
        self._qs.connect("background-requested", lambda *_a: self.choose_background())

        self._qs_revealer = Gtk.Revealer()
        self._qs_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self._qs_revealer.set_transition_duration(180)
        self._qs_revealer.set_halign(Gtk.Align.END)
        self._qs_revealer.set_valign(Gtk.Align.FILL)
        self._qs_revealer.set_child(self._qs)
        self._qs_revealer.set_reveal_child(False)
        # While hidden the revealer must not stay the topmost overlay child,
        # or popovers parented to a tile below it map with a non-topmost
        # parent and lose their grab.
        self._qs_revealer.set_visible(False)
        self._qs_revealer.connect("notify::child-revealed", self._on_qs_revealed)
        self._overlay.add_overlay(self._qs_revealer)

    def _build_topbar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bar.add_css_class("topbar")

        self._clock = Gtk.Label(xalign=0.0)
        self._clock.add_css_class("clock")
        bar.append(self._clock)

        self._date = Gtk.Label(xalign=0.0)
        self._date.add_css_class("date")
        self._date.set_hexpand(True)
        bar.append(self._date)

        self._warning = Gtk.Label(label="")
        self._warning.add_css_class("warning_chip")
        self._warning.set_visible(False)
        bar.append(self._warning)

        # A remote or controller about to go flat, said while there is still
        # time to find a charger.
        self._battery_chip = Gtk.Label(label="")
        self._battery_chip.add_css_class("warning_chip")
        self._battery_chip.set_visible(False)
        bar.append(self._battery_chip)

        # Every action is reachable with a D-pad: no letter keys required.
        # Icons only -- from across a room a row of labels reads as clutter,
        # and the hint bar already teaches the buttons.
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.add_css_class("action_bar")
        self._action_buttons = []
        # Settings sits at the end: it is the one the hand reaches for least
        # often, and the row reads left-to-right from "add" to "adjust".
        for icons, label, detail, handler in (
            (
                ("list-add-symbolic",),
                "Add App",
                "Pin an installed app to the drawer",
                self.open_picker,
            ),
            # Software's own icon reads as a storefront; Adwaita's legacy
            # install icon is a shapeless blob, so it is only the fallback.
            (
                ("org.gnome.Software-symbolic", "system-software-install-symbolic"),
                "Get More Apps",
                "Browse and install apps",
                self.open_software,
            ),
            (
                ("emblem-system-symbolic",),
                "Settings",
                "Sound, network, display and more",
                self.toggle_quick_settings,
            ),
        ):
            button = Gtk.Button()
            image = _icon(icons)
            image.set_pixel_size(22)
            button.set_child(image)
            # Icon-only buttons need a name. A remote has no hover, so the
            # bubble follows focus rather than relying on native tooltips.
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            self._bubble.attach(button, label, detail)
            button.add_css_class("action_button")
            button.connect("clicked", lambda _b, fn=handler: fn())
            focus_ctl = Gtk.EventControllerFocus()
            focus_ctl.connect(
                "enter", lambda _c, name=label: trace(f"FOCUS action:{name}")
            )
            button.add_controller(focus_ctl)
            actions.append(button)
            self._action_buttons.append(button)
        bar.append(actions)

        self._actions_nav = RowNavigator(lambda: [self._action_buttons])
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_action_key)
        actions.add_controller(keys)

        self._update_clock()
        return bar

    def _on_action_key(
        self,
        _c: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._drawer.focus_first()
            return True
        return self._actions_nav.handle(keyval)

    def focus_actions(self) -> None:
        self._actions_nav.focus_first()

    def _build_hints(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22)
        bar.add_css_class("hintbar")
        bar.set_halign(Gtk.Align.CENTER)
        bar.set_can_target(False)
        self._hints = bar
        self._hint_timer = 0
        hints = (
            ("OK", "Launch"),
            ("\u2193", "Options"),
            ("\u2191", "Actions"),
            ("Menu", "Settings"),
            ("Back", "Back"),
            ("Home", "Home"),
        )
        for key, action in hints:
            item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            chip = Gtk.Label(label=key)
            chip.add_css_class("key_chip")
            item.append(chip)
            label = Gtk.Label(label=action)
            label.add_css_class("hint_label")
            item.append(label)
            bar.append(item)
        return bar

    def _show_hints(self) -> None:
        """Reveal the key hints and start their countdown to fading out."""
        self._hints.remove_css_class("faded")
        if self._hint_timer:
            GLib.source_remove(self._hint_timer)
        self._hint_timer = GLib.timeout_add_seconds(HINT_FADE_SECONDS, self._fade_hints)

    def _fade_hints(self) -> bool:
        self._hint_timer = 0
        self._hints.add_css_class("faded")
        return GLib.SOURCE_REMOVE

    def _update_clock(self) -> None:
        now = GLib.DateTime.new_now_local()
        self._clock.set_text(
            now.format("%H:%M" if self.config.clock_24h else "%-I:%M %p")
        )
        self._date.set_text(now.format("%A, %-d %B"))

    def _schedule_clock(self) -> None:
        # Just after the minute turns over, rather than on a fixed interval
        # that can leave the clock most of a minute behind.
        now = GLib.DateTime.new_now_local()
        wait_ms = int((60 - now.get_seconds()) * 1000) + 50

        def tick() -> bool:
            self._update_clock()
            self._schedule_clock()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(wait_ms, tick)

    # Content
    # -------------------------------------------------------

    def reload(self) -> None:
        """Re-read config and rebuild the drawer + background."""
        background = self.config.background
        if background:
            # Decoding a photo is the slowest thing a reload does, and pinning
            # or moving a tile does not change it.
            try:
                shown = (background, os.stat(background).st_mtime_ns)
            except OSError:
                shown = (background, 0)
            if shown != self._background_shown:
                self._background.set_filename(background)
                self._background_shown = shown
            self._background.set_visible(True)
        else:
            self._background.set_paintable(None)
            self._background_shown = None

        # With no image the scrim *is* the background, so keep it fully opaque.
        self._scrim.set_opacity(self.config.background_dim if background else 1.0)

        self._drawer.rebuild(self.config.apps)
        self._sync_empty()
        self.refresh_running()

    def _sync_empty(self) -> None:
        # A running app that is not pinned still has a tile, so it counts.
        self._empty.set_visible(not self._drawer.all_tiles())

    def _sync_battery_warning(self) -> None:
        low = self.bluetooth.low_batteries
        self._battery_chip.set_visible(bool(low))
        if low:
            name, percent = min(low, key=lambda entry: entry[1])
            self._battery_chip.set_text(f"{name} battery low: {percent}%")
            trace(f"BATTERY low {name} {percent}")

    def refresh_running(self) -> None:
        self.bridge.list_running(self._drawer.apply_running)

    def _poll(self) -> bool:
        # Only while the launcher is in front. Behind a fullscreen film none
        # of this is on screen, and every round is five processes; coming
        # back to the front refreshes straight away.
        if self.is_active():
            self._refresh_status()
        return GLib.SOURCE_CONTINUE

    def _refresh_status(self) -> None:
        self.refresh_running()
        self.audio.refresh()
        self.network.refresh()
        self.bluetooth.refresh()

    def _on_active_changed(self) -> None:
        active = self.is_active()
        # Stick movements in a game are none of the launcher's business.
        self._pads.set_active(active)
        if active:
            self._refresh_status()

    def _focus_drawer(self) -> bool:
        self._drawer.focus_first()
        return GLib.SOURCE_REMOVE

    def _ensure_keyboard_focus(self) -> bool:
        """Poll until the window is actually keyboard-focused.

        On Wayland a client cannot focus itself, and a fullscreen window that
        maps without a user-interaction timestamp can sit there inert. The
        extension does the focusing; we just keep asking until it lands.
        """
        if self.is_active():
            return GLib.SOURCE_REMOVE

        self._focus_attempts += 1
        if self._focus_attempts > 20:  # ~10s, then stop nagging
            if not self.bridge.available:
                print(
                    "hdlounge: no keyboard focus and no shell extension; "
                    "enable it with: gnome-extensions enable "
                    "hdlounge@harmonicdesign.ca"
                )
            return GLib.SOURCE_REMOVE

        self.bridge.focus_launcher()
        return GLib.SOURCE_CONTINUE

    # Bridge
    # -------------------------------------------------------

    def _connect_bridge(self) -> None:
        self.bridge.connect("running-changed", lambda *_a: self.refresh_running())
        self.bridge.connect("home-requested", lambda *_a: self.on_home())
        self.bridge.connect("tv-power-requested", lambda *_a: self.tvpower.toggle())
        self.bridge.connect("availability-changed", self._on_availability)
        self._on_availability(self.bridge, self.bridge.available)

    def _on_availability(self, _bridge: ShellBridge, available: bool) -> None:
        self._warning.set_visible(not available)
        if not available:
            self._warning.set_text("Shell extension not running -- keyboard disabled")
        else:
            # The extension may have been enabled after we started; claim
            # keyboard focus now that there is something to ask.
            self.bridge.focus_launcher()

    def on_home(self) -> None:
        """Home was pressed: dismiss panels and take focus."""
        self.close_menu(restore_focus=False)
        self.hide_quick_settings()
        self.present()
        self.bridge.focus_launcher()
        self._drawer.focus_first()
        self._bubble.hide()
        self._show_hints()
        # Back from an app: refresh now rather than waiting for the poll, so a
        # tile that is still pulsing settles as soon as its app is up.
        self.refresh_running()

    # App actions
    # -------------------------------------------------------

    def _launch(self, app_id: str) -> None:
        self.close_menu()
        # Acknowledge the press before anything else: a cold-booting app can
        # take seconds to put a window up, and a tile that just sits there
        # invites a second press.
        self._drawer.set_launching(app_id)
        self.bridge.launch_or_focus(app_id, self.config.wants_fullscreen(app_id))

    def _on_app_activated(self, _drawer: AppDrawer, app_id: str) -> None:
        # While the options menu is open the OK button belongs to the menu.
        # A held button that opened the menu must never also launch, however
        # the remote chooses to repeat it.
        if self._open_menu is not None:
            return
        tile = self._drawer.tile_for(app_id)
        if tile is not None and not tile.pinned:
            self._switch_to(app_id)
            return
        self._launch(app_id)

    def _switch_to(self, app_id: str) -> None:
        """Bring back an app that is running without being pinned.

        Focus only, never LaunchOrFocus: that would force it fullscreen, and
        Settings or Software opened from the launcher should come back the
        way they were.
        """
        self.close_menu()
        self.bridge.focus_app(app_id)

    def _on_app_menu(self, _drawer: AppDrawer, tile: AppTile) -> None:
        trace(f"MENU opened for {tile.app_id}")
        popover = Gtk.Popover()
        # Parent to the overlay, not the tile: a popover parented to the tile
        # is one of its descendants, so the tile never sees focus leave (its
        # long-press state would stick) and its key controller sits in the
        # menu's event path.
        popover.set_parent(self._overlay)
        popover.set_position(Gtk.PositionType.BOTTOM)
        ok, bounds = tile.compute_bounds(self._overlay)
        if ok:
            rect = Gdk.Rectangle()
            rect.x = int(bounds.origin.x)
            rect.y = int(bounds.origin.y)
            rect.width = int(bounds.size.width)
            rect.height = int(bounds.size.height)
            popover.set_pointing_to(rect)
        popover.add_css_class("tile_menu")
        # A grabbing popup fights the compositor-level focus handling the
        # extension does, and cannot map cleanly while another app window is
        # stacked above the launcher. Keep focus inside our own window and
        # drive the menu with the same D-pad navigation as everything else.
        popover.set_autohide(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        app_id = tile.app_id
        items: list[Gtk.Widget] = []

        def item(
            label: str, handler: Callable[[], None], css: str | None = None
        ) -> None:
            button = Gtk.Button(label=label)
            button.set_has_frame(False)
            button.get_child().set_xalign(0.0)
            if css:
                button.add_css_class(css)

            def clicked(_b: Gtk.Button) -> None:
                popover.popdown()
                handler()

            button.connect("clicked", clicked)
            box.append(button)
            items.append(button)

        if not tile.pinned:
            # Only in the drawer because it is running: switch, close, keep.
            item("Switch to", lambda: self._switch_to(app_id))
            item("Close", lambda: self._close_app(app_id))
            item("Force Kill", lambda: self._kill_app(app_id), "destructive")
            item("Pin to Drawer", lambda: self._pin(app_id))
        else:
            if tile.running:
                item("Switch to", lambda: self._launch(app_id))
                item("Close", lambda: self._close_app(app_id))
                item("Force Kill", lambda: self._kill_app(app_id), "destructive")
            else:
                item("Launch", lambda: self._launch(app_id))

            fullscreen_label = (
                "Launch fullscreen: On"
                if self.config.wants_fullscreen(app_id)
                else "Launch fullscreen: Off"
            )
            item(fullscreen_label, lambda: self._toggle_fullscreen(app_id))
            item("Move Left", lambda: self._move(app_id, -1))
            item("Move Right", lambda: self._move(app_id, 1))
            # A website tile is nothing but its pin, so removing it goes all
            # the way rather than leaving a launcher entry behind.
            if webapps.is_web_app(app_id):
                item(
                    "Remove Website",
                    lambda: self._remove_website(app_id),
                    "destructive",
                )
            else:
                item("Unpin", lambda: self._unpin(app_id), "destructive")

        popover.set_child(box)

        nav = RowNavigator(lambda: [[w] for w in items])

        def on_menu_key(
            _c: Gtk.EventControllerKey, keyval: int, _code: int, state: Gdk.ModifierType
        ) -> bool:
            if keyval in (Gdk.KEY_Escape, Gdk.KEY_BackSpace, KEY_XF86_BACK):
                popover.popdown()
                return True
            if held["armed"] and keyval in ACTIVATE_KEYS:
                if held["settle"]:
                    GLib.source_remove(held["settle"])
                    held["settle"] = 0
                return True
            step = DRAWER_STEPS.get(keyval)
            if step is not None:
                self._leave_menu_for(app_id, step)
                return True
            if nav.handle(keyval):
                return True
            if keyval in MENU_TRAPPED_KEYS:
                return True
            # A GtkPopover is a GtkNative with its own Wayland surface, so
            # while it is open nothing reaches the window's own controller.
            # Home, Menu and the volume keys must still work.
            return self._on_key_pressed(None, keyval, 0, state)

        # When a held OK opened this menu, that key is still down and the
        # compositor auto-repeats it. Swallow it until it is actually
        # released, or the repeat would immediately activate the first item.
        # Opened any other way (Down, a pointer long-press, a controller)
        # there is nothing to swallow, and the next OK must just work.
        held = {"armed": tile.holding_key, "settle": 0}

        def disarm() -> bool:
            held["settle"] = 0
            held["armed"] = False
            return GLib.SOURCE_REMOVE

        def on_release(
            _c: Gtk.EventControllerKey,
            keyval: int,
            _code: int,
            _state: Gdk.ModifierType,
        ) -> None:
            if keyval in ACTIVATE_KEYS and held["armed"] and not held["settle"]:
                held["settle"] = GLib.timeout_add(REPEAT_SETTLE_MS, disarm)

        guard = Gtk.EventControllerKey()
        guard.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        guard.connect("key-pressed", on_menu_key)
        guard.connect("key-released", on_release)
        popover.add_controller(guard)

        def dispose() -> bool:
            # A fresh popover is built per invocation, so this one must be
            # taken out of the tile's widget tree or they accumulate and
            # later popups map against a stale surface.
            popover.unparent()
            return GLib.SOURCE_REMOVE

        def on_closed(_p: Gtk.Popover) -> None:
            trace(f"MENU closed for {app_id}")
            if self._open_menu is popover:
                self._open_menu = None
                self._open_menu_keys = None
            if held["settle"]:
                GLib.source_remove(held["settle"])
                held["settle"] = 0
            if self._menu_restores_focus:
                GLib.idle_add(self._restore_focus, app_id)
            GLib.idle_add(dispose)

        popover.connect("closed", on_closed)

        def take_focus(*_args: object) -> bool:
            # Children are only focusable once the popover has mapped; retry
            # on idle if we were still too early.
            if nav.focus_first():
                return GLib.SOURCE_REMOVE
            GLib.idle_add(take_focus)
            return GLib.SOURCE_REMOVE

        # Without a grab the popover does not take focus on its own.
        popover.connect("map", take_focus)

        self._menu_restores_focus = True
        self._open_menu = popover
        self._open_menu_keys = on_menu_key
        popover.popup()

    def _open_focused_menu(self) -> None:
        tile = next((t for t in self._drawer.all_tiles() if t.has_focus()), None)
        if tile is not None:
            self._drawer.emit("app-menu", tile)

    def _leave_menu_for(self, app_id: str, step: int) -> None:
        """Dismiss the menu and take the selection one tile along.

        Left or Right with a menu open means "done here, show me the next
        app". The tile the menu belongs to no longer has focus, so the move
        is made from its app id rather than from whatever GTK last focused.
        """
        target = self._drawer.neighbour(app_id, step)
        self.close_menu(restore_focus=False)
        # On an idle, for the same reason close_menu's own restore is: GTK
        # moves focus out of the closing popover after this returns.
        GLib.idle_add(self._restore_focus, target)

    def _restore_focus(self, app_id: str) -> bool:
        tile = self._drawer.tile_for(app_id)
        if tile is not None:
            tile.grab_focus()
        return GLib.SOURCE_REMOVE

    def _refresh_soon(self) -> bool:
        """Give the compositor a beat to tear the window down, then re-read."""
        self.refresh_running()
        return GLib.SOURCE_REMOVE

    def _close_app(self, app_id: str) -> None:
        self.bridge.close_app(app_id)
        GLib.timeout_add_seconds(1, self._refresh_soon)

    def _kill_app(self, app_id: str) -> None:
        self.bridge.kill_app(app_id)
        GLib.timeout_add_seconds(1, self._refresh_soon)

    def _toggle_fullscreen(self, app_id: str) -> None:
        self.config.set_fullscreen(app_id, not self.config.wants_fullscreen(app_id))

    def _move(self, app_id: str, offset: int) -> None:
        if self.config.move(app_id, offset):
            self.reload()
            GLib.idle_add(self._restore_focus, app_id)

    def _unpin(self, app_id: str) -> None:
        self.config.unpin(app_id)
        self.reload()
        GLib.idle_add(self._focus_drawer)

    def _pin(self, app_id: str) -> None:
        self.config.pin(app_id)
        self.reload()
        GLib.idle_add(self._restore_focus, app_id)

    def _remove_website(self, app_id: str) -> None:
        self.config.unpin(app_id)
        webapps.remove(app_id)
        self.reload()
        GLib.idle_add(self._focus_drawer)

    # Panes
    # -------------------------------------------------------

    def close_menu(self, restore_focus: bool = True) -> None:
        """Dismiss the tile options menu.

        Pass restore_focus=False when the menu is being closed because
        something else is taking over the screen -- otherwise the tile grabs
        focus back out from under it.
        """
        if self._open_menu is not None:
            self._menu_restores_focus = restore_focus
            self._open_menu.popdown()

    def show_quick_settings(self) -> None:
        self.close_menu(restore_focus=False)
        self._qs.refresh()
        self._qs_revealer.set_visible(True)
        self._qs_revealer.set_reveal_child(True)

    def _on_qs_revealed(
        self, revealer: Gtk.Revealer, _pspec: GObject.ParamSpec
    ) -> None:
        if revealer.get_child_revealed():
            # Only now are the panel's controls mapped and focusable.
            self._focus_quick_settings()
        else:
            revealer.set_visible(False)

    def _focus_quick_settings(self) -> bool:
        self._qs.focus_first()
        return GLib.SOURCE_REMOVE

    def hide_quick_settings(self) -> None:
        if self._qs_revealer.get_reveal_child():
            self._qs_revealer.set_reveal_child(False)
            self._drawer.focus_first()

    def toggle_quick_settings(self) -> None:
        if self._qs_revealer.get_reveal_child():
            self.hide_quick_settings()
        else:
            self.show_quick_settings()

    def open_picker(self) -> None:
        self.close_menu(restore_focus=False)
        picker = AppPicker(self.config, self.bridge, on_site_icon=self.reload)
        picker.connect("app-chosen", self._on_app_chosen)
        picker.present(self)

    def _on_app_chosen(self, _picker: AppPicker, app_id: str) -> None:
        self._pin(app_id)

    def open_software(self) -> None:
        """GNOME Software, for installing apps without leaving the couch."""
        self.close_menu(restore_focus=False)
        self.hide_quick_settings()
        self.bridge.open_external("org.gnome.Software.desktop", ["gnome-software"])

    def choose_background(self) -> None:
        self.hide_quick_settings()
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose Background Image")

        images = Gtk.FileFilter()
        images.set_name("Images")
        for mime in ("image/jpeg", "image/png", "image/webp", "image/avif"):
            images.add_mime_type(mime)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)
        dialog.set_filters(filters)
        dialog.set_default_filter(images)

        def chosen(source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                gfile = source.open_finish(result)
            except GLib.Error:
                return
            if gfile is not None and gfile.get_path():
                self.config.background = gfile.get_path()
                self.reload()

        dialog.open(self, None, chosen)

    # Cursor
    # -------------------------------------------------------

    def _install_cursor_hiding(self) -> None:
        """Hide the pointer once it has been still for a while.

        A stray cursor parked in the middle of the screen is the most obvious
        tell that a TV is really a PC.
        """
        self._blank = _blank_cursor()
        self._cursor_hidden = False
        self._cursor_timer = 0
        self._last_pointer: tuple[float, float] | None = None

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_pointer_motion)
        motion.connect("enter", self._on_pointer_enter)
        self.add_controller(motion)
        self._schedule_cursor_hide()

    def _on_pointer_enter(
        self, _c: Gtk.EventControllerMotion, x: float, y: float
    ) -> None:
        """The pointer entered our surface without necessarily moving.

        Minimising other windows (pressing Home) hands the pointer back to us
        and GTK reports a crossing. That is not the user moving the mouse, so
        record where it is but leave the cursor hidden.
        """
        self._last_pointer = (x, y)

    def _on_pointer_motion(
        self, _c: Gtk.EventControllerMotion, x: float, y: float
    ) -> None:
        previous = self._last_pointer
        self._last_pointer = (x, y)

        if previous is not None:
            moved = (
                abs(x - previous[0]) >= POINTER_MOVE_EPSILON
                or abs(y - previous[1]) >= POINTER_MOVE_EPSILON
            )
            if not moved:
                return  # a crossing, not a movement: stay as we are

        if self._cursor_hidden:
            self.set_cursor(None)
            self._cursor_hidden = False
            trace("CURSOR shown")
        self._schedule_cursor_hide()

    def _schedule_cursor_hide(self) -> None:
        if self._cursor_timer:
            GLib.source_remove(self._cursor_timer)
        self._cursor_timer = GLib.timeout_add_seconds(
            CURSOR_IDLE_SECONDS, self._hide_cursor
        )

    def _hide_cursor(self) -> bool:
        self._cursor_timer = 0
        if self._blank is not None:
            self.set_cursor(self._blank)
            self._cursor_hidden = True
            trace("CURSOR hidden")
        else:
            trace("CURSOR no blank cursor available")
        return GLib.SOURCE_REMOVE

    def _install_keys(self) -> None:
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

    def _on_key_pressed(
        self,
        _c: Gtk.EventControllerKey | None,
        keyval: int,
        _code: int,
        state: Gdk.ModifierType,
    ) -> bool:
        panel_open = self._qs_revealer.get_reveal_child()

        if keyval in (Gdk.KEY_Home, KEY_XF86_HOME):
            self.bridge.show_home()
            self.on_home()
            return True

        if keyval in (Gdk.KEY_Escape, Gdk.KEY_BackSpace, KEY_XF86_BACK):
            if self._open_menu is not None:
                self._open_menu.popdown()
                return True
            if panel_open:
                self.hide_quick_settings()
                return True
            return False

        if keyval == KEY_XF86_MUTE:
            self.audio.toggle_mute()
            return True
        if keyval == KEY_XF86_VOL_UP:
            self.audio.step(0.05)
            return True
        if keyval == KEY_XF86_VOL_DOWN:
            self.audio.step(-0.05)
            return True

        # A remote's menu/settings button opens quick settings from anywhere.
        if keyval in MENU_KEYS:
            self.toggle_quick_settings()
            return True

        # The deliberate way out of the kiosk, checked before bare-letter
        # shortcuts bail out on modifiers.
        if keyval in (Gdk.KEY_q, Gdk.KEY_Q) and state & Gdk.ModifierType.CONTROL_MASK:
            self.get_application().quit()
            return True

        # Letter shortcuts only make sense when a text field is not focused.
        if isinstance(self.get_focus(), (Gtk.Editable, Gtk.TextView)):
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False

        if keyval in (Gdk.KEY_s, Gdk.KEY_S, Gdk.KEY_F2):
            self.toggle_quick_settings()
            return True
        if keyval in (Gdk.KEY_a, Gdk.KEY_A, Gdk.KEY_plus, Gdk.KEY_Insert):
            self.open_picker()
            return True
        if keyval in (Gdk.KEY_b, Gdk.KEY_B):
            self.choose_background()
            return True

        return False

    # Controllers
    # -------------------------------------------------------

    def _on_pad_nav(self, action: str) -> None:
        # libmanette hears a controller whatever has focus. Only Home is meant
        # to reach through from inside a running app.
        if action != "home" and not self.is_active():
            return
        self.dispatch_nav(action)

    def dispatch_nav(self, action: str) -> bool:
        """Act on a controller action as if its key had been pressed.

        A Wayland client cannot inject key events, even into itself, so this
        walks the handlers the key controllers would have reached, in the
        same order: the open menu, the window's global keys, quick settings,
        then whatever has focus.
        """
        trace(f"PAD {action}")
        if action == "home":
            self.bridge.show_home()
            self.on_home()
            return True
        if action == "options":
            if self._open_menu is None and not self._qs_revealer.get_reveal_child():
                self._open_focused_menu()
            return True

        keyval = NAV_KEYS.get(action)
        if keyval is None:
            return False

        if self._open_menu_keys is not None:
            return self._open_menu_keys(None, keyval, 0, 0)
        if self._on_key_pressed(None, keyval, 0, 0):  # Back, Menu
            return True

        focus = self.get_focus()
        # Navigation leaves Left and Right on a slider to GTK's own key
        # handling, which an action that never was a key event cannot reach.
        if isinstance(focus, Gtk.Range) and action in ("left", "right"):
            step = focus.get_adjustment().get_step_increment() or 1.0
            focus.set_value(focus.get_value() + (step if action == "right" else -step))
            return True

        if self._qs_revealer.get_reveal_child():
            return self._qs.handle_key(keyval)

        if focus in self._action_buttons:
            return self._on_action_key(None, keyval, 0, 0)
        if isinstance(focus, AppTile):
            if action == "ok":
                focus.activate_now()
                return True
            return self._drawer.handle_key(keyval)

        # Anything else -- the app picker dialog -- gets GTK's own focus
        # movement and activation.
        dialog = self.get_visible_dialog()
        if dialog is not None and action == "back":
            dialog.close()
            return True
        if action == "ok":
            return bool(focus is not None and focus.activate())
        direction = NAV_DIRECTIONS.get(action)
        return direction is not None and self.child_focus(direction)
