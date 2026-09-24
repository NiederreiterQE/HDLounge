import Gio from "gi://Gio";
import GLib from "gi://GLib";
import GioUnix from "gi://GioUnix";
import Meta from "gi://Meta";
import Shell from "gi://Shell";

import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";
import * as Main from "resource:///org/gnome/shell/ui/main.js";

const BUS_NAME = "ca.harmonicdesign.HDLounge.Shell";
const OBJECT_PATH = "/ca/harmonicdesign/HDLounge/Shell";

const WEB_APP_PREFIX = "hdlounge-web-";

const IFACE = `
<node>
  <interface name="ca.harmonicdesign.HDLounge.Shell">
    <method name="RegisterLauncher">
      <arg type="u" direction="in"  name="pid"/>
    </method>
    <method name="LaunchOrFocus">
      <arg type="s" direction="in"  name="appId"/>
      <arg type="b" direction="in"  name="fullscreen"/>
      <arg type="s" direction="out" name="result"/>
    </method>
    <method name="FocusApp">
      <arg type="s" direction="in"  name="appId"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="CloseApp">
      <arg type="s" direction="in"  name="appId"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="KillApp">
      <arg type="s" direction="in"  name="appId"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="ListRunning">
      <arg type="a(ssub)" direction="out" name="apps"/>
    </method>
    <method name="ListWindows">
      <arg type="a(ssbb)" direction="out" name="windows"/>
    </method>
    <method name="ShowHome"/>
    <method name="FocusLauncher">
      <arg type="b" direction="out" name="ok"/>
    </method>
    <signal name="RunningChanged"/>
    <signal name="HomeRequested"/>
    <signal name="TvPowerRequested"/>
  </interface>
</node>`;

export default class HDLoungeExtension extends Extension {
	enable() {
		this._settings = this.getSettings();
		this._launcherPid = 0;
		this._focusRetryId = 0;
		this._webAppCache = null;
		this._webAppMonitor = null;
		// Apps the launcher asked us to fullscreen, pending their first frame.
		this._fullscreenApps = new Set();
		// The same for website tiles, which are known by window class.
		this._fullscreenWmClasses = new Set();
		this._panelWasVisible = false;
		this._origOverviewToggle = null;
		this._origOverviewShow = null;

		this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
		this._dbus.export(Gio.DBus.session, OBJECT_PATH);
		this._nameId = Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.REPLACE, null, null, null);

		this._appSystem = Shell.AppSystem.get_default();
		this._tracker = Shell.WindowTracker.get_default();

		// Running indicators: both state changes and focus changes matter.
		this._appSystem.connectObject("app-state-changed", () => this._emitRunningChanged(), this);
		this._tracker.connectObject("notify::focus-app", () => this._emitRunningChanged(), this);

		// Force-fullscreen newly mapped windows of apps we launched,
		// and keep keyboard focus sane.
		global.display.connectObject("window-created", (_d, win) => this._onWindowCreated(win), this);

		Main.wm.addKeybinding("show-home", this._settings, Meta.KeyBindingFlags.IGNORE_AUTOREPEAT, Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW, () => {
			this.ShowHome();
			this._dbus.emit_signal("HomeRequested", null);
		});

		// The remote's Power button.
		// The launcher owns the TV connection,
		// so all this does is pass the press on
		Main.wm.addKeybinding("tv-power", this._settings, Meta.KeyBindingFlags.IGNORE_AUTOREPEAT, Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW, () => this._dbus.emit_signal("TvPowerRequested", null));

		this._settings.connectObject("changed::kiosk-mode", () => this._syncKiosk(), this);
		this._syncKiosk();
	}

	disable() {
		this._setKiosk(false);

		if (this._focusRetryId) {
			GLib.source_remove(this._focusRetryId);
			this._focusRetryId = 0;
		}

		Main.wm.removeKeybinding("show-home");
		Main.wm.removeKeybinding("tv-power");
		this._settings?.disconnectObject(this);
		global.display.disconnectObject(this);
		this._appSystem?.disconnectObject(this);
		this._tracker?.disconnectObject(this);

		if (this._nameId) {
			Gio.bus_unown_name(this._nameId);
			this._nameId = 0;
		}
		this._dbus?.unexport();
		this._dbus = null;

		this._webAppMonitor?.disconnectObject(this);
		this._webAppMonitor?.cancel();
		this._webAppMonitor = null;
		this._webAppCache = null;

		this._fullscreenApps?.clear();
		this._fullscreenWmClasses?.clear();
		this._settings = null;
		this._appSystem = null;
		this._tracker = null;
	}

	/* Kiosk
    ------------------------------------------------------- */
	_syncKiosk() {
		this._setKiosk(this._settings.get_boolean("kiosk-mode"));
	}

	_setKiosk(on) {
		const panelBox = Main.layoutManager.panelBox;

		if (on) {
			if (this._origOverviewToggle) return; // already applied
			this._panelWasVisible = panelBox.visible;
			panelBox.hide();

			// Neutralise the overview without tearing it down,
			// so that disabling the extension restores stock behaviour
			this._origOverviewToggle = Main.overview.toggle;
			this._origOverviewShow = Main.overview.show;
			Main.overview.toggle = () => {};
			Main.overview.show = () => {};
			if (Main.overview.visible) Main.overview.hide();
		} else {
			if (!this._origOverviewToggle) return; // not applied
			if (this._panelWasVisible) panelBox.show();
			Main.overview.toggle = this._origOverviewToggle;
			Main.overview.show = this._origOverviewShow;
			this._origOverviewToggle = null;
			this._origOverviewShow = null;
		}
	}

	/* Internals
    ------------------------------------------------------- */
	_now() {
		return global.get_current_time() || global.display.get_current_time_roundtrip();
	}

	_emitRunningChanged() {
		this._dbus?.emit_signal("RunningChanged", null);
	}

	_lookup(appId) {
		const app = this._appSystem.lookup_app(appId);
		if (!app) {
			throw new Gio.DBusError({
				code: Gio.DBusError.INVALID_ARGS,
				message: `Unknown application: ${appId}`,
			});
		}
		return app;
	}

	/**
	 * Website tiles, read from their desktop files once
	 * and then kept until the applications directory changes.
	 *
	 * @returns {{id: string, info: GioUnix.DesktopAppInfo, wmClass: string}[]}
	 */
	_webApps() {
		if (this._webAppCache) return this._webAppCache;

		const dir = Gio.File.new_for_path(GLib.build_filenamev([GLib.get_user_data_dir(), "applications"]));
		if (!this._webAppMonitor) {
			try {
				this._webAppMonitor = dir.monitor_directory(Gio.FileMonitorFlags.NONE, null);
				this._webAppMonitor.connectObject(
					"changed",
					(_m, file) => {
						if (file.get_basename().startsWith(WEB_APP_PREFIX)) this._webAppCache = null;
					},
					this,
				);
			} catch {
				// No monitor: fall back on reading every time.
			}
		}
		const apps = this._readWebApps(dir);
		if (this._webAppMonitor) this._webAppCache = apps;
		return apps;
	}

	_readWebApps(dir) {
		const apps = [];
		let children;
		try {
			children = dir.enumerate_children("standard::name", Gio.FileQueryInfoFlags.NONE, null);
		} catch {
			return apps;
		}
		let file;
		while ((file = children.next_file(null)) !== null) {
			const name = file.get_name();
			if (!name.startsWith(WEB_APP_PREFIX) || !name.endsWith(".desktop")) continue;
			const info = GioUnix.DesktopAppInfo.new_from_filename(dir.get_child(name).get_path());
			const wmClass = info?.get_startup_wm_class();
			if (wmClass) apps.push({ id: name, info, wmClass });
		}
		children.close(null);
		return apps;
	}

	/** The website tile with this id; null if the id is not a website's. */
	_webApp(appId) {
		if (!appId.startsWith(WEB_APP_PREFIX)) return null;
		let web = this._webApps().find((w) => w.id === appId);
		if (!web) {
			// Written a moment ago, ahead of the monitor's notice.
			this._webAppCache = null;
			web = this._webApps().find((w) => w.id === appId);
		}
		if (!web) {
			throw new Gio.DBusError({
				code: Gio.DBusError.INVALID_ARGS,
				message: `Unknown application: ${appId}`,
			});
		}
		return web;
	}

	_windowsWithClass(wmClass) {
		return global
			.get_window_actors()
			.map((a) => a.meta_window)
			.filter((w) => w && w.window_type === Meta.WindowType.NORMAL && w.get_wm_class() === wmClass);
	}

	// Every window of an app, or of a website tile.
	_appWindows(appId) {
		const web = this._webApp(appId);
		return web ? this._windowsWithClass(web.wmClass) : this._lookup(appId).get_windows();
	}

	_raiseWindows(windows, fullscreen) {
		for (const w of windows) {
			if (w.minimized) w.unminimize();
			// Re-assert fullscreen, in case the user left it windowed.
			if (fullscreen && !w.is_fullscreen()) w.make_fullscreen();
		}
		Main.activateWindow(windows[0], this._now());
	}

	_launcherWindows() {
		if (!this._launcherPid) return [];
		return global
			.get_window_actors()
			.map((a) => a.meta_window)
			.filter((w) => w && w.get_pid() === this._launcherPid);
	}

	_onWindowCreated(win) {
		if (win.window_type !== Meta.WindowType.NORMAL) return;

		// When any window goes away, make sure focus lands somewhere useful.
		win.connectObject("unmanaged", () => this._onWindowGone(win), this);

		const actor = win.get_compositor_private();
		if (!actor) return;

		// The app association is not reliable until the window has actually
		// been mapped, so wait for its first frame before deciding.
		const id = actor.connect("first-frame", () => {
			actor.disconnect(id);
			if (this._launcherPid && win.get_pid() === this._launcherPid) {
				this._focusLauncher();
				return;
			}
			this._maybeFullscreen(win);
			// Another window of an app that is already running
			this._emitRunningChanged();
		});
	}

	/**
	 * The launcher is the desktop: when the last app window closes,
	 * focus must return to it rather than to nothing.
	 */
	_onWindowGone(closed) {
		// Once it is actually gone, not while it is still being unmanaged.
		GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
			this._emitRunningChanged();
			return GLib.SOURCE_REMOVE;
		});

		const remaining = global
			.get_window_actors()
			.map((a) => a.meta_window)
			.filter((w) => w && w !== closed && w.window_type === Meta.WindowType.NORMAL && w.get_pid() !== this._launcherPid);

		if (remaining.length === 0) this._focusLauncher();
	}

	_maybeFullscreen(win) {
		if (!this._settings?.get_boolean("force-fullscreen")) return;
		// Never touch the launcher itself; it manages its own window.
		if (this._launcherPid && win.get_pid() === this._launcherPid) return;

		const wmClass = win.get_wm_class();
		if (wmClass && this._fullscreenWmClasses.has(wmClass)) {
			this._fullscreenWmClasses.delete(wmClass);
			if (!win.is_fullscreen()) win.make_fullscreen();
			return;
		}

		const app = this._tracker.get_window_app(win);
		if (!app || !this._fullscreenApps.has(app.get_id())) return;

		this._fullscreenApps.delete(app.get_id());
		if (!win.is_fullscreen()) win.make_fullscreen();
	}

	/* D-Bus methods
    ------------------------------------------------------- */
	RegisterLauncher(pid) {
		this._launcherPid = pid;
		// The launcher is an ordinary Wayland client, so it cannot focus
		this._focusLauncherWhenReady();
	}

	_focusLauncher() {
		const windows = this._launcherWindows();
		if (windows.length === 0) return false;

		const w = windows[0];
		if (w.minimized) w.unminimize();
		Main.activateWindow(w, this._now());
		return true;
	}

	/**
	 * Focus the launcher once its window actually exists. Called on register
	 * and after the last other window closes.
	 */
	_focusLauncherWhenReady(attempts = 20) {
		// A launcher that restarts registers again; one chase at a time, or
		// the first timer's id is lost and disable() cannot remove it.
		if (this._focusRetryId) {
			GLib.source_remove(this._focusRetryId);
			this._focusRetryId = 0;
		}
		if (this._focusLauncher()) return;
		if (attempts <= 0) return;
		this._focusRetryId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 250, () => {
			this._focusRetryId = 0;
			this._focusLauncherWhenReady(attempts - 1);
			return GLib.SOURCE_REMOVE;
		});
	}

	FocusLauncher() {
		return this._focusLauncher();
	}

	LaunchOrFocus(appId, fullscreen) {
		const web = this._webApp(appId);
		if (web) return this._launchOrFocusWeb(web, fullscreen);

		const app = this._lookup(appId);
		const windows = app.get_windows().filter((w) => w.window_type === Meta.WindowType.NORMAL);
		const running = app.get_state() === Shell.AppState.RUNNING && windows.length > 0;

		if (!running) {
			if (fullscreen) this._fullscreenApps.add(appId);
			app.activate_full(-1, this._now());
			return "launched";
		}

		// Shell.App.activate() focuses a running app but does NOT restore a
		// minimised one so after Home, re-selecting an app would report
		// success while leaving it minimised and unfocused
		this._raiseWindows(windows, fullscreen);
		return "focused";
	}

	// LaunchOrFocus for a website tile: the same contract, by window class
	_launchOrFocusWeb(web, fullscreen) {
		const windows = this._windowsWithClass(web.wmClass);
		if (windows.length > 0) {
			this._raiseWindows(windows, fullscreen);
			return "focused";
		}
		if (fullscreen) this._fullscreenWmClasses.add(web.wmClass);
		web.info.launch([], global.create_app_launch_context(this._now(), -1));
		return "launched";
	}

	/**
	 * Raise a running app, never launch one.
	 * @returns {boolean} false if the app has no window yet
	 */
	FocusApp(appId) {
		const windows = this._appWindows(appId).filter((w) => w.window_type === Meta.WindowType.NORMAL);
		if (windows.length === 0) return false;

		for (const w of windows) {
			if (w.minimized) w.unminimize();
		}
		Main.activateWindow(windows[0], this._now());
		return true;
	}

	// Graceful close: politely asks each window to go away.
	CloseApp(appId) {
		const windows = this._appWindows(appId);
		if (windows.length === 0) return false;
		const now = this._now();
		for (const w of windows) w.delete(now);
		return true;
	}

	// Force kill: SIGKILL to the owning process. For hung apps.
	KillApp(appId) {
		const windows = this._appWindows(appId);
		if (windows.length === 0) return false;

		const pids = new Set();
		for (const w of windows) {
			const pid = w.get_pid();
			if (pid > 0) pids.add(pid);
			w.kill();
		}
		// Backstop: a hung client may not die from Meta's kill alone.
		for (const pid of pids) {
			try {
				GLib.spawn_command_line_async(`kill -9 ${pid}`);
			} catch (e) {
				logError(e, `HDLounge: failed to SIGKILL ${pid}`);
			}
		}
		return true;
	}

	ListRunning() {
		const focusApp = this._tracker.focus_app;
		const focusId = focusApp ? focusApp.get_id() : null;
		const focusWindow = global.display.focus_window;

		// Website tiles run as windows of their browser, so the shell counts
		// them as the browser's. Report each tile as an app of its own, and
		// the browser only for windows that are not one of them.
		const webWindows = new Set();
		const running = [];
		for (const web of this._webApps()) {
			const windows = this._windowsWithClass(web.wmClass);
			if (windows.length === 0) continue;
			windows.forEach((w) => webWindows.add(w));
			running.push([web.id, web.info.get_name(), windows.length, windows.includes(focusWindow)]);
		}

		for (const app of this._appSystem.get_running()) {
			const windows = app.get_windows();
			const own = windows.filter((w) => !webWindows.has(w));
			if (windows.length > 0 && own.length === 0) continue; // nothing open but website tiles
			running.push([app.get_id(), app.get_name(), own.length, app.get_id() === focusId && !webWindows.has(focusWindow)]);
		}
		return running;
	}

	// Diagnostic: every normal window with its app id and state.
	ListWindows() {
		const webIds = new Map(this._webApps().map((web) => [web.wmClass, web.id]));
		const out = [];
		for (const actor of global.get_window_actors()) {
			const w = actor.meta_window;
			if (!w || w.window_type !== Meta.WindowType.NORMAL) continue;
			const app = this._tracker.get_window_app(w);
			out.push([webIds.get(w.get_wm_class()) ?? (app ? app.get_id() : ""), w.get_title() || "", w.is_fullscreen(), w.minimized]);
		}
		return out;
	}

	// The Home button: minimise everything, bring the launcher forward.
	ShowHome() {
		const launcher = this._launcherWindows();
		const now = this._now();

		for (const actor of global.get_window_actors()) {
			const w = actor.meta_window;
			if (!w || w.window_type !== Meta.WindowType.NORMAL) continue;
			if (launcher.includes(w)) continue;
			if (!w.minimized) w.minimize();
		}

		if (launcher.length > 0) {
			const w = launcher[0];
			if (w.minimized) w.unminimize();
			Main.activateWindow(w, now);
		}
	}
}
