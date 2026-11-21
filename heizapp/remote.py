"""Fernwartung (Einrichtung › Fernwartung): den Pi mit Tailscale aus der Ferne erreichbar machen.

Der Pi kommt dabei in das private Tailscale-Netz dessen, der ihn anmeldet; von dort lässt sich die App öffnen –
ohne Freigaben im Router. Die App darf am System nichts ändern. Sie legt nur remote-request.json in ihren
Datenordner, darin steht nichts als eine von vier Aktionen (status, up, down, logout), und
flowthermpi-remote.path startet daraufhin als root „install.py --remote“. Das installiert Tailscale beim ersten
„up“ aus dessen eigenem Paketarchiv, meldet den Pi als flowthermpi-<Gerätenummer> an und schreibt Tailscales
Zustand nach remote-status.json – solange eine Anmeldung offen ist, mit Anmelde-Link und QR-Code. Wer mit diesem
Link die Anmeldung abschließt, bestimmt, in wessen Tailscale-Netz der Pi kommt. Der Link steht deshalb nur in der
Einrichtung (hinter der PIN), nie in /api/state.

Den Reiter gibt es nur, wenn FlowThermPi mit install.py eingerichtet ist: dessen systemd-Unit setzt
HEIZAPP_REMOTE_DIR.
"""
from __future__ import annotations

import hashlib
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

log = logging.getLogger("heizapp")
REQUEST, STATUS = "remote-request.json", "remote-status.json"
STATES = ("NoState", "NeedsLogin", "NeedsMachineAuth", "Stopped", "Starting", "Running")
PICKUP = 15.0                # seconds: by then the root service has taken the request (it starts within a second)
ANSWER = 90.0                # seconds for status, down and logout (logging out waits for Tailscale's servers)
UP_TIME = 25 * 60.0          # seconds for „up“: the first time it installs Tailscale (systemd stops it after 20 min)
WATCH_EVERY = 5.0            # seconds between two looks at the Pi while a login is open …
WATCH_FOR = 15 * 60.0        # … for at most this long; after that the page offers „Neu prüfen“
REFRESH_EVERY = 6 * 3600.0   # now and then the state is looked up again (someone may change it on the Pi itself)
MAX_STATUS = 1 << 20
BUSY = {"up": "Die Fernwartung wird gerade eingerichtet – bitte einen Moment warten.",
        "down": "Einen Moment bitte – die Fernwartung wird gerade ausgeschaltet.",
        "logout": "Einen Moment bitte – der Pi meldet sich gerade bei Tailscale ab.",
        "status": "Einen Moment bitte – der Pi wird gerade gefragt."}
LOGIN_URL = re.compile(r"https://[A-Za-z0-9.-]+(:\d{1,5})?/[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]*")
QR_CODE = re.compile(r"data:image/png;base64,[A-Za-z0-9+/]+={0,2}")
DNS_NAME = re.compile(r"[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){0,8}")
IP = re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){3}|[0-9a-fA-F:]{2,39}")
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)")
TAG = re.compile(r"tag:[A-Za-z][A-Za-z0-9-]{0,62}")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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
    return hashlib.sha256(f"flowthermpi:{machine}".encode()).hexdigest()[:8] if re.fullmatch(r"[0-9a-f]{32}", machine) else None


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
        if self.folder:
            status = self._read_status()   # from before a restart of the app
            if status:
                self._absorb(status)

    @property
    def supported(self) -> bool:
        return self.folder is not None

    def start(self) -> None:
        """When the app starts: look at the Pi once, in the background."""
        if self.supported:
            with self._lock:
                self._refreshing = True
            threading.Thread(target=self._quiet_refresh, name="remote-refresh", daemon=True).start()

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
                      "watching": self._watching, "error": self._error}
        result["via_tailscale"] = bool(local and ts and local in ts.get("ips", []))
        result["lan"] = socket.gethostname().split(".")[0].lower()   # name.local in the home network
        return result

    def summary(self) -> dict:
        """For everyone, also the residents' view: only whether remote maintenance is on – never the login link."""
        if not self.supported:
            return {"supported": False}
        self._refresh_if_old()
        with self._lock:
            return {"supported": True, "on": bool(self._ts and self._ts.get("state") == "Running")}

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
        installs Tailscale, which takes a few minutes."""
        rid = self._begin("up", job={"state": "waiting", "phase": None, "message": None, "started": _now(), "finished": None})
        threading.Thread(target=self._follow, args=(rid,), name="remote-up", daemon=True).start()
        return {"remote": self.view(local)}

    def down(self, local: str | None = None) -> dict:
        return self._act("down", local)

    def logout(self, local: str | None = None) -> dict:
        """Leave the tailnet – also ends a login that is still open."""
        return self._act("logout", local)

    def _act(self, action: str, local: str | None) -> dict:
        status = self._ask(action)
        if status.get("state") != "done":
            raise ActionError(_text(status.get("message"), 300) or "Das hat nicht geklappt.", 502)
        return {"message": _text(status.get("message"), 300), "remote": self.view(local)}

    # ------------------------------------------------------------------ looking at the Pi in the background
    def _hostname(self) -> str:
        return f"flowthermpi-{self.device}" if self.device else "flowthermpi"

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
    def _begin(self, action: str, job: dict | None = None, wait: float = 10.0) -> str:
        if not self.supported:
            raise ActionError("Fernwartung gibt es nur, wenn FlowThermPi mit dem Installationsbefehl auf dem Raspberry Pi "
                              "eingerichtet ist.", 409)
        rid = secrets.token_hex(8)
        deadline = time.monotonic() + wait
        with self._free:
            while self._doing:   # a quick look at the Pi is under way: wait for it – anything else means „busy“
                left = deadline - time.monotonic()
                if self._doing != "status" or left <= 0:
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
            raise ActionError(f"Die Anfrage an den Dienst für die Fernwartung ließ sich nicht ablegen ({exc.strerror}).",
                              500) from exc

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
        with self._free:
            if isinstance(device, str) and re.fullmatch(r"[0-9a-f]{8}", device):
                self.device = device   # as the Pi names itself in Tailscale
            if ts is not None:
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
                    if self._owner == job["id"]:   # in the same step, so the page never sees "done" but busy
                        self._doing = self._owner = None
                        self._free.notify_all()
