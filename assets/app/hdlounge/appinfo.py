"""Desktop-entry lookup, insulated from the Gio.DesktopAppInfo deprecation."""

from __future__ import annotations

import os

import gi
from gi.repository import Gio, GLib

try:
    gi.require_version("GioUnix", "2.0")
    from gi.repository import GioUnix

    _DesktopAppInfo = GioUnix.DesktopAppInfo
except (ValueError, ImportError):  # pre-2.86
    _DesktopAppInfo = Gio.DesktopAppInfo


def lookup(desktop_id: str) -> Gio.DesktopAppInfo | None:
    """Return the DesktopAppInfo for a .desktop id, or None.

    A desktop file written a moment ago -- a website tile -- is not in GIO's
    index until its directory monitor fires on a later main-loop turn, if the
    monitor works at all. So a miss falls back on the user's own applications
    directory, read directly.
    """
    if not desktop_id:
        return None
    try:
        info = _DesktopAppInfo.new(desktop_id)
    except TypeError:
        info = None
    if info is None:
        path = os.path.join(GLib.get_user_data_dir(), "applications", desktop_id)
        if os.path.isfile(path):
            try:
                info = _DesktopAppInfo.new_from_filename(path)
            except TypeError:
                info = None
    return info


def all_visible() -> list[Gio.AppInfo]:
    """Every installed application that should appear in a menu."""
    return [a for a in Gio.AppInfo.get_all() if a.should_show()]
