"""MQTT 3.1.1 mit Bordmitteln von Python – nur was die App braucht: anmelden, abonnieren und senden, beides mit QoS 0.

Die App spricht damit mit Mosquitto auf diesem Pi (127.0.0.1, mit Passwort; ohne TLS, denn Mosquitto lauscht nur
dort). Ein Client ist eine Verbindung: connect(), subscribe(), dann loop() bis die Verbindung endet. Neu verbinden
macht heizapp.zigbee_net.
"""
from __future__ import annotations

import logging
import select
import socket
import struct
import threading
import time
from typing import Callable

log = logging.getLogger("heizapp.zigbee")
MAX_PACKET = 16 << 20   # bridge/info carries Zigbee2MQTT's whole settings schema: a few hundred kB
CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP, SUBSCRIBE, SUBACK = 1, 2, 3, 4, 5, 6, 7, 8, 9
PINGREQ, PINGRESP, DISCONNECT = 12, 13, 14
REFUSED = {1: "Mosquitto kennt diese Protokollversion nicht", 2: "Mosquitto lehnt die Kennung ab",
           3: "Mosquitto ist gerade nicht bereit", 4: "Benutzername oder Passwort falsch", 5: "nicht berechtigt"}


class MqttError(Exception):
    """The connection failed, was refused or has ended."""


class _Idle(Exception):
    """Nothing arrived in time."""


def _string(text: str) -> bytes:
    data = text.encode("utf-8")
    if len(data) > 0xFFFF:
        raise ValueError("Text zu lang für MQTT")
    return struct.pack("!H", len(data)) + data


def encode(kind: int, flags: int, body: bytes) -> bytes:
    """One packet: type and flags, the remaining length (1 to 4 bytes of 7 bits), the body."""
    size, length = len(body), bytearray()
    while True:
        size, byte = divmod(size, 128)
        length.append(byte | (0x80 if size else 0))
        if not size:
            break
    return bytes([kind << 4 | flags]) + bytes(length) + body


def decode(buffer: bytearray) -> tuple[int, int, bytes] | None:
    """The first complete packet in buffer, taken out of it: type, flags, body. None while more has to arrive."""
    size = shift = 0
    for i in range(1, 5):
        if i >= len(buffer):
            return None
        size |= (buffer[i] & 0x7F) << shift
        shift += 7
        if not buffer[i] & 0x80:
            break
    else:
        raise MqttError("ungültiges Paket von Mosquitto")
    if size > MAX_PACKET:
        raise MqttError(f"Nachricht zu groß ({size >> 10} kB)")
    end = i + 1 + size
    if len(buffer) < end:
        return None
    first, body = buffer[0], bytes(buffer[i + 1:end])
    del buffer[:end]
    return first >> 4, first & 0x0F, body


class Client:
    """One connection to the broker. on_message(topic, payload, retained) runs in the thread that calls loop()."""

    def __init__(self, host: str, port: int, user: str | None = None, password: str | None = None,
                 client_id: str = "", on_message: Callable[[str, bytes, bool], None] | None = None,
                 keepalive: int = 30):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.client_id, self.on_message, self.keepalive = client_id, on_message, keepalive
        self._sock: socket.socket | None = None
        self._buffer = bytearray()
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._next = 0
        self._sent = 0.0      # monotonic time of the last packet sent
        self._ping = 0.0      # a PINGREQ is out since then, until its PINGRESP

    # ------------------------------------------------------------------ the connection
    def connect(self, timeout: float = 10.0) -> None:
        """Connect and log in. MqttError if the broker refuses or does not answer, OSError if it cannot be reached."""
        self._sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        flags, payload = 0x02, _string(self.client_id)   # clean session: nothing is kept for us while we are away
        if self.user is not None:
            flags, payload = flags | 0x80, payload + _string(self.user)
        if self.password is not None:
            flags, payload = flags | 0x40, payload + _string(self.password)
        self._send(encode(CONNECT, 0, _string("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive) + payload))
        try:
            kind, _, body = self._receive(time.monotonic() + timeout)
        except _Idle:
            raise MqttError("Mosquitto antwortet nicht") from None
        if kind != CONNACK or len(body) != 2:
            raise MqttError("unerwartete Antwort von Mosquitto")
        if body[1]:
            raise MqttError(REFUSED.get(body[1], f"Mosquitto lehnt die Anmeldung ab (Code {body[1]})"))

    def subscribe(self, filters: list[str], timeout: float = 10.0) -> None:
        """Subscribe with QoS 0 and wait for the broker's yes. Messages that come meanwhile – the retained ones – are
        handed on as usual."""
        self._next = self._next % 0xFFFF + 1
        pid = struct.pack("!H", self._next)
        self._send(encode(SUBSCRIBE, 0x02, pid + b"".join(_string(f) + b"\x00" for f in filters)))
        deadline = time.monotonic() + timeout
        while True:
            try:
                kind, flags, body = self._receive(deadline)
            except _Idle:
                raise MqttError("Mosquitto bestätigt das Abonnieren nicht") from None
            if kind == SUBACK and body[:2] == pid:
                if 0x80 in body[2:]:
                    raise MqttError("Mosquitto lehnt das Abonnieren ab")
                return
            self._handle(kind, flags, body)

    def publish(self, topic: str, payload: bytes | str, retain: bool = False) -> None:
        """Send with QoS 0 – from any thread."""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._send(encode(PUBLISH, 0x01 if retain else 0, _string(topic) + payload))

    def loop(self, stop: threading.Event | None = None) -> None:
        """Hand on messages until the connection ends (MqttError, OSError), close() is called or stop is set."""
        while not self._closed.is_set() and not (stop and stop.is_set()):
            try:
                self._keep_alive()
                kind, flags, body = self._receive(time.monotonic() + 1.0)
            except _Idle:
                continue
            except (MqttError, OSError, ValueError):
                if self._closed.is_set():
                    return
                raise
            self._handle(kind, flags, body)

    def close(self) -> None:
        """End the connection (also from another thread: loop() then returns)."""
        if self._closed.is_set():
            return
        self._closed.set()
        sock = self._sock
        if sock is None:
            return
        try:
            with self._send_lock:
                sock.sendall(encode(DISCONNECT, 0, b""))
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    # ------------------------------------------------------------------ inside
    def _send(self, data: bytes) -> None:
        sock = self._sock
        if sock is None or self._closed.is_set():
            raise MqttError("keine Verbindung zu Mosquitto")
        with self._send_lock:
            sock.sendall(data)
            self._sent = time.monotonic()

    def _receive(self, deadline: float) -> tuple[int, int, bytes]:
        while True:
            packet = decode(self._buffer)
            if packet:
                return packet
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Idle()
            sock = self._sock
            if sock is None or self._closed.is_set():
                raise MqttError("keine Verbindung zu Mosquitto")
            readable, _, _ = select.select([sock], [], [], min(left, 1.0))
            if not readable:
                continue
            chunk = sock.recv(65536)
            if not chunk:
                raise MqttError("Mosquitto hat die Verbindung beendet")
            self._buffer += chunk

    def _keep_alive(self) -> None:
        """A PINGREQ when nothing was sent for half the keepalive; no PINGRESP within the keepalive ends it."""
        now = time.monotonic()
        if self._ping and now - self._ping > self.keepalive:
            raise MqttError("Mosquitto antwortet nicht mehr")
        if not self._ping and now - self._sent >= self.keepalive / 2:
            self._send(encode(PINGREQ, 0, b""))
            self._ping = now

    def _handle(self, kind: int, flags: int, body: bytes) -> None:
        if kind == PUBLISH:
            if len(body) < 2:
                raise MqttError("ungültige Nachricht von Mosquitto")
            size = struct.unpack("!H", body[:2])[0]
            topic, rest = body[2:2 + size].decode("utf-8", "replace"), body[2 + size:]
            qos = flags >> 1 & 3
            if qos:   # we subscribe with QoS 0, so this does not happen – answered anyway
                pid, rest = rest[:2], rest[2:]
                self._send(encode(PUBACK if qos == 1 else PUBREC, 0, pid))
            if self.on_message:
                try:
                    self.on_message(topic, rest, bool(flags & 1))
                except Exception:  # a message we cannot handle must not end the connection
                    log.exception("Zigbee: Fehler bei der Nachricht %s", topic)
        elif kind == PUBREL:
            self._send(encode(PUBCOMP, 0, body[:2]))
        elif kind == PINGRESP:
            self._ping = 0.0
