"""Application entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, Gtk

from . import APP_ID
from .bridge import ShellBridge
from .config import Config
from .tvpower import TvPower
from .window import HDLoungeWindow


class HDLoungeApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.config = Config()
        self.bridge = ShellBridge()
        # Outlives the window: the screen can blank while an app is in front.
        self.tvpower = TvPower(self.config)
        self._window: HDLoungeWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # The TV interface is dark regardless of the desktop's preference.
        self.get_style_manager().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        self._load_css()

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).parent / "style.css"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def do_activate(self) -> None:
        if self._window is None:
            self._window = HDLoungeWindow(self, self.config, self.bridge, self.tvpower)
            self._window.add_css_class("hdlounge")
        self._window.present()


def main(argv: list[str] | None = None) -> int:
    return HDLoungeApplication().run(argv if argv is not None else sys.argv)
