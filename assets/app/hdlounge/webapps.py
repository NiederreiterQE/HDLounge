"""Website tiles: streaming services that have no Linux app."""

from __future__ import annotations

import re
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from gi.repository import GLib

from . import proc
from .appinfo import lookup
from .debugshot import trace

WEB_APP_PREFIX = "hdlounge-web-"

# (desktop id, name, command) of each browser that can host a website tile,
# best first. On Ubuntu: Google's own .deb, then Chromium as a Snap or .deb.
BROWSERS = (
    ("google-chrome.desktop", "Google Chrome", None),
    ("chromium_chromium.desktop", "Chromium", ("/snap/bin/chromium",)),
    ("chromium.desktop", "Chromium", None),
)

# Only Chrome ships Widevine.
DRM_BROWSERS = ("google-chrome.desktop",)

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
# on this box.
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
    return bool(_HOSTNAME_RE.match(host))


def normalise(url: str) -> str | None:
    """A full http(s) address, or None if this is not one."""
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
    """The window class a Chromium-based browser gives an --app window."""
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
    """Write a website tile's desktop entry and return its app id."""
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
    """Find a free name, or reuse the name for the same website."""
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


class _IconParser(HTMLParser):
    """Collect icon links declared by a website."""

    def __init__(self) -> None:
        super().__init__()
        self.icons: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "link":
            return

        values = dict(attrs)
        rel = (values.get("rel") or "").lower().split()
        href = values.get("href")

        if not href:
            return

        if "apple-touch-icon" in rel:
            self.icons.append((0, href))
        elif "icon" in rel:
            self.icons.append((1, href))


def _fetch_icon(
    url: str,
    icon: Path,
    app_id: str,
    on_icon: Callable[[], None] | None,
    on_settled: Callable[[], None] | None,
) -> None:
    """Fetch a site's own icon without blocking the interface."""
    parts = urllib.parse.urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    icons_dir().mkdir(parents=True, exist_ok=True)
    partial = icon.with_suffix(".part")

    def finish() -> None:
        if on_settled is not None:
            on_settled()

    def download(candidates: list[str]) -> None:
        if not candidates:
            partial.unlink(missing_ok=True)
            finish()
            return

        source = candidates.pop(0)

        def done(stdout: str | None) -> None:
            try:
                ok = stdout is not None and partial.read_bytes()[:8] == PNG_MAGIC
            except OSError:
                ok = False

            if not ok:
                partial.unlink(missing_ok=True)
                download(candidates)
                return

            partial.replace(icon)
            entry = applications_dir() / app_id

            if not entry.exists():
                finish()
                return

            text = entry.read_text(encoding="utf-8")
            entry.write_text(
                re.sub(
                    r"^Icon=.*$",
                    lambda _m: f"Icon={icon}",
                    text,
                    flags=re.M,
                ),
                encoding="utf-8",
            )

            trace(f"WEBAPP icon {app_id}")

            if on_icon is not None:
                on_icon()

            finish()

        proc.run(
            ["curl", "-fsSL", "--max-time", "20", "-o", str(partial), source],
            done,
        )

    def page_loaded(html: str | None) -> None:
        candidates: list[str] = []

        if html:
            parser = _IconParser()
            try:
                parser.feed(html)
                parser.close()
            except Exception:
                pass

            # Prefer touch icons, then ordinary favicon links.
            for _priority, href in sorted(parser.icons, key=lambda item: item[0]):
                candidate = urllib.parse.urljoin(url, href)
                if candidate not in candidates:
                    candidates.append(candidate)

        # Fallbacks for sites without icon links in their HTML.
        for fallback in (
            f"{base}/apple-touch-icon.png",
            f"{base}/favicon.png",
        ):
            if fallback not in candidates:
                candidates.append(fallback)

        download(candidates)

    proc.run(
        [
            "curl", "-fsSL", "--max-time", "20",
            "-A", "Mozilla/5.0",
            url,
        ],
        page_loaded,
    )
