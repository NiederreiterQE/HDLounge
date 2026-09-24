"""Volume, mute and output-device control via wpctl / pactl."""

from __future__ import annotations

import re

from gi.repository import GObject

from . import proc

SINK = "@DEFAULT_AUDIO_SINK@"
_VOLUME_RE = re.compile(r"Volume:\s*([0-9.]+)(\s*\[MUTED\])?")


class Audio(GObject.Object):
    """Default-sink volume plus a list of available outputs."""

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "sinks-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self.volume = 0.0  # 0..1
        self.muted = False
        self.sinks: list[dict] = []  # [{"name","description","default"}]
        # One wpctl write runs at a time; presses that arrive meanwhile are
        # gathered into the next one. Separate processes would otherwise
        # race each other, and a read landing between them would snap the
        # slider back.
        self._applying = False
        self._pending_level: float | None = None  # an absolute level to set
        self._pending_step = 0.0  # or presses to add up
        self.refresh()

    # Reading
    # -------------------------------------------------------

    def refresh(self) -> None:
        self._refresh_volume()
        self.refresh_sinks()

    def _refresh_volume(self) -> None:
        proc.run(["wpctl", "get-volume", SINK], self._on_volume)

    def _on_volume(self, stdout: str | None) -> None:
        if not stdout or self._writing:
            return
        match = _VOLUME_RE.search(stdout)
        if not match:
            return
        try:
            volume = float(match.group(1))
        except ValueError:
            return
        muted = match.group(2) is not None

        if (volume, muted) != (self.volume, self.muted):
            self.volume, self.muted = volume, muted
            self.emit("changed")

    def refresh_sinks(self) -> None:
        proc.run(["pactl", "get-default-sink"], self._on_default_sink)

    def _on_default_sink(self, stdout: str | None) -> None:
        default = (stdout or "").strip()

        def got_sinks(data: object | None) -> None:
            if not isinstance(data, list):
                return
            sinks = [
                {
                    "name": s.get("name", ""),
                    "description": s.get("description") or s.get("name", ""),
                    "default": s.get("name") == default,
                }
                for s in data
                if s.get("name")
            ]
            if sinks != self.sinks:
                self.sinks = sinks
                self.emit("sinks-changed")

        proc.run_json(["pactl", "-f", "json", "list", "sinks"], got_sinks)

    # Writing
    # -------------------------------------------------------

    @property
    def _writing(self) -> bool:
        return (
            self._applying
            or self._pending_level is not None
            or bool(self._pending_step)
        )

    def set_volume(self, value: float) -> None:
        value = min(1.0, max(0.0, value))
        self.volume = value
        self._pending_level, self._pending_step = value, 0.0
        self.emit("changed")
        self._flush()

    def step(self, delta: float) -> None:
        """Volume up or down by `delta`, relative to wherever PipeWire is.

        Up stops at 100%; a sink some other app has already pushed past that
        is left where it is rather than pulled down to meet the limit.
        """
        if delta > 0 and self.volume >= 1.0:
            return
        if self._pending_level is not None:
            self._pending_level = min(1.0, max(0.0, self._pending_level + delta))
        else:
            self._pending_step += delta
        self.volume = min(max(1.0, self.volume), max(0.0, self.volume + delta))
        self.emit("changed")
        self._flush()

    def _flush(self) -> None:
        if self._applying:
            return
        step = round(self._pending_step * 100)
        if self._pending_level is not None:
            argv = ["wpctl", "set-volume", SINK, f"{self._pending_level:.2f}"]
        elif step > 0:
            argv = ["wpctl", "set-volume", "-l", "1.0", SINK, f"{step}%+"]
        elif step < 0:
            argv = ["wpctl", "set-volume", SINK, f"{-step}%-"]
        else:
            self._pending_step = 0.0
            return
        self._pending_level, self._pending_step = None, 0.0
        self._applying = True

        def finished(_stdout: str | None) -> None:
            self._applying = False
            if self._writing:
                self._flush()
            else:
                self._refresh_volume()  # settle on what PipeWire has

        proc.run(argv, finished)

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        proc.fire(["wpctl", "set-mute", SINK, "toggle"])
        self.emit("changed")

    def set_default_sink(self, name: str) -> None:
        def then(_stdout: str | None) -> None:
            self.refresh()

        proc.run(["pactl", "set-default-sink", name], then)

    # Pretty
    # -------------------------------------------------------

    @property
    def label(self) -> str:
        if self.muted:
            return "Muted"
        return f"{round(self.volume * 100)}%"

    @property
    def icon_name(self) -> str:
        if self.muted or self.volume <= 0.0:
            return "audio-volume-muted-symbolic"
        if self.volume < 0.34:
            return "audio-volume-low-symbolic"
        if self.volume < 0.67:
            return "audio-volume-medium-symbolic"
        return "audio-volume-high-symbolic"
