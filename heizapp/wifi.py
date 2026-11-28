"""WLAN einrichten (Einrichtung › WLAN): Netze in Reichweite suchen, verbinden, vergessen.

Der Pi wird für das Netzwerkkabel ausgeliefert; das WLAN richtet man danach hier ein. Die App darf das
Netz nicht selbst ändern. Sie legt nur wifi-request.json in ihren Datenordner (0600), und
holitherm-wifi.path startet daraufhin als root „install.py --wifi“. Das prüft die Anfrage noch einmal,
löscht sie sofort (sie kann das WLAN-Passwort enthalten), spricht mit NetworkManager und schreibt das
Ergebnis samt Zustand von Kabel und WLAN nach wifi-status.json – ohne Passwort.

Den Reiter gibt es nur, wenn HoLiTherm mit install.py eingerichtet ist: dessen systemd-Unit setzt
HEIZAPP_WIFI_DIR.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

from .service import ActionError

log = logging.getLogger(__name__)
REQUEST, STATUS = "wifi-request.json", "wifi-status.json"
COUNTRIES = {"DE": "Deutschland", "AT": "Österreich", "CH": "Schweiz", "LU": "Luxemburg", "LI": "Liechtenstein",
             "NL": "Niederlande", "BE": "Belgien", "FR": "Frankreich", "IT": "Italien", "DK": "Dänemark",
             "PL": "Polen", "CZ": "Tschechien", "ES": "Spanien", "GB": "Großbritannien", "US": "USA"}
PICKUP = 15.0     # seconds: by then the WLAN service has taken the request (it starts within a second)
ANSWER = 60.0     # seconds for a search or „vergessen“ (the very first search switches the WLAN on)
CONNECT = 150.0   # seconds for connecting: NetworkManager tries 45 s, going back to the old WLAN 30 s more
MAX_STATUS = 1 << 20
BUSY = {"scan": "Die Suche nach WLAN-Netzen läuft schon.",
        "connect": "Der Pi verbindet sich gerade mit dem WLAN – bitte einen Moment warten.",
        "forget": "Einen Moment bitte – der Pi vergisst gerade ein WLAN."}
# the same text as WIFI_ONLY_LINK in install.py, which checks again with the current state of the Pi
ONLY_LINK = ("Der Pi hängt gerade nur über „{ssid}“ am Netz. Vergisst er dieses WLAN, ist er nicht mehr erreichbar – "
             "auch nicht aus der Ferne. Bitte zuerst das Netzwerkkabel am Pi einstecken.")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _text(value, limit: int = 200) -> str:
    return value[:limit] if isinstance(value, str) else ""


def check_ssid(value) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 32
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ActionError("Bitte ein WLAN auswählen oder seinen Namen eingeben (höchstens 32 Zeichen).")
    return value


def check_password(value) -> str:
    """WPA passwords: 8 to 63 characters without umlauts – or 64 hex digits. Empty: open network."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ActionError("Ungültiges Passwort.")
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value
    if not re.fullmatch(r"[\x20-\x7e]*", value):
        raise ActionError("Im Passwort steht ein Zeichen, das es in WLAN-Passwörtern nicht gibt (z. B. ä, ö, ü oder ß). "
                          "Bitte genau so eingeben, wie es am Router steht.")
    if not 8 <= len(value) <= 63:
        raise ActionError("Ein WLAN-Passwort hat 8 bis 63 Zeichen. Bitte noch einmal genau abschreiben.")
    return value


def check_country(value) -> str:
    if value in (None, ""):
        return "DE"
    if value not in COUNTRIES:
        raise ActionError("Unbekanntes Land für das WLAN.")
    return value


def _net(value) -> dict | None:
    """Cable and WLAN as the WLAN service reported them."""
    if not isinstance(value, dict) or not isinstance(value.get("interfaces"), list):
        return None
    interfaces = []
    for item in value["interfaces"]:
        if not isinstance(item, dict) or item.get("type") not in ("ethernet", "wifi"):
            continue
        state, signal = _text(item.get("state"), 60), item.get("signal")
        interfaces.append({"name": _text(item.get("name"), 40), "type": item["type"], "state": state,
                           "connected": state.startswith("connected"),
                           "ipv4": [a for a in item.get("ipv4") or [] if isinstance(a, str)][:4],
                           "ssid": _text(item.get("ssid"), 64) or None,
                           "signal": signal if isinstance(signal, int) and 0 <= signal <= 100 else None})
    country = value.get("country")
    return {"interfaces": interfaces, "country": country if country in COUNTRIES or
            (isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country)) else None}


def _networks(value) -> list[dict] | None:
    if not isinstance(value, list):
        return None
    result = []
    for item in value[:40]:
        if not isinstance(item, dict) or not _text(item.get("ssid"), 64):
            continue
        signal = item.get("signal")
        result.append({"ssid": item["ssid"][:64], "signal": signal if isinstance(signal, int) else 0,
                       "security": _text(item.get("security"), 60),
                       **{key: item.get(key) is True for key in ("active", "saved", "enterprise")}})
    return result


def _only_link(net: dict | None, ssid: str) -> bool:
    """The Pi is in this WLAN and no cable is in: forgetting it cuts the Pi off – for every browser, also one
    that comes from afar (Tailscale and the like run over that WLAN, too)."""
    up = [i for i in (net or {}).get("interfaces", []) if i["connected"]]
    return not any(i["type"] == "ethernet" for i in up) and any(i["type"] == "wifi" and i["ssid"] == ssid for i in up)


def _via(net: dict | None, local: str | None) -> str | None:
    """How this browser reached the Pi: "ethernet", "wifi" or None (unknown, e.g. on the Pi itself)."""
    for item in (net or {}).get("interfaces", []) if local else []:
        if local in item["ipv4"]:
            return item["type"]
    return None


class Wifi:
    def __init__(self, folder: str | os.PathLike | None, poll: float = 0.25):
        self.folder = Path(folder) if folder else None
        self.poll, self.pickup, self.answer, self.connect_time = poll, PICKUP, ANSWER, CONNECT
        self._lock = threading.Lock()   # guards the fields below
        self._doing: str | None = None  # "scan" | "connect" | "forget" – one request at a time …
        self._owner: str | None = None  # … namely this one (request id)
        self._job: dict | None = None   # the connection being made, or the last one
        self._networks: list[dict] | None = None
        self._scanned: str | None = None
        self._net: dict | None = None   # cable and WLAN, as last reported
        self._net_at: str | None = None
        if self.folder:
            status = self._read_status()   # from before a restart of the app: at least the state of the network
            if status:
                self._absorb(status, networks=False)

    @property
    def supported(self) -> bool:
        return self.folder is not None

    # ------------------------------------------------------------------ what the page shows
    def view(self, local: str | None = None) -> dict:
        """local: the Pi's own address this browser talks to (tells cable from WLAN)."""
        if not self.supported:
            return {"supported": False}
        status = self._read_status()
        if status:
            self._absorb(status)
        with self._lock:
            net = self._net
            result = {"supported": True, "net": net, "at": self._net_at, "networks": self._networks,
                      "scanned": self._scanned, "job": dict(self._job) if self._job else None, "busy": self._doing}
        result["via"] = _via(net, local)
        result["country"] = COUNTRIES.get((net or {}).get("country") or "")
        result["hostname"] = socket.gethostname().split(".")[0].lower()   # name.local: case does not matter
        return result

    # ------------------------------------------------------------------ the three actions
    def scan(self, body: dict, local: str | None = None) -> dict:
        status = self._ask("scan", {"action": "scan", "country": check_country(body.get("country"))})
        if status.get("state") != "done":
            log.warning("WLAN-Suche hat nicht geklappt: %s", _text(status.get("message"), 300) or "ohne Meldung")
            raise ActionError(_text(status.get("message"), 300) or "Die Suche hat nicht geklappt.", 502)
        found = _networks(status.get("networks")) or []
        log.info("WLAN-Suche: %d Netz(e)%s", len(found),
                 (": " + ", ".join(f"„{n['ssid']}“ {n['signal']} %" for n in found[:12])) if found else "")
        return {"wifi": self.view(local)}

    def connect(self, body: dict, local: str | None = None) -> dict:
        """Starts connecting and answers at once; the page follows the job (GET /api/wifi)."""
        ssid, password = check_ssid(body.get("ssid")), check_password(body.get("password"))
        request = {"action": "connect", "ssid": ssid, "password": password, "hidden": body.get("hidden") is True,
                   "country": check_country(body.get("country"))}
        rid = self._begin("connect", request, job={"ssid": ssid, "state": "waiting", "message": None,
                                                   "started": _now(), "finished": None})
        log.info("WLAN: verbinde mit „%s“%s", ssid, " (verstecktes Netz)" if request["hidden"] else "")   # no password
        threading.Thread(target=self._follow, args=(rid,), name="wifi-connect", daemon=True).start()
        return {"message": f"Verbinde mit „{ssid}“ …", "wifi": self.view(local)}

    def forget(self, body: dict, local: str | None = None) -> dict:
        ssid = check_ssid(body.get("ssid"))
        with self._lock:
            net = self._net
        if _only_link(net, ssid):   # quick answer from the last known state; install.py checks again
            raise ActionError(ONLY_LINK.format(ssid=ssid), 409)
        status = self._ask("forget", {"action": "forget", "ssid": ssid})
        if status.get("state") != "done":
            log.warning("WLAN „%s“ vergessen hat nicht geklappt: %s", ssid, _text(status.get("message"), 300))
            raise ActionError(_text(status.get("message"), 300) or "Das hat nicht geklappt.", 502)
        log.info("WLAN: %s", _text(status.get("message"), 300) or f"„{ssid}“ vergessen")
        return {"message": _text(status.get("message"), 300), "wifi": self.view(local)}

    # ------------------------------------------------------------------ talking to the WLAN service
    def _begin(self, action: str, request: dict, job: dict | None = None) -> str:
        if not self.supported:
            raise ActionError("WLAN einrichten geht nur, wenn HoLiTherm mit dem Installationsbefehl auf dem "
                              "Raspberry Pi eingerichtet ist.", 409)
        rid = secrets.token_hex(8)
        with self._lock:
            if self._doing:
                raise ActionError(BUSY[self._doing], 409)
            self._doing, self._owner = action, rid
        try:
            self._place(rid, request)
        except BaseException:
            self._release(rid)
            raise
        if job is not None:   # an answer that comes before this is read again by _follow()
            with self._lock:
                self._job = {"id": rid, **job}
        return rid

    def _release(self, rid: str) -> None:
        with self._lock:
            if self._owner == rid:
                self._doing = self._owner = None

    def _ask(self, action: str, request: dict) -> dict:
        rid = self._begin(action, request)
        try:
            return self._wait(rid, self.answer)
        finally:
            self._release(rid)

    def _follow(self, rid: str) -> None:
        """Background thread of a connection: takes the request back if nobody picks it up (password!)."""
        message = None
        try:
            self._wait(rid, self.connect_time)   # _absorb() records the result in the job and frees the way
        except ActionError as exc:
            message = str(exc)
        except Exception as exc:  # never leave the page at „verbinde …“
            log.exception("WLAN: Fehler beim Verbinden")
            message = f"interner Fehler: {exc}"
        with self._lock:
            job = self._job
            if message and job and job["id"] == rid and job["state"] in ("waiting", "connecting"):
                job.update(state="failed", message=message, finished=_now())
                log.warning("WLAN: Verbinden mit „%s“ hat nicht geklappt – %s", job.get("ssid"), message)
            if self._owner == rid:
                self._doing = self._owner = None

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
                raise ActionError("Der WLAN-Dienst auf dem Pi reagiert nicht. Bitte den Pi einmal neu starten; hilft das "
                                  "nicht, den Installationsbefehl noch einmal per SSH ausführen.", 503)
            if waited > limit:
                raise ActionError("Der WLAN-Dienst hat nicht rechtzeitig geantwortet. Bitte nachsehen, ob das WLAN "
                                  "verbunden ist, und es sonst noch einmal versuchen.", 504)
            time.sleep(self.poll)

    def _place(self, rid: str, request: dict) -> None:
        """The request for the WLAN service: written as a new file (0600) and renamed into place."""
        tmp = self.folder / f".{REQUEST}.{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"id": rid, **request}, fh, ensure_ascii=False)
            os.replace(tmp, self.folder / REQUEST)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise ActionError(f"Die Anfrage an den WLAN-Dienst ließ sich nicht ablegen ({exc.strerror}).", 500) from exc

    def _withdraw(self, rid: str) -> bool:
        """Take our request back if the WLAN service has not picked it up. True if it was still there."""
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

    def _absorb(self, status: dict, networks: bool = True) -> None:
        """Take what the WLAN service reported: network state, networks in range, the job's progress."""
        net, found = _net(status.get("net")), _networks(status.get("networks")) if networks else None
        state, at = status.get("state"), _text(status.get("at"), 40) or None
        with self._lock:
            if net is not None:
                self._net, self._net_at = net, at
            if found is not None and state in ("done", "failed"):
                self._networks, self._scanned = found, at
            job = self._job
            if job and job["id"] and status.get("id") == job["id"]:
                if state == "running" and job["state"] == "waiting":
                    job["state"] = "connecting"
                elif state in ("done", "failed") and job["state"] != state:
                    job.update(state=state, message=_text(status.get("message"), 300), finished=at or _now())
                    log.log(logging.INFO if state == "done" else logging.WARNING, "WLAN: %s",
                            job["message"] or f"Verbinden mit „{job.get('ssid')}“ {'fertig' if state == 'done' else 'fehlgeschlagen'}")
                    if self._owner == job["id"]:   # in the same step, so the page never sees "done" but busy
                        self._doing = self._owner = None
