"""A tiny MQTT 3.1.1 broker and a simulated Zigbee2MQTT – for the tests and the demo. Nothing real is reached.

FakeBroker speaks MQTT over TCP on 127.0.0.1: logins (or none), wildcards (+ and #), retained messages, QoS 0.
FakeZ2M sits inside the broker and answers like Zigbee2MQTT 2.x: bridge/state, bridge/info and bridge/devices
(retained), the answers to permit_join and device/remove, and – while the network is open – a SONOFF thermostat
that joins after a few seconds and is interviewed.

Its thermostats behave like the SONOFF TRV-ZBT in Zigbee2MQTT (zigbee-herdsman-converters, sonoff.ts, read on
25.11.2026): zigbee2mqtt/<IEEE>/set takes the same values with the same checks – a wrong one is refused without a
message, as Zigbee2MQTT only logs it – and after `delay` seconds the thermostat's whole state comes back, retained,
with a fresh last_seen; …/get brings the state, too. A thermostat in `asleep` answers nothing.

A firmware update (bridge/request/device/ota_update/update) goes as the Zigbee2MQTT guide describes it (read on
25.11.2026): the device reports its progress `ota_steps` times, `ota_every` seconds apart; only at the end comes the
answer, with the firmware before and after – or with `ota_error`.
"""
from __future__ import annotations

import json
import re
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


DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TRANSITION = re.compile(r"^(0[0-9]|1[0-9]|2[0-3]):([0-5][0-9])/(\d+(?:\.\d{1,2})?)$")


def schedule_error(text) -> str | None:
    """What Zigbee2MQTT says about a weekly_schedule_<day> value of the TRV-ZBT – None if it takes it."""
    if not isinstance(text, str):
        return "not a string"
    parts = sorted(text.strip().split())
    if len(parts) > 12:
        return "days must have no more than 12 transitions"
    for part in parts:
        found = TRANSITION.match(part)
        if not found:
            return f"transitions must be in format HH:mm/temperature, found: {part}"
        if not 5 <= float(found[3]) <= 30:
            return f"temperature value must be between 5-30 (inclusive), found: {found[3]}"
    if not parts or not parts[0].startswith("00:00/"):
        return "the first transition of each day should start at 00:00"
    return None


def trv_state(temperature: float = 20.5, setpoint: float = 21, battery: int = 87) -> dict:
    """A TRV-ZBT's state as Zigbee2MQTT publishes it (the factory plan is made up)."""
    return {"local_temperature": temperature, "occupied_heating_setpoint": setpoint, "system_mode": "heat",
            "running_state": "heat" if temperature < setpoint else "idle", "local_temperature_calibration": 0,
            "frost_protection_temperature": 7, "open_window": "ON", "open_window_detected": False,
            "child_lock": "UNLOCK", "battery": battery, "linkquality": 132, "fault_status": "none",
            "temporary_mode_mode": "disabled", "temporary_mode_duration": 30, "temporary_mode_target_temperature": 30,
            "schedule_active_group": "1", "schedule_group_to_edit": "1", "weekly_schedule_status": "success",
            **{f"weekly_schedule_{day}": "00:00/16 06:00/21 22:00/16" for day in DAYS},
            "smart_temperature_control": "OFF", "temperature_sensor_select": "local_temperature",
            "update": {"state": "idle", "installed_version": 4353, "latest_version": 4353}}


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
        self.states: dict[str, dict] = {}   # ieee -> the thermostat's state
        self.plans: dict[str, dict] = {}    # ieee -> {group: {day: schedule}} – all three plans of the thermostat
        self.delay = 0.05                   # seconds until a command has reached the thermostat (real: about 2)
        self.asleep: set[str] = set()       # thermostats that answer nothing
        self.commands: list[tuple[str, str, dict]] = []   # (ieee, "set" | "get", payload) as they came
        self.refused: list[str] = []        # why a command was refused (Zigbee2MQTT only logs it)
        self.requests: list[tuple[str, dict]] = []   # (what, body) of every bridge/request/<what> from the app
        self.ota_steps = 4                  # progress reports during a firmware update …
        self.ota_every = 0.05               # … this many seconds apart (real: every 30 s, 10 to 100 minutes in all)
        self.ota_error: str | None = None   # why a firmware update fails (e.g. the device does not take part)
        self.ota_firmware = "1.1.4"         # the firmware (software_build_id) after an update
        self.lock = threading.RLock()
        broker.hooks.append(self._request)
        broker.hooks.append(self._command)

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

    def _values(self, ieee: str) -> None:
        """The thermostat's whole state, retained – as after every message from it."""
        with self.lock:
            state = self.states.setdefault(ieee, trv_state())
            state["last_seen"] = datetime.now().astimezone().isoformat(timespec="seconds")
            payload = dict(state)
        self.broker.publish(f"{self.BASE}/{ieee}", payload, retain=True)

    def report(self, ieee: str, **values) -> None:
        """The thermostat reports by itself (someone turned the wheel, the room cooled down, …)."""
        with self.lock:
            self.states.setdefault(ieee, trv_state()).update(values)
        self._values(ieee)

    # commands to a thermostat
    def _command(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != self.BASE or parts[2] not in ("set", "get") or not self.online:
            return
        ieee, kind = parts[1], parts[2]
        try:
            body = json.loads(payload)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self.refused.append("Invalid message")
            return
        with self.lock:
            self.commands.append((ieee, kind, body))
            known = any(d["ieee_address"] == ieee and d["interview_state"] == "SUCCESSFUL" for d in self.devices)
        if not known:
            self.refused.append(f"Entity '{ieee}' is unknown")
            return
        if ieee in self.asleep:
            return
        later(self.delay, lambda: self._apply(ieee, body) if kind == "set" else self._values(ieee))

    def _apply(self, ieee: str, body: dict) -> None:
        """What the TRV-ZBT's converters do with a set: check each value, write it, then publish the state."""
        with self.lock:
            state = self.states.setdefault(ieee, trv_state())
            plans = self.plans.setdefault(ieee, {g: {d: state[f"weekly_schedule_{d}"] for d in DAYS} for g in "123"})
            new = dict(state)
            for key, value in body.items():
                error = None
                if key == "occupied_heating_setpoint":
                    if isinstance(value, (int, float)) and 5 <= value <= 30:
                        new[key] = round(value * 2) / 2
                    else:
                        error = f"{key} {value} out of range"
                elif key == "system_mode":
                    error = None if value in ("off", "auto", "heat") else f"system_mode {value} unknown"
                    new[key] = value if error is None else new[key]
                elif key == "local_temperature_calibration":
                    if isinstance(value, (int, float)) and -10 <= value <= 10:
                        new[key] = round(value, 1)
                    else:
                        error = f"{key} {value} out of range"
                elif key == "frost_protection_temperature":
                    if isinstance(value, (int, float)) and 5 <= value <= 15:
                        new[key] = value
                    else:
                        error = f"{key} {value} out of range"
                elif key in ("schedule_active_group", "schedule_group_to_edit"):
                    if value in ("1", "2", "3"):
                        new[key] = value
                    else:
                        error = f"Invalid {key}: expected one of 1, 2, 3, got {value}"
                elif key.startswith("weekly_schedule_") and key[16:] in DAYS:
                    error = schedule_error(value)
                    if error is None:
                        plans[new["schedule_group_to_edit"]][key[16:]] = value
                        new[key] = value
                elif key in ("temporary_mode_mode", "temporary_mode_duration", "temporary_mode_target_temperature"):
                    mode = body.get("temporary_mode_mode", new["temporary_mode_mode"])
                    duration = body.get("temporary_mode_duration", new["temporary_mode_duration"])
                    if mode not in ("disabled", "timer", "boost") or not isinstance(duration, int) \
                            or not 1 <= duration <= 1440:
                        error = f"Invalid temporary mode {mode} / {duration}"
                    else:
                        new["temporary_mode_mode"] = mode
                        new["temporary_mode_duration"] = min(180, duration) if mode == "boost" else duration
                elif key == "smart_temperature_control":
                    if value in ("ON", "OFF"):
                        new[key] = value
                    else:
                        error = f"Invalid value {value} for {key}, expected ON or OFF"
                else:
                    error = f"No converter available for '{key}'"
                if error:
                    self.refused.append(error)
                    return                       # Zigbee2MQTT logs the error, publishes nothing
            if new["system_mode"] == "auto" and ("system_mode" in body or "schedule_active_group" in body):
                new["occupied_heating_setpoint"] = self.plan_now(plans[new["schedule_active_group"]])
            if "schedule_group_to_edit" in body and not any(k.startswith("weekly_schedule_") for k in body):
                new.update({f"weekly_schedule_{d}": plans[new["schedule_group_to_edit"]][d] for d in DAYS})
            heats = new["temporary_mode_mode"] == "boost" or (
                new["system_mode"] != "off" and new["local_temperature"] < new["occupied_heating_setpoint"])
            new["running_state"] = "heat" if heats else "idle"
            state.clear()
            state.update(new)
        self._values(ieee)

    @staticmethod
    def plan_now(days: dict) -> float:
        """The temperature a plan wants right now."""
        now = datetime.now()
        minute, current = now.hour * 60 + now.minute, 5.0
        for part in sorted(days[DAYS[now.weekday()]].split()):
            at, temp = part.split("/")
            if int(at[:2]) * 60 + int(at[3:]) <= minute:
                current = float(temp)
        return current

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
        with self.lock:
            self.requests.append((what, body))
        if what == "device/ota_update/update":
            return self._ota(body)
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

    def _ota(self, body: dict) -> None:
        """A firmware update: the device reports its progress (update.progress, update.remaining); Zigbee2MQTT answers
        only at the end – with the firmware before and after, or with its error."""
        ieee = body.get("id")
        topic = f"{self.BASE}/bridge/response/device/ota_update/update"
        answer = {"data": {"id": ieee}, "status": "error", **({"transaction": body["transaction"]}
                                                              if "transaction" in body else {})}
        with self.lock:
            found = next((d for d in self.devices if d["ieee_address"] == ieee), None)
            update = dict((self.states.get(ieee) or {}).get("update") or {})
        if found is None:
            return self.broker.publish(topic, {**answer, "error": f"Device '{ieee}' does not exist"})
        if update.get("state") != "available":
            return self.broker.publish(topic, {**answer, "error": f"Update of '{ieee}' failed (No image available)"})
        if self.ota_error or ieee in self.asleep:
            failed = {**answer, "error": f"Update of '{ieee}' failed ({self.ota_error or 'Device did not respond'})"}
            return later(self.ota_every, lambda: self.broker.publish(topic, failed))
        old, before, latest = found.get("software_build_id"), update.get("installed_version"), update.get("latest_version")

        def step(n: int) -> None:
            if n < self.ota_steps:
                self.report(ieee, update={"state": "updating", "progress": round(100 * n / self.ota_steps, 2),
                                          "remaining": round((self.ota_steps - n) * self.ota_every),
                                          "installed_version": before, "latest_version": latest})
                return later(self.ota_every, lambda: step(n + 1))
            with self.lock:
                found["software_build_id"] = self.ota_firmware
            self.report(ieee, update={"state": "idle", "installed_version": latest, "latest_version": latest})
            self._devices()
            self.broker.publish(topic, {**answer, "status": "ok", "data": {
                "id": ieee, "from": {"software_build_id": old, "file_version": before},
                "to": {"software_build_id": self.ota_firmware, "file_version": latest}}})

        later(self.ota_every, lambda: step(0))

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
