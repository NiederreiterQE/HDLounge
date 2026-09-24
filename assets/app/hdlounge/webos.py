"""Turning an LG webOS TV off and on over the network.

Most PCs have no HDMI-CEC, so the TV cannot be told to switch off when the box
goes idle. webOS answers a plain WebSocket on port 3000 (LG call the protocol
SSAP), which is enough to power the set down and to pair with it once."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import socket
import struct
import urllib.parse
from typing import Callable

from gi.repository import Gio, GLib, GObject

from . import proc

# Older webOS answers plain WebSocket here. Newer firmware has closed it and
# accepts only TLS on 3001, where it presents a self-signed certificate for
# its own address. Which one a set wants is not worth asking a user, so
# open_client() tries the plain port and falls back to the encrypted one.
PORT = 3000
TLS_PORT = 3001
# 9 is the usual Wake-on-LAN port and 7 the old one. Both are cheap to send.
WOL_PORTS = (9, 7)
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455

SSDP_ADDRESS = "239.255.255.250"
SSDP_PORT = 1900
SSDP_TARGET = "urn:lge-com:service:webos-second-screen:1"

CONNECT_TIMEOUT = 10  # seconds to reach the TV at all
# The remote's Power button asks first and acts second, and a set in standby
# does not answer at all. On a LAN a TV that is on connects in milliseconds,
# so waiting any longer only delays the wake packet.
QUICK_CONNECT_TIMEOUT = 3
HANDSHAKE_TIMEOUT = 10  # ... and to finish the HTTP upgrade
REGISTER_TIMEOUT = 60  # a first pairing waits for someone to press OK
REQUEST_TIMEOUT = 10

# Sent with the pairing request. The TV shows its accept prompt for the app
# named here and remembers the permissions, so this must stay stable: change
# the appId and every TV asks to be paired again. CONTROL_POWER is the only
# permission HDLounge uses; the rest are what webOS expects to see in a manifest.
MANIFEST = {
    "manifestVersion": 1,
    "appVersion": "1.0",
    "signed": {
        "created": "20140509",
        "appId": "com.lge.test",
        "vendorId": "com.lge",
        "localizedAppNames": {"": "HDLounge"},
        "localizedVendorNames": {"": "LG Electronics"},
        "permissions": [
            "TEST_SECURE",
            "CONTROL_INPUT_TEXT",
            "CONTROL_MOUSE_AND_KEYBOARD",
            "READ_INSTALLED_APPS",
            "READ_LGE_SDX",
            "READ_NOTIFICATIONS",
            "SEARCH",
            "WRITE_SETTINGS",
            "WRITE_NOTIFICATION_ALERT",
            "CONTROL_POWER",
            "READ_CURRENT_CHANNEL",
            "READ_RUNNING_APPS",
            "READ_UPDATE_INFO",
            "UPDATE_FROM_REMOTE_APP",
            "READ_LGE_TV_INPUT_EVENTS",
            "READ_TV_CURRENT_TIME",
        ],
        "serial": "2f930e2d2cfe083771f68e4fe7bb07",
    },
    "permissions": [
        "LAUNCH",
        "LAUNCH_WEBAPP",
        "APP_TO_APP",
        "CLOSE",
        "TEST_OPEN",
        "TEST_PROTECTED",
        "CONTROL_AUDIO",
        "CONTROL_DISPLAY",
        "CONTROL_INPUT_JOYSTICK",
        "CONTROL_INPUT_MEDIA_RECORDING",
        "CONTROL_INPUT_MEDIA_PLAYBACK",
        "CONTROL_INPUT_TV",
        "CONTROL_POWER",
        "READ_APP_STATUS",
        "READ_CURRENT_CHANNEL",
        "READ_INPUT_DEVICE_LIST",
        "READ_NETWORK_STATE",
        "READ_RUNNING_APPS",
        "READ_TV_CHANNEL_LIST",
        "WRITE_NOTIFICATION_TOAST",
        "READ_POWER_STATE",
        "READ_COUNTRY_INFO",
        "READ_SETTINGS",
    ],
    "signatures": [],
}


# Framing
# -- Pure functions, no sockets: everything below can be exercised
#    without a TV.
# -------------------------------------------------------


def accept_key(key: str) -> str:
    """The Sec-WebSocket-Accept the server must answer our key with."""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """One complete, masked client frame. Clients must always mask."""
    header = bytearray([0x80 | opcode])  # FIN + opcode
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)  # MASK + length
    elif length < 1 << 16:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)

    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + mask + masked


def unframe(buf: bytes) -> tuple[int, bool, bytes, bytes] | None:
    """Pull one frame off the front of buf.

    Returns (opcode, final, payload, rest), or None while the frame is still
    incomplete -- a read can stop anywhere, including mid-header.
    """
    if len(buf) < 2:
        return None
    final = bool(buf[0] & 0x80)
    opcode = buf[0] & 0x0F
    masked = bool(buf[1] & 0x80)
    length = buf[1] & 0x7F
    offset = 2

    if length == 126:
        if len(buf) < offset + 2:
            return None
        length = struct.unpack(">H", buf[offset : offset + 2])[0]
        offset += 2
    elif length == 127:
        if len(buf) < offset + 8:
            return None
        length = struct.unpack(">Q", buf[offset : offset + 8])[0]
        offset += 8

    mask = b""
    if masked:  # a server should not, but cope
        if len(buf) < offset + 4:
            return None
        mask = buf[offset : offset + 4]
        offset += 4

    if len(buf) < offset + length:
        return None
    payload = buf[offset : offset + length]
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, final, payload, buf[offset + length :]


def magic_packet(mac: str) -> bytes:
    """The Wake-on-LAN payload for a MAC written as aa:bb:cc:dd:ee:ff."""
    clean = mac.replace(":", "").replace("-", "").replace(".", "")
    if len(clean) != 12:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(clean) * 16


# The client
# -------------------------------------------------------


class WebOsClient(GObject.Object):
    """One connection to one TV, driven entirely by GLib callbacks.

    Every phase has its own timeout, because a TV that has just been unplugged
    accepts a TCP connection from a stale ARP entry and then says nothing at
    all. on_error is called at most once, and the connection is closed by the
    time it fires.
    """

    def __init__(self, host: str, client_key: str = "", secure: bool = False) -> None:
        super().__init__()
        self.host = host
        self.client_key = client_key or ""
        self.secure = secure
        self.port = TLS_PORT if secure else PORT
        self._conn: Gio.SocketConnection | None = None
        self._buf = b""
        self._fragments = b""  # a message split across frames
        self._handshake_done = False
        self._next_id = 0
        self._pending: dict[str, Callable[[dict], None]] = {}
        self._on_ready: Callable[[str], None] | None = None
        self._on_error: Callable[[str], None] | None = None
        self._timeout = 0
        self._closed = False
        self._register_on_upgrade = True
        # Nothing answered at the network level: no route, or silence until
        # the timeout. Unlike a refused or reset connection, that says the
        # set itself is off the network, not that this is the wrong port.
        self.unreachable = False

    @property
    def upgraded(self) -> bool:
        """True once the WebSocket handshake has completed on this port."""
        return self._handshake_done

    # Lifecycle
    # -------------------------------------------------------

    def open(
        self,
        on_ready: Callable[[str], None],
        on_error: Callable[[str], None],
        register: bool = True,
        connect_timeout: int = CONNECT_TIMEOUT,
    ) -> None:
        """Connect, upgrade and (unless told not to) register.

        on_ready gets the client key. register=False stops at the WebSocket
        handshake, which is enough to tell whether this is the port the TV
        wants, and asks the TV for nothing -- so no prompt appears on screen.
        """
        self._on_ready = on_ready
        self._on_error = on_error
        self._register_on_upgrade = register

        def silent() -> None:
            self.unreachable = True
            self._fail("the TV did not answer")

        self._arm(connect_timeout, "the TV did not answer", silent)
        client = Gio.SocketClient.new()
        client.set_timeout(connect_timeout)
        if self.secure:
            client.set_tls(True)
            # Both Gio.SocketClient and Gio.SocketConnection have connect()
            # methods of their own, which shadow GObject's signal connect():
            # client.connect("event") tries to open a connection to a host
            # named "event". Signals on socket objects have to be attached
            # through GObject explicitly -- here and below.
            GObject.Object.connect(client, "event", self._on_client_event)
        client.connect_to_host_async(self.host, self.port, None, self._on_connected)

    def _on_client_event(
        self,
        _client: Gio.SocketClient,
        event: Gio.SocketClientEvent,
        _connectable: Gio.SocketConnectable,
        connection: Gio.IOStream | None,
    ) -> None:
        # The TV signs its own certificate for a private address, so no CA
        # could ever vouch for it and the handshake would be rejected. On a
        # set we are about to hand a pairing key to anyway, accept it.
        if event == Gio.SocketClientEvent.TLS_HANDSHAKING and connection is not None:
            GObject.Object.connect(connection, "accept-certificate", lambda *_a: True)

    def close(self) -> None:
        self._disarm()
        self._pending.clear()
        if self._conn is not None:
            try:
                self._conn.close(None)
            except GLib.Error:
                pass
            self._conn = None
        self._closed = True

    def _fail(self, message: str) -> None:
        if self._closed:
            return
        on_error = self._on_error
        self._on_error = self._on_ready = None
        self.close()
        if on_error is not None:
            on_error(message)

    def _arm(
        self, seconds: int, message: str, on_expired: Callable[[], None] | None = None
    ) -> None:
        self._disarm()

        def expired() -> bool:
            self._timeout = 0
            if on_expired is not None:
                on_expired()
            else:
                self._fail(message)
            return GLib.SOURCE_REMOVE

        self._timeout = GLib.timeout_add_seconds(seconds, expired)

    def _disarm(self) -> None:
        if self._timeout:
            GLib.source_remove(self._timeout)
            self._timeout = 0

    # Handshake
    # -------------------------------------------------------

    def _on_connected(self, client: Gio.SocketClient, result: Gio.AsyncResult) -> None:
        try:
            self._conn = client.connect_to_host_finish(result)
        except GLib.Error as exc:
            self.unreachable = any(
                exc.matches(Gio.io_error_quark(), code)
                for code in (
                    Gio.IOErrorEnum.TIMED_OUT,
                    Gio.IOErrorEnum.HOST_UNREACHABLE,
                    Gio.IOErrorEnum.NETWORK_UNREACHABLE,
                )
            )
            self._fail(exc.message)
            return

        self._key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {self._key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        ).encode("ascii")

        self._arm(HANDSHAKE_TIMEOUT, "the TV did not complete the handshake")
        self._write(request)
        self._read()

    def _write(self, data: bytes) -> None:
        if self._conn is None:
            return
        stream = self._conn.get_output_stream()

        def written(s: Gio.OutputStream, result: Gio.AsyncResult) -> None:
            try:
                s.write_all_finish(result)
            except GLib.Error as exc:
                self._fail(exc.message)

        stream.write_all_async(data, GLib.PRIORITY_DEFAULT, None, written)

    def _read(self) -> None:
        if self._conn is None:
            return
        stream = self._conn.get_input_stream()

        def read(s: Gio.InputStream, result: Gio.AsyncResult) -> None:
            try:
                data = s.read_bytes_finish(result).get_data()
            except GLib.Error as exc:
                self._fail(exc.message)
                return
            if not data:
                self._fail("the TV closed the connection")
                return
            self._buf += data
            if self._handshake_done:
                self._drain_frames()
            else:
                self._drain_headers()
            if not self._closed:
                self._read()

        stream.read_bytes_async(4096, GLib.PRIORITY_DEFAULT, None, read)

    def _drain_headers(self) -> None:
        end = self._buf.find(b"\r\n\r\n")
        if end < 0:
            return
        head = self._buf[:end].decode("latin-1")
        self._buf = self._buf[end + 4 :]

        status = head.split("\r\n", 1)[0]
        if "101" not in status:
            self._fail(f"the TV refused the connection ({status})")
            return
        expected = accept_key(self._key).lower()
        got = ""
        for line in head.split("\r\n")[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                got = value.strip().lower()
        if got != expected:
            self._fail("the TV's handshake did not match")
            return

        self._handshake_done = True
        if not self._register_on_upgrade:
            self._disarm()
            on_ready, self._on_ready = self._on_ready, None
            if on_ready is not None:
                on_ready("")
            return
        self._register()
        self._drain_frames()

    # Messages
    # -------------------------------------------------------

    def _send(self, message: dict) -> None:
        self._write(frame(json.dumps(message).encode("utf-8")))

    def _register(self) -> None:
        payload = {
            "forcePairing": False,
            "pairingType": "PROMPT",
            "manifest": MANIFEST,
        }
        if self.client_key:
            payload["client-key"] = self.client_key
            self._arm(REQUEST_TIMEOUT, "the TV did not accept the pairing key")
        else:
            # Nothing stored yet: the TV shows its accept prompt and says
            # nothing at all until someone presses OK on it.
            self._arm(REGISTER_TIMEOUT, "nobody accepted the prompt on the TV")
        self._send({"type": "register", "id": "register_0", "payload": payload})

    def _drain_frames(self) -> None:
        while not self._closed:
            result = unframe(self._buf)
            if result is None:
                return
            opcode, final, payload, self._buf = result
            if opcode == 0x8:  # close
                self._fail("the TV closed the connection")
                return
            if opcode == 0x9:  # ping -> pong
                self._write(frame(payload, 0xA))
                continue
            if opcode not in (0x0, 0x1, 0x2):
                continue

            # A reply may arrive split across frames: everything until the
            # one with FIN set is half a message, not a malformed one.
            self._fragments += payload
            if not final:
                continue
            message, self._fragments = self._fragments, b""
            try:
                self._dispatch(json.loads(message.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                continue

    def _dispatch(self, message: dict) -> None:
        kind = message.get("type", "")
        if kind == "registered":
            self._disarm()
            self.client_key = message.get("payload", {}).get(
                "client-key", self.client_key
            )
            on_ready, self._on_ready = self._on_ready, None
            if on_ready is not None:
                on_ready(self.client_key)
            return
        if kind == "error":
            # "registered" never arrives after an error on the register id.
            if message.get("id") == "register_0":
                self._fail(message.get("error", "the TV refused the pairing"))
                return
            # A request the firmware does not know is answered with an error
            # rather than a response; hand it over as a failed reply instead
            # of leaving the caller to wait out the timeout.
            handler = self._pending.pop(message.get("id", ""), None)
            if handler is not None:
                self._disarm()
                handler({"returnValue": False, "errorText": message.get("error", "")})
            return
        if kind == "response":
            handler = self._pending.pop(message.get("id", ""), None)
            if handler is not None:
                self._disarm()
                handler(message.get("payload", {}))

    def request(
        self,
        uri: str,
        payload: dict | None = None,
        on_reply: Callable[[dict], None] | None = None,
    ) -> None:
        self._next_id += 1
        message_id = f"hdlounge_{self._next_id}"
        if on_reply is not None:
            self._pending[message_id] = on_reply
            self._arm(REQUEST_TIMEOUT, "the TV did not answer")
        message = {"type": "request", "id": message_id, "uri": uri}
        if payload is not None:
            message["payload"] = payload
        self._send(message)


# Actions
# -------------------------------------------------------


def open_client(
    host: str,
    client_key: str,
    on_ready: Callable[["WebOsClient", str], None],
    on_error: Callable[[str, bool], None],
    prefer_secure: bool = False,
    connect_timeout: int = CONNECT_TIMEOUT,
) -> None:
    """Connect however this TV wants to be talked to.

    Tries one port and then the other, because the symptom of guessing wrong
    is the TV accepting the connection and then resetting it, which is
    indistinguishable from a network fault until the retry succeeds. The
    plain port goes first unless `prefer_secure` says this set is known to
    want TLS; client.secure afterwards says which one answered.

    on_ready(client, client_key). on_error(message, upgraded): `upgraded` is
    True when the TV was there and talking, and something went wrong after.
    """

    def fail_over(first: "WebOsClient", first_error: str) -> None:
        # Once a port has completed the handshake it is the right port, and
        # whatever went wrong after that -- a declined prompt, a TV hanging
        # up once turnOff has landed -- would only happen again on the other
        # one, with the accept prompt shown a second time. And a set that did
        # not answer at all is off the network, whichever port is asked.
        if first.upgraded or first.unreachable:
            on_error(first_error, first.upgraded)
            return

        second = WebOsClient(host, client_key, secure=not first.secure)

        def second_failed(error: str) -> None:
            if second.upgraded:
                on_error(error, True)
            else:
                on_error(f"{error} (and on port {first.port}: {first_error})", False)

        second.open(
            lambda key: on_ready(second, key),
            second_failed,
            connect_timeout=connect_timeout,
        )

    client = WebOsClient(host, client_key, secure=prefer_secure)
    client.open(
        lambda key: on_ready(client, key),
        lambda error: fail_over(client, error),
        connect_timeout=connect_timeout,
    )


# What became of a request to switch the TV off.
OFF = "off"  # turnOff is on its way
IN_STANDBY = "standby"  # the TV answered, and says it is not on
UNREACHABLE = "unreachable"  # nothing answered on either port
FAILED = "failed"  # the TV was there, then refused or went quiet


def turn_off(
    host: str,
    client_key: str,
    on_done: Callable[[str, str, bool | None], None],
    prefer_secure: bool = False,
    only_if_on: bool = False,
    connect_timeout: int = CONNECT_TIMEOUT,
) -> None:
    """Power the TV down. on_done(outcome, message, secure), once.

    `outcome` is one of OFF, IN_STANDBY, UNREACHABLE or FAILED. `secure` is
    the port that answered, worth remembering so the next call asks it
    first, or None if none did.

    With only_if_on the TV is asked for its power state first, and a set in
    standby is left alone: that is what lets one Power button mean on *or*
    off, whoever switched the TV off last.
    """
    reported = [False]

    def report(outcome: str, message: str, secure: bool | None) -> None:
        if reported[0]:
            return
        reported[0] = True
        on_done(outcome, message, secure)

    def ready(client: "WebOsClient", _key: str) -> None:
        def switch_off() -> None:
            # turnOff answers before the panel goes dark, but the TV may also
            # just drop the socket -- either way the command has landed, so
            # report success as soon as it is on the wire and close on a
            # short timer.
            def hang_up() -> bool:
                client.close()
                return GLib.SOURCE_REMOVE

            client.request("ssap://system/turnOff")
            GLib.timeout_add_seconds(2, hang_up)
            report(OFF, "TV off", client.secure)

        if not only_if_on:
            switch_off()
            return

        def got_state(payload: dict) -> None:
            # "Active" is on, and "Screen Off" is on with the panel dark.
            # Anything else -- "Active Standby", "Suspend" -- is a set that
            # only kept its network up. Firmware that does not know the call
            # is on, or it would not have answered at all.
            state = payload.get("state")
            if payload.get("returnValue") is False or state in (
                None,
                "Active",
                "Screen Off",
                "Screen Saver",
            ):
                switch_off()
                return
            client.close()
            report(IN_STANDBY, f"The TV is in standby ({state})", client.secure)

        client.request(
            "ssap://com.webos.service.tvpower/power/getPowerState", None, got_state
        )

    def failed(message: str, upgraded: bool) -> None:
        report(FAILED if upgraded else UNREACHABLE, message, None)

    open_client(host, client_key, ready, failed, prefer_secure, connect_timeout)


def _same_subnet(cidr: str, host: str) -> bool:
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_interface(cidr).network
    except ValueError:
        return False


def broadcasts_from(text: str, host: str = "") -> list[tuple[str, str]]:
    """[(broadcast address, address to send it from)] out of `ip addr` output.

    A magic packet is a broadcast, and a broadcast only reaches the network
    it was sent on. 255.255.255.255 leaves by whichever interface holds the
    default route, which on a box with a second NIC, a bridge or a container
    network is quite often not the one the TV is on. So: send on the
    interface whose own subnet contains the TV, and if none does, on all of
    them.
    """
    targets = []
    for line in text.splitlines():
        words = line.split()
        if "inet" not in words or "brd" not in words:
            continue
        cidr = words[words.index("inet") + 1]
        broadcast = words[words.index("brd") + 1]
        source = cidr.split("/")[0]
        if source.startswith("127."):
            continue
        targets.append((broadcast, source, cidr))

    if host:
        on_its_subnet = [t for t in targets if _same_subnet(t[2], host)]
        if on_its_subnet:
            targets = on_its_subnet
    return [(broadcast, source) for broadcast, source, _cidr in targets]


def broadcast_targets(
    host: str, on_done: Callable[[list[tuple[str, str]]], None]
) -> None:
    """Where a wake packet for `host` should be sent."""
    proc.run(
        ["ip", "-o", "-4", "addr", "show"],
        lambda stdout: on_done(broadcasts_from(stdout or "", host)),
    )


def wake(
    mac: str, host: str = "", on_done: Callable[[bool, str], None] | None = None
) -> None:
    """Send Wake-on-LAN to the TV. Harmless if it is already on.

    `host` is the TV's address, used only to pick the interface to broadcast
    on. The packet goes to the subnet broadcast and to 255.255.255.255, on
    both WoL ports, three times each: it is a few hundred bytes of UDP, and
    the cost of one going astray is a TV that stays dark.
    """

    def report(ok: bool, message: str) -> None:
        if on_done:
            on_done(ok, message)

    try:
        packet = magic_packet(mac)
    except ValueError as exc:
        report(False, str(exc))
        return

    def send(targets: list[tuple[str, str]]) -> None:
        # The TV's own address as well as the broadcasts: while the kernel
        # still has an ARP entry for it -- and it will, because HDLounge was
        # talking to the TV moments before it switched it off -- a unicast
        # packet is framed straight to the right MAC and does not depend on
        # broadcast reaching the set at all.
        if host:
            targets = targets + [(host, "")]
        sent, last_error = [], ""
        for broadcast, source in targets + [("255.255.255.255", "")]:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                if source:
                    # Leave by this interface, whatever the routing table
                    # would otherwise have chosen.
                    sock.bind((source, 0))
                for port in WOL_PORTS:
                    for _ in range(3):
                        sock.sendto(packet, (broadcast, port))
                sock.close()
                sent.append(broadcast)
            except OSError as exc:
                last_error = str(exc)
        if not sent:
            report(False, last_error or "could not send the wake packet")
            return
        report(True, "TV on")

    broadcast_targets(host, send)


def info(host: str, client_key: str, on_done: Callable[[bool, object], None]) -> None:
    """Ask the TV about itself. on_done(ok, {"network":..., "software":...}).

    Whether the set is on Ethernet or Wi-Fi decides whether it can be woken
    at all, and the TV is the only thing that knows for certain.
    """
    reported = [False]

    def report(ok: bool, value: object) -> None:
        if reported[0]:
            return
        reported[0] = True
        on_done(ok, value)

    def ready(client: "WebOsClient", _key: str) -> None:
        details: dict = {}

        def got_software(payload: dict) -> None:
            details["software"] = payload
            client.close()
            report(True, details)

        def got_network(payload: dict) -> None:
            details["network"] = payload
            # One at a time: the client keeps a single timeout, so two
            # requests in flight would disarm each other's.
            client.request(
                "ssap://com.webos.service.update/getCurrentSWInformation",
                None,
                got_software,
            )

        client.request(
            "ssap://com.webos.service.connectionmanager/getStatus", None, got_network
        )

    open_client(
        host, client_key, ready, lambda message, _upgraded: report(False, message)
    )


def pair(host: str, on_done: Callable[[bool, str, str], None]) -> None:
    """Pair with the TV at host. on_done(ok, client_key_or_error, mac), once."""
    reported = [False]

    def report(ok: bool, key_or_error: str, mac: str) -> None:
        if reported[0]:
            return
        reported[0] = True
        on_done(ok, key_or_error, mac)

    def ready(client: "WebOsClient", key: str) -> None:
        def with_mac(mac: str) -> None:
            client.close()
            report(True, key, mac)

        mac_for(host, with_mac)

    open_client(host, "", ready, lambda message, _upgraded: report(False, message, ""))


def mac_for(host: str, on_done: Callable[[str], None]) -> None:
    """The TV's MAC, from the kernel's neighbour table.

    Called straight after a successful connection, so the entry is fresh.
    """

    def parsed(stdout: str | None) -> None:
        words = (stdout or "").split()
        for i, word in enumerate(words):
            if word == "lladdr" and i + 1 < len(words):
                on_done(words[i + 1])
                return
        on_done("")

    proc.run(["ip", "neigh", "show", host], parsed)


def ssdp_answer(data: bytes) -> str | None:
    """The TV's name from an SSDP reply, "LG TV" if it gives none, or None if
    the reply is not from a webOS TV at all.

    LG sets put their on-screen name, percent-encoded, in a header of their
    own; that is what tells two sets in one house apart.
    """
    text = data.decode("latin-1")
    if "lge-com" not in text.lower() and "webos" not in text.lower():
        return None
    for line in text.split("\r\n")[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "dlnadevicename.lge.com" and value.strip():
            return urllib.parse.unquote(value.strip())
    return "LG TV"


def neighbours_from(text: str) -> list[str]:
    """IPv4 addresses the kernel has recently seen, out of `ip neigh` output."""
    hosts = []
    for line in text.splitlines():
        words = line.split()
        if (
            len(words) < 2
            or "lladdr" not in words
            or words[-1] in ("FAILED", "INCOMPLETE")
        ):
            continue
        try:
            if ipaddress.ip_address(words[0]).version == 4:
                hosts.append(words[0])
        except ValueError:
            continue
    return hosts


def discover(
    on_done: Callable[[list[tuple[str, str]]], None], seconds: int = 3
) -> None:
    """Find webOS TVs with an SSDP search. on_done([(host, name)]).

    The search is a UDP multicast, so it goes out three times: on Wi-Fi a
    single lost packet would otherwise read as "no TV". Some routers do not
    pass multicast at all, so when nothing answers, the hosts this box has
    recently talked to are asked directly whether they speak webOS.
    """
    search = (
        f"M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDRESS}:{SSDP_PORT}\r\n"
        f'MAN: "ssdp:discover"\r\n'
        f"MX: {seconds}\r\n"
        f"ST: {SSDP_TARGET}\r\n"
        f"\r\n"
    ).encode("ascii")

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setblocking(False)
        sock.sendto(search, (SSDP_ADDRESS, SSDP_PORT))
    except OSError as exc:
        print(f"hdlounge: could not search for TVs ({exc})")
        probe_neighbours(on_done)
        return

    found: dict[str, str] = {}
    closed = [False]

    def resend() -> bool:
        if not closed[0]:
            try:
                sock.sendto(search, (SSDP_ADDRESS, SSDP_PORT))
            except OSError:
                pass
        return GLib.SOURCE_REMOVE

    for delay_ms in (400, 900):
        GLib.timeout_add(delay_ms, resend)

    def readable(*_args: object) -> bool:
        try:
            data, address = sock.recvfrom(2048)
        except OSError:
            return GLib.SOURCE_CONTINUE
        name = ssdp_answer(data)
        if name is not None:
            found.setdefault(address[0], name)
        return GLib.SOURCE_CONTINUE

    watch = GLib.io_add_watch(
        GLib.IOChannel.unix_new(sock.fileno()), GLib.IO_IN, readable
    )

    def finished() -> bool:
        GLib.source_remove(watch)
        closed[0] = True
        sock.close()
        if found:
            on_done(sorted(found.items()))
        else:
            probe_neighbours(on_done)
        return GLib.SOURCE_REMOVE

    GLib.timeout_add_seconds(seconds + 1, finished)


def probe_neighbours(on_done: Callable[[list[tuple[str, str]]], None]) -> None:
    """Ask every recently seen host whether it is a webOS TV.

    Stops at the WebSocket handshake, as `probe` does, so nothing appears on
    any screen. Both ports, every host at once, with a short timeout: a home
    network's neighbour table is a handful of entries.
    """

    def probe_all(stdout: str | None) -> None:
        hosts = neighbours_from(stdout or "")
        if not hosts:
            on_done([])
            return
        found: dict[str, str] = {}
        waiting = [len(hosts) * 2]

        def settled() -> None:
            waiting[0] -= 1
            if waiting[0] == 0:
                on_done(sorted(found.items()))

        for host in hosts:
            for secure in (False, True):
                client = WebOsClient(host, secure=secure)

                def answered(_key: str, client: WebOsClient = client) -> None:
                    found.setdefault(client.host, "LG TV")
                    client.close()
                    settled()

                client.open(
                    answered,
                    lambda _message: settled(),
                    register=False,
                    connect_timeout=QUICK_CONNECT_TIMEOUT,
                )

    proc.run(["ip", "-4", "neigh", "show"], probe_all)


# The CLI
# -- Setting a TV up, and diagnosing one, without going near the launcher.
# -------------------------------------------------------
#
#   python3 -m hdlounge.webos discover
#   python3 -m hdlounge.webos probe 192.168.1.42        which port will it talk on?
#   python3 -m hdlounge.webos info 192.168.1.42 <key>   wired or wi-fi? which MAC?
#   python3 -m hdlounge.webos pair 192.168.1.42
#   python3 -m hdlounge.webos off 192.168.1.42 <client-key>
#   python3 -m hdlounge.webos on aa:bb:cc:dd:ee:ff [tv-ip]


def _main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    loop = GLib.MainLoop()
    status = [1]

    def done(ok: bool, message: str) -> None:
        print(("   " if ok else "!! ") + message)
        status[0] = 0 if ok else 1
        loop.quit()

    if command == "discover":

        def listed(tvs: list[tuple[str, str]]) -> None:
            for host, name in tvs:
                print(f"{host}\t{name}")
            if not tvs:
                print("No webOS TV answered.")
            status[0] = 0 if tvs else 1
            loop.quit()

        discover(listed)
    elif command == "probe" and len(argv) > 2:
        # Both ports, one after the other, with whatever each one says. This
        # is the first thing to run when a TV will not talk: "connection
        # reset by peer" on 3000 alone means the set wants TLS on 3001.
        # It stops at the handshake, so nothing appears on the TV screen.
        host = argv[2]
        results = []

        def finish() -> None:
            print()
            if any(ok for _port, ok, _m in results):
                port = next(p for p, ok, _m in results if ok)
                print(
                    f"Use port {port}. HDLounge finds this by itself; if pairing"
                    " still fails, the"
                )
                print("prompt on the TV was probably not accepted in time.")
            else:
                print(
                    "Neither port would talk. Check that the TV is switched"
                    " on (not in"
                )
                print(
                    "standby), on this network, and that Mobile TV On is"
                    " enabled under"
                )
                print("Settings > General > Devices > TV Management.")
            status[0] = 0 if any(ok for _p, ok, _m in results) else 1
            loop.quit()

        def attempt(secure: bool) -> None:
            port = TLS_PORT if secure else PORT
            kind = "TLS" if secure else "plain"
            client = WebOsClient(host, secure=secure)

            def next_step() -> None:
                if secure:  # both ports tried
                    finish()
                else:
                    attempt(True)

            def ok(_key: str) -> None:
                print(f"   port {port} ({kind}): the TV answered")
                results.append((port, True, ""))
                client.close()
                next_step()

            def failed(message: str) -> None:
                print(f"!! port {port} ({kind}): {message}")
                results.append((port, False, message))
                next_step()

            print(f"   trying port {port} ({kind})...")
            client.open(ok, failed, register=False)

        attempt(False)
    elif command == "info" and len(argv) > 2:

        def shown(ok: bool, value: object) -> None:
            if not ok:
                print(f"!! {value}")
                status[0] = 1
                loop.quit()
                return
            network = value.get("network", {})
            wired = network.get("wired", {})
            wifi = network.get("wifi", {})
            software = value.get("software", {})
            print(f"   model:     {software.get('product_name', '?')}")
            print(
                f"   wired:     {wired.get('state', '?')}"
                f" {wired.get('ipAddress', '')}"
            )
            print(
                f"   wi-fi:     {wifi.get('state', '?')}"
                f" {wifi.get('ipAddress', '')} {wifi.get('ssid', '')}"
            )
            print(f"   wired MAC: {software.get('device_id', '?')}")
            print()
            if wired.get("state") == "connected":
                print(
                    "On Ethernet: waking should work once TV On With Mobile"
                    " is enabled."
                )
            elif wifi.get("state") == "connected":
                print(
                    "On Wi-Fi. Waking over the network is unreliable on"
                    " Wi-Fi -- the TV has"
                )
                print(
                    "to keep its radio associated in standby. Use Ethernet"
                    " if you can; if you"
                )
                print(
                    "cannot, enable Quick Start+ as well, and note that the"
                    " Wi-Fi MAC is a"
                )
                print("different address from the wired one printed above.")
            status[0] = 0
            loop.quit()

        info(argv[2], argv[3] if len(argv) > 3 else "", shown)
    elif command == "pair" and len(argv) > 2:
        print(f"Connecting to {argv[2]} -- press OK on the TV if it asks.")

        def paired(ok: bool, key: str, mac: str) -> None:
            if ok:
                print(f"client key: {key}\nMAC: {mac or '(not found)'}")
            else:
                print(f"!! {key}")
            status[0] = 0 if ok else 1
            loop.quit()

        pair(argv[2], paired)
    elif command == "off" and len(argv) > 2:
        turn_off(
            argv[2],
            argv[3] if len(argv) > 3 else "",
            lambda outcome, message, _secure: done(outcome == OFF, message),
        )
    elif command == "on" and len(argv) > 2:
        # The TV's address is optional but worth giving: it decides which
        # interface the broadcast leaves by.
        tv_host = argv[3] if len(argv) > 3 else ""

        def show(targets: list[tuple[str, str]]) -> None:
            if not targets:
                print("   no broadcast address found; sending to 255.255.255.255 only")
            for broadcast, source in targets:
                print(
                    f"   sending from {source} to {broadcast}"
                    f" on ports {WOL_PORTS[0]} and {WOL_PORTS[1]}"
                )
            wake(argv[2], tv_host, done)

        broadcast_targets(tv_host, show)
    else:
        print(__doc__.strip().splitlines()[0])
        print(
            "usage: python3 -m hdlounge.webos discover | probe <ip>"
            " | info <ip> [key]\n"
            "       | pair <ip> | off <ip> [key] | on <mac> [tv-ip]"
        )
        return 2

    loop.run()
    return status[0]


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
