"""A tooltip bubble that follows keyboard focus as well as the pointer.

GTK's own tooltips only appear on hover, and a TV is driven from a remote:
there is no hover. This bubble is a child of the window's overlay rather than
a popup surface, so it never takes keyboard focus away from the button it
describes and never has to be mapped over another app's window.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gi.repository import GLib, Gtk

if TYPE_CHECKING:
    import cairo

from .debugshot import trace

GAP = 10  # between the button's bottom edge and the arrow tip
EDGE = 24  # the bubble never gets closer than this to the window edge
ARROW_W = 18
ARROW_H = 9


class FocusBubble(Gtk.Box):
    """One shared bubble for a set of buttons; shows for the focused one."""

    def __init__(self, overlay: Gtk.Overlay) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._overlay = overlay
        self._text: dict[Gtk.Widget, tuple[str, str]] = {}
        self._focused: Gtk.Widget | None = None
        self._hovered: Gtk.Widget | None = None
        self._showing: Gtk.Widget | None = None
        self._refresh_id = 0

        self.add_css_class("focus_bubble")
        self.set_halign(Gtk.Align.START)
        self.set_valign(Gtk.Align.START)
        # Purely informational: clicks go straight through to what is below.
        self.set_can_target(False)
        self.set_can_focus(False)

        # The arrow takes its fill from its own CSS `color`, so the stylesheet
        # stays the only place the bubble's colour is defined.
        self._arrow = Gtk.DrawingArea()
        self._arrow.add_css_class("focus_bubble_arrow")
        self._arrow.set_content_width(ARROW_W)
        self._arrow.set_content_height(ARROW_H)
        self._arrow.set_halign(Gtk.Align.START)
        self._arrow.set_draw_func(self._draw_arrow)
        self.append(self._arrow)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        body.add_css_class("focus_bubble_body")
        self._title = Gtk.Label(xalign=0.0)
        self._title.add_css_class("focus_bubble_title")
        self._detail = Gtk.Label(xalign=0.0)
        self._detail.add_css_class("focus_bubble_detail")
        body.append(self._title)
        body.append(self._detail)
        self.append(body)

    def attach(self, button: Gtk.Widget, title: str, detail: str) -> None:
        """Describe `button` with this bubble whenever it has focus or hover."""
        self._text[button] = (title, detail)
        button.update_property([Gtk.AccessibleProperty.DESCRIPTION], [detail])

        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_a: self._set("_focused", button))
        focus.connect("leave", lambda *_a: self._unset("_focused", button))
        button.add_controller(focus)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a: self._set("_hovered", button))
        motion.connect("leave", lambda *_a: self._unset("_hovered", button))
        button.add_controller(motion)

    def hide(self) -> None:
        self._focused = self._hovered = None
        self._refresh()

    # State
    # -------------------------------------------------------

    def _set(self, slot: str, button: Gtk.Widget) -> None:
        setattr(self, slot, button)
        self._queue_refresh()

    def _unset(self, slot: str, button: Gtk.Widget) -> None:
        if getattr(self, slot) is button:
            setattr(self, slot, None)
        self._queue_refresh()

    def _queue_refresh(self) -> None:
        # Moving focus between two buttons is a leave then an enter. Settle
        # once both have landed, so the bubble moves instead of blinking.
        if not self._refresh_id:
            self._refresh_id = GLib.idle_add(self._refresh)

    def _refresh(self) -> bool:
        if self._refresh_id:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = 0
        # The pointer is where the eye is, so hover wins over focus.
        target = self._hovered or self._focused
        if target is None:
            if self._showing is not None:
                trace("BUBBLE hidden")
            self._showing = None
            self.remove_css_class("shown")
        elif target is not self._showing:
            self._show_for(target)
        return GLib.SOURCE_REMOVE

    # Display
    # -------------------------------------------------------

    def _show_for(self, button: Gtk.Widget) -> None:
        ok, bounds = button.compute_bounds(self._overlay)
        if not ok:
            return
        title, detail = self._text[button]
        self._title.set_label(title)
        self._detail.set_label(detail)

        # measure() counts margins, and both still hold the last button's
        # position: clear them so only the bubble itself is measured.
        self.set_margin_start(0)
        self._arrow.set_margin_start(0)
        _min, width, _b, _b2 = self.measure(Gtk.Orientation.HORIZONTAL, -1)
        centre = bounds.origin.x + bounds.size.width / 2
        limit = self._overlay.get_width() - EDGE - width
        x = max(EDGE, min(centre - width / 2, limit))
        self.set_margin_start(int(x))
        self.set_margin_top(int(bounds.origin.y + bounds.size.height + GAP))
        # Pushed in from the screen edge, the arrow still points at the button.
        self._arrow.set_margin_start(int(centre - x - ARROW_W / 2))

        self._showing = button
        self.add_css_class("shown")
        trace(f"BUBBLE {title}")

    @staticmethod
    def _draw_arrow(
        area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int
    ) -> None:
        colour = area.get_color()
        cr.set_source_rgba(colour.red, colour.green, colour.blue, colour.alpha)
        cr.move_to(0, height)
        cr.line_to(width / 2, 0)
        cr.line_to(width, height)
        cr.close_path()
        cr.fill()
