// TEST-ONLY extension: synthesises key events via a Clutter virtual device
// so that keyboard navigation can be asserted in a headless nested session.
// This is never installed by install.sh.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IFACE = `
<node>
  <interface name="ca.harmonicdesign.HDLounge.Inject">
    <method name="Key">
      <arg type="u" direction="in" name="keyval"/>
    </method>
    <method name="KeyHold">
      <arg type="u" direction="in" name="keyval"/>
      <arg type="u" direction="in" name="millis"/>
    </method>
    <method name="KeyHoldRemote">
      <arg type="u" direction="in" name="keyval"/>
      <arg type="u" direction="in" name="millis"/>
    </method>
    <method name="MoveTo">
      <arg type="d" direction="in" name="x"/>
      <arg type="d" direction="in" name="y"/>
    </method>
    <method name="FocusInfo">
      <arg type="s" direction="out" name="info"/>
    </method>
  </interface>
</node>`;

export default class InjectExtension extends Extension {
    enable() {
        const seat = Clutter.get_default_backend().get_default_seat();
        this._device = seat.create_virtual_device(
            Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this._pointer = seat.create_virtual_device(
            Clutter.InputDeviceType.POINTER_DEVICE);

        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, '/ca/harmonicdesign/HDLounge/Inject');
        this._nameId = Gio.bus_own_name(
            Gio.BusType.SESSION, 'ca.harmonicdesign.HDLounge.Inject',
            Gio.BusNameOwnerFlags.REPLACE, null, null, null);
    }

    disable() {
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = 0;
        }
        this._dbus?.unexport();
        this._dbus = null;
        this._device = null;
        this._pointer = null;
    }

    _press(keyval) {
        this._device.notify_keyval(
            global.get_current_time() * 1000, keyval, Clutter.KeyState.PRESSED);
    }

    _release(keyval) {
        this._device.notify_keyval(
            global.get_current_time() * 1000, keyval, Clutter.KeyState.RELEASED);
    }

    Key(keyval) {
        this._press(keyval);
        this._release(keyval);
    }

    KeyHold(keyval, millis) {
        this._press(keyval);
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, millis, () => {
            this._release(keyval);
            return GLib.SOURCE_REMOVE;
        });
    }

    /**
     * Simulates an IR/USB remote that repeats a held button by re-sending a
     * full press/release pair, rather than press-without-release.
     */
    KeyHoldRemote(keyval, millis) {
        const period = 60;
        let elapsed = 0;
        this._press(keyval);
        this._release(keyval);
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, period, () => {
            elapsed += period;
            if (elapsed >= millis)
                return GLib.SOURCE_REMOVE;
            this._press(keyval);
            this._release(keyval);
            return GLib.SOURCE_CONTINUE;
        });
    }

    /** Move the real pointer, so cursor-visibility behaviour can be tested. */
    MoveTo(x, y) {
        this._pointer.notify_absolute_motion(
            global.get_current_time() * 1000, x, y);
    }

    /** Which window currently has keyboard focus, for diagnosis. */
    FocusInfo() {
        const w = global.display.focus_window;
        if (!w)
            return 'NO_FOCUS_WINDOW';
        return `${w.get_title()} | wm_class=${w.get_wm_class()} | pid=${w.get_pid()}`;
    }
}
