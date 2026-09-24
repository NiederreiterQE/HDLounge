"""The app drawer: a single horizontal strip of launchable tiles."""

from __future__ import annotations

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from .appinfo import lookup
from .debugshot import trace
from .keynav import REMOTE_OK

ICON_SIZE = 88
LONG_PRESS_MS = 550
# A held key normally repeats as press-without-release. Some IR/USB remotes
# instead emit a full press/release pair per repeat, which would look like a
# burst of taps. Wait this long after a release for a follow-up press before
# calling it a genuine tap.
REPEAT_SETTLE_MS = 90
# How long a pressed tile keeps saying "starting..." if the app never shows a
# window. Long enough for a cold flatpak start on slow storage, short enough
# that a failed launch does not pulse forever.
LAUNCH_FEEDBACK_MS = 25000

# Keys that mean "activate this tile". A media remote's OK button reports as
# Return; some report as KP_Enter or space; an Android TV remote sends its
# own keysym entirely (see keynav.REMOTE_OK). Holding OK must open the
# options menu for all of them, so they belong in one set.
ACTIVATE_KEYS = {
    Gdk.KEY_Return,
    Gdk.KEY_KP_Enter,
    Gdk.KEY_space,
    Gdk.KEY_ISO_Enter,
    REMOTE_OK,
}
# Tile options are reached with Down or by holding OK. The Menu key is
# handled globally by the window as "quick settings".


class AppTile(Gtk.Box):
    """One app in the drawer. Focusable, activatable, long-pressable."""

    __gsignals__ = {
        "activated": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "context-menu": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self, app_id: str, app_info: Gio.DesktopAppInfo, pinned: bool = True
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.app_id = app_id
        self.app_info = app_info
        # False for an app that is running without being pinned: its tile is
        # only here while it runs, so Home never strands it with no way back.
        self.pinned = pinned
        self.running = False
        self.window_count = 0

        self.set_focusable(True)
        self.add_css_class("app_tile")
        if not pinned:
            self.add_css_class("transient")
        self.set_size_request(168, -1)

        self._image = Gtk.Image()
        self._image.set_pixel_size(ICON_SIZE)
        self._set_icon(app_info)
        self.append(self._image)

        self._label = Gtk.Label(label=app_info.get_name())
        self._label.add_css_class("app_tile_label")
        self._label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self._label.set_max_width_chars(16)
        self._label.set_justify(Gtk.Justification.CENTER)
        self.append(self._label)

        self._dot = Gtk.Box()
        self._dot.add_css_class("running_dot")
        self._dot.set_halign(Gtk.Align.CENTER)
        self._dot.set_size_request(8, 8)
        self._dot.set_opacity(0.0)
        self.append(self._dot)

        self._long_press_id = 0
        self._settle_id = 0
        self._holding = False
        self._long_press_fired = False
        self._launch_id = 0
        self.launching = False

        keys = Gtk.EventControllerKey()
        # CAPTURE so we see the key before any default handling.
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        keys.connect("key-released", self._on_key_released)
        self.add_controller(keys)

        click = Gtk.GestureClick()
        click.connect("released", self._on_click_released)
        self.add_controller(click)

        long_press = Gtk.GestureLongPress()
        long_press.connect("pressed", self._on_long_pressed)
        self.add_controller(long_press)

        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_a: self.add_css_class("focused"))
        focus.connect("leave", lambda *_a: self._on_focus_leave())
        self.add_controller(focus)

    def _set_icon(self, app_info: Gio.DesktopAppInfo) -> None:
        icon = app_info.get_icon()
        if icon:
            self._image.set_from_gicon(icon)
        else:
            self._image.set_from_icon_name("application-x-executable")

    def update_info(self, app_info: Gio.DesktopAppInfo) -> None:
        """Take a re-read desktop entry: a website's icon arrives late."""
        self.app_info = app_info
        self._set_icon(app_info)
        if not self.launching:
            self._label.set_label(app_info.get_name())
        self._update_tooltip()

    # Long press
    # -------------------------------------------------------

    def _on_key_pressed(
        self,
        _c: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval not in ACTIVATE_KEYS:
            return False
        # A pending release turned out to be a repeat, not a tap.
        if self._settle_id:
            GLib.source_remove(self._settle_id)
            self._settle_id = 0

        if self._holding:
            # The compositor auto-repeats a held key; only the first press
            # starts the timer, and we stay "holding" until a real release.
            return True
        self._holding = True
        self._long_press_fired = False
        self._long_press_id = GLib.timeout_add(LONG_PRESS_MS, self._on_held)
        return True

    def _on_key_released(
        self,
        _c: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        _state: Gdk.ModifierType,
    ) -> None:
        if keyval not in ACTIVATE_KEYS or not self._holding:
            return
        # Do not decide yet: a remote that repeats by re-sending press/release
        # would otherwise register a stream of taps instead of one hold.
        if self._settle_id:
            GLib.source_remove(self._settle_id)
        self._settle_id = GLib.timeout_add(REPEAT_SETTLE_MS, self._on_release_settled)

    def _on_release_settled(self) -> bool:
        self._settle_id = 0
        self._holding = False
        if self._long_press_id:
            GLib.source_remove(self._long_press_id)
            self._long_press_id = 0
        # Only a release that beat the threshold counts as an ordinary press;
        # otherwise the menu already opened and this release just ends it.
        if not self._long_press_fired:
            self._fire_activate()
        self._long_press_fired = False
        return GLib.SOURCE_REMOVE

    def _on_held(self) -> bool:
        self._long_press_id = 0
        self._long_press_fired = True  # stay held until the real release
        self.emit("context-menu")
        return GLib.SOURCE_REMOVE

    def _on_long_pressed(self, *_args: object) -> None:
        self._long_press_fired = True
        self.emit("context-menu")

    def _on_click_released(self, *_args: object) -> None:
        # A pointer long-press already showed the menu; do not also launch.
        if self._long_press_fired:
            self._long_press_fired = False
            return
        self._fire_activate()

    def _cancel_long_press(self) -> None:
        if self._long_press_id:
            GLib.source_remove(self._long_press_id)
            self._long_press_id = 0
        self._settle_id = 0
        self._holding = False
        self._long_press_fired = False

    def _on_focus_leave(self) -> None:
        self.remove_css_class("focused")
        self._cancel_long_press()

    def _fire_activate(self) -> None:
        trace(f"ACTIVATE {self.app_id}")
        self.emit("activated")

    @property
    def holding_key(self) -> bool:
        """True while an activate key that pressed this tile is still down."""
        return self._holding

    def activate_now(self) -> None:
        """Activate straight away, for input with no press to time -- a
        controller's A button, which reaches the launcher as an action."""
        self._cancel_long_press()
        self._fire_activate()

    # Launching
    # -------------------------------------------------------

    def set_launching(self, launching: bool) -> None:
        """Show, or stop showing, that this app is starting up.

        Between the press and the app's first window there can be several
        seconds of nothing, which reads as "it did not register" -- so the
        tile stays lit and pulsing for the whole wait.
        """
        if self._launch_id:
            GLib.source_remove(self._launch_id)
            self._launch_id = 0
        if launching != self.launching:
            trace(("LAUNCHING " if launching else "LAUNCH-SETTLED ") + self.app_id)
        self.launching = launching
        if launching:
            self.add_css_class("launching")
            # The label says it outright: at three metres a colour change
            # alone is easy to miss, and the icon still names the app.
            self._label.set_label("Starting...")
            self._dot.add_css_class("starting")
            self._dot.set_opacity(1.0)
            self._launch_id = GLib.timeout_add(
                LAUNCH_FEEDBACK_MS, self._on_launch_timeout
            )
        else:
            self.remove_css_class("launching")
            self._label.set_label(self.app_info.get_name())
            self._dot.remove_css_class("starting")
            self._dot.set_opacity(1.0 if self.running else 0.0)
        self._update_tooltip()

    def _on_launch_timeout(self) -> bool:
        self._launch_id = 0
        self.set_launching(False)
        return GLib.SOURCE_REMOVE

    # Display
    # -------------------------------------------------------

    def set_running(
        self, running: bool, window_count: int = 0, focused: bool = False
    ) -> None:
        self.running = running
        self.window_count = window_count
        if running and self.launching:
            # It has a window now: the wait is over.
            self.set_launching(False)
        self._dot.set_opacity(1.0 if running or self.launching else 0.0)
        self._dot.remove_css_class("focused_app")
        if focused:
            self._dot.add_css_class("focused_app")
        self._update_tooltip()

    def _update_tooltip(self) -> None:
        tooltip = self.app_info.get_name()
        if self.launching:
            tooltip += " -- starting..."
        elif self.running:
            tooltip += f" -- running ({self.window_count} window"
            tooltip += ")" if self.window_count == 1 else "s)"
        self.set_tooltip_text(tooltip)


# The launcher's own id as the shell reports it. It is always running and must
# never turn up as a tile.
LAUNCHER_ID_PREFIX = "ca.harmonicdesign.HDLounge"


class AppDrawer(Gtk.ScrolledWindow):
    """Horizontally scrolling strip of AppTiles.

    Pinned apps come first. After them, past a divider, come apps that are
    running without being pinned -- Settings opened from quick settings, a
    game Steam started -- so that after Home there is still a way back to
    them.
    """

    __gsignals__ = {
        "app-activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "app-menu": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
        # D-pad leaving the drawer vertically.
        "nav-up": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "nav-down": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # The running-but-unpinned tiles came or went.
        "extras-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        self.set_hexpand(True)
        self.add_css_class("app_drawer")

        self._row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._row.set_halign(Gtk.Align.CENTER)
        self._row.set_valign(Gtk.Align.CENTER)
        self.set_child(self._row)

        self._tiles: dict[str, AppTile] = {}  # pinned, in config order
        self._extras: dict[str, AppTile] = {}  # running, not pinned

        # Pinned tiles are inserted before the divider, extras appended after.
        self._divider = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self._divider.add_css_class("drawer_divider")
        self._divider.set_visible(False)
        self._row.append(self._divider)

        # GTK's built-in directional focus will not walk a row of focusable
        # containers like this reliably, so drive it explicitly. It also lets
        # us trap focus inside the drawer, which is what a TV UI wants.
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

    def _on_key_pressed(
        self,
        _c: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            return self._move_focus(1)
        if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            return self._move_focus(-1)
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self.emit("nav-up")
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self.emit("nav-down")
            return True
        return False

    def handle_key(self, keyval: int) -> bool:
        """Drawer navigation for a key that did not come from a key event."""
        return self._on_key_pressed(None, keyval, 0, 0)

    def _move_focus(self, delta: int) -> bool:
        tiles = self.all_tiles()
        if not tiles:
            return False

        current = next((i for i, t in enumerate(tiles) if t.has_focus()), None)
        if current is None:
            tiles[0].grab_focus()
            return True

        target = current + delta
        if 0 <= target < len(tiles):
            tiles[target].grab_focus()
        # At either end, stay put but still consume: focus must not leave.
        return True

    def neighbour(self, app_id: str, delta: int) -> str:
        """The app one step along the row, or `app_id` itself at the ends."""
        tiles = self.all_tiles()
        current = next((i for i, t in enumerate(tiles) if t.app_id == app_id), None)
        if current is None:
            return app_id
        target = current + delta
        return tiles[target].app_id if 0 <= target < len(tiles) else app_id

    @property
    def tiles(self) -> dict[str, AppTile]:
        """The pinned tiles."""
        return self._tiles

    @property
    def extras(self) -> dict[str, AppTile]:
        """Tiles for apps that are running without being pinned."""
        return self._extras

    def all_tiles(self) -> list[AppTile]:
        """Every tile, in on-screen order."""
        return [*self._tiles.values(), *self._extras.values()]

    def tile_for(self, app_id: str) -> AppTile | None:
        return self._tiles.get(app_id) or self._extras.get(app_id)

    def _make_tile(
        self, app_id: str, info: Gio.DesktopAppInfo, pinned: bool
    ) -> AppTile:
        tile = AppTile(app_id, info, pinned)
        tile.connect(
            "activated", lambda _t, aid=app_id: self.emit("app-activated", aid)
        )
        tile.connect("context-menu", lambda t: self.emit("app-menu", t))
        tile.connect("notify::has-focus", self._on_tile_focus)
        return tile

    def rebuild(self, entries: list[dict]) -> None:
        """Bring the pinned tiles into line with the config's pinned-app list.

        Tiles that are still pinned are kept and only moved, so a reorder
        does not reload every icon, and a running app's dot does not blink
        off until the next ListRunning reply.
        """
        focused_id = next((t.app_id for t in self.all_tiles() if t.has_focus()), None)

        old, self._tiles = self._tiles, {}
        previous = None
        for entry in entries:
            app_id = entry.get("id", "")
            info = lookup(app_id)
            if info is None or app_id in self._tiles:
                continue
            tile = old.pop(app_id, None)
            if tile is None:
                tile = self._make_tile(app_id, info, pinned=True)
                self._row.insert_child_after(tile, previous)
            else:
                tile.update_info(info)
                self._row.reorder_child_after(tile, previous)
            previous = tile
            self._tiles[app_id] = tile

        for tile in old.values():
            self._row.remove(tile)

        # An app that has just been pinned is no longer an extra.
        dropped = [a for a in self._extras if a in self._tiles]
        for app_id in dropped:
            self._drop_extra(app_id)
        self._divider.set_visible(bool(self._extras))

        if focused_id:
            tile = self.tile_for(focused_id)
            if tile is not None:
                tile.grab_focus()
        if dropped:
            self.emit("extras-changed")

    def focus_first(self) -> bool:
        tiles = self.all_tiles()
        if tiles:
            tiles[0].grab_focus()
            return True
        return False

    def set_launching(self, app_id: str) -> None:
        """Mark one tile as starting up, and only that one.

        An app that is already running is skipped: switching to it is
        immediate, so "Starting..." would be both wrong and slower to read
        than the switch itself.
        """
        for tile_id, tile in self._tiles.items():
            starting = tile_id == app_id and not tile.running
            if tile.launching or starting:
                tile.set_launching(starting)

    def apply_running(self, running: dict[str, dict]) -> None:
        for app_id, tile in self._tiles.items():
            info = running.get(app_id)
            tile.set_running(
                info is not None,
                info["windows"] if info else 0,
                info["focused"] if info else False,
            )
        self._sync_extras(running)

    def _sync_extras(self, running: dict[str, dict]) -> None:
        wanted = [
            app_id
            for app_id, info in running.items()
            if app_id not in self._tiles
            and info.get("windows", 0) > 0
            and not app_id.startswith(LAUNCHER_ID_PREFIX)
        ]

        changed = refocus = False
        for app_id in [a for a in self._extras if a not in wanted]:
            refocus = refocus or self._extras[app_id].has_focus()
            self._drop_extra(app_id)
            changed = True

        for app_id in wanted:
            if app_id in self._extras:
                continue
            info = lookup(app_id)
            # A window the shell could not match to any installed app.
            if info is None:
                continue
            tile = self._make_tile(app_id, info, pinned=False)
            self._row.append(tile)
            self._extras[app_id] = tile
            trace(f"TRANSIENT {app_id}")
            changed = True

        for app_id, tile in self._extras.items():
            info = running[app_id]
            tile.set_running(True, info["windows"], info["focused"])
        self._divider.set_visible(bool(self._extras))

        if refocus:
            tiles = self.all_tiles()
            if tiles:
                tiles[-1].grab_focus()
        if changed:
            self.emit("extras-changed")

    def _drop_extra(self, app_id: str) -> None:
        self._row.remove(self._extras.pop(app_id))
        trace(f"TRANSIENT-GONE {app_id}")

    # Keep the focused tile on screen when the strip overflows.
    def _on_tile_focus(self, tile: AppTile, _pspec: GObject.ParamSpec) -> None:
        if not tile.has_focus():
            return
        trace(f"FOCUS {tile.app_id}")
        adj = self.get_hadjustment()
        ok, rect = tile.compute_bounds(self._row)
        if not ok:
            return
        left, right = rect.origin.x, rect.origin.x + rect.size.width
        margin = 40
        if left - margin < adj.get_value():
            adj.set_value(max(0, left - margin))
        elif right + margin > adj.get_value() + adj.get_page_size():
            adj.set_value(
                min(
                    adj.get_upper() - adj.get_page_size(),
                    right + margin - adj.get_page_size(),
                )
            )
