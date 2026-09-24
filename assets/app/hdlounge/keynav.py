"""Row-based D-pad navigation and activation.

GTK4 only walks the focus chain with Tab; it does not do directional focus
between arbitrary widgets in a plain container. A TV interface is driven
entirely by a D-pad, so every focusable surface here declares its layout as
rows of widgets and lets this drive it.

It also owns activation, because a remote's OK button is not a key GTK knows
how to activate a widget with. See REMOTE_OK.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gdk, Gtk

from .debugshot import trace

LEFT = (Gdk.KEY_Left, Gdk.KEY_KP_Left)
RIGHT = (Gdk.KEY_Right, Gdk.KEY_KP_Right)
UP = (Gdk.KEY_Up, Gdk.KEY_KP_Up)
DOWN = (Gdk.KEY_Down, Gdk.KEY_KP_Down)

# An Android TV remote's OK button -- the NVIDIA Shield remote and every
# clone of it -- is evdev KEY_SELECT (353), which arrives as the XF86 keysym
# described upstream as "Select button on joypads and remotes", 0x1008ffa0.
#
# GTK activates a focused widget on Return, KP_Enter and space; it has no
# binding for this one. So every D-pad surface must activate explicitly,
# rather than letting the key fall through to the widget as Return does.
#
# Mind the name. Gdk.keyval_from_name("XF86Select") is a *different* and
# unrelated keysym (0xff60) that the remote never sends, and matching on it
# fails silently.
REMOTE_OK = Gdk.keyval_from_name("XF86SelectButton")


class RowNavigator:
    """Drives focus over a grid of widgets supplied as rows.

    `get_rows` is called on every keypress so that panels which rebuild their
    contents (audio outputs appearing, apps being pinned) stay navigable
    without re-registering anything.
    """

    def __init__(self, get_rows: Callable[[], list[list[Gtk.Widget]]]) -> None:
        self._get_rows = get_rows
        self._column = 0  # remembered across vertical moves

    # Position
    # -------------------------------------------------------

    def _rows(self) -> list[list[Gtk.Widget]]:
        return [
            [w for w in row if w.get_mapped() and w.get_sensitive()]
            for row in self._get_rows()
        ]

    def _locate(self, rows: list[list[Gtk.Widget]]) -> tuple[int, int] | None:
        for r, row in enumerate(rows):
            for c, widget in enumerate(row):
                if widget.has_focus():
                    return r, c
        return None

    def _focus(self, rows: list[list[Gtk.Widget]], r: int, c: int) -> bool:
        row = rows[r]
        if not row:
            return False
        c = min(max(c, 0), len(row) - 1)
        row[c].grab_focus()
        trace(f"NAV {type(row[c]).__name__} row={r} col={c}")
        return True

    # Moving
    # -------------------------------------------------------

    def focus_first(self) -> bool:
        rows = [r for r in self._rows() if r]
        if not rows:
            return False
        self._column = 0
        return self._focus(rows, 0, 0) if rows else False

    def handle(self, keyval: int) -> bool:
        """Return True if the key was consumed."""
        rows = self._rows()
        if not any(rows):
            return False

        here = self._locate(rows)
        if here is None:
            # Focus is not on us (yet); take it on any arrow key.
            return self.focus_first()

        r, c = here

        if keyval == REMOTE_OK:
            # Gtk.Widget.activate() is a no-op returning False on a widget
            # with nothing to activate -- a Gtk.Range, say -- which correctly
            # leaves the key unconsumed and free to propagate.
            return rows[r][c].activate()

        if keyval in LEFT or keyval in RIGHT:
            # A slider owns left/right: those adjust the value, not focus.
            if isinstance(rows[r][c], Gtk.Range):
                return False
            step = -1 if keyval in LEFT else 1
            target = c + step
            if 0 <= target < len(rows[r]):
                self._column = target
                self._focus(rows, r, target)
            return True  # consume at the ends so focus cannot escape

        if keyval in UP or keyval in DOWN:
            step = -1 if keyval in UP else 1
            self._column = c
            target = r + step
            while 0 <= target < len(rows):
                if rows[target]:
                    self._focus(rows, target, self._column)
                    return True
                target += step
            return False  # nothing above/below: let the caller decide

        return False
