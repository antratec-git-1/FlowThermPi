"""„Protokoll speichern“: alles, was der Helfer braucht, wenn etwas schiefgeht – in einer ZIP-Datei.

Der Knopf steht unten in der Heizungsansicht (ohne PIN, damit die Bewohner ihn drücken können) und in der
Einrichtung › Über. Die Datei enthält:
    LIESMICH.txt     was drin ist
    uebersicht.txt   Zustand beim Speichern: Version, Heizungen, Räume, Fühler, WLAN, Fernwartung, Zigbee und die
                     letzten Warnungen
    app.log          das Protokoll der App (logs.py), ältester Eintrag zuerst
    system.txt       Systemprotokolle des Pi: Journal der Dienste, Kernel, Netz, Spannung, Temperatur, Neustarts
    daten.json       Räume, Heizpläne, Namen und Fühler aus data.json – ohne die PIN
Passwörter, Tokens und Anmelde-Links sind nicht darin: die App ersetzt sie beim Schreiben jeder Zeile, der root-Dienst
beim Einsammeln der Systemprotokolle, und beim Zusammenstellen wird alles noch einmal durchgesehen. Namen von Räumen
und Geräten, Adressen im Heimnetz und WLAN-Namen sind darin – darauf weist der Dialog hin.

Die Systemprotokolle darf die App nicht selbst lesen. Sie legt logs-request.json in ihren Datenordner,
flowthermpi-logs.path startet daraufhin als root „install.py --logs“. Das schreibt system-log.txt (nur für die App
lesbar) und logs-status.json. Diesen Teil gibt es nur, wenn FlowThermPi mit install.py eingerichtet ist (dessen
systemd-Unit setzt HEIZAPP_SYSLOG_DIR) – sonst steht in system.txt, warum er fehlt.

Danach kann die Seite eine E-Mail an den Helfer vorbereiten (mailto:). Die Adresse trägt er in der Einrichtung ein; die
Datei hängen die Bewohner selbst an – das kann ein Browser nicht für sie tun.

Ausführliches Protokoll: für DETAIL_HOURS Stunden auch die Einträge der Stufe DEBUG (logs.set_detail). Das Ende steht
in data.json, damit es einen Neustart übersteht; ein Wächter schaltet es dann wieder aus.
"""
from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import secrets
import socket
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import APP_NAME, __version__, logs
from .service import ActionError, Service

log = logging.getLogger(__name__)
REQUEST, STATUS, SYSLOG = "logs-request.json", "logs-status.json", "system-log.txt"
PICKUP = 15.0          # seconds: by then the root service has taken the request (it starts within a second)
ANSWER = 120.0         # seconds for collecting – journalctl over a week can take a while on a Pi
CACHE = 60.0           # seconds the system logs are used again (pressing the button twice asks root once)
MAX_SYSLOG = 8 << 20
MAX_STATUS = 1 << 16
KEEP = 15 * 60         # seconds a prepared file can be downloaded
MAX_FILES = 3          # prepared files kept at the same time
DETAIL_HOURS = 24
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){1,8}")
MODE_WORDS = {"manual": "Manuell", "auto": "Automatik", "boost": "Boost", "holiday": "Urlaub", "standby": "Standby",
              "off": "Aus"}
STATE_WORDS = {"done": "fertig", "failed": "fehlgeschlagen", "running": "läuft", "waiting": "wartet",
               "connecting": "verbindet"}


@dataclass(frozen=True)
class Download:
    """A file for the browser to save (instead of a JSON answer)."""
    name: str
    data: bytes
    ctype: str = "application/zip"


def _now() -> datetime:
    return datetime.now().astimezone()


def _text(value, limit: int = 300) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _time(text) -> datetime | None:
    try:
        at = datetime.fromisoformat(text) if isinstance(text, str) and text else None
    except ValueError:
        return None
    return at if at and at.tzinfo else None


def _deg(value) -> str:
    return "–" if value is None else f"{value:.1f} °C".replace(".", ",")


def _when(text) -> str:
    """ISO time -> "22.11. 14:03" (or "–")."""
    try:
        return datetime.fromisoformat(str(text)).strftime("%d.%m. %H:%M") if text else "–"
    except ValueError:
        return str(text)


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    parts = ([f"{days} Tg."] if days else []) + ([f"{hours} Std."] if hours else []) + [f"{minutes} Min."]
    return " ".join(parts)


# ---------------------------------------------------------------------- the Pi's system logs
class SystemLog:
    """The Pi's system logs, collected by the root service „install.py --logs“ (see the module's description)."""

    def __init__(self, folder: str | os.PathLike | None, poll: float = 0.25):
        self.folder = Path(folder) if folder else None
        self.poll, self.pickup, self.answer = poll, PICKUP, ANSWER
        self._lock = threading.Lock()   # one collection at a time
        self._cache: tuple[float, str] | None = None

    @property
    def supported(self) -> bool:
        return self.folder is not None

    def collect(self) -> tuple[str | None, str | None]:
        """(the system logs, None) – or (None, why they are missing)."""
        if not self.supported:
            return None, ("FlowThermPi ist hier nicht mit dem Installationsbefehl auf einem Raspberry Pi eingerichtet "
                          "(z. B. auf dem Mac oder in Docker) – dann sammelt die App keine Systemprotokolle.")
        with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < CACHE:
                return self._cache[1], None
            started = time.monotonic()
            try:
                text = self._ask()
            except ActionError as exc:
                log.warning("Systemprotokolle: %s", exc)
                return None, str(exc)
            log.info("Systemprotokolle gesammelt: %d KB in %.1f s", len(text.encode()) >> 10, time.monotonic() - started)
            self._cache = (time.monotonic(), text)
            return text, None

    def _ask(self) -> str:
        rid = secrets.token_hex(8)
        self._place(rid)
        start = time.monotonic()
        while True:
            status = self._read_status()
            if status and status.get("id") == rid and status.get("state") in ("done", "failed"):
                break
            waited = time.monotonic() - start
            if waited > self.pickup and self._withdraw(rid):
                raise ActionError("Der Dienst für die Systemprotokolle reagiert nicht. Bitte den Pi einmal neu starten; "
                                  "hilft das nicht, den Installationsbefehl noch einmal per SSH ausführen.", 503)
            if waited > self.answer:
                raise ActionError("Der Dienst für die Systemprotokolle hat nicht rechtzeitig geantwortet.", 504)
            time.sleep(self.poll)
        path = self.folder / SYSLOG
        try:
            if status["state"] != "done":
                raise ActionError(_text(status.get("message")) or "Die Systemprotokolle ließen sich nicht sammeln.", 502)
            try:
                with open(path, "rb") as fh:
                    data = fh.read(MAX_SYSLOG + 1)
            except OSError as exc:
                raise ActionError(f"Die Systemprotokolle fehlen ({exc.strerror}).", 502) from exc
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        text = data[:MAX_SYSLOG].decode("utf-8", "replace")
        return text + ("\n… (gekürzt)\n" if len(data) > MAX_SYSLOG else "")

    def _place(self, rid: str) -> None:
        """The request for the root service: written as a new file (0600) and renamed into place."""
        tmp = self.folder / f".{REQUEST}.{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"id": rid, "action": "collect"}, fh)
            os.replace(tmp, self.folder / REQUEST)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise ActionError(f"Die Anfrage an den Dienst für die Systemprotokolle ließ sich nicht ablegen "
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


# ---------------------------------------------------------------------- „Protokoll speichern“
class Support:
    def __init__(self, service: Service, updater=None, wifi=None, remote=None, zigbee=None,
                 syslog_dir: str | os.PathLike | None = None, password: bool = False, poll: float = 0.25):
        self.service, self.store = service, service.store
        self.updater, self.wifi, self.remote, self.zigbee = updater, wifi, remote, zigbee
        self.syslog = SystemLog(syslog_dir, poll)
        self.password = password
        self.started = _now()
        self._busy = threading.Lock()     # one file is put together at a time
        self._lock = threading.Lock()     # guards _files and _sizes
        self._files: dict[str, dict] = {}   # id -> {"name", "data", "made"}: prepared, ready for the download
        self._sizes: tuple[float, int, str | None] | None = None   # (when, bytes, oldest entry) of the log files
        self._wake = threading.Event()
        self._halt = threading.Event()

    # ------------------------------------------------------------------ Ausführliches Protokoll
    def start(self) -> None:
        """When the app starts: an Ausführliches Protokoll that was switched on goes on until its end."""
        until = self._until()
        if until and until > _now():
            logs.set_detail(True)
            log.info("Ausführliches Protokoll ist an – bis %s", until.strftime("%d.%m. %H:%M"))
        elif until:
            self._save_detail(None)
        threading.Thread(target=self._watch, name="protokoll", daemon=True).start()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()

    def _watch(self) -> None:
        """Ends the Ausführliches Protokoll on time – looks at least every minute (the clock of a Pi may jump)."""
        while not self._halt.is_set():
            self._wake.clear()
            until, now = self._until(), _now()
            if until and until <= now:
                if logs.detail():
                    log.info("Ausführliches Protokoll beendet (nach %d Stunden)", DETAIL_HOURS)
                logs.set_detail(False)
                self._save_detail(None)
                continue
            self._wake.wait(max(0.05, min(60.0, (until - now).total_seconds())) if until else 3600.0)

    def _until(self) -> datetime | None:
        return _time((self.store.data.get("settings") or {}).get("log_detail_until"))

    def _save_detail(self, until: datetime | None) -> None:
        with self.store.edit() as d:
            settings = d.setdefault("settings", {})
            if until:
                settings["log_detail_until"] = until.isoformat(timespec="seconds")
            else:
                settings.pop("log_detail_until", None)

    # ------------------------------------------------------------------ settings (Einrichtung › Über)
    def view(self) -> dict:
        """For /api/state – the residents' view needs the e-mail address for „E-Mail vorbereiten“."""
        settings = self.store.data.get("settings") or {}
        until = self._until()
        on = bool(until and until > _now())
        with self._lock:
            if self._sizes is None or time.monotonic() - self._sizes[0] > 30:
                self._sizes = (time.monotonic(), logs.size(), logs.oldest())
            _, size, oldest = self._sizes
        return {"email": settings.get("support_email") or None, "system": self.syslog.supported,
                "detail_until": until.isoformat(timespec="seconds") if on else None, "detail_hours": DETAIL_HOURS,
                "size": size, "oldest": oldest}

    def change(self, body: dict) -> dict:
        """The helper's e-mail address ("email", empty: none) and the Ausführliches Protokoll ("detail": true/false)."""
        messages = []
        if "email" in body:
            email = str(body.get("email") or "").strip()
            if email and (len(email) > 120 or not EMAIL.fullmatch(email)):
                raise ActionError("Das sieht nicht wie eine E-Mail-Adresse aus (z. B. name@beispiel.de).")
            with self.store.edit() as d:
                settings = d.setdefault("settings", {})
                if email:
                    settings["support_email"] = email
                else:
                    settings.pop("support_email", None)
            log.info("E-Mail-Adresse für Protokolle %s", "eingetragen" if email else "gelöscht")
            messages.append("E-Mail-Adresse gespeichert." if email else "E-Mail-Adresse gelöscht.")
        if "detail" in body:
            on = body.get("detail") is True
            until = _now().replace(microsecond=0) + timedelta(hours=DETAIL_HOURS) if on else None
            self._save_detail(until)
            if on:
                logs.set_detail(True)
                log.info("Ausführliches Protokoll eingeschaltet – bis %s", until.strftime("%d.%m. %H:%M"))
                messages.append(f"Ausführliches Protokoll an – bis {until:%d.%m. %H:%M} Uhr.")
            else:
                log.info("Ausführliches Protokoll ausgeschaltet")
                logs.set_detail(False)
                messages.append("Ausführliches Protokoll aus.")
            self._wake.set()
        return {"message": " ".join(messages) or None, "logs": self.view()}

    # ------------------------------------------------------------------ the file
    def prepare(self, local: str | None = None) -> dict:
        """Put the file together; the page then fetches it from GET /api/logs/<id>."""
        if not self._busy.acquire(blocking=False):
            raise ActionError("Das Protokoll wird gerade schon zusammengestellt – einen Moment bitte.", 409)
        try:
            started = time.monotonic()
            system, missing = self.syslog.collect()
            now = _now()
            name = f"flowthermpi-protokoll-{self._device()}-{now:%Y%m%d-%H%M}.zip"
            data = self._zip(now, system, missing, local)
            fid = secrets.token_hex(8)
            with self._lock:
                for old in [k for k, v in self._files.items() if time.monotonic() - v["made"] > KEEP]:
                    del self._files[old]
                while len(self._files) >= MAX_FILES:
                    del self._files[next(iter(self._files))]
                self._files[fid] = {"name": name, "data": data, "made": time.monotonic()}
            log.info("Protokoll zusammengestellt: %s, %d KB, %.1f s%s", name, len(data) >> 10, time.monotonic() - started,
                     "" if system is not None else " – ohne Systemprotokolle")
        finally:
            self._busy.release()
        email = (self.store.data.get("settings") or {}).get("support_email")
        mail = {"to": email, "subject": f"{APP_NAME}-Protokoll {self._device()} vom {now:%d.%m.%Y %H:%M}",
                "body": f"Hallo,\n\nanbei das Protokoll der Heizungs-App (die Datei „{name}“ aus dem Ordner "
                        "„Downloads“).\n\nWas ist passiert?\n\n\n"} if email else None
        return {"id": fid, "name": name, "size": len(data), "system": system is not None, "missing": missing,
                "mail": mail}

    def download(self, fid: str) -> Download:
        with self._lock:
            item = self._files.get(fid)
            if item is None or time.monotonic() - item["made"] > KEEP:
                raise ActionError("Dieses Protokoll ist nicht mehr da – bitte noch einmal „Protokoll speichern“.", 404)
        return Download(item["name"], item["data"])

    def _device(self) -> str:
        device = getattr(self.remote, "device", None)
        if device:
            return device
        return re.sub(r"[^A-Za-z0-9-]", "", socket.gethostname().split(".")[0])[:30] or "pi"

    def _zip(self, now: datetime, system: str | None, missing: str | None, local: str | None) -> bytes:
        app_log = logs.read_all()
        oldest = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d)", app_log)
        files = {
            "LIESMICH.txt": self._readme(now, oldest.group(1) if oldest else None, system is not None),
            "uebersicht.txt": self._overview(now, local),
            "app.log": app_log or "(Keine Protokolldatei – die App schreibt hier nur auf die Konsole.)\n",
            "system.txt": system if system is not None else f"Die Systemprotokolle des Pi fehlen in dieser Datei:\n{missing}\n",
            "daten.json": json.dumps(self._data(), ensure_ascii=False, indent=1) + "\n",
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, text in files.items():
                info = zipfile.ZipInfo(name, date_time=now.timetuple()[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, logs.redact(text).encode("utf-8"))   # once more: nothing secret leaves
        return buffer.getvalue()

    def _readme(self, now: datetime, oldest: str | None, system: bool) -> str:
        since = f" – seit {datetime.fromisoformat(oldest):%d.%m.%Y %H:%M}" if oldest else ""
        return (f"{APP_NAME} – Protokoll vom {now:%d.%m.%Y %H:%M} Uhr (Gerätenummer {self._device()})\n\n"
                "Erstellt mit „Protokoll speichern“ in der Heizungs-App, damit der Helfer sehen kann, was passiert\n"
                "ist. Darin:\n\n"
                "  uebersicht.txt  Zustand beim Speichern: Version, Heizungen, Räume, Fühler, WLAN, Fernwartung,\n"
                "                  Zigbee und die letzten Warnungen\n"
                f"  app.log         Protokoll der App, ältester Eintrag zuerst{since}\n"
                "  system.txt      Systemprotokolle des Raspberry Pi: Dienste, Netz, Spannung, Temperatur, Neustarts"
                + ("" if system else " (fehlen diesmal – warum, steht darin)") + "\n"
                "  daten.json      Räume, Heizpläne, Namen und Fühler, wie die App sie gespeichert hat (ohne PIN)\n\n"
                "Passwörter, Tokens der Heizungen und Anmelde-Links sind durch ••• ersetzt. Namen von Räumen und\n"
                "Geräten, Adressen im Heimnetz und WLAN-Namen sind enthalten.\n")

    def _data(self) -> dict:
        data = self.store.snapshot()
        settings = data.get("settings")
        if isinstance(settings, dict) and settings.get("pin"):
            settings["pin"] = logs.HIDDEN   # set – its hash stays at home
        return data

    def _overview(self, now: datetime, local: str | None) -> str:
        lines: list[str] = []
        add = lines.append
        state = self.service.state()
        data = self.store.snapshot()
        settings = data.get("settings") or {}
        installed = getattr(self.updater, "installed", None) or {}
        commit = installed.get("commit")
        add(f"{APP_NAME} – Übersicht vom {now:%d.%m.%Y %H:%M:%S} ({now:%z})")
        add("")
        add(f"Version:              {__version__}" + (f" (Commit {commit[:7]})" if commit else "")
            + (f", installiert {_when(installed.get('installed'))}" if installed.get("installed") else ""))
        add(f"Gerät:                {socket.gethostname()} · Gerätenummer {self._device()} · "
            f"Python {platform.python_version()} · {platform.system()} {platform.release()} ({platform.machine()})")
        add(f"App läuft seit:       {self.started:%d.%m.%Y %H:%M:%S} ({_duration((now - self.started).total_seconds())})")
        tz = getattr(self.service.tz, "key", None) or "Systemzeit"
        add(f"Uhrzeit:              {'abgeglichen' if self.service.clock_trusted() else 'NOCH NICHT abgeglichen'} · "
            f"Zeitzone {tz}")
        add(f"PIN der Einrichtung:  {'gesetzt' if settings.get('pin') else 'keine'} · Passwort für die App "
            f"(HEIZAPP_PASSWORD): {'gesetzt' if self.password else 'keins'}")
        detail = self.view()
        add(f"Protokoll:            {detail['size'] >> 10} KB" + (f", ältester Eintrag {_when(detail['oldest'])}" if detail["oldest"] else "")
            + (f" · ausführlich bis {_when(detail['detail_until'])}" if detail["detail_until"] else " · normal")
            + (" · E-Mail-Adresse eingetragen" if detail["email"] else ""))

        heaters = state["heaters"]
        rooms = {r["id"]: r for r in state["rooms"]}
        add("")
        add(f"Heizungen ({len(heaters)}, davon {sum(1 for h in heaters if h['online'])} erreichbar)")
        for h in heaters:
            room = rooms.get(h["room"] or "", {}).get("name")
            where = f"{h['name']} ({h['hostname']}) · {h['host']}" + (f" · Raum {room}" if room else " · ohne Raum")
            if h["online"]:
                what = (f"{_deg(h['room_temp'])}, Soll {_deg(h['setpoint'])}, {MODE_WORDS.get(h['mode'], h['mode'] or '?')}"
                        + (", heizt" if h["heating"] else "") + (", Fenster offen" if h["window_open"] else ""))
            else:
                what = "NICHT ERREICHBAR" + (f" seit {_when(h.get('offline_since'))}" if h.get("offline_since") else "")
            add(f"- {where} · {what}")
            if h["kind"] == "zigbee":
                extra = ["Zigbee-Thermostat" + ("" if h["paired"] else " (NICHT MEHR ANGELERNT)"),
                         f"Batterie {h['battery']} %" if h["battery"] is not None else "Batterie ?",
                         f"Funk {h['signal']}" if h["signal"] is not None else "Funk ?"]
                extra += [f"Störung: {', '.join(h['faults'])}"] if h["faults"] else []
            else:
                extra = [f"Token: {'ja' if h['writable'] else 'nein (nur Anzeige)'}"]
            if h["firmware"]:
                extra.append(f"Firmware {h['firmware']}" + (" (Update verfügbar)" if h.get("update") else ""))
            if h["serial"]:
                extra.append(f"{'Zigbee-Adresse' if h['kind'] == 'zigbee' else 'Seriennummer'} {h['serial']}")
            if h["clock_drift"] is not None:
                extra.append(f"Uhr {h['clock_drift']:+d} s")
            if h["temp_corr"] is not None:
                extra.append(f"Korrektur {h['temp_corr']:+.1f} °C".replace(".", ","))
            if h["plan_slot"]:
                extra.append(f"Plan {h['plan_slot']}")
            extra.append(f"zuletzt gesehen {_when(h['last_seen'])}")
            if h["error"]:
                extra.append(f"Fehler: {h['error']}")
            add("  " + " · ".join(extra))

        add("")
        add(f"Räume ({len(state['rooms'])})")
        names = {h["id"]: h["name"] for h in heaters}
        for r in state["rooms"]:
            plan = (f"Plan „{r['plan_name']}“" + (" – NICHT auf allen Heizungen aktuell" if r["plan_outdated"] else
                                                   f", übertragen {_when(r['plan_sent_at'])}")) if r["plan"] else "ohne Plan"
            members = ", ".join(names.get(i, i) for i in r["heaters"]) or "keine Heizung"
            add(f"- {r['name']}: {plan} · Heizungen: {members}" + (f" · {len(r['sensors'])} Fühler" if r["sensors"] else ""))
        add(f"Heizpläne: {len(state['plans'])}" + (": " + ", ".join(f"„{p['name']}“" for p in state["plans"])
                                                   if state["plans"] else ""))

        add("")
        add(f"Fühler ({len(state['sensors'])})")
        for s in state["sensors"]:
            room = rooms.get(s["room"] or "", {}).get("name")
            if s["online"]:
                status = "erreichbar"
            elif s["online"] is False:
                status = "NICHT ERREICHBAR" + (f" seit {_when(s['offline_since'])}" if s.get("offline_since") else "")
            else:
                status = "noch nicht gelesen"
            add(f"- {s['name']} ({s['model'] or '?'}) · {s['host'] or '?'} · {status} · {_deg(s['temp'])}"
                + (f", {s['humidity']:.0f} %" if s["humidity"] is not None else "") + f" (gemessen {_when(s['measured_at'])})"
                + (f" · Raum {room}" if room else "") + (f" · Fehler: {s['error']}" if s["error"] else "")
                + (" · Kalibrierung läuft" if (s.get("calibration") or {}).get("running") else ""))

        add("")
        add("Zusatzfunktionen")
        add(f"- Update:       {self._update_text()}")
        add(f"- WLAN:         {self._wifi_text(local)}")
        add(f"- Fernwartung:  {self._remote_text(local)}")
        add(f"- Zigbee:       {self._zigbee_text()}")
        add(f"- Systemprotokolle: {'werden vom Pi gesammelt' if self.syslog.supported else 'nicht eingerichtet'}")

        warnings = logs.recent()
        add("")
        add(f"Letzte Warnungen und Fehler seit dem Start der App ({len(warnings)})")
        lines.extend(warnings or ["(keine)"])
        return "\n".join(lines) + "\n"

    def _update_text(self) -> str:
        if not getattr(self.updater, "supported", False):
            return "nicht eingerichtet (von Hand aktualisieren)"
        status = self.updater.status()
        if not status:
            return "eingerichtet, noch kein Update per Knopf"
        return (f"zuletzt {STATE_WORDS.get(status.get('state'), status.get('state'))}, gestartet {_when(status.get('started'))}"
                + (f" – {status['message']}" if status.get("message") else ""))

    def _wifi_text(self, local: str | None) -> str:
        if not getattr(self.wifi, "supported", False):
            return "nicht eingerichtet"
        view = self.wifi.view(local)
        parts = []
        for i in (view.get("net") or {}).get("interfaces", []):
            kind = "Kabel" if i["type"] == "ethernet" else "WLAN"
            parts.append(f"{kind} {i['name']}: {'verbunden' if i['connected'] else i['state'] or 'getrennt'}"
                         + (f" mit „{i['ssid']}“" if i.get("ssid") else "") + (f", Signal {i['signal']} %" if i.get("signal") is not None else "")
                         + (f", {', '.join(i['ipv4'])}" if i.get("ipv4") else ""))
        job = view.get("job")
        if job:
            parts.append(f"zuletzt verbunden mit „{job.get('ssid')}“: {STATE_WORDS.get(job.get('state'), job.get('state'))}"
                         + (f" – {job['message']}" if job.get("message") else ""))
        return " · ".join(parts) or "noch kein Stand vom Pi"

    def _remote_text(self, local: str | None) -> str:
        if not getattr(self.remote, "supported", False):
            return "nicht eingerichtet"
        r = self.remote.summary(local)   # never view(): that holds the login link
        mode = "nur nach Freigabe der Bewohner" if r.get("mode") == "ask" else "immer an"
        return (f"{'an' if r.get('on') else 'aus'} · {'eingerichtet' if r.get('ready') else 'nicht angemeldet'} · {mode}"
                + (f" · Fernhilfe bis {_when(r['until'])}" if r.get("until") else ""))

    def _zigbee_text(self) -> str:
        if not getattr(self.zigbee, "supported", False):
            return "nicht eingerichtet"
        view = self.zigbee.view()
        p = view.get("programs")
        if not p:
            text = "noch kein Stand vom Pi"
        else:
            have = ", ".join(f"{part} {version}" for part, version in p["installed"].items() if version) or "nichts"
            text = (f"{'installiert' if p['complete'] else 'nicht (vollständig) installiert'}: {have}"
                    + (" · neue Versionen verfügbar" if p["complete"] and not p["current"] else "")
                    + f" · Mosquitto {p['mosquitto']['version'] or 'fehlt'}{' (läuft)' if p['mosquitto']['active'] else ''}"
                    + f" · USB: {', '.join(port['id'] for port in p['ports']) or 'kein Gerät'}")
            service, network = p["service"], p["network"]
            if service["present"]:
                text += (f" · Zigbee2MQTT {'an' if service['enabled'] else 'aus'}, "
                         f"{'läuft' if service['active'] else 'läuft nicht'}"
                         + (f", {service['restarts']} Neustart(s)" if service["restarts"] else "")
                         + f" · Kanal {network['channel'] or '?'} · Stick {network['port'] or '?'}"
                         f" · {network['devices']} Gerät(e) angelernt")
            elif p["complete"]:
                text += " · Zigbee2MQTT noch nicht eingerichtet"
        net = getattr(self.zigbee, "net", None)
        if net is not None and getattr(net, "folder", None) is not None:
            text += f" · Netz: {net.summary()}"
        job = view.get("job")
        if job:
            text += (f" · zuletzt {'Installation' if job.get('action') == 'install' else 'Entfernen'}: "
                     f"{STATE_WORDS.get(job.get('state'), job.get('state'))}" + (f" – {job['message']}" if job.get("message") else ""))
        if view.get("error"):
            text += f" · Fehler: {view['error']}"
        return text
