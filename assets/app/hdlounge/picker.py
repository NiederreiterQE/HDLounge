"""Full-screen picker for choosing an installed app, or a website, to pin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from gi.repository import Adw, Gdk, GObject, Gtk

from . import webapps
from .appinfo import all_visible
from .debugshot import trace
from .keynav import REMOTE_OK

if TYPE_CHECKING:
    from .bridge import ShellBridge
    from .config import Config


class AppPicker(Adw.Dialog):
    __gsignals__ = {"app-chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(
        self,
        config: Config,
        bridge: ShellBridge,
        on_site_icon: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.bridge = bridge
        # A website's icon arrives after this dialog has closed; this redraws
        # the tile with it.
        self._on_site_icon = on_site_icon
        self.set_title("Add App")
        self.set_content_width(720)
        self.set_content_height(620)

        self._nav = Adw.NavigationView()
        self._apps_page = self._build_apps_page()
        self._nav.add(self._apps_page)
        self.set_child(self._nav)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

    # Apps page
    # -------------------------------------------------------

    def _build_apps_page(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text("Search applications")
        self._search.connect(
            "search-changed", lambda *_a: self._list.invalidate_filter()
        )
        # Down from the search entry drops into the list.
        search_keys = Gtk.EventControllerKey()
        search_keys.connect("key-pressed", self._on_search_key)
        self._search.add_controller(search_keys)
        box.append(self._search)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list.add_css_class("boxed-list")
        self._list.connect("row-activated", self._on_row_activated)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(self._list)
        box.append(scroller)

        toolbar.set_content(box)

        self._names: dict[str, str] = {}
        self._list.set_filter_func(self._filter_row)
        self._populate()
        return Adw.NavigationPage.new(toolbar, "Add App to Drawer")

    def _populate(self) -> None:
        # Websites head the list: they are what a TV most often needs and a
        # Linux box most often lacks.
        self._list.append(self._build_website_row())

        apps = [a for a in all_visible() if not self.config.is_pinned(a.get_id())]
        apps.sort(key=lambda a: (a.get_name() or "").lower())

        for info in apps:
            row = Adw.ActionRow()
            row.set_title(GObject.markup_escape_text(info.get_name() or ""))
            description = info.get_description()
            if description:
                row.set_subtitle(GObject.markup_escape_text(description))
            row.set_activatable(True)

            icon = info.get_icon()
            image = (
                Gtk.Image.new_from_gicon(icon)
                if icon
                else Gtk.Image.new_from_icon_name("application-x-executable")
            )
            image.set_pixel_size(32)
            row.add_prefix(image)

            row.app_id = info.get_id()
            self._list.append(row)
            self._names[info.get_id()] = (info.get_name() or "").lower()

        if not apps:
            placeholder = Adw.StatusPage(
                title="Everything is pinned",
                description="Every installed app is already in the drawer.",
            )
            self._list.set_placeholder(placeholder)

    def _build_website_row(self) -> Gtk.Widget:
        found = webapps.browser()
        if found is None:
            subtitle = "Needs Google Chrome — select to get it from Software"
        elif webapps.plays_drm(found[0]):
            subtitle = f"Netflix, Crave and more, as tiles that open in {found[1]}"
        else:
            subtitle = (
                f"Opens in {found[1]}, which cannot play Netflix and "
                "other DRM services — install Google Chrome for those"
            )
        row = Adw.ActionRow(title="Add a Website…", subtitle=subtitle)
        image = Gtk.Image.new_from_icon_name("web-browser-symbolic")
        image.set_pixel_size(32)
        row.add_prefix(image)
        row.set_activatable(True)
        row.website = True
        return row

    def _filter_row(self, row: Gtk.ListBoxRow) -> bool:
        query = self._search.get_text().strip().lower()
        if not query:
            return True
        app_id = getattr(row, "app_id", "")
        if not app_id:
            return False  # the website row: searching means an app
        return query in self._names.get(app_id, "") or query in app_id.lower()

    def _first_app_row(self) -> Gtk.ListBoxRow | None:
        index = 0
        while (row := self._list.get_row_at_index(index)) is not None:
            if getattr(row, "app_id", None) and row.get_child_visible():
                return row
            index += 1
        return None

    # Websites page
    # -------------------------------------------------------

    def _open_websites(self) -> None:
        if webapps.browser() is None:
            self.bridge.open_external(
                "org.gnome.Software.desktop",
                ["gnome-software", "--details=com.google.Chrome"],
            )
            self.close()
            return
        trace("PICKER websites")
        self._nav.push(self._build_websites_page())

    def _build_websites_page(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        presets = Gtk.ListBox()
        presets.set_selection_mode(Gtk.SelectionMode.NONE)
        presets.add_css_class("boxed-list")
        for name, url in webapps.PRESETS:
            row = Adw.ActionRow(title=name, subtitle=url)
            image = Gtk.Image.new_from_icon_name("web-browser-symbolic")
            image.set_pixel_size(32)
            row.add_prefix(image)
            row.set_activatable(True)
            row.site = (name, url)
            presets.append(row)
        presets.connect("row-activated", lambda _l, row: self._add_site(*row.site))
        box.append(presets)

        heading = Gtk.Label(label="Any Other Website", xalign=0.0)
        heading.add_css_class("heading")
        heading.set_margin_top(6)
        box.append(heading)

        custom = Gtk.ListBox()
        custom.set_selection_mode(Gtk.SelectionMode.NONE)
        custom.add_css_class("boxed-list")
        self._site_name = Adw.EntryRow(title="Name")
        self._site_url = Adw.EntryRow(title="Address")
        custom.append(self._site_name)
        custom.append(self._site_url)
        box.append(custom)

        self._site_error = Gtk.Label(xalign=0.0)
        self._site_error.add_css_class("error")
        self._site_error.set_visible(False)
        box.append(self._site_error)

        self._site_add = Gtk.Button(label="Add Website")
        self._site_add.add_css_class("suggested-action")
        self._site_add.add_css_class("pill")
        self._site_add.set_halign(Gtk.Align.END)
        self._site_add.set_sensitive(False)
        self._site_add.connect("clicked", lambda *_a: self._add_custom())
        box.append(self._site_add)

        for entry in (self._site_name, self._site_url):
            entry.connect("changed", lambda *_a: self._sync_add())
        self._site_url.connect("entry-activated", lambda *_a: self._add_custom())

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(box)
        toolbar.set_content(scroller)

        page = Adw.NavigationPage.new(toolbar, "Add a Website")
        # A remote needs somewhere to start: the first site, once it is shown.
        page.connect("shown", lambda *_a: presets.get_row_at_index(0).grab_focus())
        return page

    def _sync_add(self) -> None:
        self._site_add.set_sensitive(
            bool(
                self._site_name.get_text().strip() and self._site_url.get_text().strip()
            )
        )

    def _add_custom(self) -> None:
        self._add_site(self._site_name.get_text(), self._site_url.get_text())

    def _add_site(self, name: str, url: str) -> None:
        app_id = webapps.create(name, url, on_icon=self._on_site_icon)
        if app_id is None:
            self._site_error.set_text("That does not look like a website address.")
            self._site_error.set_visible(True)
            return
        self.emit("app-chosen", app_id)
        self.close()

    # Keys
    # -------------------------------------------------------

    def _on_search_key(
        self,
        _c: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Down:
            first = self._list.get_row_at_index(0)
            if first is not None:
                self._list.select_row(first)
                first.grab_focus()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, REMOTE_OK):
            row = self._list.get_selected_row() or self._first_app_row()
            if row is not None:
                self._on_row_activated(self._list, row)
            return True
        return False

    def _on_key_pressed(
        self,
        _c: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_Escape, Gdk.keyval_from_name("XF86Back")):
            # Back out of the websites page before closing altogether.
            if self._nav.get_visible_page() is not self._apps_page:
                self._nav.pop()
            else:
                self.close()
            return True
        # GTK activates a focused row, button or entry on Return by itself,
        # but not on the remote's OK.
        if keyval == REMOTE_OK:
            focus = self.get_focus()
            if focus is not None and focus.activate():
                return True
            row = self._list.get_selected_row()
            if row is not None and self._nav.get_visible_page() is self._apps_page:
                self._on_row_activated(self._list, row)
                return True
        return False

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if getattr(row, "website", False):
            self._open_websites()
            return
        app_id = getattr(row, "app_id", None)
        if app_id:
            self.emit("app-chosen", app_id)
        self.close()
