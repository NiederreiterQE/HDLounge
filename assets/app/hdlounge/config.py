"""User configuration: pinned apps and appearance.

Stored as JSON in ~/.config/hdlounge/config.json so it stays hand-editable, and
seeded from the user's existing GNOME favourites the first time HDLounge runs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from gi.repository import Gio, GLib

from .appinfo import lookup

CONFIG_DIR = Path(GLib.get_user_config_dir()) / "hdlounge"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "background": None,  # absolute path to an image, or None for a plain gradient
    "background_dim": 0.55,  # 0..1 scrim over the background, for text legibility
    "apps": [],  # [{"id": "foo.desktop", "fullscreen": true}, ...]
    "clock_24h": True,
    # The TV this box is plugged into, controlled over the network because
    # most PCs have no HDMI-CEC. "key" is what the TV handed back when it was
    # paired; "mac" is for Wake-on-LAN, which is the only way to switch a TV
    # in standby back on. "secure" is whether it answered on the TLS port.
    "tv": {"enabled": False, "host": "", "mac": "", "key": "", "secure": False},
}


def _seed_from_gnome_favourites() -> list[dict]:
    """Pre-populate the drawer from org.gnome.shell favorite-apps."""
    try:
        source = Gio.SettingsSchemaSource.get_default()
        if source is None or source.lookup("org.gnome.shell", True) is None:
            return []
        settings = Gio.Settings.new("org.gnome.shell")
        favourites = settings.get_strv("favorite-apps")
    except Exception:
        return []

    apps = []
    for app_id in favourites:
        if lookup(app_id) is not None:
            apps.append({"id": app_id, "fullscreen": True})
    return apps


class Config:
    def __init__(self) -> None:
        self._data = dict(DEFAULTS)
        self.load()

    # Storage
    # -------------------------------------------------------

    def load(self) -> None:
        if not CONFIG_FILE.exists():
            self._data = dict(DEFAULTS)
            self._data["apps"] = _seed_from_gnome_favourites()
            self.save()
            return

        try:
            with CONFIG_FILE.open(encoding="utf-8") as fh:
                stored = json.load(fh)
            if not isinstance(stored, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            # The next change would write defaults over it, and a hand-edit
            # with one stray comma would cost every pin. Keep what was there.
            backup = CONFIG_FILE.with_suffix(".json.bad")
            try:
                os.replace(CONFIG_FILE, backup)
                print(
                    f"hdlounge: could not read {CONFIG_FILE} ({exc}); "
                    f"using defaults, the old file is {backup}"
                )
            except OSError:
                print(f"hdlounge: could not read {CONFIG_FILE} ({exc}); using defaults")
            stored = {}

        data = dict(DEFAULTS)
        data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        # Pins whose .desktop file cannot be found are kept, not dropped: an
        # app may only be missing for now (Flatpak exports not yet on the
        # search path, a drive not yet mounted), and the next save would
        # otherwise make its absence permanent. `apps` hides them instead.
        data["apps"] = [
            a for a in data.get("apps", []) if isinstance(a, dict) and a.get("id")
        ]
        self._data = data

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, CONFIG_FILE)
        except OSError as exc:
            print(f"hdlounge: could not write {CONFIG_FILE} ({exc})")

    # Fields
    # -------------------------------------------------------

    @property
    def background(self) -> str | None:
        path = self._data.get("background")
        if not path:
            return None
        expanded = os.path.expanduser(path)
        return expanded if os.path.isfile(expanded) else None

    @background.setter
    def background(self, value: str | None) -> None:
        self._data["background"] = value
        self.save()

    @property
    def background_dim(self) -> float:
        try:
            return min(1.0, max(0.0, float(self._data.get("background_dim", 0.55))))
        except (TypeError, ValueError):
            return 0.55

    @property
    def clock_24h(self) -> bool:
        return bool(self._data.get("clock_24h", True))

    @property
    def apps(self) -> list[dict]:
        """Pins whose app can be found right now."""
        return [a for a in self._data.get("apps", []) if lookup(a["id"]) is not None]

    @property
    def tv(self) -> dict:
        """Always all four keys, whatever a hand-edited file left out."""
        tv = dict(DEFAULTS["tv"])
        stored = self._data.get("tv")
        if isinstance(stored, dict):
            tv.update({k: v for k, v in stored.items() if k in tv})
        return tv

    def set_tv(self, **fields: object) -> None:
        tv = self.tv
        tv.update({k: v for k, v in fields.items() if k in tv})
        self._data["tv"] = tv
        self.save()

    # Pins
    # -------------------------------------------------------

    def is_pinned(self, app_id: str) -> bool:
        return any(a["id"] == app_id for a in self._data.get("apps", []))

    def pin(self, app_id: str, fullscreen: bool = True) -> None:
        if self.is_pinned(app_id):
            return
        self._data.setdefault("apps", []).append(
            {"id": app_id, "fullscreen": fullscreen}
        )
        self.save()

    def unpin(self, app_id: str) -> None:
        self._data["apps"] = [
            a for a in self._data.get("apps", []) if a["id"] != app_id
        ]
        self.save()

    def set_fullscreen(self, app_id: str, fullscreen: bool) -> None:
        for entry in self._data.get("apps", []):
            if entry["id"] == app_id:
                entry["fullscreen"] = bool(fullscreen)
                self.save()
                return

    def wants_fullscreen(self, app_id: str) -> bool:
        for entry in self._data.get("apps", []):
            if entry["id"] == app_id:
                return bool(entry.get("fullscreen", True))
        return True

    def move(self, app_id: str, offset: int) -> bool:
        """Reorder a pin within the drawer. Returns True if it moved."""
        apps = self._data.get("apps", [])
        # Positions among the pins on screen: stepping over a hidden one
        # would look like the tile had not moved at all.
        shown = [
            i
            for i, a in enumerate(apps)
            if a["id"] == app_id or lookup(a["id"]) is not None
        ]
        here = next((n for n, i in enumerate(shown) if apps[i]["id"] == app_id), None)
        if here is None or not 0 <= here + offset < len(shown):
            return False
        a, b = shown[here], shown[here + offset]
        apps[a], apps[b] = apps[b], apps[a]
        self.save()
        return True
