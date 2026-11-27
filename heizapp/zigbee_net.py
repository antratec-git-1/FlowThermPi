"""Das Zigbee-Netz aus Sicht der App: über Mosquitto mit Zigbee2MQTT reden – Geräte suchen, zeigen, entfernen.

Beim Installieren legt install.py die Anmeldung der App bei Mosquitto in zigbee-mqtt.json ab (im Datenordner, nur für
die App lesbar). Damit verbindet sich die App mit Mosquitto (nur auf diesem Pi) und liest, was Zigbee2MQTT meldet: ob
es läuft (bridge/state), Version und Funkkanal (bridge/info), die angelernten Geräte (bridge/devices), was gerade
passiert (bridge/event) und die Werte jedes Geräts (zigbee2mqtt/<IEEE-Adresse>). „Geräte suchen“ öffnet das Netz
für 3 Minuten (bridge/request/permit_join), „Entfernen“ meldet ein Gerät ab (bridge/request/device/remove).

Von jedem Thermostat hält die App alle Werte, die sie braucht (THERMOSTAT_KEYS), von jeder Steckdose mit Strommessung
die ihren (PLUG_KEYS) – und wann sich das Gerät zuletzt gemeldet hat. Befehle an ein Gerät gehen als
zigbee2mqtt/<IEEE-Adresse>/set hinaus, Nachfragen als …/get; was ein Thermostat daraus macht, steht in heizapp.trv.
Eine Steckdose gilt als Steckdose, wenn sie einen Schalter (state) und eine Leistungsmessung (power) hat – so wie die
SONOFF S60ZBTPF; die App ordnet sie einer AeroFlow zu (heizapp.service). Eine Lampe ist, was Zigbee2MQTT als Licht
(„light“) mit einem Schalter (state) beschreibt – Hue, IKEA FLOALT und JETSTRÖM, Innr und andere, ohne Liste der
Modelle; dazu, ob sie sich dimmen lässt (brightness) und welche Lichtfarben sie kann (color_temp, in Mired). Die App
hält von ihr LIGHT_KEYS.

„Firmware einspielen“ bittet Zigbee2MQTT darum (bridge/request/device/ota_update/update). Zigbee2MQTT antwortet erst,
wenn das Update vorbei ist – nach 10 bis 100 Minuten. Bis dahin meldet das Gerät alle 30 Sekunden, wie weit es ist
(update.progress, update.remaining). Es gibt immer nur ein Update zur Zeit, wie Zigbee2MQTT es empfiehlt.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

from . import logs
from .mqtt import Client, MqttError
from .service import ActionError

log = logging.getLogger("heizapp.zigbee")
LOGIN = "zigbee-mqtt.json"
BASE = "zigbee2mqtt"
SEARCH = 180               # seconds the network stays open for new thermostats (Zigbee2MQTT allows up to 254)
ANSWER = 10.0              # seconds Zigbee2MQTT has to answer a request …
REMOVE = 45.0              # … to remove a device: it waits for the device's answer first
RETRY = (2.0, 5.0, 10.0, 30.0, 60.0)   # pauses between attempts to reach Mosquitto
NEW = 15 * 60              # seconds a device that has just joined is marked as new
MAX_LOGIN = 4096
MAX_DEVICES = 200
IEEE = re.compile(r"0x[0-9a-f]{16}")
TOPICS = [f"{BASE}/bridge/state", f"{BASE}/bridge/info", f"{BASE}/bridge/devices", f"{BASE}/bridge/event",
          f"{BASE}/bridge/response/#", f"{BASE}/+"]
INTERVIEW = {"PENDING": "pending", "IN_PROGRESS": "running", "SUCCESSFUL": "done", "FAILED": "failed"}
# what the app keeps of a thermostat's state (the names of the TRV-ZBT in Zigbee2MQTT, see heizapp.trv)
THERMOSTAT_KEYS = frozenset({
    "local_temperature", "occupied_heating_setpoint", "system_mode", "running_state", "local_temperature_calibration",
    "frost_protection_temperature", "open_window", "open_window_detected", "child_lock", "battery", "linkquality",
    "fault_status", "temporary_mode_mode", "temporary_mode_duration", "temporary_mode_target_temperature",
    "schedule_active_group", "schedule_group_to_edit", "weekly_schedule_status", "temperature_sensor_select",
    "external_temperature_input", "smart_temperature_control", "heating_valve_position", "idle_valve_position",
    "temperature_accuracy", "update", "last_seen",
    *(f"weekly_schedule_{day}" for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                                           "sunday"))})
# … and of a plug with power metering (the names of the SONOFF S60ZBTPF in Zigbee2MQTT; others use the same)
PLUG_KEYS = frozenset({"state", "power", "current", "voltage", "energy", "energy_today", "energy_yesterday",
                       "energy_month", "power_on_behavior", "linkquality", "update", "last_seen"})
# … and of a lamp (the names Zigbee2MQTT uses for every light: brightness 0–254, color_temp in mired)
LIGHT_KEYS = frozenset({"state", "brightness", "color_temp", "color_mode", "power_on_behavior", "linkquality", "update",
                        "last_seen"})
KEPT_KEYS = THERMOSTAT_KEYS | PLUG_KEYS | LIGHT_KEYS
MIRED = (50, 1000)         # colour temperatures accepted from a lamp, in mired (20000 K to 1000 K)
MAX_POWER = 50000          # W accepted from a plug (the S60ZBTPF takes up to 4000 W)
MAX_ENERGY = 10 ** 7       # kWh accepted from a plug's meter
UPDATE_KEYS = ("state", "installed_version", "latest_version", "progress", "remaining")
OTA = "device/ota_update/update"   # Zigbee2MQTT answers this request only when the firmware update is over
OTA_START = 10 * 60        # seconds Zigbee2MQTT may take to begin a firmware update before the app gives up on it …
OTA_GRACE = 2 * 60         # … and to answer after the device stopped reporting its progress
OTA_KEEP = 24 * 3600       # seconds the result of a firmware update stays on the page


def _json(payload: bytes):
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _wh(kwh):
    return round(kwh * 1000, 3) if kwh is not None else None


def _text(value, limit: int = 120) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _number(value, low: float, high: float):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if low <= value <= high else None


def _plain(value):
    """A value of a device as the app keeps it – only simple values, short texts and the firmware update's state."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:300]
    if isinstance(value, dict):
        return {k: value[k] for k in UPDATE_KEYS if isinstance(value.get(k), (str, int, float))}
    return None


def _seen(value) -> float | None:
    """last_seen as Zigbee2MQTT writes it – ISO 8601 or milliseconds since 1970 – as time.time()."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str):
        try:
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (at if at.tzinfo else at.astimezone()).timestamp()
    return None


def _reason(exc: BaseException) -> str:
    """Why Mosquitto could not be reached, in words."""
    if isinstance(exc, MqttError):
        return str(exc)
    if isinstance(exc, ConnectionRefusedError):
        return "Mosquitto läuft nicht (Verbindung abgelehnt)"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Mosquitto antwortet nicht"
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


def _failure(text: str) -> str:
    """Zigbee2MQTT's error (in English) in plain words for the page – its details go to the log."""
    if re.search(r"time(d)? ?out|after \d+ ?ms|no response|delivery failed|mac_no_ack|nwk_no_route", text, re.I):
        return "Das Gerät hat nicht geantwortet – vielleicht schläft es gerade oder ist zu weit vom Pi weg."
    return f"Zigbee2MQTT meldet: {text}"


def _ota_failure(text: str) -> str:
    """Why a firmware update failed, in plain words for the page – Zigbee2MQTT's own words go to the log."""
    if re.search(r"did(n't| not) respond|no response|time(d)? ?out|delivery failed|mac_no_ack|nwk_no_route", text, re.I):
        return "Das Thermostat hat nicht geantwortet – bitte später noch einmal versuchen."
    if re.search(r"battery", text, re.I):
        return "Die Batterie ist zu schwach für ein Update – bitte zuerst neue Batterien einsetzen."
    if re.search(r"abort", text, re.I):
        return "Das Thermostat hat das Update abgebrochen – bitte später noch einmal versuchen."
    return f"Zigbee2MQTT meldet: {text}"


def _version(side) -> str:
    """The firmware before or after an update, as Zigbee2MQTT's answer gives it (software_build_id is optional)."""
    side = side if isinstance(side, dict) else {}
    return str(side.get("software_build_id") or side.get("file_version") or "?")


def _features(e: dict) -> list:
    found = e.get("features")
    return [f for f in found if isinstance(f, dict)] if isinstance(found, list) else []


def _measuring_plug(exposes: list) -> bool:
    """A plug that measures: one switch (state – not state_l1 … of a switch with several outlets) and the power it
    lets through (power, in W)."""
    switch = any(e.get("type") == "switch" and any(f.get("property") == "state" for f in _features(e))
                 for e in exposes if isinstance(e, dict))
    power = any(e.get("type") == "numeric" and e.get("property") == "power" for e in exposes if isinstance(e, dict))
    return switch and power


def _light(exposes: list) -> dict | None:
    """A lamp: one light with a switch (state – not state_l1 … of a device with several lights) – and what it can do
    besides on and off: dim (brightness) and which colour temperatures (color_temp, its range in mired). None: no
    lamp."""
    for e in exposes:
        if not (isinstance(e, dict) and e.get("type") == "light"):
            continue
        found = {f.get("property"): f for f in _features(e)}
        if "state" not in found:
            continue
        temp = found.get("color_temp")
        low = _number(temp.get("value_min"), *MIRED) if temp else None
        high = _number(temp.get("value_max"), *MIRED) if temp else None
        return {"dim": "brightness" in found,
                "temp": [low or 153, high or 500] if temp and (low or 153) < (high or 500) else None,
                "color": "color" in found}
    return None


def _definition(item: dict, device: dict) -> None:
    """Model, maker and what the device can do – from bridge/devices or the event of a finished interview."""
    definition = item.get("definition") if isinstance(item.get("definition"), dict) else None
    if definition is None:
        return
    exposes = definition.get("exposes") if isinstance(definition.get("exposes"), list) else []
    thermostat = any(isinstance(e, dict) and e.get("type") == "climate" for e in exposes)
    light = None if thermostat else _light(exposes)
    device.update(model=_text(definition.get("model"), 60) or device.get("model"),
                  vendor=_text(definition.get("vendor"), 60) or device.get("vendor"),
                  description=_text(definition.get("description"), 160),
                  thermostat=thermostat, plug=not thermostat and light is None and _measuring_plug(exposes),
                  light=light is not None, light_caps=light)
    if isinstance(item.get("supported"), bool):
        device["supported"] = item["supported"]


class ZigbeeNet:
    """The connection to Zigbee2MQTT, in a thread of its own that comes back after every break."""

    def __init__(self, folder: str | Path | None, retry: tuple[float, ...] = RETRY):
        self.folder = Path(folder) if folder else None
        self.retry = retry
        self.answer, self.remove_wait, self.search_time = ANSWER, REMOVE, SEARCH
        self._lock = threading.Lock()                # guards the fields below
        self._news = threading.Condition(self._lock)  # signalled when Zigbee2MQTT answers a request
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._client: Client | None = None
        self._broker = "off"        # off: no login yet (programs not installed) · connecting · connected · error
        self._problem: str | None = None   # why Mosquitto cannot be reached
        self._bridge: str | None = None    # Zigbee2MQTT: "online" | "offline" | None (not heard of)
        self._info: dict = {}
        self._devices: dict[str, dict] = {}   # ieee -> device, in Zigbee2MQTT's order (the order of pairing)
        self._values: dict[str, dict] = {}    # ieee -> the device's latest values, as the Zigbee page shows them
        self._states: dict[str, dict] = {}    # ieee -> {values, heard, seq, boost_since}: what a thermostat said last
        self._seq = 0                         # number of the latest message from any device
        self._joined: dict[str, float] = {}   # ieee -> when it joined
        self._permit_end: float | None = None   # time.time() at which the network closes again
        self._waiting: dict[str, dict | None] = {}   # transaction -> Zigbee2MQTT's answer, None until it comes
        # ieee -> firmware update: {transaction, started, firmware (before), seen (last progress report), progress,
        # result: None until it is over, then {ok, message, at}}
        self._ota: dict[str, dict] = {}

    # ------------------------------------------------------------------ the thread
    def start(self) -> None:
        if self.folder is None or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="zigbee-mqtt", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            client = self._client
        if client:
            client.close()

    def wake(self) -> None:
        """Try at once (e.g. right after the installation) instead of after the pause."""
        self._wake.set()

    def _pause(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _run(self) -> None:
        failures, said = 0, None
        while not self._stop.is_set():
            login = self._login()
            if login is None:   # the programs are not installed (yet)
                with self._lock:
                    self._broker, self._problem = "off", None
                self._pause(10.0)
                continue
            client = Client(login["host"], login["port"], login["user"], login["password"],
                            client_id=f"flowthermpi-{secrets.token_hex(4)}", on_message=self._message)
            with self._lock:
                self._broker = "connecting"
            try:
                client.connect()
                client.subscribe(TOPICS)
            except (MqttError, OSError, ValueError) as exc:
                client.close()
                problem = _reason(exc)
                if problem != said:
                    log.warning("Zigbee: keine Verbindung zu Mosquitto – %s", problem)
                    said = problem
                with self._lock:
                    self._broker, self._problem = "error", problem
                self._pause(self.retry[min(failures, len(self.retry) - 1)])
                failures += 1
                continue
            with self._lock:
                self._client, self._broker, self._problem = client, "connected", None
            log.info("Zigbee: mit Mosquitto verbunden")
            failures, said = 0, None
            try:
                client.loop(self._stop)
            except (MqttError, OSError, ValueError) as exc:
                if not self._stop.is_set():
                    log.warning("Zigbee: Verbindung zu Mosquitto unterbrochen – %s", _reason(exc))
            finally:
                client.close()
                with self._news:
                    self._client, self._broker, self._bridge = None, "connecting", None
                    self._news.notify_all()
            self._pause(self.retry[0])

    def _login(self) -> dict | None:
        """The app's login at Mosquitto, as install.py left it; None while there is none."""
        try:
            with open(self.folder / LOGIN, "rb") as fh:
                raw = fh.read(MAX_LOGIN + 1)
            login = json.loads(raw) if len(raw) <= MAX_LOGIN else None
        except (OSError, ValueError):
            return None
        if not (isinstance(login, dict) and isinstance(login.get("host"), str) and isinstance(login.get("user"), str)
                and isinstance(login.get("password"), str) and isinstance(login.get("port"), int)
                and 0 < login["port"] < 65536):
            return None
        logs.keep_secret(login["password"])
        return login

    # ------------------------------------------------------------------ what Zigbee2MQTT says
    def _message(self, topic: str, payload: bytes, retained: bool) -> None:
        if not topic.startswith(BASE + "/"):
            return
        name = topic[len(BASE) + 1:]
        if name == "bridge/state":
            data = _json(payload)
            state = data.get("state") if isinstance(data, dict) else payload.decode("utf-8", "replace").strip()
            self._take_state(state if state in ("online", "offline") else None)
        elif name == "bridge/info":
            self._take_info(_json(payload))
        elif name == "bridge/devices":
            self._take_devices(_json(payload))
        elif name == "bridge/event":
            self._take_event(_json(payload))
        elif name.startswith("bridge/response/"):
            self._take_answer(_json(payload))
        elif "/" not in name and name != "bridge":
            self._take_values(name, _json(payload))

    def _take_state(self, state: str | None) -> None:
        with self._lock:
            before, self._bridge = self._bridge, state
            if state != "online":
                self._permit_end = None
        if state != before and state:
            log.info("Zigbee: Zigbee2MQTT %s", "läuft" if state == "online" else "ist aus")

    def _take_info(self, data) -> None:
        if not isinstance(data, dict):
            return
        network = data.get("network") if isinstance(data.get("network"), dict) else {}
        coordinator = data.get("coordinator") if isinstance(data.get("coordinator"), dict) else {}
        end = _number(data.get("permit_join_end"), 0, 1e15)
        with self._lock:
            self._info = {"version": _text(data.get("version"), 40),
                          "channel": _number(network.get("channel"), 11, 26),
                          "coordinator": _text(coordinator.get("type"), 40),
                          "restart_required": data.get("restart_required") is True}
            if data.get("permit_join") is True:
                self._permit_end = end / 1000 if end else self._permit_end or time.time() + self.search_time
            else:
                self._permit_end = None

    def _take_devices(self, data) -> None:
        if not isinstance(data, list):
            return
        devices: dict[str, dict] = {}
        for item in data:
            if len(devices) >= MAX_DEVICES:
                break
            if not isinstance(item, dict) or item.get("type") == "Coordinator":
                continue
            ieee = item.get("ieee_address")
            if not (isinstance(ieee, str) and IEEE.fullmatch(ieee)):
                continue
            state = INTERVIEW.get(item.get("interview_state"))
            if state is None:
                state = ("running" if item.get("interviewing") is True
                         else "done" if item.get("interview_completed") is True else "pending")
            device = {"ieee": ieee, "name": _text(item.get("friendly_name"), 80) or ieee,
                      "type": item.get("type") if item.get("type") in ("Router", "EndDevice") else None,
                      "model": _text(item.get("model_id"), 60), "vendor": _text(item.get("manufacturer"), 60),
                      "description": None, "supported": item.get("supported") is True, "thermostat": False,
                      "plug": False, "light": False, "light_caps": None, "interview": state,
                      "power": _text(item.get("power_source"), 40),
                      "firmware": _text(item.get("software_build_id"), 40)}
            _definition(item, device)
            devices[ieee] = device
        with self._lock:
            for ieee in set(self._devices) - set(devices):
                self._values.pop(ieee, None)
                self._states.pop(ieee, None)
                self._ota.pop(ieee, None)
            self._devices = devices

    def _take_event(self, data) -> None:
        if not (isinstance(data, dict) and isinstance(data.get("data"), dict)):
            return
        kind, info = data.get("type"), data["data"]
        ieee = info.get("ieee_address")
        if not (isinstance(ieee, str) and IEEE.fullmatch(ieee)):
            return
        said = None
        with self._lock:
            device = self._devices.get(ieee)
            if kind in ("device_joined", "device_interview") and device is None and len(self._devices) < MAX_DEVICES:
                device = self._devices[ieee] = {
                    "ieee": ieee, "name": _text(info.get("friendly_name"), 80) or ieee, "type": None, "model": None,
                    "vendor": None, "description": None, "supported": False, "thermostat": False, "plug": False,
                    "light": False, "light_caps": None, "interview": "pending", "power": None, "firmware": None}
            if kind == "device_joined":
                self._joined[ieee] = time.time()
                said = f"neues Gerät {ieee} meldet sich an"
            elif kind == "device_interview" and device is not None:
                status = {"started": "running", "successful": "done", "failed": "failed"}.get(info.get("status"))
                if status:
                    device["interview"] = status
                    _definition(info, device)
                    if status != "running":
                        what = " ".join(filter(None, (device["vendor"], device["model"]))) or "unbekanntes Modell"
                        said = (f"Gerät {ieee} angelernt – {what}" if status == "done"
                                else f"Anlernen von {ieee} hat nicht geklappt")
            elif kind == "device_leave":
                self._devices.pop(ieee, None)
                self._values.pop(ieee, None)
                self._states.pop(ieee, None)
                self._ota.pop(ieee, None)
                said = f"Gerät {ieee} hat das Netz verlassen"
        if said:
            log.info("Zigbee: %s", said)

    def _take_answer(self, data) -> None:
        if not isinstance(data, dict):
            return
        transaction = data.get("transaction")
        if not isinstance(transaction, str):
            return
        with self._news:
            if transaction in self._waiting and self._waiting[transaction] is None:
                self._waiting[transaction] = data
                self._news.notify_all()
                return
            ieee = next((i for i, job in self._ota.items() if job["transaction"] == transaction and job["result"] is None),
                        None)
            if ieee is None:
                return
            if data.get("status") == "ok":
                body = data.get("data") if isinstance(data.get("data"), dict) else {}
                said = self._ota_over(ieee, True, f"Firmware von {ieee} eingespielt ({_version(body.get('from'))} → "
                                                  f"{_version(body.get('to'))})")
            else:
                detail = _text(data.get("error"), 300) or "ohne Meldung"
                said = self._ota_over(ieee, False, f"Firmware-Update von {ieee} hat nicht geklappt – Zigbee2MQTT "
                                                   f"meldet: {detail}", _ota_failure(detail))
        self._ota_log(*said)

    def _take_values(self, name: str, data) -> None:
        if not isinstance(data, dict):
            return
        values = {"battery": _number(data.get("battery"), 0, 100), "signal": _number(data.get("linkquality"), 0, 255),
                  "temperature": _number(data.get("local_temperature"), -40, 99),
                  "last_seen": _text(data.get("last_seen"), 40),
                  "switch": data.get("state") if data.get("state") in ("ON", "OFF") else None,   # a plug, a lamp
                  "brightness": _number(data.get("brightness"), 0, 254),   # a lamp
                  "power": _number(data.get("power"), 0, MAX_POWER),
                  "energy_today": _number(data.get("energy_today"), 0, MAX_ENERGY)}
        now = time.time()
        heard = min(_seen(data.get("last_seen")) or now, now)   # a retained message may be old: its last_seen says
        kept = {k: _plain(v) for k, v in data.items() if k in KEPT_KEYS}
        with self._news:
            ieee = name if IEEE.fullmatch(name) else next(
                (key for key, device in self._devices.items() if device["name"] == name), None)
            if ieee is None or (ieee not in self._devices and len(self._values) >= MAX_DEVICES):
                return
            self._values.setdefault(ieee, {}).update({k: v for k, v in values.items() if v is not None})
            state = self._states.setdefault(ieee, {"values": {}, "heard": 0.0, "seq": 0, "boost_since": None})
            if kept.get("temporary_mode_mode") == "boost" and state["values"].get("temporary_mode_mode") != "boost":
                state["boost_since"] = heard
            state["values"] = {**state["values"], **kept}
            update, job = kept.get("update"), self._ota.get(ieee)
            if isinstance(update, dict) and update.get("state") == "updating" and job and job["result"] is None:
                job["seen"], job["progress"] = now, _number(update.get("progress"), 0, 100)
            self._seq += 1
            state["heard"], state["seq"] = max(state["heard"], heard), self._seq
            self._news.notify_all()

    # ------------------------------------------------------------------ the actions
    def search(self, on: bool) -> None:
        """Open the network for new devices for 3 minutes – or close it again."""
        self._request("permit_join", {"time": self.search_time if on else 0}, self.answer)
        with self._lock:
            self._permit_end = time.time() + self.search_time if on else None
        log.info("Zigbee: Suche nach neuen Geräten %s", f"für {self.search_time} s gestartet" if on else "beendet")

    def remove(self, ieee: str, force: bool = False) -> dict:
        """Unpair a device. force: forget it without its answer (it is broken, gone or does not wake up)."""
        with self._lock:
            device = self._devices.get(ieee)
        if device is None:
            raise ActionError("Dieses Gerät ist nicht (mehr) angelernt.", 404)
        self._request("device/remove", {"id": ieee, "force": force}, self.answer if force else self.remove_wait)
        with self._lock:
            self._devices.pop(ieee, None)
            self._values.pop(ieee, None)
            self._joined.pop(ieee, None)
            self._states.pop(ieee, None)
            self._ota.pop(ieee, None)
        log.info("Zigbee: Gerät %s entfernt%s", ieee, " (ohne seine Antwort)" if force else "")
        return device

    # ------------------------------------------------------------------ firmware updates
    def firmware(self, ieee: str) -> None:
        """Start the firmware update of a device. It does not wait: Zigbee2MQTT answers only when the update is over;
        firmware_job() says how far it is."""
        transaction = secrets.token_hex(6)
        with self._lock:
            client, device = self._client, self._devices.get(ieee)
            if client is None:
                raise ActionError("Keine Verbindung zu Zigbee2MQTT – bitte gleich noch einmal versuchen.", 503)
            if self._bridge != "online":
                raise ActionError("Zigbee2MQTT läuft gerade nicht.", 409)
            if device is None:
                raise ActionError("Dieses Gerät ist nicht (mehr) angelernt.", 404)
            if self._updating(time.time()):
                raise ActionError("Gerade wird schon eine Firmware eingespielt – bitte warten, bis sie fertig ist.", 409)
            self._ota[ieee] = {"transaction": transaction, "started": time.time(), "firmware": device["firmware"],
                               "seen": None, "progress": None, "result": None}
        try:
            client.publish(f"{BASE}/bridge/request/{OTA}", json.dumps({"id": ieee, "transaction": transaction}))
        except (MqttError, OSError) as exc:
            with self._lock:
                self._ota.pop(ieee, None)
            raise ActionError(f"Die Nachricht an Zigbee2MQTT ging nicht hinaus ({_reason(exc)}).", 503) from exc
        log.debug("Zigbee: Firmware-Update für %s angefordert (Firmware bisher %s)", ieee, device["firmware"] or "?")

    def updating(self) -> str | None:
        """The device whose firmware is being updated right now – None if none."""
        with self._lock:
            return self._updating(time.time())

    def _updating(self, now: float) -> str | None:   # with self._lock held
        for ieee, state in self._states.items():
            update = state["values"].get("update")
            if isinstance(update, dict) and update.get("state") == "updating":
                return ieee
        for ieee, job in self._ota.items():
            if job["result"] is None and (now - job["started"] < OTA_START if job["seen"] is None
                                          else now - job["seen"] < OTA_GRACE):
                return ieee
        return None

    def firmware_job(self, ieee: str) -> dict | None:
        """The device's firmware update as the page shows it – {state: running | done | failed, progress (%),
        remaining (s), message} – or None. Without Zigbee2MQTT's answer (the app was not connected when it came) the
        firmware the device reports after it decides."""
        said = None
        with self._lock:
            now, job = time.time(), self._ota.get(ieee)
            update = ((self._states.get(ieee) or {}).get("values") or {}).get("update")
            if isinstance(update, dict) and update.get("state") == "updating":
                return {"state": "running", "progress": _number(update.get("progress"), 0, 100),
                        "remaining": _number(update.get("remaining"), 0, 10 ** 6), "message": None}
            if job is None:
                return None
            if job["result"] is None:
                firmware = (self._devices.get(ieee) or {}).get("firmware")
                if job["firmware"] and firmware and firmware != job["firmware"]:
                    said = self._ota_over(ieee, True, f"Firmware von {ieee} eingespielt ({job['firmware']} → {firmware})")
                elif job["seen"] is not None and now - job["seen"] < OTA_GRACE:   # the answer is about to come
                    return {"state": "running", "progress": job["progress"], "remaining": None, "message": None}
                elif job["seen"] is None and now - job["started"] < OTA_START:     # it has not begun yet
                    return {"state": "running", "progress": None, "remaining": None, "message": None}
                elif job["seen"] is not None:
                    said = self._ota_over(ieee, False, f"Firmware-Update von {ieee} unterbrochen (keine Antwort von "
                                                       "Zigbee2MQTT)", "Das Einspielen wurde unterbrochen – bitte noch "
                                                                       "einmal versuchen.")
                else:
                    said = self._ota_over(ieee, False, f"Firmware-Update von {ieee} hat nicht begonnen (keine Antwort "
                                                       "von Zigbee2MQTT)", "Zigbee2MQTT hat mit dem Einspielen nicht "
                                                                           "begonnen – bitte noch einmal versuchen.")
            result = job["result"]
            if now - result["at"] > OTA_KEEP:
                self._ota.pop(ieee, None)
                return None
            view = {"state": "done" if result["ok"] else "failed", "progress": None, "remaining": None,
                    "message": result["message"]}
        if said:
            self._ota_log(*said)
        return view

    def _ota_over(self, ieee: str, ok: bool, logged: str, message: str = "Neue Firmware eingespielt.") -> tuple:
        """Note the end of a firmware update (with self._lock held); returns what the log is to say."""
        self._ota[ieee]["result"] = {"ok": ok, "message": message, "at": time.time()}
        return ok, logged

    @staticmethod
    def _ota_log(ok: bool, logged: str) -> None:
        (log.info if ok else log.warning)("Zigbee: %s", logged)

    # ------------------------------------------------------------------ the thermostats (heizapp.trv)
    def problem(self) -> str | None:
        """Why no device can be reached at the moment, in words – None while Zigbee2MQTT runs."""
        with self._lock:
            broker, problem, bridge = self._broker, self._problem, self._bridge
        if broker == "off":
            return "Zigbee ist auf diesem Pi noch nicht eingerichtet."
        if broker == "connecting":
            return "Die App verbindet sich gerade mit Zigbee2MQTT."
        if broker != "connected":
            return f"Keine Verbindung zu Mosquitto ({problem})."
        if bridge != "online":
            return "Zigbee2MQTT läuft gerade nicht."
        return None

    def paired(self, model: str) -> list[str]:
        """The devices of this model that are paired (interview done), in the order of pairing."""
        with self._lock:
            return [ieee for ieee, d in self._devices.items() if d["model"] == model and d["interview"] == "done"]

    # ------------------------------------------------------------------ the plugs (heizapp.service)
    def plugs(self) -> list[dict]:
        """The paired plugs that measure (interview done), in the order of pairing: {ieee, model}."""
        with self._lock:
            return [{"ieee": ieee, "model": " ".join(filter(None, (d["vendor"], d["model"]))) or None}
                    for ieee, d in self._devices.items() if d.get("plug") and d["interview"] == "done"]

    def plug(self, ieee: str) -> dict | None:
        """What a plug reported last: {state: ON | OFF | None, power (W), energy_today (kWh), energy_wh (its meter, Wh),
        power_on_behavior, heard, seq} – None if it is not paired as a plug (any more)."""
        with self._lock:
            device = self._devices.get(ieee)
            if not (device and device.get("plug") and device["interview"] == "done"):
                return None
            state = self._states.get(ieee) or {"values": {}, "heard": 0.0, "seq": 0}
            values = state["values"]
            return {"state": values.get("state") if values.get("state") in ("ON", "OFF") else None,
                    "power": _number(values.get("power"), 0, MAX_POWER),
                    "energy_today": _number(values.get("energy_today"), 0, MAX_ENERGY),
                    "energy_wh": _wh(_number(values.get("energy"), 0, MAX_ENERGY)),
                    "power_on_behavior": _text(values.get("power_on_behavior"), 20),
                    "heard": state["heard"] or None, "seq": state["seq"]}

    # ------------------------------------------------------------------ the lamps (heizapp.service)
    def lights(self) -> list[dict]:
        """The paired lamps (interview done), in the order of pairing: {ieee, model, caps (see _light)}."""
        with self._lock:
            return [{"ieee": ieee, "model": " ".join(filter(None, (d["vendor"], d["model"]))) or None,
                     "caps": dict(d["light_caps"])}
                    for ieee, d in self._devices.items() if d.get("light") and d["interview"] == "done"]

    def light(self, ieee: str) -> dict | None:
        """What a lamp reported last: {state: ON | OFF | None, brightness (1–254), color_temp (mired), caps, heard,
        seq} – None if it is not paired as a lamp (any more)."""
        with self._lock:
            device = self._devices.get(ieee)
            if not (device and device.get("light") and device["interview"] == "done"):
                return None
            state = self._states.get(ieee) or {"values": {}, "heard": 0.0, "seq": 0}
            values = state["values"]
            return {"state": values.get("state") if values.get("state") in ("ON", "OFF") else None,
                    "brightness": _number(values.get("brightness"), 0, 254),
                    "color_temp": _number(values.get("color_temp"), *MIRED), "caps": dict(device["light_caps"]),
                    "heard": state["heard"] or None, "seq": state["seq"]}

    def thermostat(self, ieee: str) -> dict | None:
        """What the app knows of a device: {device (None if not paired), values, heard, seq, boost_since}."""
        with self._lock:
            state, device = self._states.get(ieee), self._devices.get(ieee)
            if state is None and device is None:
                return None
            state = state or {"values": {}, "heard": 0.0, "seq": 0, "boost_since": None}
            return {"device": dict(device) if device else None, "values": dict(state["values"]),
                    "heard": state["heard"], "seq": state["seq"], "boost_since": state["boost_since"]}

    def send(self, ieee: str, payload: dict, what: str = "set") -> int | None:
        """A command (set) or a question (get) to a device. Returns the number of the latest message so far – wait()
        waits for a newer one – or None if it cannot go out (no connection, Zigbee2MQTT not running)."""
        with self._lock:
            client, seq = (self._client, self._seq) if self._bridge == "online" else (None, None)
        if client is None:
            return None
        try:
            client.publish(f"{BASE}/{ieee}/{what}", json.dumps(payload))
        except (MqttError, OSError, ValueError) as exc:
            log.warning("Zigbee: Nachricht an %s ging nicht hinaus – %s", ieee, _reason(exc))
            return None
        return seq

    def wait(self, ieee: str, after: int, check, timeout: float) -> dict | None:
        """Wait until the device reports values newer than message number after for which check(values) is true;
        returns them – or None after timeout seconds or when the connection ends."""
        deadline = time.monotonic() + timeout
        with self._news:
            while True:
                state = self._states.get(ieee)
                if state and state["seq"] > after and check(state["values"]):
                    return dict(state["values"])
                left = deadline - time.monotonic()
                if left <= 0 or self._client is None:
                    return None
                self._news.wait(min(left, 1.0))

    def _request(self, what: str, body: dict, wait: float) -> dict:
        """Ask Zigbee2MQTT (bridge/request/<what>) and wait for its answer to exactly this request."""
        transaction = secrets.token_hex(6)
        with self._lock:
            client = self._client
            if client is None:
                raise ActionError("Keine Verbindung zu Zigbee2MQTT – bitte gleich noch einmal versuchen.", 503)
            if self._bridge != "online":
                raise ActionError("Zigbee2MQTT läuft gerade nicht.", 409)
            self._waiting[transaction] = None
        try:
            client.publish(f"{BASE}/bridge/request/{what}", json.dumps({**body, "transaction": transaction}))
            deadline = time.monotonic() + wait
            with self._news:
                while self._waiting.get(transaction) is None:
                    left = deadline - time.monotonic()
                    if left <= 0 or self._client is not client:
                        raise ActionError("Zigbee2MQTT hat nicht rechtzeitig geantwortet – bitte noch einmal "
                                          "versuchen.", 504)
                    self._news.wait(min(left, 1.0))
                answer = self._waiting[transaction]
        except (MqttError, OSError) as exc:
            raise ActionError(f"Die Nachricht an Zigbee2MQTT ging nicht hinaus ({_reason(exc)}).", 503) from exc
        finally:
            with self._lock:
                self._waiting.pop(transaction, None)
        if answer.get("status") != "ok":
            detail = _text(answer.get("error"), 300) or "ohne Meldung"
            log.warning("Zigbee: %s hat nicht geklappt – Zigbee2MQTT meldet: %s", what, detail)
            raise ActionError(_failure(detail), 502)
        return answer.get("data") if isinstance(answer.get("data"), dict) else {}

    # ------------------------------------------------------------------ what the page shows
    def view(self) -> dict:
        with self._lock:
            now = time.time()
            left = max(0, round(self._permit_end - now)) if self._permit_end and self._bridge == "online" else 0
            devices = []
            for ieee, device in self._devices.items():
                values, joined = self._values.get(ieee, {}), self._joined.get(ieee)
                devices.append({**device, "battery": values.get("battery"), "signal": values.get("signal"),
                                "temperature": values.get("temperature"), "last_seen": values.get("last_seen"),
                                "switch": values.get("switch"), "brightness": values.get("brightness"),
                                "power_w": values.get("power"),
                                "energy_today": values.get("energy_today"), "new": bool(joined and now - joined < NEW)})
            return {"broker": self._broker, "problem": self._problem, "bridge": self._bridge, **self._info,
                    "search": {"active": left > 0, "left": left, "seconds": self.search_time}, "devices": devices}

    def summary(self) -> str:
        """One line for the protocol overview."""
        v = self.view()
        if v["broker"] != "connected":
            return {"off": "keine Anmeldung bei Mosquitto", "connecting": "verbinde mit Mosquitto"}.get(
                v["broker"], f"Mosquitto nicht erreichbar ({v['problem']})")
        if v["bridge"] != "online":
            return "Zigbee2MQTT läuft nicht"
        thermostats = sum(1 for d in v["devices"] if d["thermostat"])
        plugs = sum(1 for d in v["devices"] if d["plug"])
        lights = sum(1 for d in v["devices"] if d["light"])
        kinds = [f"{thermostats} Thermostat(e)"] + [f"{n} {what}" for n, what in ((plugs, "Steckdose(n)"),
                                                                                    (lights, "Lampe(n)")) if n]
        return (f"Zigbee2MQTT {v.get('version') or '?'} läuft · Kanal {v.get('channel') or '?'} · "
                f"{len(v['devices'])} Gerät(e), davon " + (", ".join(kinds[:-1]) + " und " if len(kinds) > 1 else "")
                + kinds[-1] + (f" · Suche läuft noch {v['search']['left']} s" if v["search"]["active"] else ""))
