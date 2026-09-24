"""Website tiles: streaming services that have no Linux app."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Callable

from gi.repository import GLib

from . import proc
from .appinfo import lookup
from .debugshot import trace

WEB_APP_PREFIX = "hdlounge-web-"

# (desktop id, name, command) of each browser that can host a website tile,
# best first. The Flatpaks, and the packages: Google's own RPM, which GNOME
# Software offers as a third-party repository, and Fedora's chromium. A None
# command is read from the browser's own desktop entry.
BROWSERS = (
    (
        "com.google.Chrome.desktop",
        "Google Chrome",
        ["flatpak", "run", "com.google.Chrome"],
    ),
    ("google-chrome.desktop", "Google Chrome", None),
    (
        "org.chromium.Chromium.desktop",
        "Chromium",
        ["flatpak", "run", "org.chromium.Chromium"],
    ),
    ("chromium-browser.desktop", "Chromium", None),
)
# Only Chrome ships Widevine.
DRM_BROWSERS = ("com.google.Chrome.desktop", "google-chrome.desktop")

PRESETS = (
    ("Netflix", "https://www.netflix.com"),
    ("Prime Video", "https://www.primevideo.com"),
    ("Disney+", "https://www.disneyplus.com"),
    ("Crave", "https://www.crave.ca"),
    ("CBC Gem", "https://gem.cbc.ca"),
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def applications_dir() -> Path:
    return Path(GLib.get_user_data_dir()) / "applications"


def icons_dir() -> Path:
    # Not under the launcher's own data directory: install.sh replaces that
    # wholesale on every reinstall.
    return Path(GLib.get_user_data_dir()) / "hdlounge-websites"


def browser(drm: bool = False) -> tuple[str, str] | None:
    """(desktop id, name) of the best installed browser, or None.

    With drm, only one that can play Netflix and the like counts.
    """
    for desktop_id, name, _command in BROWSERS:
        if drm and not plays_drm(desktop_id):
            continue
        if lookup(desktop_id) is not None:
            return desktop_id, name
    return None


def plays_drm(desktop_id: str) -> bool:
    return desktop_id in DRM_BROWSERS


def _command(desktop_id: str) -> str | None:
    for known, _name, command in BROWSERS:
        if known != desktop_id:
            continue
        if command is not None:
            return " ".join(command)
        info = lookup(desktop_id)
        executable = info.get_executable() if info is not None else ""
        return executable or None
    return None


def is_web_app(app_id: str) -> bool:
    return app_id.startswith(WEB_APP_PREFIX)


# A host is either a dotted name, or "localhost" for someone running something
# on this box. Without this almost any typed-in phrase passes: urlsplit happily
# reports "my favourite show" as the hostname of https://my favourite show, and
# the tile that results can never load.
_HOSTNAME_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+$"
)


def is_hostname(host: str | None) -> bool:
    """True for something that could actually resolve: a.b, or localhost."""
    if not host:
        return False
    if host == "localhost":
        return True
    # An IPv4 literal is all digits and dots, which the name pattern allows.
    return bool(_HOSTNAME_RE.match(host))


def normalise(url: str) -> str | None:
    """A full http(s) address, or None if this is not one.

    Anything the desktop entry's Exec quoting would have to escape is
    percent-encoded instead, so the address can go in as it is.
    """
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not is_hostname(parts.hostname):
        return None
    url = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path or "/", parts.query, "")
    )
    return urllib.parse.quote(url, safe=":/?#[]@!&'()*+,;=-._~%")


def wm_class(url: str) -> str:
    """The window class a Chromium-based browser gives an --app window.

    Built from the address's host and path, with slashes made underscores:
    https://www.netflix.com becomes chrome-www.netflix.com__-Default, the last
    part being the browser profile.
    """
    parts = urllib.parse.urlsplit(url)
    name = f"{parts.hostname}_{parts.path or '/'}"
    return f"chrome-{name}-Default".replace("/", "_")


def slug_of(app_id: str) -> str:
    return app_id[len(WEB_APP_PREFIX) :].removesuffix(".desktop")


def create(
    name: str,
    url: str,
    on_icon: Callable[[], None] | None = None,
    on_settled: Callable[[], None] | None = None,
) -> str | None:
    """Write a website tile's desktop entry and return its app id.

    None if there is no browser to open it in, or the address is not a
    website. The site's icon is fetched afterwards: `on_icon` runs if one
    arrives, so the tile can be redrawn with it, and `on_settled` once the
    fetch is over either way -- straight away if there was nothing to fetch.
    """
    found = browser()
    url = normalise(url)
    name = " ".join(name.split())
    command = _command(found[0]) if found is not None else None
    if command is None or url is None or not name:
        return None

    _desktop_id, browser_name = found
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "website"
    slug = _free_slug(base, url)
    app_id = f"{WEB_APP_PREFIX}{slug}.desktop"
    icon = icons_dir() / f"{slug}.png"

    applications_dir().mkdir(parents=True, exist_ok=True)
    (applications_dir() / app_id).write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment=Opens {url} in {browser_name}\n"
        # Wayland explicitly, never a quiet fallback to XWayland.
        f"Exec={command} --ozone-platform=wayland "
        f"\"--app={url.replace('%', '%%')}\"\n"
        f"Icon={icon if icon.exists() else 'web-browser'}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;\n"
        f"StartupWMClass={wm_class(url)}\n"
        f"X-HDLounge-Web={url}\n",
        encoding="utf-8",
    )
    trace(f"WEBAPP created {app_id}")

    if not icon.exists():
        _fetch_icon(url, icon, app_id, on_icon, on_settled)
    elif on_settled is not None:
        on_settled()
    return app_id


def _free_slug(base: str, url: str) -> str:
    """`base`, or `base-2` and so on if that name already opens another site.

    The same site under the same name is the same tile, and is rewritten in
    place; a second "News" for a different address must not replace the
    first one's address and icon.
    """
    slug, n = base, 1
    while True:
        entry = applications_dir() / f"{WEB_APP_PREFIX}{slug}.desktop"
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            return slug
        if f"\nX-HDLounge-Web={url}\n" in f"\n{text}":
            return slug
        n += 1
        slug = f"{base}-{n}"


def remove(app_id: str) -> None:
    if not is_web_app(app_id):
        return
    (applications_dir() / app_id).unlink(missing_ok=True)
    (icons_dir() / f"{slug_of(app_id)}.png").unlink(missing_ok=True)
    trace(f"WEBAPP removed {app_id}")


def _fetch_icon(
    url: str,
    icon: Path,
    app_id: str,
    on_icon: Callable[[], None] | None,
    on_settled: Callable[[], None] | None,
) -> None:
    """Best effort: the site's own touch icon, big enough for a tile.

    Only the site itself is asked -- no third-party favicon service learns
    what is being watched. Without one the tile keeps the generic icon.
    """
    parts = urllib.parse.urlsplit(url)
    source = f"{parts.scheme}://{parts.netloc}/apple-touch-icon.png"
    icons_dir().mkdir(parents=True, exist_ok=True)
    partial = icon.with_suffix(".part")

    def done(stdout: str | None) -> None:
        try:
            _use_icon(stdout)
        finally:
            if on_settled is not None:
                on_settled()

    def _use_icon(stdout: str | None) -> None:
        try:
            ok = stdout is not None and partial.read_bytes()[:8] == PNG_MAGIC
        except OSError:
            ok = False
        if not ok:
            partial.unlink(missing_ok=True)
            return
        partial.replace(icon)
        entry = applications_dir() / app_id
        if not entry.exists():
            return  # removed while the icon was on its way
        text = entry.read_text(encoding="utf-8")
        # A lambda, not a replacement string: re.sub reads backslashes and
        # \g<...> in a replacement as escapes, and the path is a filename,
        # not a pattern.
        entry.write_text(
            re.sub(r"^Icon=.*$", lambda _m: f"Icon={icon}", text, flags=re.M),
            encoding="utf-8",
        )
        trace(f"WEBAPP icon {app_id}")
        if on_icon is not None:
            on_icon()

    proc.run(["curl", "-fsSL", "--max-time", "20", "-o", str(partial), source], done)
