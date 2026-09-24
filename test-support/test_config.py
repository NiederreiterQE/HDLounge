#!/usr/bin/env python3
"""The config file, without touching the real one: pins survive an app that
cannot be found for the moment, and a file that will not parse is kept.

    python3 test-support/test_config.py
"""

import json
import os
import sys
import tempfile

HOME = tempfile.mkdtemp(prefix="hdlounge-config-")
os.environ["XDG_CONFIG_HOME"] = HOME       # before GLib first reads it

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from hdlounge import config  # noqa: E402

FAILURES = []


def check(name, got, want):
    if got == want:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}\n        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


# Apps "installed" for this test: everything else is missing.
installed = {"kodi.desktop", "steam.desktop", "chrome.desktop"}
config.lookup = lambda app_id: object() if app_id in installed else None


def write(data):
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(
        data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def stored_ids():
    return [a["id"] for a in json.loads(config.CONFIG_FILE.read_text())["apps"]]


write({"apps": [{"id": "kodi.desktop"}, {"id": "gone.desktop"},
                {"id": "steam.desktop"}]})
c = config.Config()
check("an app that cannot be found is not shown",
      [a["id"] for a in c.apps], ["kodi.desktop", "steam.desktop"])
c.pin("chrome.desktop")
check("  but an unrelated save keeps its pin",
      stored_ids(), ["kodi.desktop", "gone.desktop", "steam.desktop",
                     "chrome.desktop"])

check("moving right steps over a hidden pin", c.move("kodi.desktop", 1), True)
check("  and lands after the next visible one",
      [a["id"] for a in c.apps], ["steam.desktop", "kodi.desktop", "chrome.desktop"])
check("moving off the end does nothing", c.move("chrome.desktop", 1), False)

installed.add("gone.desktop")
check("once the app is back, so is its tile",
      "gone.desktop" in [a["id"] for a in config.Config().apps], True)

write('{"apps": [{"id": "kodi.desktop"},]}')      # one stray comma
c = config.Config()
bad = config.CONFIG_FILE.with_suffix(".json.bad")
check("a file that will not parse is kept aside", bad.exists(), True)
check("  with its contents intact", "kodi.desktop" in bad.read_text(), True)

write("[1, 2, 3]")
c = config.Config()
check("a file that is not an object does not crash", c.apps, [])

check("the TV defaults include the remembered port",
      config.Config().tv["secure"], False)

print(f"\n  {'FAILED: ' + str(len(FAILURES)) if FAILURES else 'all passed'}")
sys.exit(1 if FAILURES else 0)
