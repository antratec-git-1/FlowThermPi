"""A tiny MQTT 3.1.1 broker and a simulated Zigbee2MQTT – for the tests and the demo. Nothing real is reached.

FakeBroker speaks MQTT over TCP on 127.0.0.1: logins (or none), wildcards (+ and #), retained messages, QoS 0.
FakeZ2M sits inside the broker and answers like Zigbee2MQTT 2.x: bridge/state, bridge/info and bridge/devices
(retained), the answers to permit_join and device/remove, and – while the network is open – a SONOFF thermostat
that joins after a few seconds and is interviewed.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
from datetime import datetime

CONNECT, CONNACK, PUBLISH, PUBACK, SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 4, 8, 9, 12, 13, 14


def matches(pattern: str, topic: str) -> bool:
    want, have = pattern.split("/"), topic.split("/")
    for i, part in enumerate(want):
        if part == "#":
            return True
        if i >= len(have) or (part != "+" and part != have[i]):
            return False
    return len(want) == len(have)


def _packet(kind: int, flags: int, body: bytes) -> bytes:
    size, length = len(body), bytearray()
    while True:
        size, byte = divmod(size, 128)
        length.append(byte | (0x80 if size else 0))
        if not size:
            break
    return bytes([kind << 4 | flags]) + bytes(length) + body


def _string(text: str) -> bytes:
    data = text.encode()
    return struct.pack("!H", len(data)) + data


class _Session:
    def __init__(self, broker: "FakeBroker", sock: socket.socket):
        self.broker, self.sock = broker, sock
        self.filters: list[str] = []
        self.client_id = self.user = None
        self.lock = threading.Lock()
        self.alive = True

    def send(self, data: bytes) -> None:
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                self.alive = False

    def deliver(self, topic: str, payload: bytes, retain: bool) -> None:
        if self.alive and any(matches(f, topic) for f in self.filters):
            self.send(_packet(PUBLISH, 1 if retain else 0, _string(topic) + payload))

    def read(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def run(self) -> None:
        try:
            while self.alive:
                first = self.read(1)[0]
                size = shift = 0
                while True:
                    byte = self.read(1)[0]
                    size |= (byte & 0x7F) << shift
                    shift += 7
                    if not byte & 0x80:
                        break
                body = self.read(size) if size else b""
                if not self.handle(first >> 4, first & 0x0F, body):
                    break
        except (EOFError, OSError):
            pass
        finally:
            self.alive = False
            self.broker._gone(self)
            try:
                self.sock.close()
            except OSError:
                pass

    def handle(self, kind: int, flags: int, body: bytes) -> bool:
        b = self.broker
        if kind == CONNECT:
            pos = 2 + struct.unpack("!H", body[:2])[0]
            level, connect_flags = body[pos], body[pos + 1]
            pos += 4

            def field() -> str:
                nonlocal pos
                size = struct.unpack("!H", body[pos:pos + 2])[0]
                value = body[pos + 2:pos + 2 + size].decode()
                pos += 2 + size
                return value

            self.client_id = field()
            user = field() if connect_flags & 0x80 else None
            password = field() if connect_flags & 0x40 else None
            code = 1 if level != 4 else 0
            if not code and b.users is not None and b.users.get(user) != password:
                code = 4 if user in b.users else 5
            if b.refuse is not None:
                code = b.refuse
            self.user = user
            b.connects.append((self.client_id, user, code))
            self.send(_packet(CONNACK, 0, bytes([0, code])))
            return code == 0
        if kind == SUBSCRIBE:
            pid, pos, codes = body[:2], 2, []
            while pos < len(body):
                size = struct.unpack("!H", body[pos:pos + 2])[0]
                self.filters.append(body[pos + 2:pos + 2 + size].decode())
                pos += 3 + size
                codes.append(0)
            self.send(_packet(SUBACK, 0, pid + bytes(codes)))
            with b.lock:
                retained = list(b.retained.items())
            for topic, payload in retained:
                if any(matches(f, topic) for f in self.filters[-len(codes):]):
                    self.send(_packet(PUBLISH, 1, _string(topic) + payload))
            return True
        if kind == PUBLISH:
            size = struct.unpack("!H", body[:2])[0]
            topic, rest = body[2:2 + size].decode(), body[2 + size:]
            qos = flags >> 1 & 3
            if qos:
                pid, rest = rest[:2], rest[2:]
                self.send(_packet(PUBACK, 0, pid))
            b.publish(topic, rest, retain=bool(flags & 1), source=self)
            return True
        if kind == PINGREQ:
            if not b.mute_pings:
                self.send(_packet(PINGRESP, 0, b""))
            return True
        if kind == DISCONNECT:
            return False
        return True


class FakeBroker:
    """users: {user: password} – None lets everybody in. refuse: a CONNACK code for every login (e.g. 3)."""

    def __init__(self, users: dict[str, str] | None = None, port: int = 0):
        self.users, self.refuse, self.mute_pings = users, None, False
        self.lock = threading.Lock()
        self.retained: dict[str, bytes] = {}
        self.sessions: list[_Session] = []
        self.connects: list[tuple] = []             # (client id, user, CONNACK code)
        self.published: list[tuple[str, bytes]] = []   # what the clients sent
        self.hooks: list = []                        # hook(topic, payload) for every message a client sends
        self.server = socket.create_server(("127.0.0.1", port))
        self.port = self.server.getsockname()[1]
        self.closed = False
        threading.Thread(target=self._accept, name="fake-broker", daemon=True).start()

    def _accept(self) -> None:
        while not self.closed:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            session = _Session(self, sock)
            with self.lock:
                self.sessions.append(session)
            threading.Thread(target=session.run, name="fake-broker-session", daemon=True).start()

    def _gone(self, session: _Session) -> None:
        with self.lock:
            if session in self.sessions:
                self.sessions.remove(session)

    def publish(self, topic: str, payload, retain: bool = False, source: _Session | None = None) -> None:
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        if isinstance(payload, str):
            payload = payload.encode()
        with self.lock:
            if retain:
                if payload:
                    self.retained[topic] = payload
                else:
                    self.retained.pop(topic, None)
            sessions = list(self.sessions)
        if source is not None:
            self.published.append((topic, payload))
        for session in sessions:
            session.deliver(topic, payload, False)
        if source is not None:
            for hook in list(self.hooks):
                hook(topic, payload)

    def clients(self) -> int:
        with self.lock:
            return sum(1 for s in self.sessions if s.client_id is not None and s.alive)

    def drop(self) -> None:
        """Cut every connection (as when Mosquitto restarts); the broker itself stays."""
        with self.lock:
            sessions = list(self.sessions)
        for session in sessions:
            session.alive = False
            try:
                session.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self) -> None:
        self.closed = True
        try:
            self.server.close()
        except OSError:
            pass
        self.drop()


def later(seconds: float, action) -> None:
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()


def thermostat(ieee: str, interview: str = "SUCCESSFUL") -> dict:
    """A SONOFF TRV Gen2 as bridge/devices lists it – the definition only once the interview is done."""
    done = interview == "SUCCESSFUL"
    return {"ieee_address": ieee, "type": "EndDevice", "network_address": 0x4A21, "supported": done,
            "friendly_name": ieee, "disabled": False, "description": None,
            "definition": {"source": "native", "model": "TRV-ZBT", "vendor": "SONOFF",
                           "description": "Thermostatic radiator valve",
                           "exposes": [{"type": "climate", "features": []},
                                       {"type": "numeric", "name": "battery", "property": "battery"}],
                           "supports_ota": True, "options": []} if done else None,
            "power_source": "Battery", "software_build_id": "1.1.3" if done else None, "model_id": "TRV-ZBT",
            "manufacturer": "SONOFF", "interviewing": interview == "IN_PROGRESS", "interview_completed": done,
            "interview_state": interview, "endpoints": {}}


class FakeZ2M:
    """Zigbee2MQTT 2.x as the app sees it over MQTT. join_after: seconds after permit_join until a thermostat joins
    (None: nobody comes); interview: seconds its interview takes."""

    BASE = "zigbee2mqtt"
    COORDINATOR = {"ieee_address": "0x00124b0000000001", "type": "Coordinator", "friendly_name": "Coordinator",
                   "network_address": 0, "supported": True, "interview_state": "SUCCESSFUL", "definition": None}

    def __init__(self, broker: FakeBroker, channel: int = 25, join_after: float | None = 2.0, interview: float = 1.0):
        self.broker, self.channel, self.join_after, self.interview = broker, channel, join_after, interview
        self.version = "2.14.3"
        self.devices: list[dict] = []
        self.online = False
        self.permit_end: float | None = None
        self.silent = False             # True: requests are not answered
        self.remove_error: str | None = None   # error for a removal without force (device asleep)
        self.next_ieee = 0x8c65a3fffe1d0001
        self.lock = threading.RLock()
        broker.hooks.append(self._request)

    # what Zigbee2MQTT publishes by itself
    def start(self, devices: list[dict] | None = None) -> None:
        with self.lock:
            if devices is not None:
                self.devices = devices
            self.online = True
        self.broker.publish(f"{self.BASE}/bridge/state", {"state": "online"}, retain=True)
        self._info()
        self._devices()
        for device in self.devices:
            if device["interview_state"] == "SUCCESSFUL":
                self._values(device["ieee_address"])

    def stop(self) -> None:
        with self.lock:
            self.online, self.permit_end = False, None
        self.broker.publish(f"{self.BASE}/bridge/state", {"state": "offline"}, retain=True)

    def _info(self) -> None:
        with self.lock:
            end = self.permit_end if self.permit_end and self.permit_end > time.time() else None
            info = {"version": self.version, "commit": "3c841fdf", "coordinator": {
                        "ieee_address": "0x00124b0000000001", "type": "EmberZNet", "meta": {"revision": "8.0.2"}},
                    "network": {"pan_id": 0x2B47, "extended_pan_id": "0x1122334455667788", "channel": self.channel},
                    "log_level": "info", "permit_join": end is not None,
                    "permit_join_end": int(end * 1000) if end else None, "restart_required": False,
                    "config": {"advanced": {"channel": self.channel}}, "config_schema": {"type": "object"}}
        self.broker.publish(f"{self.BASE}/bridge/info", info, retain=True)

    def _devices(self) -> None:
        with self.lock:
            devices = [self.COORDINATOR, *self.devices]
        self.broker.publish(f"{self.BASE}/bridge/devices", devices, retain=True)

    def _event(self, kind: str, data: dict) -> None:
        self.broker.publish(f"{self.BASE}/bridge/event", {"type": kind, "data": data})

    def _values(self, ieee: str, battery: int = 87) -> None:
        self.broker.publish(f"{self.BASE}/{ieee}", {
            "battery": battery, "linkquality": 132, "local_temperature": 20.5, "occupied_heating_setpoint": 21,
            "system_mode": "heat", "last_seen": datetime.now().astimezone().isoformat(timespec="seconds")}, retain=True)

    # requests from the app
    def _request(self, topic: str, payload: bytes) -> None:
        prefix = f"{self.BASE}/bridge/request/"
        if not topic.startswith(prefix) or not self.online or self.silent:
            return
        what = topic[len(prefix):]
        try:
            body = json.loads(payload)
        except ValueError:
            body = {}
        answer: dict = {"data": {}, "status": "ok"}
        if what == "permit_join":
            seconds = body.get("time")
            if not isinstance(seconds, int) or not 0 <= seconds <= 254:
                answer = {"data": {}, "status": "error", "error": "Invalid payload"}
            else:
                with self.lock:
                    self.permit_end = time.time() + seconds if seconds else None
                answer["data"] = {"time": seconds}
                self._info()
                if seconds and self.join_after is not None:
                    later(self.join_after, self._join)
        elif what == "device/remove":
            ieee, force = body.get("id"), body.get("force") is True
            with self.lock:
                found = next((d for d in self.devices if d["ieee_address"] == ieee), None)
            if found is None:
                answer = {"data": {}, "status": "error", "error": f"Device '{ieee}' does not exist"}
            elif self.remove_error and not force:
                answer = {"data": {}, "status": "error", "error": self.remove_error}
            else:
                with self.lock:
                    self.devices.remove(found)
                answer["data"] = {"id": ieee, "block": False, "force": force}
                self.broker.publish(f"{self.BASE}/{ieee}", b"", retain=True)
                self._devices()
        else:
            answer = {"data": {}, "status": "error", "error": f"Unknown request '{what}'"}
        if "transaction" in body:
            answer["transaction"] = body["transaction"]
        self.broker.publish(f"{self.BASE}/bridge/response/{what}", answer)

    def _join(self) -> None:
        """A thermostat whose middle button was held for 2 s: it joins, is interviewed, reports its values."""
        with self.lock:
            if not (self.online and self.permit_end and self.permit_end > time.time()):
                return
            ieee = f"0x{self.next_ieee:016x}"
            self.next_ieee += 1
            device = thermostat(ieee, "IN_PROGRESS")
            self.devices.append(device)
        self._event("device_joined", {"friendly_name": ieee, "ieee_address": ieee})
        self._event("device_interview", {"friendly_name": ieee, "status": "started", "ieee_address": ieee})
        self._devices()

        def done() -> None:
            finished = thermostat(ieee)
            with self.lock:
                if device not in self.devices:
                    return
                self.devices[self.devices.index(device)] = finished
            self._event("device_interview", {"friendly_name": ieee, "status": "successful", "ieee_address": ieee,
                                             "supported": True, "definition": finished["definition"]})
            self._devices()
            self._values(ieee)

        later(self.interview, done)
