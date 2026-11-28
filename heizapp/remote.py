"""Fernwartung (Einrichtung › Fernwartung): den Pi mit Tailscale aus der Ferne erreichbar machen.

Der Pi kommt dabei in das private Tailscale-Netz dessen, der ihn anmeldet; von dort lässt sich die App öffnen –
ohne Freigaben im Router. Die App darf am System nichts ändern. Sie legt nur remote-request.json in ihren
Datenordner, darin steht nichts als eine von fünf Aktionen (status, up, on, down, logout), und
holitherm-remote.path startet daraufhin als root „install.py --remote“. Das installiert Tailscale beim ersten
„up“ aus dessen eigenem Paketarchiv, meldet den Pi als holitherm-<Gerätenummer> mit dem Tag tag:holitherm an
und schreibt Tailscales Zustand nach remote-status.json – solange eine Anmeldung offen ist, mit Anmelde-Link und
QR-Code. Wer mit diesem Link die Anmeldung abschließt, bestimmt, in wessen Tailscale-Netz der Pi kommt. Der Link
steht deshalb nur in der Einrichtung (hinter der PIN), nie in /api/state.

Fernhilfe: In der Einstellung „ask“ (Vorgabe) ist Tailscale nur an, solange die Bewohner es erlaubt haben –
„Fernhilfe erlauben“ in der Heizungsansicht schaltet es für zwei Stunden ein („on“ schaltet nur ein, was schon
eingerichtet ist), danach schaltet ein Wächter es wieder aus. Erlauben und verlängern geht nur im Heimnetz, nicht
aus der Ferne; beenden geht überall. Mit „always“ bleibt die Fernwartung dauerhaft an. Einstellung und Ende der
Fernhilfe stehen in remote-settings.json, damit sie einen Neustart überstehen.

Den Reiter gibt es nur, wenn HoLiTherm mit install.py eingerichtet ist: dessen systemd-Unit setzt
HEIZAPP_REMOTE_DIR.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .service import ActionError

log = logging.getLogger(__name__)   # never the login link (logs.py hides one that slips through)
REQUEST, STATUS, SETTINGS = "remote-request.json", "remote-status.json", "remote-settings.json"
MODES = ("ask", "always")    # Fernhilfe only when the residents allow it | always reachable
STATES = ("NoState", "NeedsLogin", "NeedsMachineAuth", "Stopped", "Starting", "Running")
PICKUP = 15.0                # seconds: by then the root service has taken the request (it starts within a second)
ANSWER = 90.0                # seconds for status, down and logout (logging out waits for Tailscale's servers)
UP_TIME = 25 * 60.0          # seconds for „up“: the first time it installs Tailscale (systemd stops it after 20 min)
WATCH_EVERY = 5.0            # seconds between two looks at the Pi while a login is open …
WATCH_FOR = 15 * 60.0        # … for at most this long; after that the page offers „Neu prüfen“
REFRESH_EVERY = 6 * 3600.0   # now and then the state is looked up again (someone may change it on the Pi itself)
HELP_TIME = 2 * 3600.0       # seconds a Fernhilfe lasts
GUARD_EVERY = 30.0           # seconds between two looks of the guard (it also wakes when a Fernhilfe ends)
# where Tailscale's addresses come from: a browser talking to the Pi on one of them comes from afar
TAILSCALE_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
MAX_STATUS = 1 << 20
BUSY = {"up": "Die Fernwartung wird gerade eingerichtet – bitte einen Moment warten.",
        "on": "Einen Moment bitte – die Fernhilfe wird gerade eingeschaltet.",
        "down": "Einen Moment bitte – die Fernwartung wird gerade ausgeschaltet.",
        "logout": "Einen Moment bitte – der Pi meldet sich gerade bei Tailscale ab.",
        "status": "Einen Moment bitte – der Pi wird gerade gefragt."}
LOGIN_URL = re.compile(r"https://[A-Za-z0-9.-]+(:\d{1,5})?/[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]*")
QR_CODE = re.compile(r"data:image/png;base64,[A-Za-z0-9+/]+={0,2}")
DNS_NAME = re.compile(r"[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){0,8}")
IP = re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){3}|[0-9a-fA-F:]{2,39}")
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)")
TAG = re.compile(r"tag:[A-Za-z][A-Za-z0-9-]{0,62}")
STATE_WORDS = {"NoState": "unbekannt", "NeedsLogin": "nicht angemeldet", "NeedsMachineAuth": "wartet auf Freigabe im "
               "Tailscale-Netz", "Stopped": "aus", "Starting": "startet", "Running": "an"}


def _when(text) -> str:
    """ISO time -> "22.11. 14:27", for the log."""
    try:
        return datetime.fromisoformat(str(text)).strftime("%d.%m. %H:%M") if text else "?"
    except ValueError:
        return str(text)


def _state_word(ts: dict | None) -> str:
    """Tailscale's state for the log."""
    if not ts:
        return "unbekannt"
    if not ts.get("installed"):
        return "nicht installiert"
    if not ts.get("daemon"):
        return "Dienst läuft nicht" + (f" ({ts['problem']})" if ts.get("problem") else "")
    return STATE_WORDS.get(ts.get("state"), str(ts.get("state")))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _later(seconds: float) -> str:
    at = datetime.now().astimezone() + timedelta(seconds=seconds)
    if at.microsecond:   # rounded up: a Fernhilfe never ends before the time it promised
        at = at.replace(microsecond=0) + timedelta(seconds=1)
    return at.isoformat(timespec="seconds")


def _time(text: str | None) -> datetime | None:
    try:
        at = datetime.fromisoformat(text) if text else None
    except ValueError:
        return None
    return at if at and at.tzinfo else None


def _text(value, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def device_number(root: str | os.PathLike = "/") -> str | None:
    """Eight hex digits that tell this Pi from others: the end of its serial number, part of its name in Tailscale.
    The same as device_number in install.py, which gives the Pi that name."""
    root = Path(root)
    serial = ""
    try:
        serial = (root / "sys/firmware/devicetree/base/serial-number").read_bytes().decode("ascii", "replace")
    except OSError:
        pass
    if not serial.strip("\x00 \t\n"):
        try:
            found = re.search(r"^Serial\s*:\s*(\S+)\s*$", (root / "proc/cpuinfo").read_text(encoding="ascii", errors="replace"), re.M)
            serial = found.group(1) if found else ""
        except OSError:
            pass
    serial = serial.strip("\x00 \t\n").lower()
    if re.fullmatch(r"[0-9a-f]{8,}", serial) and int(serial, 16):
        return serial[-8:]
    try:   # not a Raspberry Pi: derived from the machine id (which itself should not be shown)
        machine = (root / "etc/machine-id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return hashlib.sha256(f"holitherm:{machine}".encode()).hexdigest()[:8] if re.fullmatch(r"[0-9a-f]{32}", machine) else None


def _tailscale(value) -> dict | None:
    """Tailscale's state as the root service reported it."""
    if not isinstance(value, dict):
        return None
    if value.get("installed") is not True:
        return {"installed": False}
    if value.get("daemon") is not True:
        return {"installed": True, "daemon": False, "problem": _text(value.get("problem"), 200)}
    url, dns, expiry = value.get("auth_url"), _text(value.get("dns_name"), 253), _text(value.get("key_expiry"), 40)
    dns = dns if dns and DNS_NAME.fullmatch(dns) else None
    host = _text(value.get("host_name"), 63)
    version = _text(value.get("version"), 20)
    ips = [ip for ip in value.get("ips") or [] if isinstance(ip, str) and IP.fullmatch(ip)][:4] \
        if isinstance(value.get("ips"), list) else []
    tags = [tag for tag in value.get("tags") or [] if isinstance(tag, str) and TAG.fullmatch(tag)][:5] \
        if isinstance(value.get("tags"), list) else []
    return {"installed": True, "daemon": True,
            "state": value.get("state") if value.get("state") in STATES else None,
            "version": version if version and re.fullmatch(r"\d[\d.]*", version) else None,
            "auth_url": url if isinstance(url, str) and len(url) <= 300 and LOGIN_URL.fullmatch(url) else None,
            "name": dns.split(".")[0] if dns else host if host and DNS_NAME.fullmatch(host) else None,
            "dns_name": dns, "ips": ips, "ipv4": next((ip for ip in ips if "." in ip), None),
            # a tag does not end the expiry: tags given in Tailscale's admin console keep it until it is disabled there
            "key_expiry": expiry if expiry and TIME.fullmatch(expiry) and not expiry.startswith("0001") else None,
            "tags": tags, "magic_dns": value.get("magic_dns") is True,
            "tailnet": _text(value.get("tailnet"), 100), "user": _text(value.get("user"), 100)}


class Remote:
    def __init__(self, folder: str | os.PathLike | None, poll: float = 0.25, root: str | os.PathLike = "/"):
        self.folder = Path(folder) if folder else None
        self.poll, self.pickup, self.answer, self.up_time = poll, PICKUP, ANSWER, UP_TIME
        self.watch_every, self.watch_for, self.refresh_every = WATCH_EVERY, WATCH_FOR, REFRESH_EVERY
        self.help_time, self.guard_every = HELP_TIME, GUARD_EVERY
        self.device = device_number(root) if self.folder else None
        self._lock = threading.Lock()                 # guards the fields below
        self._free = threading.Condition(self._lock)  # signalled when a request is answered
        self._doing: str | None = None   # "status" | "up" | "down" | "logout" – one request at a time …
        self._owner: str | None = None   # … namely this one (request id)
        self._job: dict | None = None    # the setup („up“) under way, or the last one
        self._ts: dict | None = None     # Tailscale, as last reported …
        self._at: str | None = None      # … at this time
        # (login link, its QR code or None) of the setup started here. Only this link is shown: one that was open
        # before – e.g. from „tailscale up“ by hand – would sign the Pi in under its plain name.
        self._login: tuple[str, str | None] | None = None
        self._watching = False           # a thread looks at the Pi every few seconds while a login is open
        self._refreshing = False
        self._tried = time.monotonic()   # last look at the Pi (start() looks at once)
        self._error: str | None = None   # why the last look at the Pi failed
        # {"mode": "ask" | "always", "until": end of the Fernhilfe or None} – as saved in remote-settings.json
        self._settings = self._load_settings() if self.folder else {"mode": "ask", "until": None}
        self._wake = threading.Event()   # the guard looks at once (a Fernhilfe began or ended, the state changed)
        self._halt = threading.Event()
        if self.folder:
            status = self._read_status()   # from before a restart of the app
            if status:
                self._absorb(status)

    @property
    def supported(self) -> bool:
        return self.folder is not None

    def start(self) -> None:
        """When the app starts: look at the Pi once, in the background, and start the guard of the Fernhilfe."""
        if self.supported:
            with self._lock:
                self._refreshing = True
            threading.Thread(target=self._quiet_refresh, name="remote-refresh", daemon=True).start()
            threading.Thread(target=self._guard, name="remote-guard", daemon=True).start()

    def stop(self) -> None:
        """Ends the guard (for tests – in the app it runs as long as the app does)."""
        self._halt.set()
        self._wake.set()

    # ------------------------------------------------------------------ what the page shows
    def view(self, local: str | None = None) -> dict:
        """local: the Pi's own address this browser talks to (tells whether it comes through Tailscale)."""
        if not self.supported:
            return {"supported": False}
        status = self._read_status()
        if status:
            self._absorb(status)
        with self._lock:
            ts = dict(self._ts) if self._ts else None
            if ts and ts.get("daemon"):
                own = self._own_login(ts)
                ts["other_login"] = bool(ts.get("auth_url")) and not own   # „Fernwartung einrichten“ starts a new one
                ts["auth_url"] = ts["auth_url"] if own else None
                ts["qr"] = self._login[1] if own else None
            result = {"supported": True, "device": self.device, "hostname": self._hostname(), "tailscale": ts,
                      "at": self._at, "job": dict(self._job) if self._job else None, "busy": self._doing,
                      "watching": self._watching, "error": self._error, "via_tailscale": self._from_afar(local),
                      **self._help_view()}
        result["lan"] = socket.gethostname().split(".")[0].lower()   # name.local in the home network
        return result

    def summary(self, local: str | None = None) -> dict:
        """For everyone, also the residents' view: whether remote maintenance is on and until when the Fernhilfe
        lasts – never the login link."""
        if not self.supported:
            return {"supported": False}
        self._refresh_if_old()
        with self._lock:
            return {"supported": True, "on": bool(self._ts and self._ts.get("state") == "Running"),
                    "ready": self._ready(), "afar": self._from_afar(local), **self._help_view()}

    def _help_view(self) -> dict:
        """The Fernhilfe for the pages (call with the lock held): mode, its end while it lasts, how long one lasts."""
        until = self._until()
        active = until is not None and until > datetime.now().astimezone()
        return {"mode": self._settings["mode"], "until": self._settings["until"] if active else None,
                "help_minutes": round(self.help_time / 60)}

    # ------------------------------------------------------------------ the actions
    def refresh(self, local: str | None = None) -> dict:
        with self._lock:
            self._tried = time.monotonic()
        try:
            status = self._ask("status")
        except ActionError as exc:
            with self._lock:
                self._error = str(exc)
            raise
        with self._lock:
            self._error = None
        if status.get("state") != "done":
            raise ActionError(_text(status.get("message"), 300) or "Das Nachsehen hat nicht geklappt.", 502)
        self._watch_if_waiting()
        return {"remote": self.view(local)}

    def up(self, local: str | None = None) -> dict:
        """Set up or switch on; answers at once, the page follows the job (GET /api/remote). The first time this
        installs Tailscale, which takes a few minutes. In „ask“ mode it is a Fernhilfe: on for two hours."""
        self._require()
        with self._lock:
            before, mine = self._open_help(local) if self._settings["mode"] == "ask" else (None, None)
        try:
            rid = self._begin("up", job={"state": "waiting", "phase": None, "message": None, "started": _now(),
                                         "finished": None})
        except BaseException:
            self._reset_help(mine, before)
            raise
        log.info("Fernwartung: einrichten bzw. einschalten angestoßen%s", " – als Fernhilfe bis " + mine[11:16]
                 if mine else "")
        threading.Thread(target=self._follow, args=(rid,), name="remote-up", daemon=True).start()
        return {"remote": self.view(local)}

    def down(self, local: str | None = None) -> dict:
        """Switch off – a Fernhilfe ends with it."""
        return self._switch_off("down", local)

    def logout(self, local: str | None = None) -> dict:
        """Leave the tailnet – also ends a login that is still open, and a Fernhilfe."""
        return self._switch_off("logout", local)

    def help(self, local: str | None = None) -> dict:
        """„Fernhilfe erlauben“ in the residents' view: the Pi is reachable for the next two hours, then the guard
        switches it off. Pressed again, the two hours start anew. Only switches on what is set up („on“)."""
        self._require()
        with self._lock:
            if self._settings["mode"] != "ask":
                raise ActionError("Die Fernwartung ist hier dauerhaft eingeschaltet – eine Freigabe ist nicht nötig.", 409)
            if not self._ready():
                raise ActionError("Die Fernwartung ist auf diesem Pi noch nicht eingerichtet.", 409)
            before, mine = self._open_help(local)
        try:
            status = self._ask("on")
            if status.get("state") != "done":
                raise ActionError(_text(status.get("message"), 300) or "Die Fernhilfe ließ sich nicht einschalten.", 502)
        except BaseException as exc:
            self._reset_help(mine, before)
            log.warning("Fernhilfe ließ sich nicht einschalten: %s", exc)
            raise
        result = self.summary(local)
        until = _time(result["until"])
        log.info("Fernhilfe von den Bewohnern erlaubt – bis %s", f"{until:%H:%M}" if until else "?")
        message = (f"Die Fernhilfe ist an – bis {until:%H:%M} Uhr." if result["on"] and until
                   else _text(status.get("message"), 300))
        return {"message": message, "remote": result}

    def end(self, local: str | None = None) -> dict:
        """„Beenden“: the Fernhilfe ends now. Also from afar – when the helper is done."""
        self._require()
        with self._lock:
            if self._settings["mode"] != "ask":
                raise ActionError("Die Fernwartung ist hier dauerhaft eingeschaltet. Ausschalten lässt sie sich in "
                                  "der Einrichtung.", 409)
        self._switch_off("down", local)
        log.info("Fernhilfe beendet (Knopf „Beenden“%s)", ", aus der Ferne" if self._from_afar(local) else "")
        return {"message": "Die Fernhilfe ist beendet.", "remote": self.summary(local)}

    def set_mode(self, mode, local: str | None = None) -> dict:
        """Einrichtung: „ask“ – reachable only during a Fernhilfe – or „always“. Only in the home network: from afar,
        nobody may give themselves more than the residents allowed."""
        self._require()
        if mode not in MODES:
            raise ActionError("Unbekannte Einstellung für die Fernwartung.", 400)
        with self._lock:
            if self._from_afar(local):
                raise ActionError("Das lässt sich nur hier im Heimnetz ändern, nicht aus der Ferne.", 403)
            same = mode == self._settings["mode"]   # as it is: a Fernhilfe under way goes on
            if not same:
                self._store(mode=mode, until=None)
            state = (self._ts or {}).get("state")
        if same:
            return {"message": None, "remote": self.view(local)}
        log.info("Fernwartung: Einstellung jetzt „%s“", "nur nach Freigabe der Bewohner" if mode == "ask" else "immer an")
        self._wake.set()
        message = ("Gespeichert: Die Fernwartung ist nur an, wenn die Bewohner sie erlauben." if mode == "ask"
                   else "Gespeichert: Die Fernwartung ist immer an.")
        if mode == "ask" and state in ("Running", "Starting"):
            self._act("down")   # off now – the guard would do it a moment later
            message += " Sie ist jetzt aus."
        elif mode == "always" and state == "Stopped":
            self._act("on")
            message += " Sie ist jetzt eingeschaltet."
        return {"message": message, "remote": self.view(local)}

    def _act(self, action: str, local: str | None = None) -> dict:
        status = self._ask(action)
        if status.get("state") != "done":
            raise ActionError(_text(status.get("message"), 300) or "Das hat nicht geklappt.", 502)
        return {"message": _text(status.get("message"), 300), "remote": self.view(local)}

    def _switch_off(self, action: str, local: str | None) -> dict:
        with self._lock:   # a Fernhilfe ends first: if switching off fails, the guard tries again
            ended = self._end_help()
        try:
            result = self._act(action, local)
        except ActionError as exc:
            log.warning("Fernwartung %s hat nicht geklappt: %s", "ausschalten" if action == "down" else "abmelden", exc)
            raise
        log.info("Fernwartung: %s", result.get("message") or ("ausgeschaltet" if action == "down" else "abgemeldet"))
        self._reset_help(ended, None)   # over – the guard has nothing left to do
        return result

    # ------------------------------------------------------------------ the Fernhilfe and its guard
    def _open_help(self, local: str | None) -> tuple[str | None, str]:
        """A Fernhilfe of two hours from now (call with the lock held) – saved before the Pi is switched on, so the
        guard leaves it on. Returns (the end saved before, the new one)."""
        if self._from_afar(local):
            raise ActionError("Fernhilfe erlauben oder verlängern können nur die Bewohner, hier im Heimnetz – nicht "
                              "aus der Ferne.", 403)
        before = self._settings["until"]
        self._store(until=_later(self.help_time))
        self._wake.set()
        return before, self._settings["until"]

    def _reset_help(self, expected: str | None, value: str | None) -> None:
        """The Fernhilfe ends at `value` instead – if it is still the one ending at `expected` (none began meanwhile).
        After a failure that is the end saved before; after switching off, None: over."""
        if expected is None:
            return
        with self._lock:
            if self._settings["until"] == expected:
                self._note(until=value)
        self._wake.set()

    def _end_help(self) -> str | None:
        """A Fernhilfe that lasts ends now (call with the lock held). Returns its end as it is now, or None."""
        until = self._until()
        if until is None:
            return None
        if until > datetime.now().astimezone():
            self._note(until=_now())
        return self._settings["until"]

    def _guard(self) -> None:
        """In „ask“ mode Tailscale is on only during a Fernhilfe: the guard switches it off when one ends, and when
        the Pi is found on without one (after a restart of the app, or switched on by hand)."""
        while not self._halt.is_set():
            self._wake.clear()   # before looking: what changes after this wakes the next wait at once
            try:
                wait = self._enforce()
            except Exception:  # the guard must keep going
                log.exception("Fernwartung: Fehler beim Wächter der Fernhilfe")
                wait = self.guard_every
            self._wake.wait(wait)

    def _enforce(self) -> float:
        """One look of the guard. Returns the seconds until the next one."""
        now = datetime.now().astimezone()
        with self._lock:
            if self._settings["mode"] != "ask":
                return self.guard_every
            until = self._until()
            if until and until - now > timedelta(seconds=self.help_time + 60):   # the clock went back: one
                self._note(until=_later(self.help_time))                          # Fernhilfe at most
                until = self._until()
            if until and until > now:
                return max(0.05, min(self.guard_every, (until - now).total_seconds()))   # wakes when it ends
            on = (self._ts or {}).get("state") in ("Running", "Starting")
            if until is None and not on:
                return self.guard_every
            ended = self._settings["until"]
        try:
            status = self._ask("down")
        except ActionError as exc:
            if exc.status != 409:   # 409: something else is under way – it is tried again in a moment
                log.warning("Fernwartung: Ausschalten nach der Fernhilfe hat nicht geklappt: %s", exc)
            return self.guard_every if exc.status == 409 else 10 * self.guard_every
        if status.get("state") != "done":
            log.warning("Fernwartung: Ausschalten nach der Fernhilfe hat nicht geklappt: %s", status.get("message"))
            return 10 * self.guard_every
        self._reset_help(ended, None)
        log.info("Fernwartung: Fernhilfe zu Ende – Tailscale ist aus.")
        return self.guard_every

    def _until(self) -> datetime | None:
        return _time(self._settings["until"])

    def _ready(self) -> bool:
        """Tailscale is set up: signed in, on or off (call with the lock held)."""
        return bool(self._ts) and self._ts.get("state") in ("Running", "Stopped", "Starting")

    def _from_afar(self, local: str | None) -> bool:
        """The browser talks to the Pi through Tailscale (call with the lock held)."""
        if not local:
            return False
        if self._ts and local in self._ts.get("ips", []):
            return True
        try:
            address = ipaddress.ip_address(local)
        except ValueError:
            return False
        return any(address in net for net in TAILSCALE_NETS)

    def _load_settings(self) -> dict:
        try:
            raw = json.loads((self.folder / SETTINGS).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        raw = raw if isinstance(raw, dict) else {}
        until = raw.get("until")
        return {"mode": raw["mode"] if raw.get("mode") in MODES else "ask",
                "until": until if isinstance(until, str) and TIME.fullmatch(until) and _time(until) else None}

    def _store(self, **changes) -> None:
        """Change the settings and save them (call with the lock held). If saving fails, nothing changes."""
        settings = {**self._settings, **changes}
        self._write(SETTINGS, settings, "Die Einstellung der Fernwartung ließ sich nicht speichern")
        self._settings = settings

    def _note(self, **changes) -> None:
        """Like _store, for endings: they count even if saving fails (then only until the app restarts)."""
        try:
            self._store(**changes)
        except ActionError as exc:
            log.warning("Fernwartung: %s", exc)
            self._settings = {**self._settings, **changes}

    # ------------------------------------------------------------------ looking at the Pi in the background
    def _hostname(self) -> str:
        return f"holitherm-{self.device}" if self.device else "holitherm"

    def _own_login(self, ts: dict) -> bool:
        """Tailscale's open login link is the one the setup started here (call with the lock held)."""
        return bool(self._login and ts.get("auth_url") and ts["auth_url"] == self._login[0])

    def _waiting(self) -> bool:
        """A login is under way (call with the lock held)."""
        ts = self._ts or {}
        return ts.get("state") in ("NeedsMachineAuth", "Starting") or (ts.get("state") == "NeedsLogin" and self._own_login(ts))

    def _watch_if_waiting(self) -> None:
        with self._lock:
            if self._watching or not self._waiting():
                return
            self._watching = True
        threading.Thread(target=self._watch, name="remote-watch", daemon=True).start()

    def _watch(self) -> None:
        """While a login is open: look every few seconds, so the page sees at once when it is done."""
        end = time.monotonic() + self.watch_for
        try:
            while time.monotonic() < end:
                time.sleep(self.watch_every)
                with self._lock:
                    if not self._waiting():
                        return
                try:
                    self._ask("status", wait=0)
                except ActionError as exc:
                    if exc.status == 409:   # the page is asking something itself right now
                        continue
                    with self._lock:
                        self._error = str(exc)
                    return
                with self._lock:
                    self._error = None
        finally:
            with self._lock:
                self._watching = False

    def _refresh_if_old(self) -> None:
        with self._lock:
            if self._doing or self._refreshing or time.monotonic() - self._tried < self.refresh_every:
                return
            self._refreshing = True
        threading.Thread(target=self._quiet_refresh, name="remote-refresh", daemon=True).start()

    def _quiet_refresh(self) -> None:
        try:
            self.refresh()
        except ActionError as exc:
            log.warning("Fernwartung: %s", exc)
        except Exception:  # a background look must never take the app down
            log.exception("Fernwartung: Fehler beim Nachsehen")
        finally:
            with self._lock:
                self._refreshing = False

    # ------------------------------------------------------------------ talking to the root service
    def _require(self) -> None:
        if not self.supported:
            raise ActionError("Fernwartung gibt es nur, wenn HoLiTherm mit dem Installationsbefehl auf dem Raspberry Pi "
                              "eingerichtet ist.", 409)

    def _begin(self, action: str, job: dict | None = None, wait: float = 10.0) -> str:
        self._require()
        rid = secrets.token_hex(8)
        deadline = time.monotonic() + wait
        with self._free:
            while self._doing:   # a look at the Pi or switching off is over in a moment: wait – anything else is „busy“
                left = deadline - time.monotonic()
                if self._doing not in ("status", "down") or left <= 0:
                    raise ActionError(BUSY[self._doing], 409)
                self._free.wait(left)
            self._doing, self._owner = action, rid
        try:
            self._place(rid, {"action": action})
        except BaseException:
            self._release(rid)
            raise
        if job is not None:   # an answer that comes before this is read again by _follow()
            with self._lock:
                self._job = {"id": rid, **job}
        return rid

    def _release(self, rid: str) -> None:
        with self._free:
            if self._owner == rid:
                self._doing = self._owner = None
                self._free.notify_all()

    def _ask(self, action: str, wait: float = 10.0) -> dict:
        rid = self._begin(action, wait=wait)
        try:
            return self._wait(rid, self.answer)
        finally:
            self._release(rid)

    def _follow(self, rid: str) -> None:
        """Background thread of the setup: records how it ends, also when nobody picks the request up."""
        message = None
        try:
            self._wait(rid, self.up_time)   # _absorb() records the result in the job and frees the way
        except ActionError as exc:
            message = str(exc)
        except Exception as exc:  # never leave the page at „richte ein …“
            log.exception("Fernwartung: Fehler beim Einrichten")
            message = f"interner Fehler: {exc}"
        with self._free:
            job = self._job
            if message and job and job["id"] == rid and job["state"] in ("waiting", "running"):
                job.update(state="failed", phase=None, message=message, finished=_now())
                log.warning("Fernwartung einrichten hat nicht geklappt: %s", message)
            if self._owner == rid:
                self._doing = self._owner = None
                self._free.notify_all()
        self._watch_if_waiting()

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
                raise ActionError("Der Dienst für die Fernwartung reagiert nicht. Bitte den Pi einmal neu starten; hilft das "
                                  "nicht, den Installationsbefehl noch einmal per SSH ausführen.", 503)
            if waited > limit:
                raise ActionError("Der Dienst für die Fernwartung hat nicht rechtzeitig geantwortet. Bitte gleich noch "
                                  "einmal nachsehen.", 504)
            time.sleep(self.poll)

    def _place(self, rid: str, request: dict) -> None:
        """The request for the root service."""
        self._write(REQUEST, {"id": rid, **request}, "Die Anfrage an den Dienst für die Fernwartung ließ sich nicht ablegen")

    def _write(self, name: str, data: dict, failed: str) -> None:
        """A file in the folder: written as a new file (0600) and renamed into place."""
        tmp = self.folder / f".{name}.{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.folder / name)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise ActionError(f"{failed} ({exc.strerror}).", 500) from exc

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

    def _absorb(self, status: dict) -> None:
        """Take what the root service reported: Tailscale's state, the login's QR code, the job's progress."""
        ts, qr, device = _tailscale(status.get("tailscale")), status.get("qr"), status.get("device")
        login, state, at = status.get("login_url"), status.get("state"), _text(status.get("at"), 40)
        follow = False
        with self._free:
            if isinstance(device, str) and re.fullmatch(r"[0-9a-f]{8}", device):
                self.device = device   # as the Pi names itself in Tailscale
            if ts is not None:
                if ts.get("state") != (self._ts or {}).get("state"):
                    self._wake.set()   # e.g. on now: the guard decides whether that may be
                if _state_word(ts) != _state_word(self._ts):
                    log.info("Fernwartung: Tailscale %s%s", _state_word(ts),
                             f" (vorher {_state_word(self._ts)})" if self._ts else f" (Stand vom {_when(at)})")
                self._ts, self._at = ts, at
                if not ts.get("auth_url"):
                    self._login = None   # signed in, logged out or given up: no link is open any more
                elif isinstance(login, str) and login == ts["auth_url"]:   # the setup's own login (ts is checked)
                    self._login = (login, qr if isinstance(qr, str) and len(qr) <= 100_000 and QR_CODE.fullmatch(qr) else None)
            job = self._job
            if job and status.get("id") == job["id"]:
                if state == "running" and job["state"] in ("waiting", "running"):
                    phase = status.get("phase")
                    job.update(state="running", phase=phase if phase in ("installing", "connecting") else job["phase"])
                elif state in ("done", "failed") and job["state"] not in ("done", "failed"):
                    job.update(state=state, phase=None, message=_text(status.get("message"), 300), finished=at or _now())
                    log.log(logging.INFO if state == "done" else logging.WARNING, "Fernwartung einrichten: %s",
                            job["message"] or ("fertig" if state == "done" else "fehlgeschlagen"))
                    if self._owner == job["id"]:   # in the same step, so the page never sees "done" but busy …
                        self._doing = self._owner = None
                        self._free.notify_all()
                    if not self._watching and self._waiting():   # … or "done" with a login nobody follows
                        self._watching = follow = True
        if follow:
            threading.Thread(target=self._watch, name="remote-watch", daemon=True).start()
