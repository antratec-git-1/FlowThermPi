"""A tiny MQTT 3.1.1 broker and a simulated Zigbee2MQTT – for the tests and the demo. Nothing real is reached.

FakeBroker speaks MQTT over TCP on 127.0.0.1: logins (or none), wildcards (+ and #), retained messages, QoS 0.
FakeZ2M sits inside the broker and answers like Zigbee2MQTT 2.x: bridge/state, bridge/info and bridge/devices
(retained), the answers to permit_join and device/remove, and – while the network is open – a SONOFF thermostat
that joins after a few seconds and is interviewed.

Its thermostats behave like the SONOFF TRV-ZBT in Zigbee2MQTT (zigbee-herdsman-converters, sonoff.ts, read on
25.11.2026): zigbee2mqtt/<IEEE>/set takes the same values with the same checks – a wrong one is refused without a
message, as Zigbee2MQTT only logs it – and after `delay` seconds the thermostat's whole state comes back, retained,
with a fresh last_seen; …/get brings the state, too. A thermostat in `asleep` answers nothing.

Its plugs behave like the SONOFF S60ZBTPF (plug with power metering, a router): …/set takes state (ON, OFF, TOGGLE)
and power_on_behavior (off, on, toggle, previous); switched off, it measures nothing. What it measures is made up –
report() sets it, as if a heater behind it heated. join_kind says what joins during a search: a thermostat, a plug or
a lamp (light_kind: which one).

Its lamps behave as Zigbee2MQTT's light converters describe them (zigbee-herdsman-converters, read on 27.11.2026):
…/set takes state (ON, OFF, TOGGLE), brightness (0–254; it switches the lamp on, 0 off), brightness_step and
color_temp (in mired, kept within the lamp's range) and publishes the new state. Models after Zigbee2MQTT's device pages
(IKEA FLOALT, Innr RCL 240 T, Hue); their ranges of colour temperature are made up. A lamp in `asleep` has no power and
answers nothing.

Its motion sensors behave as the device pages of Zigbee2MQTT describe them (Hue motion sensor 1st and 2nd
generation, IKEA VALLHORN, read on 28.11.2026): move() is someone moving or no longer – occupancy, with the brightness
(illuminance, in lux) –; …/set takes motion_sensitivity, led_indication and occupancy_timeout (the Hue) and publishes
the new state. A sensor in `asleep` answers nothing.

A firmware update (bridge/request/device/ota_update/update) goes as the Zigbee2MQTT guide describes it (read on
25.11.2026): the device reports its progress `ota_steps` times, `ota_every` seconds apart; only at the end comes the
answer, with the firmware before and after – or with `ota_error`. When it restarts with the new firmware, the
thermostat forgets `ota_forgets` – as the real one did (1.1.0 → 1.1.3, 25.11.2026: „sanft“ off, switched off).
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
            "smart_temperature_control": "OFF", "heating_valve_position": 100, "idle_valve_position": 0,
            "temperature_accuracy": -1, "temperature_sensor_select": "local_temperature",
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


PLUG_MODEL = "S60ZBTPF"


def plug_state(power: float = 0.0, on: bool = True) -> dict:
    """A S60ZBTPF's state as Zigbee2MQTT publishes it (the meter readings are made up)."""
    return {"state": "ON" if on else "OFF", "power": power if on else 0, "current": round(power / 230, 2) if on else 0,
            "voltage": 231, "energy": 12.34, "energy_today": 0.42, "energy_yesterday": 1.8, "energy_month": 12.34,
            "power_on_behavior": "previous", "network_indicator": True, "outlet_control_protect": False,
            "overload_protection": 16, "linkquality": 168,
            "update": {"state": "idle", "installed_version": 4101, "latest_version": 4101}}


def plug(ieee: str, interview: str = "SUCCESSFUL") -> dict:
    """A SONOFF S60ZBTPF as bridge/devices lists it – a router on mains power, the definition only once interviewed."""
    done = interview == "SUCCESSFUL"
    numeric = lambda name, unit: {"type": "numeric", "name": name, "property": name, "unit": unit, "access": 5}  # noqa: E731
    return {"ieee_address": ieee, "type": "Router", "network_address": 0x6B12, "supported": done,
            "friendly_name": ieee, "disabled": False, "description": None,
            "definition": {"source": "native", "model": PLUG_MODEL, "vendor": "SONOFF",
                           "description": "Zigbee smart plug with power metering",
                           "exposes": [{"type": "switch", "features": [
                                           {"type": "binary", "name": "state", "property": "state", "access": 7,
                                            "value_on": "ON", "value_off": "OFF", "value_toggle": "TOGGLE"}]},
                                       {"type": "enum", "name": "power_on_behavior", "property": "power_on_behavior",
                                        "values": ["off", "on", "toggle", "previous"], "access": 7},
                                       numeric("power", "W"), numeric("current", "A"), numeric("voltage", "V"),
                                       numeric("energy", "kWh"), numeric("energy_today", "kWh"),
                                       {"type": "numeric", "name": "linkquality", "property": "linkquality",
                                        "access": 1}],
                           "supports_ota": True, "options": []} if done else None,
            "power_source": "Mains (single phase)", "software_build_id": "1.0.5" if done else None,
            "model_id": PLUG_MODEL, "manufacturer": "SONOFF", "interviewing": interview == "IN_PROGRESS",
            "interview_completed": done, "interview_state": interview, "endpoints": {}}


# three lamps as bridge/devices lists them: model id, maker, description, colour temperatures (mired), colour or not
LIGHTS = {"floalt": ("L1529", "IKEA", "FLOALT LED light panel, dimmable, white spectrum (60x60 cm)", (250, 454), False),
          "innr": ("RCL 240 T", "Innr", "Round ceiling light, white spectrum", (200, 454), False),
          "hue": ("9290012573A", "Philips", "Hue white and color ambiance E26/E27/E14", (153, 500), True)}
LIGHT_MODELS = {model for model, *_ in LIGHTS.values()}


def light_state(on: bool = False, brightness: int = 203, color_temp: int = 370) -> dict:
    """A lamp's state as Zigbee2MQTT publishes it."""
    return {"state": "ON" if on else "OFF", "brightness": brightness, "color_temp": color_temp, "color_mode": "color_temp",
            "power_on_behavior": "previous", "linkquality": 141,
            "update": {"state": "idle", "installed_version": 16777287, "latest_version": 16777287}}


def light(ieee: str, kind: str = "floalt", interview: str = "SUCCESSFUL") -> dict:
    """A lamp as bridge/devices lists it – a router on mains power, the definition only once it is interviewed."""
    model, vendor, description, (low, high), color = LIGHTS[kind]
    done = interview == "SUCCESSFUL"
    features = [{"type": "binary", "name": "state", "property": "state", "access": 7, "value_on": "ON",
                 "value_off": "OFF", "value_toggle": "TOGGLE"},
                {"type": "numeric", "name": "brightness", "property": "brightness", "access": 7, "value_min": 0,
                 "value_max": 254},
                {"type": "numeric", "name": "color_temp", "property": "color_temp", "access": 7, "unit": "mired",
                 "value_min": low, "value_max": high}]
    if color:
        features.append({"type": "composite", "name": "color_xy", "property": "color", "access": 7, "features": [
            {"type": "numeric", "name": "x", "property": "x", "access": 7},
            {"type": "numeric", "name": "y", "property": "y", "access": 7}]})
    return {"ieee_address": ieee, "type": "Router", "network_address": 0x2C31, "supported": done,
            "friendly_name": ieee, "disabled": False, "description": None,
            "definition": {"source": "native", "model": model, "vendor": vendor, "description": description,
                           "exposes": [{"type": "light", "features": features},
                                       {"type": "enum", "name": "effect", "property": "effect", "access": 2,
                                        "values": ["blink", "breathe", "okay", "finish_effect", "stop_effect"]},
                                       {"type": "enum", "name": "power_on_behavior", "property": "power_on_behavior",
                                        "values": ["off", "on", "toggle", "previous"], "access": 7},
                                       {"type": "numeric", "name": "linkquality", "property": "linkquality",
                                        "access": 1}],
                           "supports_ota": True, "options": []} if done else None,
            "power_source": "Mains (single phase)", "software_build_id": "1.0.21" if done else None,
            "model_id": model, "manufacturer": vendor, "interviewing": interview == "IN_PROGRESS",
            "interview_completed": done, "interview_state": interview, "endpoints": {}}


# four switches as bridge/devices lists them: Zigbee model id, Zigbee2MQTT's model, maker, description, the values of
# action (as on the device pages of Zigbee2MQTT)
DIMMER_ACTIONS = [f"{b}_{a}" for b in ("on", "off", "up", "down")
                  for a in ("press", "hold", "press_release", "hold_release")] + ["recall_0", "recall_1"]
BUTTONS = {"rodret": ("RODRET Dimmer", "E2201", "IKEA", "RODRET wireless dimmer/power switch",
                      ["on", "off", "brightness_move_up", "brightness_move_down", "brightness_stop"]),
           "smart": ("ROM001", "8718699693985", "Philips", "Hue smart button", ["on", "off", "press", "hold", "release"]),
           "dimmer": ("RWL022", "929002398602", "Philips", "Hue dimmer switch gen 2", DIMMER_ACTIONS),
           "sonoff": ("SNZB-01P", "SNZB-01P", "SONOFF", "Wireless button", ["single", "double", "long"])}
BUTTON_MODELS = {model_id for model_id, *_ in BUTTONS.values()}


def button_state(battery: int = 90) -> dict:
    """A switch's state as Zigbee2MQTT publishes it (without action: that comes with a press)."""
    return {"battery": battery, "voltage": 2900, "linkquality": 120}


def button(ieee: str, kind: str = "rodret", interview: str = "SUCCESSFUL") -> dict:
    """A switch as bridge/devices lists it – an end device on a battery that sleeps."""
    model_id, model, vendor, description, actions = BUTTONS[kind]
    done = interview == "SUCCESSFUL"
    return {"ieee_address": ieee, "type": "EndDevice", "network_address": 0x5D21, "supported": done,
            "friendly_name": ieee, "disabled": False, "description": None,
            "definition": {"source": "native", "model": model, "vendor": vendor, "description": description,
                           "exposes": [{"type": "numeric", "name": "battery", "property": "battery", "access": 1,
                                        "unit": "%", "value_min": 0, "value_max": 100},
                                       {"type": "enum", "name": "action", "property": "action", "access": 1,
                                        "values": actions},
                                       {"type": "numeric", "name": "linkquality", "property": "linkquality",
                                        "access": 1}],
                           "supports_ota": True, "options": []} if done else None,
            "power_source": "Battery", "software_build_id": "1.0.57" if done else None,
            "model_id": model_id, "manufacturer": vendor, "interviewing": interview == "IN_PROGRESS",
            "interview_completed": done, "interview_state": interview, "endpoints": {}}


# three motion sensors as bridge/devices lists them: Zigbee model id, Zigbee2MQTT's model, maker, description, the
# sensitivities it takes (None: none to set) – as on the device pages of Zigbee2MQTT
DETECTORS = {"hue": ("SML001", "9290012607", "Philips", "Hue motion sensor", ["low", "medium", "high"]),
             "hue2": ("SML003", "9290030675", "Philips", "Hue motion sensor",
                      ["low", "medium", "high", "very_high", "max"]),
             "vallhorn": ("VALLHORN Wireless Motion Sensor", "E2134", "IKEA", "VALLHORN wireless motion sensor", None)}
DETECTOR_MODELS = {model_id for model_id, *_ in DETECTORS.values()}


def detector_state(occupied: bool = False, lux: int = 12, battery: int = 100) -> dict:
    """A motion sensor's state as Zigbee2MQTT publishes it."""
    return {"occupancy": occupied, "illuminance": lux, "temperature": 20.4, "battery": battery,
            "motion_sensitivity": "high", "led_indication": False, "occupancy_timeout": 0, "linkquality": 116}


def detector(ieee: str, kind: str = "hue", interview: str = "SUCCESSFUL") -> dict:
    """A motion sensor as bridge/devices lists it – an end device on a battery that sleeps."""
    model_id, model, vendor, description, sensitivities = DETECTORS[kind]
    done = interview == "SUCCESSFUL"
    read = 5 if kind == "vallhorn" else 1
    exposes = [{"type": "binary", "name": "occupancy", "property": "occupancy", "access": read, "value_on": True,
                "value_off": False},
               {"type": "numeric", "name": "illuminance", "property": "illuminance", "access": 5, "unit": "lx"},
               {"type": "numeric", "name": "battery", "property": "battery", "access": read, "unit": "%",
                "value_min": 0, "value_max": 100},
               {"type": "enum", "name": "identify", "property": "identify", "access": 2, "values": ["identify"]},
               {"type": "numeric", "name": "linkquality", "property": "linkquality", "access": 1}]
    if sensitivities:   # the Hue
        exposes += [{"type": "numeric", "name": "temperature", "property": "temperature", "access": 1, "unit": "°C"},
                    {"type": "enum", "name": "motion_sensitivity", "property": "motion_sensitivity", "access": 7,
                     "values": sensitivities},
                    {"type": "binary", "name": "led_indication", "property": "led_indication", "access": 7,
                     "value_on": True, "value_off": False},
                    {"type": "numeric", "name": "occupancy_timeout", "property": "occupancy_timeout", "access": 7,
                     "value_min": 0, "value_max": 65535, "unit": "s"}]
    else:               # the VALLHORN
        exposes += [{"type": "numeric", "name": "voltage", "property": "voltage", "access": 5, "unit": "mV"},
                    {"type": "enum", "name": "lux_value", "property": "lux_value", "access": 7,
                     "values": ["always", "only_when_dark"]},
                    {"type": "numeric", "name": "timer", "property": "timer", "access": 7, "value_min": 10,
                     "value_max": 65534, "unit": "s"}]
    return {"ieee_address": ieee, "type": "EndDevice", "network_address": 0x6E32, "supported": done,
            "friendly_name": ieee, "disabled": False, "description": None,
            "definition": {"source": "native", "model": model, "vendor": vendor, "description": description,
                           "exposes": exposes, "supports_ota": True, "options": []} if done else None,
            "power_source": "Battery", "software_build_id": "2.53.6" if done else None,
            "model_id": model_id, "manufacturer": vendor, "interviewing": interview == "IN_PROGRESS",
            "interview_completed": done, "interview_state": interview, "endpoints": {}}


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
        self.unreported: dict[str, set[str]] = {}   # device -> values it has not said yet (an IKEA lamp its
                                                    # brightness): left out of its state until a …/get asks for them
        self.commands: list[tuple[str, str, dict]] = []   # (ieee, "set" | "get", payload) as they came
        self.refused: list[str] = []        # why a command was refused (Zigbee2MQTT only logs it)
        self.requests: list[tuple[str, dict]] = []   # (what, body) of every bridge/request/<what> from the app
        self.ota_steps = 4                  # progress reports during a firmware update …
        self.ota_every = 0.05               # … this many seconds apart (real: every 30 s, 10 to 100 minutes in all)
        self.ota_error: str | None = None   # why a firmware update fails (e.g. the device does not take part)
        self.ota_firmware = "1.1.4"         # the firmware (software_build_id) after an update
        self.ota_forgets = {"smart_temperature_control": "OFF", "system_mode": "off"}   # … and what it forgets then
        self.join_kind = "thermostat"       # what joins during a search: "thermostat", "plug", "light", "button" …
        self.light_kind = "floalt"          # … which lamp (see LIGHTS)
        self.button_kind = "rodret"         # … which switch (see BUTTONS)
        self.detector_kind = "hue"          # … which motion sensor (see DETECTORS)
        self.groups: dict[str, dict] = {}   # friendly name -> {id, members: [ieee]}
        self.next_group = 1
        self.binds: dict[str, set[str]] = {}   # switch -> the groups it is coupled with
        self.awake: dict[str, float] = {}   # switch -> until when (monotonic) it is awake after a press
        self.awake_for = 3.0                # seconds a switch stays awake after a press (real: a few)
        self.bind_wait = 0.2                # seconds Zigbee2MQTT waits for a sleeping switch before it gives up
        self.turns: dict[str, str] = {}     # switch -> what it sent last by turns (Hue Smart Button, Dimmschalter)
        self.hears_on_off = True            # False: coupled, the Smart Button no longer tells the stick on / off
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
        self._groups()
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

    def _groups(self) -> None:
        with self.lock:
            groups = [{"id": g["id"], "friendly_name": name, "scenes": [],
                       "members": [{"ieee_address": m, "endpoint": 1} for m in g["members"]]}
                      for name, g in self.groups.items()]
        self.broker.publish(f"{self.BASE}/bridge/groups", groups, retain=True)

    def _event(self, kind: str, data: dict) -> None:
        self.broker.publish(f"{self.BASE}/bridge/event", {"type": kind, "data": data})

    def _fresh(self, ieee: str) -> dict:
        """The state a device starts with – a plug's, a lamp's or a thermostat's."""
        model = self._model(ieee)
        return (plug_state() if model == PLUG_MODEL else light_state() if model in LIGHT_MODELS
                else button_state() if model in BUTTON_MODELS else detector_state() if model in DETECTOR_MODELS
                else trv_state())

    def _model(self, ieee: str) -> str | None:
        with self.lock:
            return next((d["model_id"] for d in self.devices if d["ieee_address"] == ieee), None)

    def _is_plug(self, ieee: str) -> bool:
        return self._model(ieee) == PLUG_MODEL

    def _values(self, ieee: str, asked=()) -> None:
        """The device's whole state, retained – as after every message from it; what it has not said yet (unreported)
        only once a …/get asked for it."""
        with self.lock:
            state = self.states.setdefault(ieee, self._fresh(ieee))
            state["last_seen"] = datetime.now().astimezone().isoformat(timespec="seconds")
            hidden = self.unreported.get(ieee, set())
            hidden.difference_update(asked)
            payload = {k: v for k, v in state.items() if k not in hidden}
        self.broker.publish(f"{self.BASE}/{ieee}", payload, retain=True)

    def report(self, ieee: str, **values) -> None:
        """The device reports by itself (someone turned the wheel, the room cooled down, the heater behind a plug
        heats, someone pressed the plug's button …)."""
        with self.lock:
            self.states.setdefault(ieee, self._fresh(ieee)).update(values)
        self._values(ieee)

    # a person at a motion sensor
    def move(self, ieee: str, occupied: bool = True, lux: int | None = None) -> None:
        """Someone begins to move in front of the sensor (occupied) – or it says that the motion is over; lux: the
        brightness it measured with it."""
        with self.lock:
            state = self.states.setdefault(ieee, detector_state())
            state["occupancy"] = occupied
            if lux is not None:
                state["illuminance"] = lux
        self._values(ieee)

    # a person at a switch
    def press(self, ieee: str, button: str = "") -> None:
        """Someone presses a switch (it wakes up for awake_for seconds): it tells the lamps of the groups it is coupled
        with by itself, and Zigbee2MQTT publishes what was pressed (action) – retained, as with retain: true.
        RODRET: button on, off, up (held: brightness_move_up … brightness_stop), down. Hue Smart Button: press, release,
        then on or off by turns. Hue Dimmschalter: on (on_press, on or off by turns, on_press_release), up, down.
        SONOFF SNZB-01P: single – coupled it does nothing."""
        with self.lock:
            self.awake[ieee] = time.monotonic() + self.awake_for
            model = self._model(ieee)
        kind = next(k for k, b in BUTTONS.items() if b[0] == model)

        def turn() -> str:
            with self.lock:
                self.turns[ieee] = "OFF" if self.turns.get(ieee, "OFF") == "ON" else "ON"
                return self.turns[ieee]
        if kind == "rodret":
            if button in ("on", "off"):
                self._tell(ieee, button, {"state": button.upper()})
            else:
                self._tell(ieee, f"brightness_move_{button}", {"brightness_step": 40 if button == "up" else -40,
                                                               **({"state": "ON"} if button == "up" else {})})
                self._tell(ieee, "brightness_stop", None)
        elif kind == "smart":
            self._tell(ieee, "press", None)
            self._tell(ieee, "release", None)
            said = turn()
            self._tell(ieee, said.lower() if self.hears_on_off else None, {"state": said})
        elif kind == "dimmer":   # the command goes out when the button is let go (a short press, not a hold)
            self._tell(ieee, f"{button}_press", None)
            self._tell(ieee, f"{button}_press_release", {"state": turn()} if button == "on" else
                       {"brightness_step": 30 if button == "up" else -30} if button in ("up", "down") else None)
        else:
            self._tell(ieee, "single", None)

    def _tell(self, ieee: str, action: str | None, command: dict | None) -> None:
        """One message of a press: the action to the stick, the command to the lamps of the switch's groups – their
        reports of the new state come after."""
        if action:
            with self.lock:
                state = self.states.setdefault(ieee, button_state())
                state["last_seen"] = datetime.now().astimezone().isoformat(timespec="seconds")
                payload = {**state, "action": action}
            self.broker.publish(f"{self.BASE}/{ieee}", payload, retain=True)
        with self.lock:
            lamps = [m for g in sorted(self.binds.get(ieee, ())) for m in (self.groups.get(g) or {}).get("members", [])]
            devices = {d["ieee_address"]: d for d in self.devices}
        if command and self._model(ieee) != "SNZB-01P":   # the SNZB-01P's coupling does nothing
            for lamp in lamps:
                if lamp not in self.asleep and lamp in devices:
                    later(self.delay, lambda lamp=lamp: self._apply_light(lamp, dict(command), devices[lamp]))

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
        later(self.delay, lambda: self._apply(ieee, body) if kind == "set" else self._values(ieee, asked=body))

    def _apply_plug(self, ieee: str, body: dict) -> None:
        """What the S60ZBTPF's converters do with a set: state and power_on_behavior; switched off it measures 0 W."""
        with self.lock:
            state = self.states.setdefault(ieee, plug_state())
            new = dict(state)
            for key, value in body.items():
                if key == "state" and isinstance(value, str) and value.upper() in ("ON", "OFF", "TOGGLE"):
                    value = value.upper()
                    new["state"] = ("OFF" if new["state"] == "ON" else "ON") if value == "TOGGLE" else value
                elif key == "power_on_behavior" and value in ("off", "on", "toggle", "previous"):
                    new[key] = value
                else:
                    self.refused.append(f"No converter available for '{key}'" if key not in ("state", "power_on_behavior")
                                        else f"Invalid value {value} for {key}")
                    return
            if new["state"] == "OFF":
                new.update(power=0, current=0)
            state.clear()
            state.update(new)
        self._values(ieee)

    def _apply_light(self, ieee: str, body: dict, device: dict) -> None:
        """What the light converters do with a set: state, brightness (switches on; 0 off), brightness_step,
        color_temp (within the lamp's range) and – a lamp with colours – color as hue and saturation; transition is
        taken and ignored. Then the lamp's new state comes back, with what the set said."""
        features = device["definition"]["exposes"][0]["features"]
        temp = next(f for f in features if f["property"] == "color_temp")
        colors = any(f["property"] == "color" for f in features)
        number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
        with self.lock:
            state = self.states.setdefault(ieee, light_state())
            new = dict(state)
            for key, value in body.items():
                if key == "state" and isinstance(value, str) and value.upper() in ("ON", "OFF", "TOGGLE"):
                    value = value.upper()
                    new["state"] = ("OFF" if new["state"] == "ON" else "ON") if value == "TOGGLE" else value
                elif key == "brightness" and number(value) and 0 <= value <= 254:
                    new["brightness"], new["state"] = (round(value), "ON") if value else (new["brightness"], "OFF")
                elif key == "brightness_step" and number(value):
                    level = max(0, min(254, new["brightness"] + round(value)))
                    new["brightness"], new["state"] = (level, "ON") if level else (new["brightness"], "OFF")
                elif key == "color_temp" and number(value):
                    new["color_temp"] = min(max(round(value), temp["value_min"]), temp["value_max"])
                    new["color_mode"] = "color_temp"
                elif key == "color" and colors and isinstance(value, dict) and number(value.get("hue")) \
                        and number(value.get("saturation")):
                    new["color"] = {"hue": value["hue"] % 360, "saturation": min(max(value["saturation"], 0), 100)}
                    new["color_mode"] = "hs"
                elif key != "transition":
                    self.refused.append(f"No converter available for '{key}'" if key not in
                                        ("state", "brightness", "brightness_step", "color_temp", "color") or
                                        (key == "color" and not colors) else f"Invalid value {value} for {key}")
                    return
            state.clear()
            state.update(new)
        self._values(ieee, asked=body)   # what it was told, Zigbee2MQTT knows

    def _apply_detector(self, ieee: str, body: dict, device: dict) -> None:
        """What a motion sensor's converters do with a set: motion_sensitivity (one of its values), led_indication,
        occupancy_timeout – as far as it has them; then its new state."""
        exposes = {e["property"]: e for e in device["definition"]["exposes"]}
        with self.lock:
            state = self.states.setdefault(ieee, detector_state())
            for key, value in body.items():
                e = exposes.get(key)
                good = e is not None and e["access"] & 2 and (
                    value in e["values"] if e["type"] == "enum" else isinstance(value, bool) if e["type"] == "binary"
                    else isinstance(value, int) and not isinstance(value, bool))
                if not good:
                    self.refused.append(f"No converter available for '{key}'" if e is None else
                                        f"Invalid value {value} for {key}")
                    return
                state[key] = value
        self._values(ieee)

    def _apply(self, ieee: str, body: dict) -> None:
        """What the TRV-ZBT's converters do with a set: check each value, write it, then publish the state."""
        with self.lock:
            device = next((d for d in self.devices if d["ieee_address"] == ieee), None)
        if device is None:   # removed while the command was on its way: nothing happens
            return
        if device["model_id"] == PLUG_MODEL:
            return self._apply_plug(ieee, body)
        if device["model_id"] in LIGHT_MODELS:
            return self._apply_light(ieee, body, device)
        if device["model_id"] in DETECTOR_MODELS:
            return self._apply_detector(ieee, body, device)
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
                elif key in ("heating_valve_position", "idle_valve_position"):
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
                        new[key] = value
                    else:
                        error = f"{key} {value} out of range"
                elif key == "temperature_accuracy":
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and -1 <= value <= -0.2:
                        new[key] = round(value, 1)
                    else:
                        error = f"{key} {value} out of range"
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
        if what in ("device/bind", "device/unbind"):
            return self._bind(what, body)
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
        elif what.startswith("group/"):
            answer = self._group_request(what, body)
        else:
            answer = {"data": {}, "status": "error", "error": f"Unknown request '{what}'"}
        if "transaction" in body:
            answer["transaction"] = body["transaction"]
        self.broker.publish(f"{self.BASE}/bridge/response/{what}", answer)

    def _group_request(self, what: str, body: dict) -> dict:
        """group/add, group/remove, group/members/add, group/members/remove – a lamp that is asleep (no power) cannot
        be put into a group or taken out."""
        with self.lock:
            if what == "group/add":
                name = body.get("friendly_name")
                if not isinstance(name, str) or name in self.groups:
                    return {"data": {}, "status": "error", "error": f"friendly_name '{name}' is already in use"}
                gid = body.get("id") or self.next_group
                self.next_group = max(self.next_group, gid) + 1
                self.groups[name] = {"id": gid, "members": []}
                data = {"id": gid, "friendly_name": name}
            elif what == "group/remove":
                name = body.get("id")
                if name not in self.groups:
                    return {"data": {}, "status": "error", "error": f"Group '{name}' does not exist"}
                self.groups.pop(name)
                data = {"id": name, "force": body.get("force") is True}
            elif what in ("group/members/add", "group/members/remove"):
                name, device = body.get("group"), body.get("device")
                if name not in self.groups:
                    return {"data": {}, "status": "error", "error": f"Group '{name}' does not exist"}
                if not any(d["ieee_address"] == device for d in self.devices):
                    return {"data": {}, "status": "error", "error": f"Device '{device}' does not exist"}
                if device in self.asleep:
                    return {"data": {}, "status": "error", "error": f"Failed to {what.rsplit('/', 1)[1]} '{device}' "
                                                                     f"(Delivery failed for '{device}')"}
                members = self.groups[name]["members"]
                if what.endswith("add") and device not in members:
                    members.append(device)
                elif what.endswith("remove") and device in members:
                    members.remove(device)
                data = {"device": device, "endpoint": "default", "group": name}
            else:
                return {"data": {}, "status": "error", "error": f"Unknown request '{what}'"}
        self._groups()
        return {"data": data, "status": "ok"}

    def _bind(self, what: str, body: dict) -> None:
        """device/bind and device/unbind of a switch with a group: it takes it only while it is awake – pressed just
        before, or within bind_wait seconds after the request."""
        src, target, clusters = body.get("from"), body.get("to"), body.get("clusters") or ["genOnOff", "genLevelCtrl"]
        verb = what.split("/")[1]
        topic = f"{self.BASE}/bridge/response/{what}"
        extra = {"transaction": body["transaction"]} if "transaction" in body else {}
        with self.lock:
            known = any(d["ieee_address"] == src for d in self.devices)
            group = target in self.groups
        if not known:
            return self.broker.publish(topic, {"data": {}, "status": "error", "error": f"Source device '{src}' does not exist", **extra})
        if not group:
            return self.broker.publish(topic, {"data": {}, "status": "error", "error": f"Target '{target}' does not exist", **extra})

        def finish() -> None:
            with self.lock:
                awake = self.awake.get(src, 0) > time.monotonic()
                if awake:
                    coupled = self.binds.setdefault(src, set())
                    (coupled.add if verb == "bind" else coupled.discard)(target)
            if awake:
                return self.broker.publish(topic, {"data": {"from": src, "from_endpoint": "default", "to": target,
                                                            "clusters": clusters, "failed": []}, "status": "ok", **extra})
            self.broker.publish(topic, {"data": {}, "status": "error", **extra, "error":
                                        f"Failed to {verb} cluster '{clusters[0]}' from '{src}' to '{target}' "
                                        f"(Error: Delivery failed for '{src}')"})
        with self.lock:
            awake = self.awake.get(src, 0) > time.monotonic()
        if awake:
            finish()
        else:
            later(self.bind_wait, finish)

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
                state = self.states.setdefault(ieee, trv_state())
                plans = self.plans.setdefault(ieee, {g: {d: state[f"weekly_schedule_{d}"] for d in DAYS} for g in "123"})
                for key, value in self.ota_forgets.items():   # a forgotten plan is gone from the thermostat too
                    if key.startswith("weekly_schedule_"):
                        plans[state["schedule_group_to_edit"]][key[16:]] = value
                forgets = {**self.ota_forgets, **({"running_state": "idle"}
                                                 if self.ota_forgets.get("system_mode") == "off" else {})}
            self.report(ieee, update={"state": "idle", "installed_version": latest, "latest_version": latest},
                        **forgets)
            self._devices()
            self.broker.publish(topic, {**answer, "status": "ok", "data": {
                "id": ieee, "from": {"software_build_id": old, "file_version": before},
                "to": {"software_build_id": self.ota_firmware, "file_version": latest}}})

        later(self.ota_every, lambda: step(0))

    def _join(self) -> None:
        """A thermostat whose middle button was held for 2 s (a plug whose button was held for 5 s, a lamp that was
        reset): it joins, is interviewed, reports its values."""
        kind, switch, sensor = self.light_kind, self.button_kind, self.detector_kind
        make = plug if self.join_kind == "plug" else (lambda i, *a: light(i, kind, *a)) if self.join_kind == "light" \
            else (lambda i, *a: button(i, switch, *a)) if self.join_kind == "button" \
            else (lambda i, *a: detector(i, sensor, *a)) if self.join_kind == "detector" else thermostat
        with self.lock:
            if not (self.online and self.permit_end and self.permit_end > time.time()):
                return
            ieee = f"0x{self.next_ieee:016x}"
            self.next_ieee += 1
            device = make(ieee, "IN_PROGRESS")
            self.devices.append(device)
        self._event("device_joined", {"friendly_name": ieee, "ieee_address": ieee})
        self._event("device_interview", {"friendly_name": ieee, "status": "started", "ieee_address": ieee})
        self._devices()

        def done() -> None:
            finished = make(ieee)
            with self.lock:
                if device not in self.devices:
                    return
                self.devices[self.devices.index(device)] = finished
            self._event("device_interview", {"friendly_name": ieee, "status": "successful", "ieee_address": ieee,
                                             "supported": True, "definition": finished["definition"]})
            self._devices()
            self._values(ieee)

        later(self.interview, done)
