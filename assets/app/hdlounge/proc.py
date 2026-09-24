"""Small async subprocess helper.

Quick settings shell out to wpctl/pactl/nmcli. Those are normally fast, but a
wedged PipeWire or NetworkManager should degrade the panel, not freeze the
whole TV interface -- so every call goes through here asynchronously.
"""

from __future__ import annotations

import json
from typing import Callable

from gi.repository import Gio, GLib


def run(argv: list[str], on_done: Callable[[str | None], None] | None = None) -> None:
    """Run argv, calling on_done(stdout) -- or on_done(None) on failure."""
    try:
        proc = Gio.Subprocess.new(
            argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE
        )
    except GLib.Error as exc:
        print(f"hdlounge: cannot run {argv[0]}: {exc.message}")
        if on_done:
            on_done(None)
        return

    def finished(p: Gio.Subprocess, result: Gio.AsyncResult) -> None:
        try:
            ok, stdout, _stderr = p.communicate_utf8_finish(result)
        except GLib.Error as exc:
            print(f"hdlounge: {argv[0]} failed: {exc.message}")
            stdout, ok = None, False
        if on_done:
            on_done(stdout if ok else None)

    proc.communicate_utf8_async(None, None, finished)


def run_json(argv: list[str], on_done: Callable[[object | None], None]) -> None:
    """Run argv and parse its stdout as JSON."""

    def parse(stdout: str | None) -> None:
        if not stdout:
            on_done(None)
            return
        try:
            on_done(json.loads(stdout))
        except ValueError:
            on_done(None)

    run(argv, parse)


def fire(argv: list[str]) -> None:
    """Run argv and ignore the result."""
    run(argv, None)
