#!/usr/bin/env python3
"""The webOS protocol, without a TV: frames in, frames out.

HDLounge speaks WebSocket to the TV by hand rather than through a PyPI library,
so the framing is ours to get right. Everything checked here is either a pure
function or a stubbed-out client -- no sockets, no network, no TV.

    python3 test-support/test_webos_framing.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from hdlounge import webos as w  # noqa: E402

FAILURES = []


def check(name, got, want):
    if got == want:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}\n        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


def raises(name, fn, *args):
    try:
        fn(*args)
    except ValueError:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}\n        no ValueError raised")
        FAILURES.append(name)


# Handshake
# -------------------------------------------------------

# RFC 6455 section 1.3's worked example.
check("handshake accept matches the RFC's example",
      w.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

# Framing
# -------------------------------------------------------

short = w.frame(b'{"type":"request"}')
check("a client frame is FIN + text", short[0], 0x81)
check("a client frame is always masked", bool(short[1] & 0x80), True)
check("short payloads use the inline length", short[1] & 0x7F, 18)
check("short frame round-trips", w.unframe(short)[2], b'{"type":"request"}')

# The register payload is ~1 KB, so the 16-bit length path is not optional:
# a TV given a truncated register message simply never answers.
register = json.dumps({"type": "register", "id": "register_0",
                       "payload": {"manifest": w.MANIFEST}}).encode("utf-8")
check("the register payload needs the extended length path", len(register) > 125, True)
check("payloads over 125 bytes use the 16-bit length",
      w.frame(register)[1] & 0x7F, 126)
check("extended frame round-trips", w.unframe(w.frame(register))[2], register)

huge = b"y" * 70000
check("payloads over 64K use the 64-bit length", w.frame(huge)[1] & 0x7F, 127)
check("64-bit frame round-trips", w.unframe(w.frame(huge))[2], huge)

# A read can stop anywhere, including mid-header. Every prefix must be
# reported as "not yet", never mis-parsed as a short frame.
partial = w.frame(b"z" * 300)
check("every truncation of a frame is incomplete",
      [i for i in range(len(partial)) if w.unframe(partial[:i]) is not None], [])
check("the whole frame parses once it has all arrived",
      w.unframe(partial)[2], b"z" * 300)

# One read can carry several replies.
pair = w.frame(b"one") + w.frame(b"two")
opcode, final, payload, rest = w.unframe(pair)
check("the first of two frames parses", payload, b"one")
check("the second frame is left for the next pass", w.unframe(rest)[2], b"two")
check("nothing is left after the second", w.unframe(rest)[3], b"")

# Servers do not mask; a masked bit we did not expect must still decode.
check("an unmasked server frame decodes",
      w.unframe(bytes([0x81, 3]) + b"abc"), (0x1, True, b"abc", b""))
check("a ping is reported as opcode 9", w.unframe(bytes([0x89, 0]))[0], 0x9)
check("a close is reported as opcode 8", w.unframe(bytes([0x88, 0]))[0], 0x8)
check("a continuation frame is not marked final",
      w.unframe(bytes([0x01, 1]) + b"a")[1], False)

# wake-on-lan
# -------------------------------------------------------

packet = w.magic_packet("aa:bb:cc:dd:ee:ff")
check("a magic packet is 102 bytes", len(packet), 102)
check("it opens with six 0xff", packet[:6], b"\xff" * 6)
check("then sixteen copies of the MAC",
      packet[6:], bytes.fromhex("aabbccddeeff") * 16)
check("dashes and dots are accepted too",
      (w.magic_packet("AA-BB-CC-DD-EE-FF"), w.magic_packet("aabb.ccdd.eeff")),
      (packet, packet))
raises("an empty MAC is refused", w.magic_packet, "")
raises("a short MAC is refused", w.magic_packet, "aa:bb:cc")
raises("a non-MAC is refused", w.magic_packet, "the living room")

# Reassembling a reply
# -------------------------------------------------------

# A reply can arrive split across frames. Everything up to the frame with FIN
# set is half a message, and parsing it early would drop the reply entirely.


def drained(buf):
    """Feed raw bytes to a client that is past its handshake."""
    client = w.WebOsClient("10.0.0.1")
    seen = []
    client._dispatch = seen.append
    client._handshake_done = True
    client._buf = buf
    client._drain_frames()
    return seen, client


whole = w.frame(b'{"type":"registered","payload":{"client-key":"k"}}')
check("a reply in one frame is dispatched", drained(whole)[0],
      [{"type": "registered", "payload": {"client-key": "k"}}])

head, mid, tail = b'{"type":"resp', b'onse","id"', b':"hdlounge_1"}'
split = (bytes([0x01, len(head)]) + head          # text, not final
         + bytes([0x00, len(mid)]) + mid          # continuation
         + bytes([0x80, len(tail)]) + tail)       # continuation, final
seen, client = drained(split)
check("a reply split across three frames is reassembled", seen,
      [{"type": "response", "id": "hdlounge_1"}])
check("  and nothing is left half-read", client._fragments, b"")

tail = b'a":1}'
pinged = (bytes([0x01, 2]) + b'{"'
          + bytes([0x89, 0])                      # a ping arrives mid-message
          + bytes([0x80, len(tail)]) + tail)
check("a ping in the middle does not corrupt the message",
      drained(pinged)[0], [{"a": 1}])

# Which port, though
# -------------------------------------------------------

# Older sets answer plain WebSocket on 3000; newer ones have closed it and
# want TLS on 3001, where the failure is the TV accepting the connection and
# then resetting it. open_client() tries both so nobody has to know which.


class FakeClient:
    """Stands in for WebOsClient: answers however the script says.

    A port's script is True (it pairs), a message (refused before the
    handshake), ("upgraded", message) (talked, then failed) or ("silent",
    message) (nothing answered at all).
    """

    log = []
    script = {}
    requests = []
    power_state = {"returnValue": True, "state": "Active"}

    def __init__(self, host, client_key="", secure=False):
        self.host, self.client_key, self.secure = host, client_key, secure
        self.port = w.TLS_PORT if secure else w.PORT
        self.upgraded = self.unreachable = False
        FakeClient.log.append(self.port)

    def open(self, on_ready, on_error, register=True, connect_timeout=None):
        outcome = FakeClient.script[self.secure]
        if outcome is True:
            self.upgraded = True
            on_ready("key-from-%d" % self.port)
        elif isinstance(outcome, tuple):
            kind, message = outcome
            self.upgraded = kind == "upgraded"
            self.unreachable = kind == "silent"
            on_error(message)
        else:
            on_error(outcome)

    def request(self, uri, payload=None, on_reply=None):
        FakeClient.requests.append(uri)
        if uri.endswith("getPowerState") and on_reply is not None:
            on_reply(FakeClient.power_state)

    def close(self):
        pass


def attempt(plain, tls, prefer_secure=False):
    """Run open_client against a scripted pair of outcomes."""
    FakeClient.log, FakeClient.script = [], {False: plain, True: tls}
    real, w.WebOsClient = w.WebOsClient, FakeClient
    try:
        got = []
        w.open_client("10.0.0.9", "",
                      lambda client, key: got.append(("ready", client.port, key)),
                      lambda message, upgraded: got.append(
                          ("error", message, upgraded)),
                      prefer_secure=prefer_secure)
        return got, list(FakeClient.log)
    finally:
        w.WebOsClient = real


got, tried = attempt(plain=True, tls="not reached")
check("a TV that answers on 3000 is used straight away", got,
      [("ready", 3000, "key-from-3000")])
check("  and the TLS port is never opened", tried, [3000])

got, tried = attempt(plain="Connection reset by peer", tls=True)
check("a TV that resets 3000 is retried on 3001", got,
      [("ready", 3001, "key-from-3001")])
check("  in that order", tried, [3000, 3001])

got, tried = attempt(plain="Connection reset by peer", tls="Handshake failed")
check("when neither port works, both ports are tried", tried, [3000, 3001])
check("  and the error names both", got,
      [("error", "Handshake failed (and on port 3000: Connection reset by peer)",
        False)])

# A port that got as far as the handshake is the right port. Trying the other
# one after a declined prompt would put the prompt on screen a second time.
got, tried = attempt(plain=("upgraded", "nobody accepted the prompt on the TV"),
                     tls=True)
check("a failure after the handshake is not retried on the other port",
      tried, [3000])
check("  and is reported as the TV having answered", got,
      [("error", "nobody accepted the prompt on the TV", True)])

got, tried = attempt(plain=("silent", "the TV did not answer"), tls=True)
check("a TV that does not answer at all is not asked on a second port",
      tried, [3000])
check("  and is reported as unreached", got,
      [("error", "the TV did not answer", False)])

got, tried = attempt(plain=True, tls=True, prefer_secure=True)
check("a set known to want TLS is asked on 3001 first", tried, [3001])
got, tried = attempt(plain=True, tls="Connection reset by peer",
                     prefer_secure=True)
check("  and still falls back to 3000 after a firmware change", tried,
      [3001, 3000])


def switched(plain, tls=True, state=None, only_if_on=False):
    """Run turn_off against a scripted TV; returns (outcomes, requests)."""
    FakeClient.log, FakeClient.script = [], {False: plain, True: tls}
    FakeClient.requests = []
    FakeClient.power_state = state or {"returnValue": True, "state": "Active"}
    real, w.WebOsClient = w.WebOsClient, FakeClient
    try:
        got = []
        w.turn_off("10.0.0.9", "k",
                   lambda outcome, _message, secure: got.append((outcome, secure)),
                   only_if_on=only_if_on)
        return got, [uri.rsplit("/", 1)[-1] for uri in FakeClient.requests]
    finally:
        w.WebOsClient = real


check("turn_off reports the port that answered",
      switched(plain=True), ([(w.OFF, False)], ["turnOff"]))
check("a TV that is on is switched off when asked only-if-on",
      switched(plain=True, only_if_on=True),
      ([(w.OFF, False)], ["getPowerState", "turnOff"]))
check("a TV in standby is left alone",
      switched(plain=True, only_if_on=True,
               state={"returnValue": True, "state": "Active Standby"}),
      ([(w.IN_STANDBY, False)], ["getPowerState"]))
check("firmware without getPowerState is switched off anyway",
      switched(plain=True, only_if_on=True,
               state={"returnValue": False, "errorText": "404"}),
      ([(w.OFF, False)], ["getPowerState", "turnOff"]))
check("a TV that does not answer is unreachable",
      switched(plain=("silent", "the TV did not answer"))[0],
      [(w.UNREACHABLE, None)])
check("a TV that answers and then refuses has failed",
      switched(plain=("upgraded", "403 insufficient permissions"))[0],
      [(w.FAILED, None)])

# An error reply to a request is a failed reply, not a hang until the timeout.
client = w.WebOsClient("10.0.0.1")
replies = []
client._pending["hdlounge_1"] = replies.append
client._dispatch({"type": "error", "id": "hdlounge_1", "error": "404 no such method"})
check("an error reply is handed to the request's callback",
      replies, [{"returnValue": False, "errorText": "404 no such method"}])

# Discovery
# -------------------------------------------------------

LG_REPLY = (b"HTTP/1.1 200 OK\r\n"
            b"CACHE-CONTROL: max-age=1800\r\n"
            b"SERVER: WebOS/4.1.0 UPnP/1.0\r\n"
            b"ST: urn:lge-com:service:webos-second-screen:1\r\n"
            b"DLNADeviceName.lge.com: %5BLG%5D%20webOS%20TV%20OLED55C1\r\n"
            b"\r\n")
check("an LG reply is named from its own header",
      w.ssdp_answer(LG_REPLY), "[LG] webOS TV OLED55C1")
check("an LG reply without a name is still an LG TV",
      w.ssdp_answer(LG_REPLY.replace(b"DLNADeviceName", b"X-Other")), "LG TV")
check("a reply from some other device is ignored",
      w.ssdp_answer(b"HTTP/1.1 200 OK\r\nSERVER: Linux UPnP/1.0 Sonos\r\n\r\n"),
      None)

NEIGH = """192.168.1.1 dev eno1 lladdr 11:22:33:44:55:66 REACHABLE
192.168.1.42 dev eno1 lladdr aa:bb:cc:dd:ee:ff STALE
192.168.1.77 dev eno1 FAILED
192.168.1.78 dev eno1 INCOMPLETE
fe80::1 dev eno1 lladdr 11:22:33:44:55:66 router STALE
"""
check("neighbours are the IPv4 hosts with a known MAC",
      w.neighbours_from(NEIGH), ["192.168.1.1", "192.168.1.42"])

check("the plain client targets 3000", w.WebOsClient("h").port, 3000)
check("the secure client targets 3001", w.WebOsClient("h", secure=True).port, 3001)

# Where to shout it
# -------------------------------------------------------

# A magic packet is a broadcast, and a broadcast only reaches the network it
# went out on. Picking the wrong interface is silent: the packet is sent, the
# TV never hears it.

SERVER = r"""1: lo    inet 127.0.0.1/8 scope host lo\       valid_lft forever preferred_lft forever
2: eno1    inet 192.168.1.50/24 brd 192.168.1.255 scope global dynamic noprefixroute eno1\       valid_lft 3421sec preferred_lft 3421sec
3: eno2    inet 192.168.40.5/24 brd 192.168.40.255 scope global noprefixroute eno2\       valid_lft forever preferred_lft forever
4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\       valid_lft forever preferred_lft forever
"""

check("a TV on a second NIC is woken on that NIC, not the default route",
      w.broadcasts_from(SERVER, "192.168.40.17"),
      [("192.168.40.255", "192.168.40.5")])
check("a TV on the default NIC uses that one",
      w.broadcasts_from(SERVER, "192.168.1.99"),
      [("192.168.1.255", "192.168.1.50")])
check("a TV on no known subnet falls back to every interface",
      len(w.broadcasts_from(SERVER, "10.9.9.9")), 3)
check("loopback is never a target",
      [src for _b, src in w.broadcasts_from(SERVER) if src.startswith("127.")], [])

VPN = (SERVER
       + "5: tun0    inet 10.8.0.2/24 scope global tun0\\       valid_lft forever\n")
check("an interface with no broadcast address is skipped",
      w.broadcasts_from(VPN, "10.8.0.9"), w.broadcasts_from(SERVER, "10.8.0.9"))

for junk in ("", "not ip output at all",
             "2: x inet notanaddress brd alsonot scope global"):
    w.broadcasts_from(junk, "192.168.40.17")
check("malformed ip output does not raise", True, True)

check("both WoL ports are used", tuple(w.WOL_PORTS), (9, 7))


class FakeSocket:
    """Catches the datagrams wake() would put on the wire."""

    sent = []
    bound = []

    def __init__(self, *_a):
        pass

    def setsockopt(self, *_a):
        pass

    def bind(self, address):
        FakeSocket.bound.append(address[0])

    def sendto(self, _data, address):
        FakeSocket.sent.append(address)

    def close(self):
        pass


class FakeSocketModule:
    AF_INET = SOCK_DGRAM = SOL_SOCKET = SO_BROADCAST = 0
    socket = FakeSocket
    error = OSError


class FakeProc:
    """Answers `ip addr` from a fixture instead of the running machine."""

    def __init__(self, text):
        self.text = text

    def run(self, _argv, on_done):
        on_done(self.text)


def woken(mac, host):
    FakeSocket.sent, FakeSocket.bound = [], []
    real_socket, real_proc = w.socket, w.proc
    w.socket, w.proc = FakeSocketModule, FakeProc(SERVER)
    try:
        result = []
        w.wake(mac, host, lambda ok, message: result.append((ok, message)))
        return result, list(FakeSocket.sent), list(FakeSocket.bound)
    finally:
        w.socket, w.proc = real_socket, real_proc


result, sent, bound = woken("aa:bb:cc:dd:ee:ff", "192.168.40.17")
check("waking reports success", result, [(True, "TV on")])
check("  it broadcasts on the TV's own subnet",
      ("192.168.40.255", 9) in sent, True)
check("  it also sends straight to the TV, for when broadcast does not carry",
      ("192.168.40.17", 9) in sent, True)
check("  and keeps 255.255.255.255 as a last resort",
      ("255.255.255.255", 9) in sent, True)
check("  every target is tried on both ports",
      sorted({port for _addr, port in sent}), [7, 9])
check("  the subnet broadcast leaves by the matching interface",
      "192.168.40.5" in bound, True)
check("  nothing is sent to another interface's network",
      [a for a, _p in sent if a.startswith(("192.168.1.", "172.17."))], [])

result, sent, _bound = woken("nonsense", "192.168.40.17")
check("a bad MAC is refused before anything is sent", sent, [])
check("  and says why", result[0][0], False)

# Manifest
# -------------------------------------------------------

# The TV remembers the pairing against this appId: change it and every
# already-paired TV asks to be paired again.
check("the manifest still claims CONTROL_POWER",
      "CONTROL_POWER" in w.MANIFEST["permissions"], True)
check("the manifest appId has not drifted",
      w.MANIFEST["signed"]["appId"], "com.lge.test")

print(f"\n  {'FAILED: ' + str(len(FAILURES)) if FAILURES else 'all passed'}")
sys.exit(1 if FAILURES else 0)
