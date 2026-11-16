"""Find FlexiSmart Pro heaters in the local network.

The heaters do not answer the multicast "WHEREIS" from Thermotec's documentation, but every heater
answers a plain read without token. So the search asks every address of the local network once for
its serial number ("idu") – all from one UDP socket, a few milliseconds apart – and collects the
answers. 254 addresses take about three seconds. Nothing is written to any device.
"""
from __future__ import annotations

import errno
import ipaddress
import json
import os
import random
import re
import select
import socket
import time

from . import coap

MAX_ADDRESSES = 1024   # largest network the search accepts (a /22)
PACE = 0.003           # seconds between two requests (keeps ARP and Wi-Fi queues happy)
ROUNDS = 2             # a second round catches heaters whose Wi-Fi was dozing
WAIT = 1.0             # seconds to collect late answers after each round


class DiscoveryError(ValueError):
    pass


def parse_network(text) -> ipaddress.IPv4Network:
    """"192.168.178", "192.168.178.0/24" or any address in it -> 192.168.178.0/24.
    Only private networks with at most MAX_ADDRESSES addresses."""
    raw = str(text or "").strip().rstrip(".")
    if re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", raw):
        raw += ".0/24"
    elif re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", raw):
        raw += "/24"
    try:
        net = ipaddress.IPv4Network(raw, strict=False)
    except ValueError:
        raise DiscoveryError(f"„{text}“ ist kein Netz – bitte z. B. 192.168.178 oder 192.168.178.0/24 angeben.") from None
    if not net.is_private or net.is_loopback or net.is_link_local or net.is_multicast:
        raise DiscoveryError(f"{net} ist kein Heimnetz (erlaubt sind private Netze wie 192.168.x.x oder 10.x.x.x).")
    if net.num_addresses > MAX_ADDRESSES:
        raise DiscoveryError(f"{net} ist zu groß – höchstens {MAX_ADDRESSES} Adressen (/22).")
    return net


def local_networks(known_hosts=()) -> list[ipaddress.IPv4Network]:
    """The /24 of this computer's main address, plus the /24 of every heater that is configured by IP."""
    candidates = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))   # sends nothing; only picks the interface towards the router
            candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    candidates += [h for h in known_hosts if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", h)]
    nets: list[ipaddress.IPv4Network] = []
    for host in candidates:
        try:
            net = parse_network(host)
        except DiscoveryError:
            continue   # e.g. a Docker bridge address or a VPN – not the home network
        if net not in nets:
            nets.append(net)
    return nets


def targets_of(networks) -> list[tuple[str, int]]:
    return [(str(ip), coap.DEFAULT_PORT) for net in networks for ip in net.hosts()]


def scan(targets: list[tuple[str, int]], rounds: int = ROUNDS, wait: float = WAIT,
         pace: float = PACE) -> dict[tuple[str, int], str]:
    """Ask every (ip, port) for its serial number; returns {(ip, port): serial} of all heaters that answered.
    Answers are matched by their CoAP token, so other devices cannot slip anything in."""
    body = json.dumps({"id": 1, "method": "get", "params": {"name": "idu"}}, separators=(",", ":")).encode()
    options = [(coap.OPT_URI_PATH, b"Json"), (coap.OPT_CONTENT_FORMAT, bytes([coap.CONTENT_FORMAT_JSON]))]
    pending: dict[bytes, tuple[str, int]] = {}
    found: dict[tuple[str, int], str] = {}

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)

        def collect(seconds: float) -> None:
            until = time.monotonic() + seconds
            while (left := until - time.monotonic()) > 0:
                ready, _, _ = select.select([sock], [], [], left)
                if not ready:
                    return
                try:
                    data, src = sock.recvfrom(4096)
                except OSError:      # nothing there after all, or an ICMP error of an earlier request
                    continue
                try:
                    msg = coap.decode(data)
                except coap.CoapError:
                    continue
                if msg["type"] == coap.CON:   # separate response: acknowledge it
                    try:
                        sock.sendto(coap.encode(coap.ACK, 0, msg["mid"], b""), src)
                    except OSError:
                        pass
                target = pending.get(bytes(msg["token"]))
                if target is None or coap.code_str(msg["code"]) != "2.05":
                    continue
                try:
                    serial = (json.loads(msg["payload"]).get("result") or {}).get("value")
                except (ValueError, AttributeError):
                    continue
                if serial not in (None, ""):
                    found[target] = str(serial)

        def send(packet: bytes, target: tuple[str, int]) -> None:
            for _attempt in range(3):
                try:
                    sock.sendto(packet, target)
                    return
                except BlockingIOError:
                    collect(0.02)
                except OSError as exc:   # host known to be down, no route, …: skip it
                    if exc.errno != errno.ENOBUFS:
                        return
                    collect(0.05)

        for _round in range(rounds):
            for target in targets:
                if target in found:
                    continue
                token = os.urandom(4)
                pending[token] = target
                send(coap.encode(coap.CON, coap.METHODS["GET"], random.randint(0, 0xFFFF), token, options, body), target)
                collect(pace)
            collect(wait)
    return found
