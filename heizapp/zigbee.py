"""Zigbee (Einrichtung › Zigbee): die Zusatzprogramme für die SONOFF-Thermostate, das Zigbee-Netz, Geräte anlernen.

Die Thermostate funken mit Zigbee. Dafür braucht der Pi drei Programme: Zigbee2MQTT (spricht über den USB-Stick mit
den Thermostaten), Mosquitto (reicht die Nachrichten zwischen Zigbee2MQTT und dieser App weiter) und Node.js (darauf
läuft Zigbee2MQTT). Die App darf am System nichts ändern. Sie legt nur zigbee-request.json in ihren Datenordner, darin
steht eine Aktion (status, install, remove, setup, start, stop) – bei „setup“ dazu der Funkkanal und der Stick –, und
flowthermpi-zigbee.path startet daraufhin als root „install.py --zigbee“. Welche Versionen das lädt, steht fest in
install.py; jede Datei wird nach dem Laden geprüft. Fortschritt und Ergebnis schreibt es nach zigbee-status.json, am
Ende immer mit dem Stand der Programme und des Dienstes von Zigbee2MQTT.

Läuft Zigbee2MQTT, redet die App über Mosquitto direkt mit ihm (heizapp.zigbee_net): Geräte suchen und entfernen.

Den Reiter gibt es nur, wenn HoLiTherm mit install.py eingerichtet ist: dessen systemd-Unit setzt
HEIZAPP_ZIGBEE_DIR.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from .service import ActionError
from .zigbee_net import IEEE, ZigbeeNet

log = logging.getLogger(__name__)
REQUEST, STATUS = "zigbee-request.json", "zigbee-status.json"
PARTS = ("node", "pnpm", "zigbee2mqtt")
PHASES = ("check", "mosquitto", "node", "pnpm", "zigbee2mqtt", "dependencies", "build", "finish", "remove")
PICKUP = 15.0              # seconds: by then the root service has taken the request (it starts within a second)
ANSWER = 90.0              # seconds for „status“
TIMES = {"install": 65 * 60.0, "remove": 10 * 60.0}   # systemd stops the root service after 60 minutes
MAX_STATUS = 1 << 20
BUSY = {"install": "Die Zigbee-Programme werden gerade installiert – das dauert eine Weile.",
        "remove": "Die Zigbee-Programme werden gerade entfernt – bitte einen Moment warten.",
        "status": "Einen Moment bitte – der Pi wird gerade gefragt.",
        "setup": "Zigbee wird gerade eingerichtet – einen Moment bitte.",
        "start": "Zigbee2MQTT wird gerade gestartet – einen Moment bitte.",
        "stop": "Zigbee2MQTT wird gerade angehalten – einen Moment bitte."}
FAILED = {"status": "Das Nachsehen hat nicht geklappt.", "setup": "Das Einrichten hat nicht geklappt.",
          "start": "Das Starten hat nicht geklappt.", "stop": "Das Anhalten hat nicht geklappt."}
VERSION = re.compile(r"[0-9][0-9A-Za-z.+~:-]{0,40}")
PORT = re.compile(r"[\w.:+-]{1,200}")
# the stick's name under /dev/serial/by-id, e.g. usb-SONOFF_SONOFF_Dongle_Plus_MG24_…-if00-port0
STICK = re.compile(r"sonoff|zigbee|dongle|mg24|efr32|ember|itead", re.I)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _text(value, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _number(value, low: int, high: int) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high else None


def _version(value) -> str | None:
    return value if isinstance(value, str) and VERSION.fullmatch(value) else None


def _boot_time() -> float | None:
    """When this computer started (Linux); None elsewhere."""
    try:
        with open("/proc/uptime", encoding="ascii") as fh:
            return time.time() - float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _interrupted(started: str | None, limit: float) -> bool:
    """A job that began before the Pi was last started, or longer ago than it may take, is not running any more."""
    try:
        at = datetime.fromisoformat(started).timestamp() if started else None
    except ValueError:
        at = None
    if at is None:
        return True
    boot = _boot_time()
    return time.time() - at > limit or (boot is not None and at < boot - 5)


def _programs(value) -> dict | None:
    """The programs as the root service reported them – only what the page shows."""
    if not isinstance(value, dict):
        return None

    def obj(item) -> dict:
        return item if isinstance(item, dict) else {}

    installed, wanted = obj(value.get("installed")), obj(value.get("wanted"))
    have = {part: _version(installed.get(part)) for part in PARTS}
    want = {part: _version(wanted.get(part)) for part in PARTS}
    complete = value.get("complete") is True and all(have.values())
    mosquitto, service, network = obj(value.get("mosquitto")), obj(value.get("service")), obj(value.get("network"))
    port = network.get("port")
    ports = []
    for item in value.get("ports") if isinstance(value.get("ports"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and PORT.fullmatch(item["id"]):
            device = item.get("device")
            ports.append({"id": item["id"], "stick": bool(STICK.search(item["id"])),
                          "device": device if isinstance(device, str) and re.fullmatch(r"tty\w{1,20}", device) else None})
    return {"arch": _text(value.get("arch"), 20), "arch_ok": value.get("arch_ok") is True,
            "free_mb": _number(value.get("free_mb"), 0, 1 << 40), "installed": have, "wanted": want,
            "complete": complete, "current": complete and all(want.values()) and have == want,
            "mosquitto": {"version": _version(mosquitto.get("version")), "active": mosquitto.get("active") is True},
            "service": {"present": service.get("present") is True, "enabled": service.get("enabled") is True,
                        "active": service.get("active") is True, "failed": service.get("failed") is True,
                        "restarts": _number(service.get("restarts"), 0, 1 << 30) or 0,
                        "log": _text(service.get("log"), 4000)},
            "network": {"configured": network.get("configured") is True,
                        "channel": _number(network.get("channel"), 11, 26),
                        "port": port if isinstance(port, str) and PORT.fullmatch(port) else None,
                        "devices": _number(network.get("devices"), 0, 1 << 20) or 0},
            "ports": ports[:8]}


class Zigbee:
    def __init__(self, folder: str | os.PathLike | None, poll: float = 0.25):
        self.folder = Path(folder) if folder else None
        self.poll, self.pickup, self.answer, self.times = poll, PICKUP, ANSWER, dict(TIMES)
        self._lock = threading.Lock()                 # guards the fields below
        self._free = threading.Condition(self._lock)  # signalled when a request is answered
        self._doing: str | None = None   # "status" | "install" | "remove" – one request at a time …
        self._owner: str | None = None   # … namely this one (request id)
        self._job: dict | None = None    # the installation or removal under way, or the last one
        self._info: dict | None = None   # the programs, as last reported …
        self._at: str | None = None      # … at this time
        self._error: str | None = None   # why the last look at the Pi failed
        self._adopted = False            # a job from before a restart of the app is followed by start()
        self.net = ZigbeeNet(self.folder)   # Zigbee2MQTT itself, over Mosquitto
        if self.folder:
            status = self._read_status()
            if status:
                self._adopt(status)

    @property
    def supported(self) -> bool:
        return self.folder is not None

    def start(self) -> None:
        """When the app starts: follow an installation that was under way (the app may have been restarted by an
        update meanwhile), else look at the Pi once, in the background."""
        if not self.supported:
            return
        self.net.start()
        with self._lock:
            job = self._job if self._adopted and self._job and self._job["state"] == "running" else None
        if job:
            threading.Thread(target=self._follow, args=(job["id"], self.times[job["action"]]),
                             name="zigbee-follow", daemon=True).start()
        else:
            threading.Thread(target=self._quiet_refresh, name="zigbee-refresh", daemon=True).start()

    def stop(self) -> None:
        self.net.stop()

    # ------------------------------------------------------------------ what the page shows
    def view(self) -> dict:
        if not self.supported:
            return {"supported": False}
        status = self._read_status()
        if status:
            self._absorb(status)
        with self._lock:
            result = {"supported": True, "programs": dict(self._info) if self._info else None, "at": self._at,
                      "job": dict(self._job) if self._job else None, "busy": self._doing, "error": self._error}
        result["net"] = self.net.view()
        return result

    # ------------------------------------------------------------------ the actions
    def refresh(self) -> dict:
        """Ask the Pi what is installed (quick: a few seconds)."""
        return self._quick("status")

    def setup(self, channel, port=None) -> dict:
        """„Zigbee einrichten“: Zigbee2MQTT with this radio channel and stick, started at once."""
        if _number(channel, 11, 26) is None:
            raise ActionError("Bitte einen Funkkanal von 11 bis 26 wählen.", 400)
        if port is not None and not (isinstance(port, str) and PORT.fullmatch(port)):
            raise ActionError("Unbekannter Zigbee-Stick.", 400)
        return self._quick("setup", {"channel": channel, "port": port})

    def service(self, on) -> dict:
        """„Starten“ and „Anhalten“ of Zigbee2MQTT."""
        if not isinstance(on, bool):
            raise ActionError("Ungültige Anfrage.", 400)
        return self._quick("start" if on else "stop")

    def search(self, on) -> dict:
        """„Geräte suchen“: the network is open for new devices for 3 minutes (on=False closes it again)."""
        self._require()
        if not isinstance(on, bool):
            raise ActionError("Ungültige Anfrage.", 400)
        self.net.search(on)
        return {"zigbee": self.view()}

    def remove_device(self, ieee, force=None) -> dict:
        """Unpair a device; force: without its answer."""
        self._require()
        if not (isinstance(ieee, str) and IEEE.fullmatch(ieee)) or not isinstance(force, (bool, type(None))):
            raise ActionError("Ungültige Anfrage.", 400)
        device = self.net.remove(ieee, force is True)
        return {"zigbee": self.view(),
                "message": "Das Thermostat ist entfernt." if device["thermostat"]
                else "Die Steckdose ist entfernt." if device.get("plug")
                else "Die Lampe ist entfernt." if device.get("light")
                else "Der Schalter ist entfernt." if device.get("button")
                else "Der Bewegungsmelder ist entfernt." if device.get("detector") else "Das Gerät ist entfernt."}

    def _quick(self, action: str, params: dict | None = None) -> dict:
        """A request that the root service answers in seconds; the page waits for it."""
        try:
            status = self._ask(action, params)
        except ActionError as exc:
            if action == "status":
                with self._lock:
                    self._error = str(exc)
            raise
        if action == "status":
            with self._lock:
                self._error = None
        if status.get("state") != "done":
            raise ActionError(_text(status.get("message"), 300) or FAILED[action], 502)
        if action != "status":
            log.info("Zigbee: %s", _text(status.get("message"), 300) or action)
        return {"zigbee": self.view(), "message": None if action == "status" else _text(status.get("message"), 300)}

    def install(self) -> dict:
        """Install or bring up to date; answers at once, the page follows the job (GET /api/zigbee)."""
        return self._start("install")

    def remove(self) -> dict:
        return self._start("remove")

    def _start(self, action: str) -> dict:
        self._require()
        with self._lock:
            if self._job and self._job["state"] in ("waiting", "running"):
                raise ActionError(BUSY[self._job["action"]], 409)
        rid = self._begin(action, job={"action": action, "state": "waiting", "phase": None, "step": None,
                                       "steps": None, "progress": None, "message": None, "details": None,
                                       "started": _now(), "finished": None})
        log.info("Zigbee: %s angestoßen", "Installation" if action == "install" else "Entfernen")
        threading.Thread(target=self._follow, args=(rid, self.times[action]), name=f"zigbee-{action}", daemon=True).start()
        return {"zigbee": self.view()}

    # ------------------------------------------------------------------ talking to the root service
    def _require(self) -> None:
        if not self.supported:
            raise ActionError("Zigbee gibt es nur, wenn HoLiTherm mit dem Installationsbefehl auf dem Raspberry Pi "
                              "eingerichtet ist.", 409)

    def _begin(self, action: str, job: dict | None = None, wait: float = 10.0, params: dict | None = None) -> str:
        self._require()
        rid = secrets.token_hex(8)
        deadline = time.monotonic() + wait
        with self._free:
            while self._doing:   # a look at the Pi is over in a moment: wait – anything else is „busy“
                left = deadline - time.monotonic()
                if self._doing != "status" or left <= 0:
                    raise ActionError(BUSY[self._doing], 409)
                self._free.wait(left)
            self._doing, self._owner = action, rid
            if job is not None:
                self._job = {"id": rid, **job}
        try:
            self._place(rid, {"action": action, **(params or {})})
        except BaseException:
            with self._free:
                if job is not None and self._job and self._job["id"] == rid:
                    self._job = None
                self._release(rid)
            raise
        return rid

    def _release(self, rid: str) -> None:
        """Free the way (call with the lock held)."""
        if self._owner == rid:
            self._doing = self._owner = None
            self._free.notify_all()

    def _ask(self, action: str, params: dict | None = None) -> dict:
        rid = self._begin(action, params=params)
        try:
            return self._wait(rid, self.answer)
        finally:
            with self._free:
                self._release(rid)

    def _follow(self, rid: str, limit: float) -> None:
        """Background thread of an installation or removal: records how it ends, also when nobody picks it up."""
        message = None
        try:
            self._wait(rid, limit)   # _absorb() records the result in the job and frees the way
        except ActionError as exc:
            message = str(exc)
        except Exception as exc:  # never leave the page at „installiere …“
            log.exception("Zigbee: Fehler beim Verfolgen der Installation")
            message = f"interner Fehler: {exc}"
        with self._free:
            job = self._job
            if message and job and job["id"] == rid and job["state"] in ("waiting", "running"):
                job.update(state="failed", phase=None, progress=None, message=message, finished=_now())
                log.warning("Zigbee: %s hat nicht geklappt – %s", "Installation" if job["action"] == "install"
                            else "Entfernen", message)
            self._release(rid)

    def _wait(self, rid: str, limit: float) -> dict:
        start = time.monotonic()
        while True:
            status = self._read_status()
            if status and status.get("id") == rid:
                self._absorb(status)
                if status.get("state") in ("done", "failed"):
                    return status
            waited = time.monotonic() - start
            if waited > self.pickup and self._withdraw(rid):
                raise ActionError("Der Dienst für die Zigbee-Programme reagiert nicht. Bitte den Pi einmal neu starten; "
                                  "hilft das nicht, den Installationsbefehl noch einmal per SSH ausführen.", 503)
            if waited > limit:
                raise ActionError("Der Dienst für die Zigbee-Programme hat nicht rechtzeitig geantwortet. Bitte gleich "
                                  "noch einmal nachsehen.", 504)
            time.sleep(self.poll)

    def _quiet_refresh(self) -> None:
        try:
            self.refresh()
        except ActionError as exc:
            log.warning("Zigbee: %s", exc)
        except Exception:  # a background look must never take the app down
            log.exception("Zigbee: Fehler beim Nachsehen")

    def _place(self, rid: str, request: dict) -> None:
        """The request for the root service: written as a new file (0600) and renamed into place."""
        tmp = self.folder / f".{REQUEST}.{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"id": rid, **request}, fh)
            os.replace(tmp, self.folder / REQUEST)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise ActionError(f"Die Anfrage an den Dienst für die Zigbee-Programme ließ sich nicht ablegen "
                              f"({exc.strerror}).", 500) from exc

    def _withdraw(self, rid: str) -> bool:
        """Take our request back if the root service has not picked it up. True if it was still there."""
        path = self.folder / REQUEST
        try:
            placed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(placed, dict) or placed.get("id") != rid:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def _read_status(self) -> dict | None:
        try:
            with open(self.folder / STATUS, "rb") as fh:
                data = fh.read(MAX_STATUS + 1)
            status = json.loads(data) if len(data) <= MAX_STATUS else None
        except (OSError, ValueError):
            return None
        return status if isinstance(status, dict) else None

    def _adopt(self, status: dict) -> None:
        """At the start of the app: the last installation or removal, as the root service left it. One that was still
        running is followed by start() – the root service goes on without the app."""
        info = _programs(status.get("zigbee"))
        if info is not None:
            self._info, self._at = info, _text(status.get("at"), 40)
        rid, action, state = status.get("id"), status.get("action"), status.get("state")
        if not (isinstance(rid, str) and re.fullmatch(r"[0-9a-f]{8,32}", rid) and action in TIMES
                and state in ("running", "done", "failed")):
            return
        started = _text(status.get("started"), 40)
        self._job = {"id": rid, "action": action, "state": "waiting", "phase": None, "step": None, "steps": None,
                     "progress": None, "message": None, "details": None, "started": started, "finished": None}
        if state == "running" and _interrupted(started, self.times[action]):   # power cut or restart of the Pi
            self._job.update(state="failed", finished=_text(status.get("at"), 40), message=(
                "Die Installation wurde unterbrochen (Neustart oder Strom weg?). Bitte noch einmal „Installieren“ drücken."
                if action == "install" else "Das Entfernen wurde unterbrochen. Bitte noch einmal „Entfernen“ drücken."))
            return
        if state == "running":
            self._doing, self._owner, self._adopted = action, rid, True
        self._absorb(status, quiet=True)   # fills in the progress – or how it ended, with its message
        log.info("Zigbee: zuletzt %s – %s%s", "Installation" if action == "install" else "Entfernen",
                 {"running": "läuft noch", "done": "fertig", "failed": "fehlgeschlagen"}[state],
                 f" ({_text(status.get('message'), 300)})" if state != "running" and status.get("message") else "")

    def _absorb(self, status: dict, quiet: bool = False) -> None:
        """Take what the root service reported: the programs, the job's progress and result. quiet: at the start of
        the app – how an earlier job ended is not news."""
        info, state, at = _programs(status.get("zigbee")), status.get("state"), _text(status.get("at"), 40)
        with self._free:
            if info is not None:
                self._info, self._at = info, at
            job = self._job
            if not job or status.get("id") != job["id"]:
                return
            if state == "running" and job["state"] in ("waiting", "running"):
                phase = status.get("phase")
                if not quiet and phase in PHASES and phase != job["phase"]:
                    log.info("Zigbee: Schritt %s von %s – %s", status.get("step") or "?", status.get("steps") or "?", phase)
                job.update(state="running", phase=phase if phase in PHASES else job["phase"],
                           step=_number(status.get("step"), 1, 20), steps=_number(status.get("steps"), 1, 20),
                           progress=_number(status.get("progress"), 0, 100))
            elif state in ("done", "failed") and job["state"] not in ("done", "failed"):
                job.update(state=state, phase=None, progress=None, message=_text(status.get("message"), 300),
                           details=_text(status.get("details"), 4000),
                           finished=_text(status.get("finished"), 40) or at or _now())
                if not quiet:
                    what = "Installation" if job["action"] == "install" else "Entfernen"
                    if state == "done":
                        log.info("Zigbee: %s fertig – %s", what, job["message"] or "ohne Meldung")
                    else:
                        log.warning("Zigbee: %s fehlgeschlagen – %s%s", what, job["message"] or "ohne Meldung",
                                    f"\n{job['details']}" if job["details"] else "")
                    self.net.wake()   # a new login (or none any more): no need to wait for the next try
                self._release(job["id"])   # in the same step, so the page never sees "done" but busy
