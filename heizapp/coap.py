"""Minimal CoAP client (RFC 7252) over UDP — standard library only.

Covers exactly what the FlexiSmart Pro interface needs: confirmable requests with
Uri-Path / Content-Format / Uri-Query options, piggybacked or separate responses,
and retransmission with the same message ID (so the device can de-duplicate writes).
"""
from __future__ import annotations

import logging
import os
import random
import socket
import time

CON, NON, ACK, RST = 0, 1, 2, 3
METHODS = {"GET": 1, "POST": 2, "PUT": 3, "DELETE": 4}
OPT_URI_PATH = 11
OPT_CONTENT_FORMAT = 12
OPT_URI_QUERY = 15
CONTENT_FORMAT_JSON = 50
DEFAULT_PORT = 5683
log = logging.getLogger(__name__)


class CoapError(Exception):
    """Transport problem: no answer, unreachable host or malformed packet."""


class CoapTimeout(CoapError):
    """The device did not answer in time."""


def _nibble(n: int) -> tuple[int, bytes]:
    if n < 13:
        return n, b""
    if n < 269:
        return 13, bytes([n - 13])
    return 14, (n - 269).to_bytes(2, "big")


def encode(mtype: int, code: int, mid: int, token: bytes,
           options: list[tuple[int, bytes]] | None = None, payload: bytes = b"") -> bytes:
    if len(token) > 8:
        raise ValueError("CoAP token longer than 8 bytes")
    out = bytearray([0x40 | (mtype << 4) | len(token), code, (mid >> 8) & 0xFF, mid & 0xFF])
    out += token
    prev = 0
    # sorted() is stable, so repeated options (several Uri-Path segments) keep their order
    for number, value in sorted(options or [], key=lambda o: o[0]):
        d, d_ext = _nibble(number - prev)
        l, l_ext = _nibble(len(value))
        out += bytes([(d << 4) | l]) + d_ext + l_ext + value
        prev = number
    if payload:
        out += b"\xff" + payload
    return bytes(out)


def decode(data: bytes) -> dict:
    if len(data) < 4 or data[0] >> 6 != 1:
        raise CoapError("kein gültiges CoAP-Paket")
    tkl = data[0] & 0x0F
    msg = {
        "type": (data[0] >> 4) & 0x03,
        "code": data[1],
        "mid": (data[2] << 8) | data[3],
        "token": data[4:4 + tkl],
        "options": [],
        "payload": b"",
    }
    i, number = 4 + tkl, 0
    try:
        while i < len(data):
            if data[i] == 0xFF:
                msg["payload"] = data[i + 1:]
                break
            d, l = data[i] >> 4, data[i] & 0x0F
            i += 1
            if d == 13:
                d, i = data[i] + 13, i + 1
            elif d == 14:
                d, i = int.from_bytes(data[i:i + 2], "big") + 269, i + 2
            if l == 13:
                l, i = data[i] + 13, i + 1
            elif l == 14:
                l, i = int.from_bytes(data[i:i + 2], "big") + 269, i + 2
            if d == 15 or l == 15:
                raise CoapError("ungültige CoAP-Option")
            number += d
            msg["options"].append((number, data[i:i + l]))
            i += l
    except IndexError as exc:
        raise CoapError("abgeschnittenes CoAP-Paket") from exc
    return msg


def code_str(code: int) -> str:
    return f"{code >> 5}.{code & 0x1F:02d}"


def request(host: str, method: str, path: str, payload: bytes = b"", query: list[str] | None = None,
            port: int = DEFAULT_PORT, timeout: float = 1.5, retries: int = 2) -> tuple[str, bytes]:
    """Send a confirmable request; return (response code like '2.05', payload)."""
    token = os.urandom(4)
    mid = random.randint(0, 0xFFFF)
    options = [(OPT_URI_PATH, seg.encode()) for seg in path.strip("/").split("/") if seg]
    if payload:
        options.append((OPT_CONTENT_FORMAT, bytes([CONTENT_FORMAT_JSON])))
    options += [(OPT_URI_QUERY, q.encode()) for q in (query or [])]
    packet = encode(CON, METHODS[method], mid, token, options, payload)

    try:
        addr = (socket.gethostbyname(host), port)
    except OSError as exc:
        raise CoapError(f"{host}: Adresse nicht auflösbar") from exc

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for attempt in range(retries + 1):
            if attempt:   # the detailed log: a heater whose Wi-Fi dozes or that is gone (never the query: token)
                log.debug("%s %s %s: keine Antwort nach %g s – Versuch %d von %d", host, method, path, timeout,
                          attempt + 1, retries + 1)
            try:
                sock.sendto(packet, addr)
            except OSError as exc:
                raise CoapError(f"{host}: {exc}") from exc
            deadline = time.monotonic() + timeout
            while (remaining := deadline - time.monotonic()) > 0:
                sock.settimeout(remaining)
                try:
                    data, _src = sock.recvfrom(4096)
                except socket.timeout:
                    break
                except OSError as exc:
                    raise CoapError(f"{host}: {exc}") from exc
                try:
                    answer = decode(data)
                except CoapError:
                    continue
                if answer["type"] == ACK and answer["code"] == 0 and answer["mid"] == mid:
                    deadline = time.monotonic() + 3 * timeout  # empty ACK: separate response follows
                    log.debug("%s %s %s: Antwort kommt getrennt (leeres ACK)", host, method, path)
                    continue
                if answer["type"] == RST and answer["mid"] == mid:
                    log.debug("%s %s %s: zurückgewiesen (RST)", host, method, path)
                    raise CoapError(f"{host}: Anfrage zurückgewiesen (RST)")
                if answer["token"] != token:
                    continue
                if answer["type"] == CON:
                    sock.sendto(encode(ACK, 0, answer["mid"], b""), addr)
                return code_str(answer["code"]), answer["payload"]
    raise CoapTimeout(f"{host}: keine Antwort")
