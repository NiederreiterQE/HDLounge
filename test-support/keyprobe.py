#!/usr/bin/env python3
"""Report what a remote button really sends, at both layers that matter.

Default mode opens a GTK window and prints the keysym the *compositor*
delivers -- what an application can match on.

    python3 test-support/keyprobe.py

--evdev reads /dev/input directly and prints the kernel keycode and the HID
scancode underneath it -- what a udev hwdb rule has to match on to remap the
key for every application at once. Needs root, since /dev/input/event* is
root:input and carries no ACL for an ordinary user.

    sudo python3 test-support/keyprobe.py --evdev

Either way: run it, press the button, Ctrl+C.
"""

from __future__ import annotations

import fcntl
import glob
import os
import re
import select
import struct
import sys

# struct input_event { struct timeval time; __u16 type, code; __s32 value; }
# Native sizes, so this is right on both 32- and 64-bit.
_EVENT_FMT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)

EV_KEY = 0x01
EV_MSC = 0x04
MSC_SCAN = 0x04

# Enough of the remote-relevant codes to be useful when the kernel headers
# are not installed; the header supersedes this whenever it is present.
EVDEV_NAMES = {
    1: "KEY_ESC", 28: "KEY_ENTER", 57: "KEY_SPACE", 96: "KEY_KPENTER",
    102: "KEY_HOME", 103: "KEY_UP", 105: "KEY_LEFT", 106: "KEY_RIGHT",
    108: "KEY_DOWN", 113: "KEY_MUTE", 114: "KEY_VOLUMEDOWN",
    115: "KEY_VOLUMEUP", 116: "KEY_POWER", 139: "KEY_MENU", 158: "KEY_BACK",
    164: "KEY_PLAYPAUSE", 168: "KEY_REWIND", 172: "KEY_HOMEPAGE",
    208: "KEY_FASTFORWARD", 217: "KEY_SEARCH", 352: "KEY_OK",
    353: "KEY_SELECT", 393: "KEY_VIDEO",
}


def evdev_names() -> dict[int, str]:
    names = dict(EVDEV_NAMES)
    try:
        with open("/usr/include/linux/input-event-codes.h") as fh:
            for line in fh:
                parts = line.split()
                if (len(parts) >= 3 and parts[0] == "#define"
                        and re.match(r"(KEY|BTN)_", parts[1])):
                    try:
                        names.setdefault(int(parts[2], 0), parts[1])
                    except ValueError:
                        pass                # an alias, e.g. KEY_MIN_INTERESTING
    except OSError:
        pass
    return names


# evdev
# -------------------------------------------------------

def device_name(fd: int) -> str:
    buf = bytearray(256)
    # EVIOCGNAME(len) == _IOR('E', 0x06, len)
    request = (2 << 30) | (len(buf) << 16) | (ord("E") << 8) | 0x06
    try:
        fcntl.ioctl(fd, request, buf)
    except OSError:
        return "?"
    return buf.split(b"\x00", 1)[0].decode("utf-8", "replace")


def run_evdev(paths: list[str]) -> int:
    names = evdev_names()
    paths = paths or sorted(glob.glob("/dev/input/event*"))

    devices = {}
    denied = 0
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except PermissionError:
            denied += 1
            continue
        except OSError:
            continue
        devices[fd] = (path, device_name(fd))

    if not devices:
        print("Could not open any input device."
              + (" Re-run with sudo." if denied else ""), file=sys.stderr)
        return 1

    print("Watching:")
    for fd, (path, name) in sorted(devices.items()):
        print(f"  {path:<20} {name}")
    print("\nPress a button. Ctrl+C to stop.\n")

    scancode = None     # MSC_SCAN precedes the EV_KEY it belongs to
    seen: dict[tuple, str] = {}
    try:
        while True:
            ready, _, _ = select.select(list(devices), [], [])
            for fd in ready:
                data = os.read(fd, _EVENT_SIZE * 64)
                for i in range(0, len(data) - _EVENT_SIZE + 1, _EVENT_SIZE):
                    _s, _us, etype, code, value = struct.unpack_from(
                        _EVENT_FMT, data, i)

                    if etype == EV_MSC and code == MSC_SCAN:
                        scancode = value
                    elif etype == EV_KEY and value == 1:      # press only
                        name = names.get(code, "?")
                        _, dev = devices[fd]
                        if scancode is None:
                            where = "no scancode reported"
                            hwdb = None
                        else:
                            where = f"scancode 0x{scancode & 0xffffffff:08x}"
                            hwdb = f"KEYBOARD_KEY_{scancode & 0xffffffff:x}="
                        line = (f"{dev:<24} {name:<16} keycode {code:<5} {where}")
                        print(line, flush=True)
                        if hwdb:
                            print(f"{'':<24} hwdb: {hwdb}<new key, "
                                  f"e.g. enter>", flush=True)
                        seen[(dev, code)] = line
                        scancode = None
    except KeyboardInterrupt:
        pass
    finally:
        for fd in devices:
            os.close(fd)

    print("\n--- every distinct key seen ---", file=sys.stderr)
    for _, line in sorted(seen.items()):
        print(line, file=sys.stderr)
    return 0


# GTK
# -------------------------------------------------------

def run_gtk() -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gtk

    names = evdev_names()
    seen: dict[int, str] = {}

    class Probe(Gtk.Application):
        def __init__(self) -> None:
            super().__init__(application_id="ca.harmonicdesign.HDLounge.KeyProbe")

        def do_activate(self) -> None:
            self.label = Gtk.Label(label="Press a button on the remote")
            self.label.set_wrap(True)
            self.label.set_justify(Gtk.Justification.CENTER)
            self.label.add_css_class("title-1")

            window = Gtk.ApplicationWindow(application=self,
                                           title="HDLounge key probe")
            window.set_default_size(900, 400)
            window.set_child(self.label)

            keys = Gtk.EventControllerKey()
            keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            keys.connect("key-pressed", self.on_key)
            window.add_controller(keys)
            window.present()

        def on_key(self, _c, keyval, keycode, _state) -> bool:
            evdev = keycode - 8         # the compositor passes evdev code + 8
            evdev_name = names.get(evdev, "?")
            keyname = Gdk.keyval_name(keyval)

            if keyval == 0 or keyname is None:
                keyname = "-"
                verdict = "NO KEYSYM: nothing can match this key"
            else:
                verdict = "ok"

            line = (f"evdev {evdev:>4} {evdev_name:<16} keycode {keycode:>4}  "
                    f"keyval 0x{keyval:04x} {keyname:<22} {verdict}")
            print(line, flush=True)
            seen[keycode] = line
            self.label.set_label(
                f"{evdev_name}\nevdev {evdev} · keycode {keycode}\n"
                f"keysym: {keyname}\n{verdict}")
            return True                 # consume, so nothing else acts on it

    app = Probe()
    try:
        app.run([])
    except KeyboardInterrupt:
        pass
    print("\n--- every distinct key seen ---", file=sys.stderr)
    for _, line in sorted(seen.items()):
        print(line, file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    if "--evdev" in argv:
        rest = [a for a in argv if a != "--evdev"]
        return run_evdev(rest)
    return run_gtk()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
