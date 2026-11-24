"""Das Zigbee-Netz aus Sicht der App: über Mosquitto mit Zigbee2MQTT reden – Thermostate suchen, zeigen, entfernen.

Beim Installieren legt install.py die Anmeldung der App bei Mosquitto in zigbee-mqtt.json ab (im Datenordner, nur für
die App lesbar). Damit verbindet sich die App mit Mosquitto (nur auf diesem Pi) und liest, was Zigbee2MQTT meldet: ob
es läuft (bridge/state), Version und Funkkanal (bridge/info), die angelernten Geräte (bridge/devices), was gerade
passiert (bridge/event) und die Werte jedes Geräts (zigbee2mqtt/<IEEE-Adresse>). „Thermostate suchen“ öffnet das Netz
für 3 Minuten (bridge/request/permit_join), „Entfernen“ meldet ein Gerät ab (bridge/request/device/remove).
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import socket
import threading
import time
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


def _json(payload: bytes):
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _text(value, limit: int = 120) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _number(value, low: float, high: float):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if low <= value <= high else None


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


def _definition(item: dict, device: dict) -> None:
    """Model, maker and what the device can do – from bridge/devices or the event of a finished interview."""
    definition = item.get("definition") if isinstance(item.get("definition"), dict) else None
    if definition is None:
        return
    exposes = definition.get("exposes") if isinstance(definition.get("exposes"), list) else []
    device.update(model=_text(definition.get("model"), 60) or device.get("model"),
                  vendor=_text(definition.get("vendor"), 60) or device.get("vendor"),
                  description=_text(definition.get("description"), 160),
                  thermostat=any(isinstance(e, dict) and e.get("type") == "climate" for e in exposes))
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
        self._values: dict[str, dict] = {}    # ieee -> the device's latest values
        self._joined: dict[str, float] = {}   # ieee -> when it joined
        self._permit_end: float | None = None   # time.time() at which the network closes again
        self._waiting: dict[str, dict | None] = {}   # transaction -> Zigbee2MQTT's answer, None until it comes

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
                      "interview": state, "power": _text(item.get("power_source"), 40),
                      "firmware": _text(item.get("software_build_id"), 40)}
            _definition(item, device)
            devices[ieee] = device
        with self._lock:
            for ieee in set(self._devices) - set(devices):
                self._values.pop(ieee, None)
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
                    "vendor": None, "description": None, "supported": False, "thermostat": False,
                    "interview": "pending", "power": None, "firmware": None}
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
                said = f"Gerät {ieee} hat das Netz verlassen"
        if said:
            log.info("Zigbee: %s", said)

    def _take_answer(self, data) -> None:
        if not isinstance(data, dict):
            return
        transaction = data.get("transaction")
        with self._news:
            if isinstance(transaction, str) and transaction in self._waiting and self._waiting[transaction] is None:
                self._waiting[transaction] = data
                self._news.notify_all()

    def _take_values(self, name: str, data) -> None:
        if not isinstance(data, dict):
            return
        values = {"battery": _number(data.get("battery"), 0, 100), "signal": _number(data.get("linkquality"), 0, 255),
                  "temperature": _number(data.get("local_temperature"), -40, 99),
                  "last_seen": _text(data.get("last_seen"), 40)}
        with self._lock:
            ieee = name if IEEE.fullmatch(name) else next(
                (key for key, device in self._devices.items() if device["name"] == name), None)
            if ieee is None or (ieee not in self._devices and len(self._values) >= MAX_DEVICES):
                return
            self._values.setdefault(ieee, {}).update({k: v for k, v in values.items() if v is not None})

    # ------------------------------------------------------------------ the actions
    def search(self, on: bool) -> None:
        """Open the network for new devices for 3 minutes – or close it again."""
        self._request("permit_join", {"time": self.search_time if on else 0}, self.answer)
        with self._lock:
            self._permit_end = time.time() + self.search_time if on else None
        log.info("Zigbee: Suche nach Thermostaten %s", f"für {self.search_time} s gestartet" if on else "beendet")

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
        log.info("Zigbee: Gerät %s entfernt%s", ieee, " (ohne seine Antwort)" if force else "")
        return device

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
                                "new": bool(joined and now - joined < NEW)})
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
        return (f"Zigbee2MQTT {v.get('version') or '?'} läuft · Kanal {v.get('channel') or '?'} · "
                f"{len(v['devices'])} Gerät(e), davon {thermostats} Thermostat(e)"
                + (f" · Suche läuft noch {v['search']['left']} s" if v["search"]["active"] else ""))
