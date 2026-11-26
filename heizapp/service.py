"""Background polling, clock keeping and every action the web UI can trigger."""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Callable

from . import coap, discovery, energy, history, plans, shelly, trv
from .device import FAST_FIELDS, IDENTITY_FIELDS, MODES, SLOW_FIELDS, TIME_FORMAT, DeviceError, Heater
from .store import DataStore, HeaterConfig, load_config, new_id

log = logging.getLogger(__name__)

DEVICE_PLAN_SLOT = 1   # the app always writes its plan into plan 1 of a heater
AVG_WINDOW = 1800      # seconds of heater readings averaged for calibration
MAX_CORRECTION = 99    # temp_corr limit on the device: ±9.9 K
SLOW_EVERY = 6         # slow fields (incl. clock) every 6th poll, ~1 min at 10 s
DRIFT_LIMIT = 60       # seconds of clock deviation before the app corrects it
DAILY_SYNC_HOUR = 3    # daily clock sync; also covers summer/winter time changes
DEFAULT_ON_TEMP = 200  # 0.1 °C: "Einschalten" without a remembered temperature and without a plan
LOST_AFTER = 180       # seconds offline before the app looks for the heater at another address
HEAL_EVERY = 900       # at most one automatic network search per 15 minutes
STEADY_GAP = 300       # seconds offline after which a heater counts as changed (the room may have cooled meanwhile)
MODE_LABELS = {"manual": "Manuell", "auto": "Automatik (Heizplan)", "boost": "Boost",
               "boost_off": "Boost beendet", "off": "Aus (Frostschutz)", "on": "Eingeschaltet"}
MODE_WORDS = {0: "Manuell", 1: "Automatik", 2: "Boost", 3: "Urlaub", 4: "Standby",
              trv.OFF: "Aus"}   # operating_mode, for the log
VIEW_MODES = {**MODES, trv.OFF: "off"}   # operating_mode -> mode on the page; „off“ only from a thermostat
REGULATION_WORDS = {"werk": "regelt jetzt wie ab Werk (ganz auf, ganz zu)", "sanft": "regelt jetzt sanft"}   # „Regelung“
SUMMARY_EVERY = 15 * 60   # seconds between two lines „Stand: …“ in the log
RESTORE_SETTLE = 60       # seconds after a firmware update until the app writes the thermostat's settings back …
RESTORE_RETRY = 5 * 60    # … and between attempts while it does not confirm them
RESTORE_TRIES = 3
RESTORE_KEEP = 24 * 3600  # seconds the page says what came back
RESTORE_FAILED = ("Die Einstellungen von vorher ließen sich nicht wieder einstellen – bitte Regelung, Aus/Automatik/Ein "
                  "und Heizplan prüfen.")
PLUG_HEATING_W = 50     # W from which a plug counts as letting the heating through (a heater on standby: under 1 W)
PLUG_SAMPLES = 360      # values a plug measured while its heater heated, kept per heater: an hour at one every 10 s
PLUG_MIN_SAMPLES = 6    # values before the page shows a measured power: a minute of heating
PLUG_ANSWER = 10.0      # seconds a plug has to confirm „Einschalten“


def host_clock_synced() -> bool:
    """Can this computer's clock be trusted? A Raspberry Pi has no battery clock: after a power cut it
    starts with the time it was switched off until it reaches a time server. Only Linux/systemd can tell."""
    if not sys.platform.startswith("linux"):
        return True
    if os.path.exists("/run/systemd/timesync/synchronized"):
        return True
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True  # no systemd (e.g. Docker on a NAS): behave as before
    return out.returncode != 0 or out.stdout.strip() != "no"


def _num(value: float, fmt: str = ".1f") -> str:
    """German number format for messages: 21.5 -> "21,5"."""
    return format(value, fmt).replace(".", ",")


def _deg10(value) -> str:
    """A temperature as the heater reports it (0.1 °C) for the log: 215 -> "21,5 °C"."""
    return f"{_num(value / 10)} °C" if isinstance(value, int) and not isinstance(value, bool) else "?"


def _span(seconds: float) -> str:
    """A clock's deviation for the log: "40 s", "12 Min.", "3 Std.", "5000 Tage"."""
    seconds = abs(seconds)
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 2 * 3600:
        return f"{seconds // 60:.0f} Min."
    if seconds < 2 * 86400:
        return f"{seconds // 3600:.0f} Std."
    return f"{seconds // 86400:.0f} Tage"


def _device_title(value) -> str | None:
    """A name a device gives itself (a Shelly plug: its name in the Shelly app), fit for the page and the protocol: one
    line, at most 40 characters – None if it has none."""
    if not isinstance(value, str):
        return None
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())[:40].strip()
    return text or None


def _since(start: float | None) -> str:
    """How long ago, for the log: "7 Min.", "2 Std. 5 Min."."""
    if start is None:
        return "?"
    minutes = max(0, int((time.time() - start) // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} Std. {minutes} Min." if hours else f"{minutes} Min."


class ActionError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _local_tz():
    name = os.environ.get("HEIZAPP_TZ", "Europe/Berlin")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # no tz database available: use the system's local time
        log.warning("Zeitzone %s nicht verfügbar – nutze Systemzeit", name)
        return None


class HeaterState:
    """Runtime state of one configured heater."""

    zigbee = False   # a SONOFF thermostat over Zigbee (ThermostatState) instead of an AeroFlow

    def __init__(self, cfg: HeaterConfig, device=None):
        self.cfg = cfg
        self.device = device or Heater(cfg.host, cfg.token, cfg.port)
        self.key: str | None = None          # serial number (idu) once known
        self.values: dict = {}
        self.online = False
        self.error: str | None = None
        self.last_seen: float | None = None
        self.offline_since: float | None = None
        self.polls = 0
        self.clock_drift: float | None = None
        self.last_clock_sync: float | None = None
        self.refresh_lock = threading.Lock()
        # recent readings (time, room_temp in 0.1 °C, temp_corr at that time) for the calibration average
        self.temps: deque[tuple[float, int, int | None]] = deque(maxlen=1000)
        # mode, setpoint and window as last seen, and since when (time.time()): a calibration run counts a minute
        # as calm only a while after one of them changed
        self.steady: tuple | None = None
        self.steady_since: float | None = None
        # what the log has said about this heater: it answers (True), it does not (False), nothing yet (None) –
        # and the last odd answer, so each is written once
        self.reported: bool | None = None
        self.reported_error: str | None = None

    def room_temp_average(self, window: float = AVG_WINDOW) -> tuple[float, int, int] | None:
        """Average room temperature (0.1 °C) over the last `window` seconds, only from readings taken with
        the current correction. Returns (average, number of readings, minutes covered) or None."""
        corr, now = self.values.get("temp_corr"), time.time()
        samples = [(t, v) for t, v, c in self.temps if now - t <= window and c == corr]
        if len(samples) < 5 or samples[-1][0] - samples[0][0] < 300:
            return None
        return (sum(v for _, v in samples) / len(samples), len(samples),
                round((samples[-1][0] - samples[0][0]) / 60))

    @property
    def label(self) -> str:
        """Address as written in heizungen.conf (port only if not the default)."""
        return self.cfg.host if self.cfg.port == coap.DEFAULT_PORT else f"{self.cfg.host}:{self.cfg.port}"

    @property
    def id(self) -> str:
        return self.key or f"host:{self.label}"


class ThermostatState(HeaterState):
    """A SONOFF thermostat over Zigbee (heizapp.trv): known by its IEEE address, never in heizungen.conf."""

    zigbee = True

    def __init__(self, ieee: str, net):
        super().__init__(HeaterConfig("zigbee", 0, None, 0), trv.Thermostat(ieee, net))
        self.key = ieee

    @property
    def label(self) -> str:
        return "Zigbee"


class Service:
    def __init__(self, config_path: str, data_path: str, poll_interval: float = 10.0):
        self.config_path = config_path
        self.entries = [HeaterState(c) for c in load_config(config_path)]
        self.store = DataStore(data_path)
        self.zigbee = None   # heizapp.zigbee_net.ZigbeeNet, from attach_zigbee(): brings the thermostats
        self.poll_interval = poll_interval
        self.tz = _local_tz()
        # Verlauf: five-minute entries of every device in history/ next to data.json (heizapp.history)
        self.history = history.History(os.path.join(os.path.dirname(os.path.abspath(data_path)), "history"), self.tz)
        self._record_failed = False   # a failing Verlauf is said once in the protocol, not every 10 s
        # Strom der Steckdosen: quarter hours of every plug in energy/ next to data.json (heizapp.energy)
        self.energy = energy.PlugEnergy(os.path.join(os.path.dirname(os.path.abspath(data_path)), "energy"), self.tz)
        self._energy_failed = False
        self._stop = threading.Event()
        self._entries_lock = threading.Lock()  # guards changes of the heater list / heizungen.conf
        self._pool = ThreadPoolExecutor(max_workers=24, thread_name_prefix="heizung")
        self._scan_lock = threading.Lock()   # one network search at a time
        self._sensor_lock = threading.Lock()   # one sweep of the sensor search at a time
        self._found_pills: dict[str, dict] = {}    # Pill id -> {mono, at, heaters, pill}: recent search results
        self._pill_status: dict[str, dict] = {}    # Pill id -> {online, since, error, mode, probes …}: last reading
        self._sensor_live: dict[str, dict] = {}    # sensor id -> latest reading (data.json gets one every 15 min)
        self._sensors_polled = self._sensors_saved = self._sensors_searched = 0.0
        self._last_heal = 0.0
        self._restore: dict[str, dict] = {}   # thermostat -> its settings coming back after a firmware update
        self._plug_samples: dict[str, deque] = {}   # AeroFlow -> watts its plug measured while it heated (in memory)
        self._plugs_failed = False   # a failing measurement is said once in the protocol, not every 10 s
        self._wifi_plugs: dict[str, dict] = {}    # WLAN plug id -> its last reading {on, power, energy_today, mono …}
        self._found_plugs: dict[str, dict] = {}   # WLAN plug id -> {mono, plug}: recent results of „Steckdosen suchen“
        self._plugs_searched = 0.0
        self._plug_names_read: dict[str, float] = {}   # WLAN plug id -> when its name was read last (monotonic)
        self._wifi_failed = False
        self._restore_lock = threading.Lock()
        self._last_daily_sync = None
        self._clock_trusted = False
        self._clock_warned = False
        self._drop_old_sensors()
        self._calib_lock = threading.Lock()
        # sensor id -> calibration run; data.json gets the tally every 15 minutes and at every start, end or change
        self._calib_runs: dict[str, dict] = json.loads(json.dumps({
            sid: m["calibration"] for sid, m in self.store.data["sensors"].items()
            if isinstance(m.get("calibration"), dict) and m["calibration"].get("until")}))
        known = {m.get("last_host"): k for k, m in self.store.data["heaters"].items()
                 if not k.startswith("host:") and not m.get("zigbee")}
        for e in self.entries:
            e.key = known.get(e.label)

    # ------------------------------------------------------------------ time
    def now(self) -> datetime:
        """Local wall-clock time (naive), as the heaters expect it."""
        return datetime.now(self.tz).replace(tzinfo=None) if self.tz else datetime.now()

    def clock_trusted(self) -> bool:
        """Only set heater clocks once this computer's own clock is right (checked until it is)."""
        if not self._clock_trusted:
            self._clock_trusted = host_clock_synced()
            if not self._clock_trusted and not self._clock_warned:
                self._clock_warned = True
                log.warning("Uhrzeit dieses Geräts ist noch nicht übers Internet abgeglichen – "
                            "die Heizungsuhren werden erst danach gestellt.")
            elif self._clock_trusted and self._clock_warned:   # the times above this line may have been wrong
                log.info("Uhrzeit dieses Geräts ist jetzt übers Internet abgeglichen.")
        return self._clock_trusted

    def _iso(self, ts: float | None) -> str | None:
        return datetime.fromtimestamp(ts, self.tz).isoformat(timespec="seconds") if ts else None

    # ------------------------------------------------------------------ polling
    def start(self) -> None:
        threading.Thread(target=self._run, name="heizung-poller", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.history.flush()   # the finished entries of the last minutes
        self.energy.flush()

    def _run(self) -> None:
        self.poll_all(full=True)
        self.sync_clocks(only_drifted=True)
        summarized = time.monotonic()
        while not self._stop.wait(self.poll_interval):
            try:
                self.poll_all()
                self._heal()
                self.poll_sensors()
                self._record()
                self._record_energy()
                now = self.now()
                if now.hour == DAILY_SYNC_HOUR and self._last_daily_sync != now.date():
                    self._last_daily_sync = now.date()
                    self.sync_clocks()
                else:
                    self.sync_clocks(only_drifted=True)
                if time.monotonic() - summarized >= SUMMARY_EVERY:
                    summarized = time.monotonic()
                    self.log_summary()
            except Exception:  # the poller must never die
                log.exception("Fehler im Abfrage-Thread")

    def log_summary(self) -> None:
        """One line „Stand: …“ – how warm it is where, what each heater does; every 15 minutes."""
        parts = []
        for e in self.entries:
            v = e.values
            if not e.online:
                parts.append(f"{self._name(e)} nicht erreichbar seit {_since(e.offline_since)}{self._plug_words(e)}")
                continue
            parts.append(f"{self._name(e)} {_deg10(v.get('room_temp'))} (Soll {_deg10(v.get('setpoint'))}, "
                         f"{MODE_WORDS.get(v.get('operating_mode'), '?')}"
                         + (", heizt" if v.get("heater_state") == 1 else "")
                         + (", Fenster offen" if v.get("open_window") == 1 else "") + self._plug_words(e) + ")")
        data = self.store.snapshot()
        for sid in data["sensors"]:
            live = self._sensor_live.get(sid) or {}
            if live.get("temp") is not None:
                parts.append(f"Fühler {self._sensor_name(sid, data)} {_num(live['temp'])} °C")
        if parts:
            log.info("Stand: %s", " · ".join(parts))

    def poll_all(self, full: bool = False) -> None:
        self._sync_thermostats()
        list(self._pool.map(lambda e: self.refresh(e, full), self.entries))
        self._after_updates()
        self._measure_plugs(self._read_wifi_plugs())

    # ------------------------------------------------------------------ thermostats (Zigbee)
    def attach_zigbee(self, net) -> None:
        """The connection to Zigbee2MQTT (heizapp.zigbee_net.ZigbeeNet): from now on its thermostats are heaters."""
        self.zigbee = net
        self._sync_thermostats()

    def _sync_thermostats(self) -> None:
        """Every thermostat paired in Zigbee2MQTT is a heater of the app, after the AeroFlow heaters. One the app
        knows (it has a name or a room) stays when it is missing – as not reachable, until it is paired again or
        removed in the app."""
        if self.zigbee is None:
            return
        wanted = self.zigbee.paired(trv.MODEL)
        wanted += [k for k, m in self.store.snapshot()["heaters"].items() if m.get("zigbee") and k not in wanted]
        with self._entries_lock:
            have = {e.id: e for e in self.entries if e.zigbee}
            if list(have) == wanted:
                return
            self.entries = [e for e in self.entries if not e.zigbee] + [have.get(i) or ThermostatState(i, self.zigbee)
                                                                        for i in wanted]
        for ieee in wanted:
            if ieee not in have:
                log.info("Thermostat %s ist jetzt in der App", ieee)

    def _aeroflows(self) -> list[HeaterState]:
        """The heaters from heizungen.conf (with an address) – without the thermostats."""
        return [e for e in self.entries if not e.zigbee]

    def forget_thermostat(self, ieee: str, result: dict | None = None) -> dict:
        """After „Entfernen“ in the Zigbee network: the app forgets the thermostat's name and room too – and a plug no
        longer belongs to its heater or its room."""
        heaters = self.store.data["heaters"]
        thermostat = bool(heaters.get(ieee, {}).get("zigbee"))
        owners = [hid for hid, meta in heaters.items() if meta.get("plug") == ieee]
        placed = ieee in (self.store.data.get("plug_rooms") or {})
        if thermostat or owners or placed:
            with self.store.edit() as d:
                if thermostat:
                    d["heaters"].pop(ieee, None)
                for hid in owners:
                    d["heaters"].get(hid, {}).pop("plug", None)
                (d.get("plug_rooms") or {}).pop(ieee, None)
        if thermostat:
            log.info("Thermostat %s aus der App entfernt", ieee)
        for hid in owners:
            self._plug_samples.pop(hid, None)
            log.info("%s (%s) gehört zu keiner Heizung mehr", self._plug_name(ieee), ieee)
        with self._restore_lock:
            self._restore.pop(ieee, None)
        self._sync_thermostats()
        return result if result is not None else {}

    def _thermostat(self, heater_id: str) -> HeaterState:
        e = self._entry(heater_id)
        if not e.zigbee:
            raise ActionError("Das geht nur beim Thermostat.", 409)
        return e

    def set_regulation(self, heater_id: str, body) -> dict:
        """„Regelung“ of a thermostat (see trv.regulation): mode „werk“ – fully open until the temperature is
        reached, then closed, as from the factory; „sanft“ – its own smart control opens the valve only as far as
        needed, like a normal thermostatic head; „stufen“ – open and closed, but only as far as the Stufe says (heat,
        per cent), a gap open in the pauses (idle, per cent) and heating again sooner (spielraum, °C below the
        setpoint). What is not given stays."""
        e = self._thermostat(heater_id)
        body = body if isinstance(body, dict) else {}
        mode, heat, idle, span = (body.get(k) for k in ("mode", "heat", "idle", "spielraum"))
        whole = lambda x, low, high: isinstance(x, int) and not isinstance(x, bool) and low <= x <= high   # noqa: E731
        if mode not in trv.REGULATIONS or (mode != "stufen" and (heat, idle, span) != (None, None, None)) \
                or not (heat is None or whole(heat, 1, 100)) or not (idle is None or whole(idle, 0, 99)) \
                or not (span is None or (isinstance(span, (int, float)) and not isinstance(span, bool)
                                         and any(abs(span - s) < 0.01 for s in trv.SPANS))):
            raise ActionError("Ungültige Anfrage.")
        if not e.online:
            raise ActionError("Das Thermostat ist gerade nicht erreichbar.", 409)
        try:
            e.device.set_regulation(mode, heat=heat, idle=idle, spielraum=None if span is None else round(span, 1))
        except ValueError as exc:
            raise ActionError(str(exc)) from None
        except (DeviceError, coap.CoapError) as exc:
            raise ActionError(f"Nicht übernommen: {exc}", 502) from None
        r = e.device.info()["regulation"]
        words = (f"regelt jetzt in {trv.stufen_text(r['heat'], r['idle'], r['spielraum'])}" if r["mode"] == "stufen"
                 else REGULATION_WORDS.get(r["mode"], "hat die neue Regelung bekommen"))
        log.info("Thermostat %s %s", self._who(e), words)
        self.refresh(e, full=True)
        return {"message": f"{self._name(e)} {words}."}

    def update_firmware(self, heater_id: str) -> dict:
        """„Firmware einspielen“: start the update of a thermostat's firmware. It runs 10 to 100 minutes; the page shows
        its progress (ota in the heater's view). One at a time, and only with a battery that can take it."""
        e = self._thermostat(heater_id)
        info = e.device.info()
        if not e.online:
            raise ActionError("Das Thermostat ist gerade nicht erreichbar.", 409)
        if (info["ota"] or {}).get("state") == "running":
            raise ActionError("Die neue Firmware wird schon eingespielt.", 409)
        if not info["update"]:
            raise ActionError("Für dieses Thermostat gibt es gerade keine neue Firmware.", 409)
        if info["battery"] is None or info["battery"] < trv.OTA_BATTERY:
            raise ActionError(f"Zum Einspielen braucht die Batterie mindestens {trv.OTA_BATTERY} % (jetzt "
                              f"{info['battery'] if info['battery'] is not None else '?'} %) – bitte zuerst neue "
                              "Batterien einsetzen.", 409)
        other = next((x for x in self.entries if x.zigbee and x is not e
                      and (x.device.info()["ota"] or {}).get("state") == "running"), None)
        if other:
            raise ActionError(f"Gerade wird die Firmware von „{self._name(other)}“ eingespielt – bitte warten, bis sie "
                              "fertig ist.", 409)
        kept = e.device.keep_settings()   # to come back afterwards, with what the app sets meanwhile
        try:
            self.zigbee.firmware(e.id)
        except Exception:
            e.device.forget_settings()
            raise
        with self._restore_lock:
            if kept:
                self._restore[e.id] = {"state": "waiting", "done": None, "next": 0.0, "tries": 0, "text": None, "at": None}
            else:
                self._restore.pop(e.id, None)
        if not kept:   # it has not reported any setting yet: nothing to write back
            e.device.forget_settings()
        log.info("Thermostat %s: neue Firmware wird eingespielt (Firmware bisher %s, Batterie %d %%)", self._who(e),
                 e.values.get("active_fw_ver") or "?", info["battery"])
        if kept:
            log.info("Thermostat %s: gemerkt für nach dem Update: %s", self._who(e), trv.describe(kept))
        return {"message": "Das Einspielen hat begonnen. Es dauert 10 bis 100 Minuten – das Thermostat heizt dabei "
                           "weiter und startet am Ende kurz neu."}

    def _after_updates(self) -> None:
        """After a firmware update a thermostat may have forgotten its settings: the real TRV-ZBT came back from
        1.1.0 → 1.1.3 (25.11.2026) „ab Werk“ instead of „sanft“, and switched off. So once an update is done and the
        thermostat has had a minute to settle, the app writes back what it had (Thermostat.keep_settings – with what
        was set during the update). After a failed update the thermostat keeps its settings: nothing to do."""
        if not self._restore:
            return
        now = time.time()
        for e in [x for x in self.entries if x.zigbee]:
            with self._restore_lock:
                job = self._restore.get(e.id)
                if job is None or job["state"] == "busy":
                    continue
                if job["state"] in ("done", "failed"):
                    if now - job["at"] > RESTORE_KEEP:
                        self._restore.pop(e.id, None)
                    continue
            ota = (e.device.info()["ota"] or {}).get("state")
            if ota == "running":
                continue
            if ota != "done":   # failed – or its result is a day old and the thermostat never came back
                e.device.forget_settings()
                with self._restore_lock:
                    self._restore.pop(e.id, None)
                if job["done"]:
                    log.warning("Thermostat %s: %s", self._who(e), RESTORE_FAILED)
                continue
            with self._restore_lock:
                job["done"] = job["done"] or now
                if now - job["done"] < RESTORE_SETTLE or now < job["next"] or not e.online:
                    continue
                job["state"] = "busy"
            self._pool.submit(self._restore_settings, e, job)

    def _restore_settings(self, e: HeaterState, job: dict) -> None:
        """Write back the settings from before the firmware update – in the pool: it may take a minute."""
        try:
            differed, written = e.device.restore_settings()
        except Exception as exc:   # not confirmed (or something unexpected): again in 5 minutes, 3 times in all
            reason = str(exc) if isinstance(exc, (DeviceError, coap.CoapError)) else f"{type(exc).__name__}: {exc}"
            with self._restore_lock:
                job["tries"] += 1
                last = job["tries"] >= RESTORE_TRIES
                if last:
                    job.update(state="failed", at=time.time(), text=RESTORE_FAILED)
                else:
                    job.update(state="waiting", next=time.time() + RESTORE_RETRY)
            if last:
                e.device.forget_settings()
                log.warning("Thermostat %s: Einstellungen nach dem Update nicht zurückgestellt (%s) – bitte Regelung, "
                            "Aus/Automatik/Ein und Heizplan prüfen", self._who(e), reason)
            else:
                log.info("Thermostat %s: Zurückstellen nach dem Update hat nicht geklappt (%s) – nächster Versuch in "
                         "%d Minuten", self._who(e), reason, RESTORE_RETRY // 60)
            return
        text = trv.describe(written)
        with self._restore_lock:
            job.update(state="done", at=time.time(), text=text)
        log.info("Thermostat %s nach dem Update zurückgestellt: %s – %s", self._who(e), text,
                 f"es meldete vorher: {trv.describe(differed, 'einen anderen Heizplan')}" if differed
                 else "es meldete nichts Abweichendes")
        self.refresh(e, full=True)

    def _restore_view(self, ieee: str) -> dict | None:
        """For the page: the settings coming back after a firmware update – {state: waiting | done | failed, text}."""
        with self._restore_lock:
            job = self._restore.get(ieee)
            return None if job is None else {"state": "waiting" if job["state"] == "busy" else job["state"],
                                             "text": job["text"]}

    def refresh(self, e: HeaterState, full: bool = False) -> None:
        with e.refresh_lock:
            started, before = time.monotonic(), dict(e.values)
            try:
                vals = e.device.read(FAST_FIELDS)
                slow = full or e.polls % SLOW_EVERY == 0 or "system_time" not in e.values
                if slow:
                    vals.update(e.device.read(SLOW_FIELDS))
                    try:
                        device_time = datetime.strptime(str(vals["system_time"]), TIME_FORMAT)
                        e.clock_drift = (device_time - self.now()).total_seconds()
                    except ValueError:
                        e.clock_drift = None
                if slow or "idu" not in e.values:
                    # re-checked every minute: after the router shuffled addresses, another heater may answer here
                    ident = e.device.read(IDENTITY_FIELDS)
                    vals.update(ident)
                    if str(ident["idu"]) != e.key:
                        if e.key:
                            log.info("Unter %s antwortet jetzt Heizung %s (bisher %s)", e.label, ident["idu"], e.key)
                            e.temps.clear()
                        self._bind(e, str(ident["idu"]))
            except coap.CoapError as exc:
                e.online, e.error = False, str(exc)
                if e.offline_since is None:
                    e.offline_since = time.time()
                if e.reported is not False:
                    e.reported = False
                    log.warning("Heizung %s antwortet nicht: %s", self._who(e), exc)
                log.debug("Heizung %s: keine Antwort (%.1f s)", self._who(e), time.monotonic() - started)
            except (DeviceError, ValueError, KeyError, TypeError) as exc:
                e.online, e.error = True, f"unerwartete Antwort: {exc}"
                if e.error != e.reported_error:
                    e.reported_error = e.error
                    log.warning("Heizung %s: %s", self._who(e), e.error)
            else:
                offline_since = e.offline_since
                e.values.update(vals)
                now = time.time()
                steady = (e.values.get("operating_mode"), e.values.get("setpoint"), e.values.get("open_window"))
                if steady != e.steady or (e.offline_since is not None and now - e.offline_since > STEADY_GAP):
                    e.steady, e.steady_since = steady, now
                e.online, e.error, e.last_seen, e.offline_since = True, None, now, None
                if isinstance(vals.get("room_temp"), int):
                    e.temps.append((e.last_seen, vals["room_temp"], e.values.get("temp_corr")))
                self._log_reading(e, before, vals, offline_since, time.monotonic() - started)
            finally:
                e.polls += 1

    def _who(self, e: HeaterState) -> str:
        """A heater in the log: its name and address."""
        name = self._name(e)
        if e.zigbee:
            return f"{name} ({e.key})"
        return name if name == e.label else f"{name} ({e.label})"

    def _log_reading(self, e: HeaterState, before: dict, vals: dict, offline_since: float | None,
                     seconds: float) -> None:
        """What the log says about one successful reading: the first one, the heater back after a gap, what changed
        at the heater since the last reading (also through a button on the device or the plan) – and in the detailed
        log every reading."""
        v = e.values
        if e.reported is None and e.zigbee:
            log.info("Thermostat %s erreichbar: %s, Soll %s, %s · Firmware %s", self._who(e), _deg10(v.get("room_temp")),
                     _deg10(v.get("setpoint")), MODE_WORDS.get(v.get("operating_mode"), "?"), v.get("active_fw_ver") or "?")
        elif e.reported is None:
            log.info("Heizung %s erreichbar: %s, Soll %s, %s · Firmware %s · Seriennummer %s%s", self._who(e),
                     _deg10(v.get("room_temp")), _deg10(v.get("setpoint")), MODE_WORDS.get(v.get("operating_mode"), "?"),
                     v.get("active_fw_ver") or "?", v.get("idu") or "?", "" if e.device.writable else " · ohne Token")
        elif e.reported is False:
            log.info("Heizung %s antwortet wieder (nach %s)", self._who(e), _since(offline_since))
        if e.reported_error:
            log.info("Heizung %s antwortet wieder normal", self._who(e))
        e.reported, e.reported_error = True, None
        if before.get("operating_mode") is not None:   # not the first reading: compare
            changes = []
            for name, label, show in (("operating_mode", "Modus", lambda x: MODE_WORDS.get(x, str(x))),
                                      ("setpoint", "Soll", _deg10),
                                      ("open_window", "Fenster", lambda x: "offen" if x == 1 else "zu"),
                                      ("temp_corr", "Korrektur", lambda x: f"{_num(x / 10, '+.1f')} °C"
                                       if isinstance(x, int) else "?"),
                                      ("heating_plan", "Planplatz", lambda x: str(x + 1) if isinstance(x, int) else "?"),
                                      ("child_lock_en", "Kindersicherung", lambda x: "an" if x == 1 else "aus"),
                                      ("active_fw_ver", "Firmware", str)):
                if name in vals and before.get(name) is not None and vals[name] != before[name]:
                    changes.append(f"{label} {show(before[name])} → {show(vals[name])}")
            if changes:
                log.info("Heizung %s: %s", self._who(e), " · ".join(changes))
        if log.isEnabledFor(logging.DEBUG):
            log.debug("Heizung %s: %s, Soll %s, %s%s%s, %d Werte in %.2f s", self._who(e), _deg10(v.get("room_temp")),
                      _deg10(v.get("setpoint")), MODE_WORDS.get(v.get("operating_mode"), "?"),
                      ", heizt" if v.get("heater_state") == 1 else "",
                      ", Fenster offen" if v.get("open_window") == 1 else "", len(vals), seconds)

    def _bind(self, e: HeaterState, idu: str) -> None:
        """Key a heater by its serial number, so names/rooms survive a changed IP."""
        placeholder = f"host:{e.label}"
        with self.store.edit() as data:
            heaters = data["heaters"]
            meta = heaters.setdefault(idu, {})
            for k, v in (heaters.pop(placeholder, None) or {}).items():
                meta.setdefault(k, v)
            meta["last_host"] = e.label
        e.key = idu

    @staticmethod
    def _device_name(e: HeaterState) -> str:
        """The name the heater announces in the network (e.g. FRITZ!Box): "Heat4U-" + first and
        last three digits of the serial number. Derived locally, because reverse DNS can hang.
        A thermostat: "Thermostat" + the end of its IEEE address."""
        if e.zigbee:
            return f"Thermostat {e.key[-4:].upper()}"
        idu = str(e.values.get("idu") or "")
        return f"Heat4U-{idu[:3]}{idu[-3:]}" if len(idu) >= 6 else e.label

    # ------------------------------------------------------------------ helpers
    def _run_parallel(self, entries: list[HeaterState], action: Callable[[HeaterState], str],
                      what: str = "Befehl") -> list[dict]:
        def one(e: HeaterState) -> dict:
            started = time.monotonic()
            try:
                message, ok = action(e), True
            except (DeviceError, coap.CoapError, ValueError) as exc:
                message, ok = str(exc), False
            if ok:
                log.info("Heizung %s: %s (%.1f s)", self._who(e), message, time.monotonic() - started)
            else:
                log.warning("Heizung %s: %s hat nicht geklappt – %s", self._who(e), what, message)
            self.refresh(e, full=True)
            return {"heater": e.id, "name": self._name(e), "ok": ok, "message": message}
        return list(self._pool.map(one, entries))

    def _name(self, e: HeaterState) -> str:
        return self.store.data["heaters"].get(e.id, {}).get("name") or self._device_name(e)

    @staticmethod
    def _new_meta(e: HeaterState) -> dict:
        """The app's data for a heater it has none for yet."""
        return {"zigbee": True} if e.zigbee else {"last_host": e.label}

    def _entry(self, heater_id: str) -> HeaterState:
        for e in self.entries:
            if e.id == heater_id:
                return e
        raise ActionError("Heizung nicht gefunden", 404)

    def _room(self, room_id: str) -> dict:
        room = self.store.data["rooms"].get(room_id)
        if room is None:
            raise ActionError("Raum nicht gefunden", 404)
        return room

    def _targets(self, kind: str, ident: str) -> list[HeaterState]:
        if kind == "heater":
            return [self._entry(ident)]
        self._room(ident)
        heaters = self.store.data["heaters"]
        members = [e for e in self.entries if heaters.get(e.id, {}).get("room") == ident]
        if not members:
            raise ActionError("Diesem Raum ist noch keine Heizung zugeordnet.")
        return members

    @staticmethod
    def _tenths(temp, lo: float = 5.0, hi: float = 30.0) -> int:
        try:
            t = float(temp)
        except (TypeError, ValueError):
            raise ActionError("Temperatur fehlt") from None
        if not lo <= t <= hi:
            raise ActionError(f"Temperatur muss zwischen {lo:g} und {hi:g} °C liegen")
        return int(round(t * 2)) * 5

    @staticmethod
    def _clean_name(name, allow_empty: bool = False) -> str:
        name = str(name or "").strip()[:40]
        if not name and not allow_empty:
            raise ActionError("Name fehlt")
        return name

    # ------------------------------------------------------------------ heater actions
    def set_setpoint(self, kind: str, ident: str, temp) -> dict:
        value = self._tenths(temp)

        def apply(e: HeaterState) -> str:
            e.device.set("setpoint", value)
            return f"Solltemperatur {value / 10:g} °C".replace(".", ",")
        return {"results": self._run_parallel(self._targets(kind, ident), apply,
                                              f"Solltemperatur {value / 10:g} °C".replace(".", ","))}

    def set_mode(self, kind: str, ident: str, mode: str, minutes=30) -> dict:
        """Modes of the device plus "on": undo "off" – back to the mode and temperature the heater
        had before (remembered in data.json), else to the room's plan, else DEFAULT_ON_TEMP."""
        if mode not in MODE_LABELS:
            raise ActionError("Unbekannter Modus")

        def apply(e: HeaterState) -> str:
            if mode == "on":
                return self._switch_on(e)
            before = self._mode_before_off(e) if mode == "off" else None
            e.device.set_mode(mode, plan=DEVICE_PLAN_SLOT, boost_minutes=int(minutes or 30))
            if mode == "off":
                if before:  # an already switched-off heater keeps what was remembered first
                    self._heater_meta_set(e, "before_off", before)
            else:
                self._heater_meta_set(e, "before_off", None)
            return MODE_LABELS[mode]
        return {"results": self._run_parallel(self._targets(kind, ident), apply, f"Modus „{MODE_LABELS[mode]}“")}

    def _mode_before_off(self, e: HeaterState) -> dict | None:
        """What "on" should restore later; None if the heater is already off (manual at frost level)."""
        mode, manual_sp, frost = (e.device.get(n) for n in ("operating_mode", "manual_sp", "antifrost_sp"))
        if mode == trv.OFF or (mode in (0, 4) and isinstance(manual_sp, int) and isinstance(frost, int)
                               and manual_sp <= frost):
            return None
        return {"mode": "auto" if mode == 1 else "manual", "setpoint": manual_sp}

    def _switch_on(self, e: HeaterState) -> str:
        before = self.store.data["heaters"].get(e.id, {}).get("before_off") or {}
        if before.get("mode") == "auto" or (not before and self._plan_sent_to(e)):
            e.device.set_mode("auto", plan=DEVICE_PLAN_SLOT)
            text = "Eingeschaltet – nach Plan"
        else:
            frost = e.device.get("antifrost_sp")
            temp = before.get("setpoint")
            if not isinstance(temp, int) or (isinstance(frost, int) and temp <= frost):
                temp = DEFAULT_ON_TEMP
            e.device.set_mode("manual")
            e.device.set("manual_sp", temp)
            text = f"Eingeschaltet – {_num(temp / 10, 'g')} °C"
        self._heater_meta_set(e, "before_off", None)
        return text

    def _plan_sent_to(self, e: HeaterState) -> bool:
        """Has this heater received the plan that is assigned to its room?"""
        data = self.store.data
        room = data["rooms"].get(data["heaters"].get(e.id, {}).get("room") or "")
        if not room or not room.get("plan"):
            return False
        sent = room.get("sent") or {}
        if sent.get("plan") != room["plan"]:
            return False
        return e.id in sent["heaters"] if "heaters" in sent else bool(sent.get("complete"))

    def _heater_meta_set(self, e: HeaterState, key: str, value) -> None:
        """Set (or with None: remove) one value in the app's data for this heater."""
        if value is None and key not in self.store.data["heaters"].get(e.id, {}):
            return
        with self.store.edit() as d:
            meta = d["heaters"].setdefault(e.id, self._new_meta(e))
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value

    def send_plan(self, room_id: str) -> dict:
        room = self._room(room_id)
        plan = self.store.data["plans"].get(room.get("plan") or "")
        if not plan:
            raise ActionError("Diesem Raum ist kein Heizplan zugeordnet.")
        windows = plans.to_device(plan)

        def write(e: HeaterState) -> str:
            changed = e.device.write_plan(DEVICE_PLAN_SLOT, windows)
            return f"übertragen ({len(changed)} Tag(e) geändert)" if changed else "war bereits aktuell"
        targets = self._targets("room", room_id)
        for e in targets:
            problems = trv.plan_problems(windows, e.values.get("antifrost_sp")) if e.zigbee else []
            if problems:
                raise ActionError(f"Der Heizplan „{plan['name']}“ passt nicht auf „{self._name(e)}“: "
                                  + "; ".join(problems) + ". Bitte Zeitfenster zusammenlegen – nichts wurde gesendet.")
        log.info("Heizplan „%s“ an %s senden: %s", plan["name"], room["name"], ", ".join(self._name(e) for e in targets))
        results = self._run_parallel(targets, write, f"Heizplan „{plan['name']}“ übertragen")
        if any(r["ok"] for r in results):
            with self.store.edit() as d:
                d["rooms"][room_id]["sent"] = {
                    "plan": room["plan"], "fingerprint": plans.fingerprint(plan),
                    "at": self.now().isoformat(timespec="seconds"), "complete": all(r["ok"] for r in results),
                    "heaters": [r["heater"] for r in results if r["ok"]],
                }
        return {"results": results}

    def import_plans(self, heater_id: str) -> dict:
        """Read all three plan slots of a heater; store every non-empty, not yet known plan in the app."""
        e = self._entry(heater_id)
        if e.zigbee:
            raise ActionError("Beim Thermostat kann die App den Heizplan noch nicht auslesen.", 409)
        if not e.online:
            raise ActionError("Die Heizung ist offline.", 409)
        try:
            active = e.device.get("heating_plan")
            slots = {slot: e.device.read_plan(slot) for slot in (1, 2, 3)}
        except (DeviceError, coap.CoapError) as exc:
            raise ActionError(f"Auslesen fehlgeschlagen: {exc}", 502) from None
        known_fp = {plans.fingerprint(p): p["name"] for p in self.store.data["plans"].values()}
        names = {p["name"] for p in self.store.data["plans"].values()}
        stamp = self.now()
        created, existing, empty, skipped = [], [], [], 0
        for slot, days in slots.items():
            if not any(days.values()):
                empty.append(slot)
                continue
            name = f"{self._name(e)} – Plan {slot}"[:40]
            if name in names:
                name = f"{name[:26]} ({stamp:%d.%m. %H:%M})"
            plan, bad = plans.from_device(name, days)
            skipped += bad
            fp = plans.fingerprint(plan)
            if fp in known_fp:
                existing.append({"slot": slot, "plan": known_fp[fp]})
                continue
            plan["source"] = {"heater": e.id, "slot": slot, "at": stamp.isoformat(timespec="seconds")}
            plan_id = new_id("p")
            with self.store.edit() as d:
                d["plans"][plan_id] = plan
            known_fp[fp], names = name, names | {name}
            created.append({"slot": slot, "id": plan_id, "name": name})
        active_slot = active + 1 if isinstance(active, int) and 0 <= active <= 2 else None

        parts = []
        if created:
            parts.append("Als neue Heizpläne übernommen: " + ", ".join(f"„{c['name']}“" for c in created) + ".")
        for x in existing:
            parts.append(f"Plan {x['slot']} ist schon als „{x['plan']}“ in der App.")
        if len(empty) == 3:
            parts.append("Auf der Heizung ist kein Heizplan gespeichert (alle drei Planplätze sind leer).")
        if skipped:
            parts.append(f"{skipped} unlesbare Zeitfenster wurden übersprungen.")
        if active_slot and len(empty) < 3:
            parts.append(f"Ausgewählt auf der Heizung: Plan {active_slot}.")
        log.info("Heizpläne von %s ausgelesen: %s", self._who(e), " ".join(parts) or "nichts Neues")
        return {"created": created, "existing": existing, "empty": empty, "skipped": skipped,
                "active_slot": active_slot, "message": " ".join(parts)}

    def calibrate(self, heater_id: str, body: dict) -> dict:
        """Set the heater's temperature correction from a reference measurement ("measured", °C) or directly
        ("correction", K). The heater reports room_temp WITH its correction, so:
        new correction = old correction + (measured − shown), shown = 30-min average if available."""
        e = self._entry(heater_id)
        step, limit = (trv.CORR_STEP, trv.CORR_MAX) if e.zigbee else (1, MAX_CORRECTION)
        if not e.device.writable:
            raise ActionError("Die Heizung ist nur lesbar – für die Kalibrierung wird ihr Token gebraucht.")
        if not e.online:
            raise ActionError("Die Heizung ist offline.", 409)
        try:
            old = e.device.get("temp_corr")
            if "correction" in body:
                new = int(round(float(str(body["correction"]).replace(",", ".")) * 10))
                basis_text = ""
            else:
                try:
                    measured = float(str(body.get("measured")).replace(",", "."))
                except ValueError:
                    raise ActionError("Bitte die gemessene Raumtemperatur angeben.") from None
                if not 0 <= measured <= 40:
                    raise ActionError("Die gemessene Temperatur muss zwischen 0 und 40 °C liegen.")
                # the cached average only counts if it was taken with the correction the heater has now
                average = e.room_temp_average() if old == e.values.get("temp_corr") else None
                if average:
                    shown, _count, minutes = average
                    basis_text = f"Heizung im Mittel der letzten {minutes} min: {_num(shown / 10)} °C"
                else:
                    shown = e.device.get("room_temp")
                    basis_text = f"Heizung zeigte {_num(shown / 10)} °C"
                new = old + int(round(measured * 10 - shown))
                basis_text = f" ({basis_text}, gemessen {_num(measured)} °C)"
        except (DeviceError, coap.CoapError) as exc:
            raise ActionError(f"Heizung nicht erreichbar: {exc}", 502) from None
        new = int(round(new / step)) * step
        if abs(new) > limit:
            raise ActionError(f"Die nötige Korrektur ({_num(new / 10, '+.1f')} °C) liegt außerhalb von "
                              f"±{_num(limit / 10, 'g')} °C – bitte die Messung prüfen.")
        try:
            e.device.set("temp_corr", new)
        except (DeviceError, coap.CoapError) as exc:
            raise ActionError(f"Korrektur nicht übernommen: {exc}", 502) from None
        e.temps.clear()  # older readings were taken with the old correction
        self.refresh(e, full=True)
        log.info("Temperatur-Korrektur %s: %+d -> %+d (in 0,1 °C)", e.label, old, new)
        return {"old": old / 10, "new": new / 10,
                "message": f"Temperatur-Korrektur {_num(old / 10, '+.1f')} °C → {_num(new / 10, '+.1f')} °C{basis_text}."}

    def sync_clocks(self, only_drifted: bool = False, heater_id: str | None = None) -> dict:
        if not self.clock_trusted():
            if heater_id:
                raise ActionError("Die Uhrzeit dieses Geräts ist noch nicht übers Internet abgeglichen – "
                                  "bitte gleich noch einmal versuchen.", 409)
            return {"results": []}
        entries = [self._entry(heater_id)] if heater_id else self._aeroflows()
        if heater_id and entries[0].zigbee:
            raise ActionError("Das Thermostat bekommt die Uhrzeit über Zigbee.", 409)
        targets = [e for e in entries if e.online and e.device.writable and
                   (not only_drifted or (e.clock_drift is not None and abs(e.clock_drift) > DRIFT_LIMIT))]
        if heater_id and not targets:
            raise ActionError("Heizung ist offline oder nur lesbar (Token fehlt).")

        def apply(e: HeaterState) -> str:
            if e.clock_drift is not None and abs(e.clock_drift) >= 1:
                log.info("Heizung %s: Uhr geht %s %s – wird gestellt", self._who(e), _span(e.clock_drift),
                         "vor" if e.clock_drift > 0 else "nach")
            e.clock_drift = e.device.set_clock(self.now)
            e.last_clock_sync = time.time()
            return "Uhr gestellt"
        return {"results": self._run_parallel(targets, apply, "Uhr stellen") if targets else []}

    # ------------------------------------------------------------------ network search
    def _scan_targets(self, networks) -> list[tuple[str, int]]:
        return discovery.targets_of(networks)

    def discover(self, network=None) -> dict:
        """Search the home network (or the given one) for heaters. Heaters the app knows by serial number but
        cannot reach any more are moved to the address where they answer now."""
        try:
            nets = [discovery.parse_network(network)] if str(network or "").strip() else \
                discovery.local_networks([e.cfg.host for e in self._aeroflows()])
        except discovery.DiscoveryError as exc:
            raise ActionError(str(exc)) from None
        if not nets:
            raise ActionError("Das Heimnetz wurde nicht erkannt – bitte das Netz angeben, z. B. 192.168.178.")
        started = time.monotonic()
        with self._scan_lock:
            found = discovery.scan(self._scan_targets(nets))
        moved = self._adopt(found)

        def describe(item) -> dict:
            (ip, port), serial = item
            info = {"address": ip if port == coap.DEFAULT_PORT else f"{ip}:{port}", "serial": serial,
                    "hostname": f"Heat4U-{serial[:3]}{serial[-3:]}" if len(serial) >= 6 else None,
                    "room_temp": None, "firmware": None}
            try:
                vals = Heater(ip, None, port, timeout=1.0, retries=1).read(("room_temp", "active_fw_ver"))
                info["room_temp"] = vals["room_temp"] / 10 if isinstance(vals["room_temp"], int) else None
                info["firmware"] = vals["active_fw_ver"]
            except (coap.CoapError, DeviceError):
                pass
            return info
        heaters = sorted(self._pool.map(describe, found.items()),
                         key=lambda h: tuple(int(x) for x in h["address"].split(":")[0].split(".")))
        log.info("Netzsuche in %s: %d Heizung(en)", ", ".join(map(str, nets)), len(heaters))
        return {"networks": [str(n) for n in nets], "seconds": round(time.monotonic() - started, 1),
                "heaters": heaters, "moved": moved}

    def _sensor_targets(self, networks) -> list[tuple[str, int]]:
        return [(str(ip), shelly.PORT) for net in networks for ip in net.hosts()]

    def _sweep(self, nets, kinds: tuple = ("pill",)) -> list[dict]:
        with self._sensor_lock:
            return shelly.scan(self._sensor_targets(nets), kinds=kinds)

    def _search_networks(self, network) -> list:
        """What „Fühler suchen“ and „Steckdosen suchen“ sweep: the given network, else the home network."""
        try:
            nets = [discovery.parse_network(network)] if str(network or "").strip() else \
                discovery.local_networks([e.cfg.host for e in self._aeroflows()])
        except discovery.DiscoveryError as exc:
            raise ActionError(str(exc)) from None
        if not nets:
            raise ActionError("Das Heimnetz wurde nicht erkannt – bitte das Netz angeben, z. B. 192.168.178.")
        return nets

    def find_sensors(self, network=None) -> dict:
        """„Fühler suchen“: one sweep of the home network for Shelly Pills (read only)."""
        nets = self._search_networks(network)
        started = time.monotonic()
        pills = sorted(self._sweep(nets), key=lambda p: tuple(int(x) for x in p["address"].split(":")[0].split(".")))
        at, heater_temps = self.now().isoformat(timespec="seconds"), self._heater_temps()
        for p in pills:
            self._found_pills[p["id"]] = {"mono": time.monotonic(), "at": at, "heaters": heater_temps, "pill": p}
            self._note_pill(p, at, heater_temps, save=True)
        log.info("Fühlersuche in %s: %s", ", ".join(map(str, nets)),
                 ", ".join(f"{p['name'] or p['model']} {p['address']}" for p in pills) or "keine Pill gefunden")
        return {"networks": [str(n) for n in nets], "seconds": round(time.monotonic() - started, 1), "pills": pills}

    # ------------------------------------------------------------------ room sensors (Shelly Pill)
    FOUND_KEEP = 30 * 60          # seconds a search result can still be saved with „Übernehmen“
    SENSOR_EVERY = 60.0           # seconds between two readings of the saved Pills (they never sleep)
    SENSOR_SAVE_EVERY = 15 * 60   # seconds between two readings kept in data.json (shown again after a restart)
    SENSOR_SEARCH_EVERY = 15 * 60  # a Pill gone from its address: search the home network for it at most this often
    NOT_ANSWERING = "Die Pill antwortet nicht – ist sie am Netzteil und im WLAN?"

    def _heater_temps(self) -> dict:
        """Room temperature and correction (°C) of every heater that is online right now – for the comparison
        with a sensor reading of the same moment."""
        return {e.id: {"temp": e.values["room_temp"] / 10,
                       "corr": e.values["temp_corr"] / 10 if isinstance(e.values.get("temp_corr"), int) else None}
                for e in self.entries if e.online and isinstance(e.values.get("room_temp"), int)}

    def _drop_old_sensors(self) -> None:
        """Sensors saved before the Pill (Shelly H&T): the app reads only Shelly Pills now, so they go."""
        old = {sid: m for sid, m in self.store.data["sensors"].items() if not m.get("pill")}
        if not old:
            return
        with self.store.edit() as d:
            for sid in old:
                d["sensors"].pop(sid, None)
        for sid, m in old.items():
            log.info("Fühler entfernt – die App liest nur noch Shelly Pills: %s (%s)",
                     m.get("name") or m.get("device_name") or sid, m.get("model") or "unbekanntes Modell")

    def _note_pill(self, pill: dict, at: str, heater_temps: dict | None = None, save: bool = False) -> dict:
        """A Pill answered: remember what it measures for its saved sensors, together with the room temperature
        of the heaters in their rooms at that moment (the pair the calibration compares). With save, the
        readings also go to data.json; a new address always does. Returns the readings (sensor id -> reading)."""
        probes = {p["channel"]: p for p in pill["probes"]}
        self._log_pill(pill)
        self._pill_status[pill["id"]] = {
            "online": True, "since": None, "mode": pill["mode"], "firmware": pill["firmware"], "update": pill["update"],
            "probes": probes, "error": "Die Pill ist mit einem Passwort geschützt – ihre Messwerte lassen sich nicht "
                                       "lesen." if pill["protected"] else None}
        data = self.store.snapshot()
        saved = {sid: m for sid, m in data["sensors"].items() if m.get("pill") == pill["id"]}
        if not saved:
            return {}
        heater_temps = self._heater_temps() if heater_temps is None else heater_temps
        readings = {}
        for sid, meta in saved.items():
            p = probes.get(meta.get("channel"))
            if p is None or p["temp"] is None:
                continue
            in_room = [hid for hid, h in data["heaters"].items() if meta.get("room") and h.get("room") == meta["room"]]
            readings[sid] = {"temp": p["temp"], "humidity": p["humidity"], "at": at,
                             "heaters": {hid: heater_temps[hid] for hid in in_room if hid in heater_temps}}
        self._sensor_live.update(readings)
        if save or any(m.get("host") != pill["address"] for m in saved.values()):
            with self.store.edit() as d:
                for sid in saved:
                    meta = d["sensors"].get(sid)
                    if meta is None:   # removed in the meantime
                        continue
                    meta.update(host=pill["address"], device_name=pill["name"], model=pill["model"], mac=pill["mac"])
                    if save and sid in readings:
                        meta["last"] = readings[sid]
        return readings

    def _pill_offline(self, pill_id: str) -> None:
        old = self._pill_status.get(pill_id) or {}
        since = old.get("since") if old.get("online") is False else self.now().isoformat(timespec="seconds")
        if old.get("online") is not False:
            log.warning("Fühler: Pill %s antwortet nicht", self._pill_name(pill_id))
        self._pill_status[pill_id] = {**old, "online": False, "since": since, "error": self.NOT_ANSWERING}

    def _pill_name(self, pill_id: str, pill: dict | None = None) -> str:
        """A Pill in the log: its name (or id) and address."""
        meta = next((m for m in self.store.snapshot()["sensors"].values() if m.get("pill") == pill_id), {})
        name = (pill or {}).get("name") or meta.get("device_name") or pill_id
        address = (pill or {}).get("address") or meta.get("host")
        return f"{name} ({address})" if address else name

    def _log_pill(self, pill: dict) -> None:
        """The log about one Pill that answered: the first time since the start, back after a gap, and in the
        detailed log every reading."""
        old = self._pill_status.get(pill["id"])
        probes = ", ".join(f"Fühler {p['channel']}: " + (f"{_num(p['temp'])} °C" if p["temp"] is not None else "kein Wert")
                           + (f", {p['humidity']:.0f} %" if p.get("humidity") is not None else "")
                           + (f" (Fehler {p['error']})" if p.get("error") else "") for p in pill["probes"]) or "kein Fühler"
        if old is None:
            log.info("Fühler: Pill %s erreichbar – %s · Modus %s · Firmware %s%s", self._pill_name(pill["id"], pill), probes,
                     pill.get("mode") or "?", pill.get("firmware") or "?",
                     " · mit Passwort geschützt" if pill.get("protected") else "")
        elif old.get("online") is False:
            log.info("Fühler: Pill %s antwortet wieder – %s", self._pill_name(pill["id"], pill), probes)
        elif log.isEnabledFor(logging.DEBUG):
            log.debug("Fühler: Pill %s – %s%s", self._pill_name(pill["id"], pill), probes,
                      f", WLAN {pill['rssi']:.0f} dBm" if pill.get("rssi") is not None else "")

    def _read_pill(self, pill_id: str, address) -> dict | None:
        """The Pill pill_id at its last address, or None if it does not answer there (or another device does)."""
        host, _, port = str(address or "").partition(":")
        port = int(port) if port.isdigit() else shelly.PORT
        info = shelly.probe(host, port, timeout=1.5) if host else None
        if not info or str(info.get("id")) != pill_id:
            return None
        pill = shelly.describe(host, port, info)
        return None if pill["offline"] else pill

    def _locate(self, ids: set, addresses: list) -> dict:
        """Pills that did not answer at their last address: one sweep of the home network for them."""
        nets = discovery.local_networks([e.cfg.host for e in self._aeroflows()]
                                        + [str(a).partition(":")[0] for a in addresses if a])
        return {p["id"]: p for p in self._sweep(nets) if p["id"] in ids} if nets else {}

    def poll_sensors(self, force: bool = False) -> None:
        """Read every saved Pill about once a minute. One that is gone from its address is searched for in the
        home network, at most every 15 minutes (the router may have given it a new one)."""
        now = time.monotonic()
        if not force and now - self._sensors_polled < self.SENSOR_EVERY:
            return
        self._sensors_polled = now
        pills = {}
        for meta in self.store.snapshot()["sensors"].values():
            if meta.get("pill"):
                pills.setdefault(meta["pill"], meta.get("host"))
        if not pills:
            return
        save = force or now - self._sensors_saved >= self.SENSOR_SAVE_EVERY
        at, heater_temps = self.now().isoformat(timespec="seconds"), self._heater_temps()
        found = dict(zip(pills, self._pool.map(lambda item: self._read_pill(*item), pills.items())))
        missing = {pid for pid, p in found.items() if p is None}
        if missing and (force or now - self._sensors_searched >= self.SENSOR_SEARCH_EVERY):
            self._sensors_searched = now
            found.update(self._locate(missing, list(pills.values())))
        fresh = {}
        for pid, p in found.items():
            if p is None:
                self._pill_offline(pid)
            else:
                fresh.update(self._note_pill(p, at, heater_temps, save=save))
        self._calib_tick(fresh, save)
        if save:
            self._sensors_saved = now

    def _sensor(self, sensor_id: str) -> dict:
        meta = self.store.data["sensors"].get(sensor_id)
        if meta is None:
            raise ActionError("Fühler nicht gefunden", 404)
        return meta

    def _room_or_none(self, room_id) -> str | None:
        room_id = room_id or None
        if room_id is not None:
            self._room(str(room_id))
        return room_id

    def add_sensor(self, body: dict) -> dict:
        """„Übernehmen“: save one probe of a Pill from the search, optionally with name and room."""
        pill_id, channel = str(body.get("pill") or ""), body.get("channel")
        hit = self._found_pills.get(pill_id)
        if not hit or time.monotonic() - hit["mono"] > self.FOUND_KEEP:
            raise ActionError("Diese Pill hat die Suche nicht gefunden – bitte erneut suchen.", 404)
        p = hit["pill"]
        if isinstance(channel, bool) or channel not in [x["channel"] for x in p["probes"]]:
            raise ActionError("Diesen Fühler hat die Pill nicht gemeldet – steckt er? Dann erneut suchen.", 404)
        sensor_id = f"{pill_id}-{channel}"
        if sensor_id in self.store.data["sensors"]:
            raise ActionError("Dieser Fühler ist schon gespeichert.", 409)
        name = self._clean_name(body.get("name"), allow_empty=True)
        room_id = self._room_or_none(body.get("room"))
        with self.store.edit() as d:
            d["sensors"][sensor_id] = {"name": name, "room": room_id, "pill": pill_id, "channel": channel,
                                       "device_name": p["name"], "host": p["address"], "model": p["model"],
                                       "mac": p["mac"], "added": self.now().isoformat(timespec="seconds")}
        self._note_pill(p, hit["at"], hit["heaters"], save=True)
        log.info("Fühler gespeichert: %s %s, Fühler %s", p["model"], p["address"], channel)
        return {"id": sensor_id, "message": f"Fühler „{self._sensor_name(sensor_id, self.store.snapshot())}“ gespeichert."}

    def update_sensor(self, sensor_id: str, body: dict) -> dict:
        meta = self._sensor(sensor_id)
        name = self._clean_name(body.get("name"), allow_empty=True)
        room_id = self._room_or_none(body.get("room")) if "room" in body else None
        if "room" in body and room_id != meta.get("room"):
            with self._calib_lock:
                calibrating = sensor_id in self._calib_runs
            if calibrating:
                raise ActionError("Der Fühler hat eine Kalibrierung – erst abbrechen oder schließen, dann den Raum "
                                  "wechseln.", 409)
        with self.store.edit() as d:
            meta = d["sensors"][sensor_id]
            if "name" in body:
                meta["name"] = name
            if "room" in body:
                meta["room"] = room_id
        log.info("Fühler %s gespeichert:%s%s", sensor_id, f" Name „{name}“" if "name" in body else "",
                 f" Raum {self.store.data['rooms'][room_id]['name'] if room_id else '(keiner)'}" if "room" in body else "")
        return {"message": "Gespeichert."}

    def refresh_sensor(self, sensor_id: str) -> dict:
        """„Aktualisieren“: read the sensor's Pill now – at its last address, else search the home network for it."""
        meta = self._sensor(sensor_id)
        pill = self._read_pill(meta["pill"], meta.get("host")) \
            or self._locate({meta["pill"]}, [meta.get("host")]).get(meta["pill"])
        if pill is None:
            self._pill_offline(meta["pill"])
            raise ActionError(self.NOT_ANSWERING, 504)
        self._note_pill(pill, self.now().isoformat(timespec="seconds"), save=True)
        if pill["protected"]:
            raise ActionError("Die Pill ist mit einem Passwort geschützt – ihre Messwerte lassen sich nicht lesen.", 409)
        probe = next((p for p in pill["probes"] if p["channel"] == meta.get("channel")), None)
        if probe is None:
            raise ActionError("Die Pill meldet diesen Fühler nicht – steckt er? Sonst die Pill unter „Einrichten“ "
                              "neu starten.", 409)
        if probe["temp"] is None:
            raise ActionError("Der Fühler liefert gerade keinen Wert – steckt er richtig in der Pill?", 409)
        name = self._sensor_name(sensor_id, self.store.snapshot())
        return {"updated": True, "message": f"{name}: {probe['temp']:.1f} °C".replace(".", ",")}

    def delete_sensor(self, sensor_id: str) -> dict:
        self._sensor(sensor_id)
        with self._calib_lock:
            self._calib_runs.pop(sensor_id, None)
        with self.store.edit() as d:
            d["sensors"].pop(sensor_id)
        self._sensor_live.pop(sensor_id, None)
        log.info("Fühler entfernt: %s", sensor_id)
        return {"message": "Fühler entfernt."}

    def _pill_address(self, pill_id: str) -> tuple[str, int]:
        """Where the Pill was seen last (search or saved sensors) – checked again before anything is written."""
        hit = self._found_pills.get(pill_id)
        address = hit["pill"]["address"] if hit else next(
            (m["host"] for m in self.store.snapshot()["sensors"].values() if m.get("pill") == pill_id and m.get("host")),
            None)
        if not address:
            raise ActionError("Diese Pill kennt die App nicht – bitte zuerst mit „+ Gerät hinzufügen“ suchen.", 404)
        host, _, port = address.partition(":")
        port = int(port) if port.isdigit() else shelly.PORT
        info = shelly.probe(host, port, timeout=2.0)
        if not info or str(info.get("id")) != pill_id:   # gone, or another device has taken the address
            raise ActionError("Die Pill antwortet nicht unter ihrer letzten Adresse – bitte unter „+ Gerät hinzufügen“ › „Raumfühler“ erneut suchen.", 504)
        return host, port

    def set_pill_mode(self, pill_id: str, mode) -> dict:
        """„Einrichten“: tell the Pill what is plugged in, and restart it if it needs that. Writes to the Pill."""
        if mode not in shelly.MODES:
            raise ActionError("Bitte wählen, was in der Pill steckt: DS18B20 oder DHT22.")
        host, port = self._pill_address(pill_id)
        try:
            restart = shelly.set_mode(host, port, mode)
            if restart:
                shelly.restart(host, port)
        except shelly.ShellyError as exc:
            raise ActionError(str(exc), exc.status) from None
        hit = self._found_pills.get(pill_id)
        if hit:
            hit["pill"]["mode"] = mode
        log.info("Pill %s eingerichtet: %s%s", pill_id, shelly.MODES[mode], ", Neustart" if restart else "")
        text = f"Die Pill ist auf {shelly.MODES[mode]} eingestellt."
        return {"restart": restart,
                "message": text + (" Sie startet neu – in etwa 15 Sekunden ist sie wieder da." if restart else "")}

    def restart_pill(self, pill_id: str) -> dict:
        """Restart a Pill, e.g. after plugging in a probe it has not found yet. Writes to the Pill."""
        host, port = self._pill_address(pill_id)
        try:
            shelly.restart(host, port)
        except shelly.ShellyError as exc:
            raise ActionError(str(exc), exc.status) from None
        log.info("Pill %s neu gestartet", pill_id)
        return {"restart": True, "message": "Die Pill startet neu – in etwa 15 Sekunden ist sie wieder da."}

    CALIB_TOLERANCE = 0.5   # °C – the deviation the user accepts (heater vs. room sensor)
    CALIB_DEFAULT_HOURS = 24  # a calibration run: the „Ein-Tages-Kalibrierung“ …
    CALIB_MAX_HOURS = 72      # … or up to three days
    CALIB_SETTLE = 60 * 60    # seconds after a change of mode, setpoint or window before a minute counts as calm
    CALIB_MIN_MINUTES = 120   # calm minutes a heater needs before the app proposes a correction
    CALIB_SPREAD_WARN = 1.0   # °C – middle half of the minutes wider than this: the proposal is uncertain

    @staticmethod
    def _probe_number(sensor_id: str, data: dict) -> int | None:
        """1, 2, … if the sensor's Pill has more than one saved probe (so their default names differ), else None."""
        meta = data["sensors"][sensor_id]
        channels = sorted(m.get("channel") for m in data["sensors"].values() if m.get("pill") == meta.get("pill"))
        return channels.index(meta.get("channel")) + 1 if len(channels) > 1 else None

    def _sensor_name(self, sensor_id: str, data: dict) -> str:
        meta = data["sensors"][sensor_id]
        if meta.get("name"):
            return meta["name"]
        number = self._probe_number(sensor_id, data)
        return (meta.get("device_name") or meta.get("model") or sensor_id) + (f" · Fühler {number}" if number else "")

    def _sensor_views(self, data: dict, heaters: list[dict]) -> list[dict]:
        views = []
        with self._calib_lock:
            runs = json.loads(json.dumps(self._calib_runs))
        now_iso = self.now().isoformat(timespec="seconds")
        for sensor_id, meta in data["sensors"].items():
            saved, live = meta.get("last") or {}, self._sensor_live.get(sensor_id) or {}
            last = live if live and str(live.get("at") or "") >= str(saved.get("at") or "") else saved
            room = meta.get("room") if meta.get("room") in data["rooms"] else None
            status = self._pill_status.get(meta.get("pill")) or {}
            error = status.get("error")
            if status.get("online") and not error:
                probe = (status.get("probes") or {}).get(meta.get("channel"))
                if probe is None:
                    error = "Die Pill meldet diesen Fühler nicht – steckt er? Sonst die Pill unter „Einrichten“ neu starten."
                elif probe.get("temp") is None:
                    error = "Der Fühler liefert gerade keinen Wert – steckt er richtig in der Pill?"
            pairs = last.get("heaters") or {}
            compare = []
            for h in (h for h in heaters if room and h["room"] == room):
                then = pairs.get(h["id"]) or {}
                deviation = round(then["temp"] - last["temp"], 1) if then.get("temp") is not None and \
                    last.get("temp") is not None else None
                compare.append({"heater": h["id"], "name": h["name"], "temp": then.get("temp"), "corr": then.get("corr"),
                                "deviation": deviation,
                                "ok": None if deviation is None else abs(deviation) <= self.CALIB_TOLERANCE + 1e-9})
            views.append({"id": sensor_id, "name": self._sensor_name(sensor_id, data), "custom_name": meta.get("name") or "",
                          "device_name": meta.get("device_name"), "room": room, "host": meta.get("host"),
                          "model": meta.get("model"), "mac": meta.get("mac"), "pill": meta.get("pill"),
                          "channel": meta.get("channel"), "probe_number": self._probe_number(sensor_id, data),
                          "mode": status.get("mode"), "firmware": status.get("firmware"), "update": status.get("update"),
                          "online": status.get("online"), "offline_since": status.get("since"), "error": error,
                          "temp": last.get("temp"), "humidity": last.get("humidity"), "measured_at": last.get("at"),
                          "compare": compare, "tolerance": self.CALIB_TOLERANCE,
                          "calibration": self._calib_view(runs[sensor_id], heaters, now_iso) if sensor_id in runs else None})
        return sorted(views, key=lambda v: v["name"].lower())

    # ------------------------------------------------------------------ calibration run („Kalibrierlauf“)
    def _calib_reason(self, e: HeaterState, now: float) -> str | None:
        """Why this minute of heater e does not count for a calibration – or None: the room is calm."""
        v = e.values
        if not e.online or not isinstance(v.get("room_temp"), int) or not isinstance(v.get("temp_corr"), int):
            return "offline"
        if v.get("open_window") == 1:
            return "window"
        if v.get("operating_mode") == 2:
            return "boost"
        if v.get("operating_mode") in (3, 4):   # holiday, standby („Aus“)
            return "off"
        if e.steady_since is None or now - e.steady_since < self.CALIB_SETTLE:
            return "settling"
        return None

    def _calib_tick(self, fresh: dict, save: bool) -> None:
        """One calibration minute: for every running calibration whose sensor was just read, each heater of its room
        either adds a calm minute to its tally – the correction that would have made it show the sensor's value
        (0.1 °C steps) – or counts as left out, with the reason."""
        if not self._calib_runs:
            return
        now_iso, now = self.now().isoformat(timespec="seconds"), time.time()
        rooms = {hid: m.get("room") for hid, m in self.store.snapshot()["heaters"].items()}
        ended = []
        with self._calib_lock:
            for sid, run in self._calib_runs.items():
                if run.get("finished"):
                    continue
                if now_iso >= run["until"]:
                    run["finished"] = run["until"]
                    ended.append(sid)
                    continue
                reading = fresh.get(sid)
                if reading is None or reading.get("temp") is None:
                    run["missing"] = run.get("missing", 0) + 1
                    continue
                for e in self._aeroflows():
                    if rooms.get(e.id) != run["room"]:
                        continue
                    tally = run["heaters"].setdefault(e.id, {"bins": {}, "skipped": {}})
                    pair = (reading.get("heaters") or {}).get(e.id) or {}
                    reason = self._calib_reason(e, now) or (None if pair.get("corr") is not None else "offline")
                    if reason:
                        tally["skipped"][reason] = tally["skipped"].get(reason, 0) + 1
                        continue
                    key = str(round((reading["temp"] - pair["temp"] + pair["corr"]) * 10))
                    tally["bins"][key] = tally["bins"].get(key, 0) + 1
        for sid in ended:
            log.info("Kalibrierung zu Ende: %s", sid)
        if save or ended:
            self._calib_save()

    @staticmethod
    def _calib_stats(bins: dict) -> dict | None:
        """Median and middle half (q1…q3) of the tally, in 0.1 °C; None without a calm minute."""
        values = sorted((int(k), n) for k, n in bins.items() if n > 0)
        total = sum(n for _, n in values)
        if not total:
            return None

        def at(pos: int) -> int:   # the value of the pos-th minute (0-based) in sorted order
            for value, n in values:
                if pos < n:
                    return value
                pos -= n
            return values[-1][0]
        return {"minutes": total, "median": (at((total - 1) // 2) + at(total // 2)) / 2,
                "q1": at((total - 1) // 4), "q3": at(3 * (total - 1) // 4)}

    def _calib_view(self, run: dict, heaters: list[dict], now_iso: str) -> dict:
        """A calibration run for the setup page: progress while it runs, then one proposal per heater."""
        finished = run.get("finished") or (run["until"] if now_iso >= run["until"] else None)
        by_id = {h["id"]: h for h in heaters}
        ids = list(run["heaters"]) + [h["id"] for h in heaters if h["room"] == run["room"] and h["id"] not in run["heaters"]
                                      and h["kind"] != "zigbee"]
        rows = []
        for hid in ids:
            tally, h = run["heaters"].get(hid) or {"bins": {}, "skipped": {}}, by_id.get(hid)
            stats = self._calib_stats(tally["bins"])
            minutes = stats["minutes"] if stats else 0
            estimate = round(stats["median"]) / 10 if stats else None
            enough = stats is not None and minutes >= self.CALIB_MIN_MINUTES
            spread = round((stats["q3"] - stats["q1"]) / 10, 1) if stats else None
            current = h["temp_corr"] if h else None
            applied = (run.get("applied") or {}).get(hid)
            why = None
            if finished and not applied:
                why = ("zu wenig ruhige Messung" if not enough
                       else "außerhalb von ±9,9 °C – bitte Fühler und Aufstellort prüfen" if abs(estimate) > MAX_CORRECTION / 10
                       else "Heizung nicht mehr eingerichtet" if h is None
                       else "nur lesbar – für die Korrektur wird ihr Token gebraucht" if not h["writable"]
                       else "Heizung offline" if not h["online"] else None)
            rows.append({"id": hid, "name": h["name"] if h else hid, "minutes": minutes, "skipped": tally["skipped"],
                         "estimate": estimate, "proposal": estimate if enough else None, "current": current,
                         "change": round(estimate - current, 1) if enough and current is not None else None,
                         "spread": spread, "uncertain": spread is not None and spread > self.CALIB_SPREAD_WARN + 1e-9,
                         "applied": applied, "can_apply": bool(finished and enough and not applied and why is None),
                         "why": why})
        return {"running": not finished, "started": run["started"], "until": run["until"], "finished": finished,
                "hours": run.get("hours"), "missing": run.get("missing", 0),
                "can_finish": not finished and any(r["proposal"] is not None for r in rows),
                "heaters": rows}

    def _calib_save(self) -> None:
        """Write the calibration runs into data.json – every 15 minutes, and at once after a start, end or change."""
        with self._calib_lock:
            runs = json.loads(json.dumps(self._calib_runs))
        with self.store.edit() as d:
            for sid, meta in d["sensors"].items():
                if sid in runs:
                    meta["calibration"] = runs[sid]
                else:
                    meta.pop("calibration", None)

    def _when(self, moment: datetime) -> str:
        """„heute 22:10 Uhr“, „morgen 20:40 Uhr“ or „23.11. 08:00 Uhr“."""
        days = (moment.date() - self.now().date()).days
        day = "heute" if days == 0 else "morgen" if days == 1 else moment.strftime("%d.%m.")
        return f"{day} {moment.strftime('%H:%M')} Uhr"

    @staticmethod
    def _duration(minutes: int) -> str:
        hours, rest = divmod(int(minutes), 60)
        return (f"{hours} Std." + (f" {rest} Min." if rest else "")) if hours else f"{rest} Min."

    def start_calibration(self, sensor_id: str, body: dict) -> dict:
        """Start a calibration run: from now on every calm minute pairs the sensor with each heater of its room."""
        meta = self._sensor(sensor_id)
        data = self.store.snapshot()
        room = meta.get("room") if meta.get("room") in data["rooms"] else None
        if not room:
            raise ActionError("Bitte den Fühler zuerst einem Raum zuordnen.", 409)
        members = [e.id for e in self._aeroflows() if data["heaters"].get(e.id, {}).get("room") == room]
        if not members:
            raise ActionError("In diesem Raum ist keine AeroFlow-Heizung. Thermostate melden ihre Temperatur nur in "
                              "Schritten von 0,5 °C – sie kalibrierst du von Hand.", 409)
        hours = body.get("hours", self.CALIB_DEFAULT_HOURS)
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= self.CALIB_MAX_HOURS:
            raise ActionError(f"Die Dauer muss zwischen 1 und {self.CALIB_MAX_HOURS} Stunden liegen.")
        now = self.now().replace(microsecond=0)
        until = now + timedelta(hours=hours)
        with self._calib_lock:
            if sensor_id in self._calib_runs:
                raise ActionError("Für diesen Fühler gibt es schon eine Kalibrierung – erst abbrechen oder schließen.", 409)
            self._calib_runs[sensor_id] = {"room": room, "hours": hours, "started": now.isoformat(),
                                           "until": until.isoformat(), "finished": None, "missing": 0,
                                           "heaters": {hid: {"bins": {}, "skipped": {}} for hid in members},
                                           "applied": {}}
        self._calib_save()
        log.info("Kalibrierung gestartet: %s in %s, %d Std.", sensor_id, data["rooms"][room]["name"], hours)
        return {"message": f"Kalibrierung gestartet – sie läuft bis {self._when(until)}."}

    def finish_calibration(self, sensor_id: str) -> dict:
        """„Jetzt auswerten“: end a run early – once at least one heater has enough calm minutes."""
        self._sensor(sensor_id)
        now_iso = self.now().isoformat(timespec="seconds")
        with self._calib_lock:
            run = self._calib_runs.get(sensor_id)
            if run is None:
                raise ActionError("Für diesen Fühler läuft keine Kalibrierung.", 404)
            if not run.get("finished") and now_iso < run["until"]:
                if not any((self._calib_stats(t["bins"]) or {}).get("minutes", 0) >= self.CALIB_MIN_MINUTES
                           for t in run["heaters"].values()):
                    raise ActionError("Noch zu wenig ruhige Messung – jede Heizung braucht mindestens "
                                      f"{self._duration(self.CALIB_MIN_MINUTES)}.", 409)
                run["finished"] = now_iso
        self._calib_save()
        log.info("Kalibrierung ausgewertet: %s", sensor_id)
        return {"message": "Kalibrierung ausgewertet."}

    def discard_calibration(self, sensor_id: str) -> dict:
        """„Abbrechen“ while it runs, „Schließen“ afterwards: the run and its tally go."""
        self._sensor(sensor_id)
        now_iso = self.now().isoformat(timespec="seconds")
        with self._calib_lock:
            run = self._calib_runs.pop(sensor_id, None)
        if run is None:
            raise ActionError("Für diesen Fühler gibt es keine Kalibrierung.", 404)
        self._calib_save()
        running = not run.get("finished") and now_iso < run["until"]
        log.info("Kalibrierung %s: %s", "abgebrochen" if running else "geschlossen", sensor_id)
        return {"message": "Kalibrierung abgebrochen." if running else "Kalibrierung geschlossen."}

    def apply_calibration(self, sensor_id: str, body: dict) -> dict:
        """„Übernehmen“: write the proposed correction into one heater of a finished run. Writes to the heater."""
        self._sensor(sensor_id)
        heater_id = str(body.get("heater") or "")
        data = self.store.snapshot()
        heaters = [self._heater_view(e, data) for e in self.entries]
        now_iso = self.now().isoformat(timespec="seconds")
        with self._calib_lock:
            run = self._calib_runs.get(sensor_id)
            view = self._calib_view(json.loads(json.dumps(run)), heaters, now_iso) if run else None
        if view is None:
            raise ActionError("Für diesen Fühler gibt es keine Kalibrierung.", 404)
        if view["running"]:
            raise ActionError("Die Kalibrierung läuft noch – erst auswerten.", 409)
        row = next((r for r in view["heaters"] if r["id"] == heater_id), None)
        if row is None:
            raise ActionError("Diese Heizung gehört nicht zu dieser Kalibrierung.", 404)
        if not row["can_apply"]:
            raise ActionError(f"{row['name']}: {'schon übernommen' if row['applied'] else row['why'] or 'nicht möglich'}.", 409)
        res = self.calibrate(heater_id, {"correction": row["proposal"]})
        with self._calib_lock:
            run = self._calib_runs.get(sensor_id)
            if run is not None:
                run.setdefault("applied", {})[heater_id] = {"at": now_iso, "old": res["old"], "new": res["new"]}
        self._calib_save()
        return {"message": f"{row['name']}: {res['message']}"}

    def _adopt(self, found: dict) -> list[dict]:
        """Known heaters (by serial number) that are offline but answered at another address: take that address."""
        moved = []
        for (ip, port), serial in found.items():
            e = next((x for x in self._aeroflows() if x.key == serial), None)
            if e is None or e.online or (e.cfg.host, e.cfg.port) == (ip, port):
                continue
            old = e.label
            if self._move(e, ip, port):
                moved.append({"heater": e.id, "name": self._name(e), "old": old, "new": e.label})
        return moved

    def _move(self, e: HeaterState, host: str, port: int) -> bool:
        with self._entries_lock:
            if any(x is not e and (x.cfg.host, x.cfg.port) == (host, port) for x in self._aeroflows()):
                return False   # address already belongs to another entry
            old = e.label
            e.cfg = HeaterConfig(host, port, e.cfg.token, e.cfg.line)
            e.device = Heater(host, e.cfg.token, port)
            self._save_config()
        log.info("Heizung %s hat eine neue Adresse: %s (bisher %s)", e.key, e.label, old)
        self._heater_meta_set(e, "last_host", e.label)
        self.refresh(e, full=True)
        return True

    def _heal(self) -> None:
        """Called by the poller: look for heaters that have been unreachable for a while (at most every 15 min)."""
        now = time.time()
        lost = [e for e in self._aeroflows() if e.key and not e.online and e.offline_since
                and now - e.offline_since >= LOST_AFTER]
        if not lost or now - self._last_heal < HEAL_EVERY:
            return
        self._last_heal = now
        try:
            nets = discovery.local_networks([e.cfg.host for e in self._aeroflows()])
            log.info("Suche im Heimnetz (%s) nach Heizungen, die nicht antworten: %s", ", ".join(map(str, nets)) or "?",
                     ", ".join(self._who(e) for e in lost))
            with self._scan_lock:
                found = discovery.scan(self._scan_targets(nets))
            moved = self._adopt(found)
            if not moved:
                log.info("Suche im Heimnetz: %d Heizung(en) antworten, keine der gesuchten unter einer neuen Adresse",
                         len(found))
        except Exception:  # never disturb the poller
            log.exception("Automatische Netzsuche fehlgeschlagen")

    # ------------------------------------------------------------------ rooms, heaters, plans
    def create_room(self, body: dict) -> dict:
        name = self._clean_name(body.get("name"))
        members = self._heater_entries(body["heaters"]) if "heaters" in body else None
        room_id = new_id("r")
        with self.store.edit() as d:
            d["rooms"][room_id] = {"name": name, "plan": None}
            if members is not None:
                self._assign(d, room_id, members)
        log.info("Raum angelegt: %s%s", name, f" mit {', '.join(self._name(e) for e in members)}" if members else "")
        return {"id": room_id}

    def update_room(self, room_id: str, body: dict) -> dict:
        self._room(room_id)
        name = self._clean_name(body["name"]) if "name" in body else None
        plan_id = body.get("plan") or None
        if plan_id and plan_id not in self.store.data["plans"]:
            raise ActionError("Heizplan nicht gefunden", 404)
        members = self._heater_entries(body["heaters"]) if "heaters" in body else None
        with self.store.edit() as d:
            room = d["rooms"][room_id]
            old_name = room["name"]
            if name is not None:
                room["name"] = name
            if "plan" in body:
                room["plan"] = plan_id
            if members is not None:
                self._assign(d, room_id, members)
        changed = ([f"Name „{name}“"] if name is not None and name != old_name else []) \
            + ([f"Plan „{self.store.data['plans'][plan_id]['name']}“" if plan_id else "kein Plan"] if "plan" in body else []) \
            + ([f"Heizungen: {', '.join(self._name(e) for e in members) or 'keine'}"] if members is not None else [])
        log.info("Raum %s geändert: %s", old_name, " · ".join(changed) or "nichts")
        return {}

    def _heater_entries(self, ids) -> list[HeaterState]:
        if not isinstance(ids, list):
            raise ActionError("„heaters“ muss eine Liste sein")
        known = {e.id: e for e in self.entries}
        if any(i not in known for i in ids):
            raise ActionError("Heizung nicht gefunden", 404)
        return [known[i] for i in ids]

    @staticmethod
    def _assign(data: dict, room_id: str, members: list[HeaterState]) -> None:
        """Make exactly these heaters the members of the room (they leave their previous room)."""
        wanted = {e.id for e in members}
        for e in members:
            data["heaters"].setdefault(e.id, Service._new_meta(e))["room"] = room_id
        for heater_id, meta in data["heaters"].items():
            if meta.get("room") == room_id and heater_id not in wanted:
                meta["room"] = None

    def delete_room(self, room_id: str) -> dict:
        log.info("Raum gelöscht: %s", self._room(room_id)["name"])
        with self.store.edit() as d:
            d["rooms"].pop(room_id)
            for meta in list(d["heaters"].values()) + list(d["sensors"].values()):
                if meta.get("room") == room_id:
                    meta["room"] = None
            for pid, room in list((d.get("plug_rooms") or {}).items()):
                if room == room_id:
                    d["plug_rooms"].pop(pid)
        with self._calib_lock:   # a calibration of the room ends with it
            gone = [sid for sid, run in self._calib_runs.items() if run.get("room") == room_id]
            for sid in gone:
                self._calib_runs.pop(sid)
        if gone:
            self._calib_save()
        return {}

    def update_heater(self, heater_id: str, body: dict) -> dict:
        """Name and room (app data) and/or address and token (heizungen.conf)."""
        e = self._entry(heater_id)
        room_id = body.get("room") or None
        if room_id and room_id not in self.store.data["rooms"]:
            raise ActionError("Raum nicht gefunden", 404)
        name = self._clean_name(body.get("name"), allow_empty=True)
        warning = ""
        if "host" in body or str(body.get("token") or "").strip() or body.get("remove_token"):
            if e.zigbee:
                raise ActionError("Ein Thermostat hat keine Adresse und keinen Token – es funkt über Zigbee.")
            warning = self._reconfigure(e, body)
        with self.store.edit() as d:  # after _reconfigure: the heater may now be known by its serial number
            meta = d["heaters"].setdefault(e.id, self._new_meta(e))
            if "name" in body:
                meta["name"] = name
            if "room" in body:
                meta["room"] = room_id
        if "name" in body or "room" in body:
            log.info("Heizung %s gespeichert:%s%s", self._who(e), f" Name „{name}“" if "name" in body else "",
                     f" Raum {self.store.data['rooms'][room_id]['name'] if room_id else '(keiner)'}" if "room" in body else "")
        return {"message": "Gespeichert." + warning, "warning": bool(warning)}

    # ------------------------------------------------------------------ adding / changing heaters
    def add_heater(self, body: dict) -> dict:
        host, port = self._parse_host(body.get("host"))
        token, qr_idu = self._parse_token(body.get("token"))
        name = self._clean_name(body.get("name"), allow_empty=True)
        self._ensure_unique(host, port)
        online, warning = self._verify(host, port, token, qr_idu)
        e = HeaterState(HeaterConfig(host, port, token, 0))
        with self._entries_lock:
            self._ensure_unique(host, port)
            self.entries = self._aeroflows() + [e] + [x for x in self.entries if x.zigbee]   # thermostats stay last
            self._save_config()
        if online:
            self.refresh(e, full=True)
        if name:
            with self.store.edit() as d:
                d["heaters"].setdefault(e.id, self._new_meta(e))["name"] = name
        log.info("Heizung hinzugefügt: %s", e.label)
        return {"id": e.id, "warning": bool(warning),
                "message": ("Heizung hinzugefügt." if token else "Heizung hinzugefügt (ohne Token: nur Anzeige).") + warning}

    def delete_heater(self, heater_id: str) -> dict:
        e = self._entry(heater_id)
        if e.zigbee:   # in the Zigbee network it stays paired (that is „Entfernen“ there) – and in the list, unnamed
            self.forget_thermostat(e.id)
            return {"message": "Thermostat aus der App entfernt."}
        with self._entries_lock:
            self.entries = [x for x in self.entries if x is not e]
            self._save_config()
        with self.store.edit() as d:
            d["heaters"].pop(e.id, None)
        log.info("Heizung entfernt: %s", e.label)
        return {"message": "Heizung entfernt."}

    def _reconfigure(self, e: HeaterState, body: dict) -> str:
        """Apply a new address and/or token; returns a warning text (empty if all is fine)."""
        host, port = self._parse_host(body["host"]) if "host" in body else (e.cfg.host, e.cfg.port)
        moved = (host, port) != (e.cfg.host, e.cfg.port)
        if moved:
            self._ensure_unique(host, port, exclude=e)
        token, qr_idu, new_token = e.cfg.token, None, False
        if body.get("remove_token"):
            token = None
        elif str(body.get("token") or "").strip():
            token, qr_idu = self._parse_token(body["token"])
            new_token = True
        if not moved and token == e.cfg.token:
            return ""
        online, warning = self._verify(host, port, token if (moved or new_token) else None, qr_idu)
        with self._entries_lock:
            e.cfg = HeaterConfig(host, port, token, e.cfg.line)
            e.device = Heater(host, token, port)
            if moved:  # re-read identity and clock from the (possibly other) device
                e.values, e.online, e.clock_drift = {}, False, None
            self._save_config()
        if online:
            self.refresh(e, full=True)
        log.info("Heizung geändert: %s", e.label)
        return warning

    def _verify(self, host: str, port: int, token: str | None, qr_idu: str | None) -> tuple[bool, str]:
        """Ask the device before saving: reachable? QR code from this heater? token accepted?"""
        probe = Heater(host, token, port, timeout=1.0, retries=1)
        try:
            idu = str(probe.get("idu"))
        except coap.CoapError:
            return False, (" Unter dieser Adresse antwortet gerade keine Heizung – bitte prüfen."
                           + (" Der Token konnte deshalb nicht geprüft werden." if token else ""))
        except DeviceError as exc:
            raise ActionError(f"Unter {host} antwortet keine passende Heizung ({exc}).") from None
        if qr_idu and qr_idu != idu:
            raise ActionError(f"Der QR-Code gehört zur Heizung mit Seriennummer {qr_idu}, "
                              f"unter {host} antwortet aber die Heizung {idu}.")
        if token:
            try:
                probe.check_token()
            except DeviceError:
                raise ActionError("Die Heizung lehnt diesen Token ab – bitte den Token aus dem QR-Code "
                                  "(Feld „pop“) prüfen.") from None
            except coap.CoapError:
                return True, " Der Token konnte nicht geprüft werden (keine Antwort)."
        return True, ""

    @staticmethod
    def _parse_host(text) -> tuple[str, int]:
        raw = str(text or "").strip()
        if not raw:
            raise ActionError("Bitte eine IP-Adresse angeben.")
        host, port = raw, coap.DEFAULT_PORT
        if raw.count(":") == 1:
            host, port_text = raw.split(":")
            if not port_text.isdigit() or not 0 < int(port_text) < 65536:
                raise ActionError(f"Ungültiger Port in „{raw}“.")
            port = int(port_text)
        ipv4 = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})", host)
        if ipv4:
            if any(int(part) > 255 for part in ipv4.groups()):
                raise ActionError(f"„{host}“ ist keine gültige IP-Adresse.")
        elif not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
                              r"[A-Za-z0-9])?)*", host):
            raise ActionError(f"„{host}“ ist weder eine IP-Adresse noch ein gültiger Gerätename.")
        return host, port

    @staticmethod
    def _parse_token(text) -> tuple[str | None, str | None]:
        """Accepts the bare token or the whole QR content {"ver":…,"name":"…_<serial>","pop":"…"}."""
        raw = str(text or "").strip()
        if not raw:
            return None, None
        qr_idu = None
        if raw.startswith("{"):
            try:
                qr = json.loads(raw)
            except ValueError:
                raise ActionError("Der eingefügte QR-Inhalt ist kein gültiges JSON.") from None
            name = str(qr.get("name") or "")
            qr_idu = name.rsplit("_", 1)[-1] if "_" in name else None
            raw = str(qr.get("pop") or "").strip()
            if not raw:
                raise ActionError("Im QR-Inhalt fehlt das Feld „pop“ (der Token).")
        if not re.fullmatch(r"[A-Za-z0-9+/=._-]{8,64}", raw):
            raise ActionError("Der Token sieht nicht richtig aus – erwartet werden die 24 Zeichen aus dem QR-Code.")
        return raw, qr_idu

    def _ensure_unique(self, host: str, port: int, exclude: HeaterState | None = None) -> None:
        for x in self._aeroflows():
            if x is not exclude and (x.cfg.host, x.cfg.port) == (host, port):
                raise ActionError("Diese Adresse ist schon eingetragen.", 409)

    def _save_config(self) -> None:
        """Rewrite heizungen.conf (keeps the leading comment block), readable only by the owner."""
        header = []
        if os.path.exists(self.config_path):
            with open(self.config_path, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip() and not line.lstrip().startswith("#"):
                        break
                    header.append(line.rstrip("\n"))
        while header and not header[-1].strip():
            header.pop()
        if not header:
            header = ["# Heizungen – eine pro Zeile: IP-Adresse und Token (siehe heizungen.conf.example)",
                      "# IP-Adresse        Token"]
        lines = header + [f"{x.label:<19} {x.cfg.token or ''}".rstrip() for x in self._aeroflows()]
        tmp = f"{self.config_path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, self.config_path)

    def _plan_from(self, body: dict) -> dict:
        try:
            return plans.normalize(body)
        except plans.PlanError as exc:
            raise ActionError(str(exc)) from None

    def create_plan(self, body: dict) -> dict:
        plan = self._plan_from(body)
        plan_id = new_id("p")
        with self.store.edit() as d:
            d["plans"][plan_id] = plan
        log.info("Heizplan angelegt: „%s“ (%d Zeitfenster)", plan["name"], sum(map(len, plan["days"].values())))
        return {"id": plan_id}

    def update_plan(self, plan_id: str, body: dict) -> dict:
        if plan_id not in self.store.data["plans"]:
            raise ActionError("Heizplan nicht gefunden", 404)
        plan = self._plan_from(body)
        with self.store.edit() as d:
            d["plans"][plan_id] = plan
        log.info("Heizplan geändert: „%s“ (%d Zeitfenster)", plan["name"], sum(map(len, plan["days"].values())))
        return {}

    def delete_plan(self, plan_id: str) -> dict:
        if plan_id not in self.store.data["plans"]:
            raise ActionError("Heizplan nicht gefunden", 404)
        log.info("Heizplan gelöscht: „%s“", self.store.data["plans"][plan_id]["name"])
        with self.store.edit() as d:
            d["plans"].pop(plan_id)
            for room in d["rooms"].values():
                if room.get("plan") == plan_id:
                    room["plan"] = None
        return {}

    # ------------------------------------------------------------------ Verlauf und Kosten (heizapp.history)
    MAX_PRICE_CT = 200   # ct/kWh accepted as electricity price
    MAX_POWER_W = 5000   # W accepted for a heater (the FlexiSmart Pro models have 650 to 2450 W)

    def _record(self, now: float | None = None) -> None:
        """What every device shows now, for its five-minute entry – only with a clock that is right."""
        if not self.clock_trusted():
            return
        try:   # the Verlauf must never hold up the rest of the loop (clocks, protocol)
            devices = [{"id": e.id, "online": e.online, "temp": e.values.get("room_temp"),
                        "soll": e.values.get("setpoint"), "heating": e.values.get("heater_state") == 1,
                        "window": e.values.get("open_window") == 1} for e in list(self.entries)]
            sensors = [{"id": sid, "at": r.get("at"), "temp": r.get("temp"), "humidity": r.get("humidity")}
                       for sid, r in list(self._sensor_live.items())]
            self.history.sample(time.time() if now is None else now, devices, sensors)
            self._record_failed = False
        except Exception:
            if not self._record_failed:
                self._record_failed = True
                log.exception("Verlauf: Aufzeichnen fehlgeschlagen")

    def _record_energy(self, now: float | None = None) -> None:
        """What every plug reported last, for its quarter hours (heizapp.energy) – only with a clock that is right."""
        if not self.clock_trusted():
            return
        try:   # the recording must never hold up the rest of the loop
            data = self.store.snapshot()
            readings = []
            for pid in data.get("plugs") or {}:   # WLAN plugs: read this round – or earlier (counted once)
                r = self._wifi_plugs.get(pid) or {}
                if not r.get("protected") and r.get("heard"):
                    readings.append({"id": pid, "at": r["heard"], "wh": r.get("energy_wh"), "w": r.get("power")})
            for p in (self.zigbee.plugs() if self.zigbee else []):
                v = self.zigbee.plug(p["ieee"]) or {}
                if v.get("heard"):
                    readings.append({"id": p["ieee"], "at": v["heard"], "wh": v.get("energy_wh"), "w": v.get("power")})
            for r in readings:
                r.update(self._plug_place(r["id"], data))
            self.energy.sample(time.time() if now is None else now, readings)
            self._energy_failed = False
        except Exception:
            if not self._energy_failed:
                self._energy_failed = True
                log.exception("Strom der Steckdosen: Aufzeichnen fehlgeschlagen")

    @staticmethod
    def _costs(data: dict) -> dict:
        c = (data.get("settings") or {}).get("costs") or {}
        return {"price_ct": c.get("price_ct"), "show": bool(c.get("show"))}

    def _heater_infos(self, data: dict, ids: list[str]) -> list[dict]:
        """Name, kind and power of heaters, for heizapp.history.totals."""
        by_id = {e.id: e for e in self.entries}
        return [{"id": i, "name": data["heaters"].get(i, {}).get("name") or self._device_name(by_id[i]),
                 "kind": "zigbee" if by_id[i].zigbee else "aeroflow", "power_w": data["heaters"].get(i, {}).get("power_w")}
                for i in ids if i in by_id]

    def _today(self, rows: dict, data: dict, heaters: list[str], sensors: list[str]) -> dict:
        """Heated so far today, with energy and costs as far as power and price are known – for the tiles."""
        tot = history.totals(rows, self._heater_infos(data, heaters), history.series(rows, heaters, sensors),
                             self._costs(data)["price_ct"])
        return {"heat_s": tot["heat_s"], "kwh": tot["kwh"], "eur": tot["eur"], "power_missing": tot["power_missing"]}

    def set_costs(self, body: dict) -> dict:
        """Strom und Kosten: the electricity price, whether residents see energy and costs, the power of heaters."""
        price = body.get("price_ct")
        if price in ("", None):
            price = None
        else:
            try:
                price = round(float(str(price).strip().replace(",", ".")), 2)
            except ValueError:
                raise ActionError("Der Strompreis muss eine Zahl sein, zum Beispiel 32,5.") from None
            if not 0 < price <= self.MAX_PRICE_CT:
                raise ActionError(f"Der Strompreis muss über 0 und höchstens {self.MAX_PRICE_CT} ct/kWh sein.")
        powers = body.get("powers") or {}
        if not isinstance(powers, dict):
            raise ValueError("powers muss ein Objekt sein")
        clean = {}
        for heater_id, watts in powers.items():
            e = self._entry(heater_id)
            if e.zigbee:
                raise ActionError("Ein Thermostat hat keine eigene Leistung – die Wärme kommt vom Heizkessel.")
            if watts in ("", None):
                clean[e.id] = None
                continue
            try:
                watts = int(str(watts).strip())
            except ValueError:
                raise ActionError(f"{self._name(e)}: Die Leistung bitte als ganze Zahl in Watt, "
                                  "zum Beispiel 1300.") from None
            if not 0 < watts <= self.MAX_POWER_W:
                raise ActionError(f"{self._name(e)}: Die Leistung muss zwischen 1 und {self.MAX_POWER_W} W liegen.")
            clean[e.id] = watts
        show = bool(body.get("show"))
        plugs = self._plugs_from(body)
        changed = {i: p for i, p in plugs.items() if self.store.data["heaters"].get(i, {}).get("plug") != p}
        with self.store.edit() as d:
            d.setdefault("settings", {})["costs"] = {"price_ct": price, "show": show}
            for heater_id, watts in clean.items():
                meta = d["heaters"].setdefault(heater_id, self._new_meta(self._entry(heater_id)))
                if watts is None:
                    meta.pop("power_w", None)
                else:
                    meta["power_w"] = watts
            for heater_id, ieee in plugs.items():
                meta = d["heaters"].setdefault(heater_id, self._new_meta(self._entry(heater_id)))
                if ieee is None:
                    meta.pop("plug", None)
                else:
                    meta["plug"] = ieee
        log.info("Strom und Kosten gespeichert: Strompreis %s, auf der Startseite %s%s",
                 f"{_num(price, '.2f')} ct/kWh" if price is not None else "keiner", "ja" if show else "nein",
                 "".join(f", {self._name(self._entry(i))} {f'{w} W' if w else 'ohne Leistung'}" for i, w in clean.items()))
        for heater_id, ieee in changed.items():   # what it measured belongs to the plug it had
            self._plug_samples.pop(heater_id, None)
            log.info("%s hängt %s", self._who(self._entry(heater_id)),
                     f"jetzt an {self._plug_name(ieee)} ({ieee})" if ieee else "an keiner Steckdose mehr")
        missed = self._plugs_on_after_outage(plugs)
        if missed:
            one = len(missed) == 1
            return {"message": f"Gespeichert. {' und '.join(missed)} {'war' if one else 'waren'} gerade nicht zu "
                               "erreichen – bitte später noch einmal speichern, damit sie nach einem Stromausfall wieder "
                               f"{'angeht' if one else 'angehen'}.", "warning": True}
        return {"message": "Gespeichert."}

    # ------------------------------------------------------------------ plugs (Zigbee and WLAN)
    # An AeroFlow can hang on a plug that measures: a Zigbee plug (e.g. SONOFF S60ZBTPF, heizapp.zigbee_net) or a WLAN
    # plug (Shelly, e.g. Plug M Gen3, heizapp.shelly). The plug's tile in Geräte assigns it (set_plug_heater; Strom und
    # Kosten could until 0.4.14, PUT /api/costs still takes "plugs"). While the heater heats, the app notes
    # what the plug lets through – the typical value is the heater's measured power. If the plug is switched off (its
    # button), the heater has no power: the tile says so and switches it on again. The app never switches a plug off.
    # A plug's id is the IEEE address of a Zigbee plug („0x…“) or Shelly's device id of a WLAN plug („shellyplug…“).
    # The WLAN plugs taken over from „Steckdosen suchen“ are in data.json under "plugs" (address, model, MAC and where
    # its energy counter stood at the first reading of the day).
    WIFI_PLUG_FRESH = 60.0   # seconds a WLAN plug's reading counts as current (the app reads it every round)
    PLUG_ERRORS = {"overtemp": "Die Steckdose ist zu heiß geworden und hat abgeschaltet.",
                   "overpower": "Die Steckdose hat abgeschaltet: Es floss mehr Strom, als sie erlaubt.",
                   "overcurrent": "Die Steckdose hat abgeschaltet: Es floss mehr Strom, als sie erlaubt.",
                   "overvoltage": "Die Steckdose hat abgeschaltet: Die Spannung im Netz war zu hoch."}

    @staticmethod
    def _zigbee_plug_id(pid) -> bool:
        return str(pid).startswith("0x")

    @classmethod
    def _plug_default_name(cls, pid: str) -> str:
        return f"{'Steckdose' if cls._zigbee_plug_id(pid) else 'WLAN-Steckdose'} {pid[-4:].upper()}"

    def _plug_name(self, pid: str, data: dict | None = None) -> str:
        """What the app calls a plug: a WLAN plug by its name in the Shelly app – without one (and a Zigbee plug)
        „WLAN-Steckdose“ or „Steckdose“ with the last four characters of its id."""
        meta = ((data if data is not None else self.store.data).get("plugs") or {}).get(pid) or {}
        return _device_title(meta.get("device_name")) or self._plug_default_name(pid)

    def _note_plug_name(self, pid: str, name) -> None:
        """The name a WLAN plug has in the Shelly app, as read just now: into data.json if it changed."""
        self._plug_names_read[pid] = time.monotonic()
        new = _device_title(name)
        meta = (self.store.data.get("plugs") or {}).get(pid)
        if meta is None or _device_title(meta.get("device_name")) == new:
            return
        before = self._plug_name(pid)
        with self.store.edit() as d:
            if pid in d.get("plugs") or {}:
                d["plugs"][pid]["device_name"] = new
        log.info("%s heißt jetzt „%s“", before, self._plug_name(pid))

    @staticmethod
    def _plug_place(pid: str, data: dict) -> dict:
        """Where a plug is: the AeroFlow it gives power and that heater's room – without a heater its own room."""
        heater = next((hid for hid, meta in data["heaters"].items() if meta.get("plug") == pid), None)
        room = data["heaters"][heater].get("room") if heater else (data.get("plug_rooms") or {}).get(pid)
        return {"heizung": heater, "raum": room if room in data["rooms"] else None}

    def _assigned_plug(self, e: HeaterState) -> tuple[str | None, dict | None]:
        """The plug an AeroFlow hangs on (its id) and what it reported last: None if it is not in the Zigbee network, or
        for a WLAN plug that has not answered for a minute."""
        pid = None if e.zigbee else self.store.data["heaters"].get(e.id, {}).get("plug")
        if not pid:
            return None, None
        if self._zigbee_plug_id(pid):
            return pid, (self.zigbee.plug(pid) if self.zigbee else None)
        return pid, self._wifi_plug(pid)

    def _measure_plugs(self, wifi_fresh: set | frozenset = frozenset()) -> None:
        """While an AeroFlow heats, what its plug lets through – only fresh values: a Zigbee plug's while Zigbee2MQTT
        runs, a WLAN plug's from this round (wifi_fresh: the WLAN plugs that answered)."""
        zigbee_ok = self.zigbee is not None and self.zigbee.problem() is None
        try:   # the measurement must never hold up the rest of the loop
            for e in self._aeroflows():
                pid, plug = self._assigned_plug(e)
                if plug is None or not (zigbee_ok if self._zigbee_plug_id(pid) else pid in wifi_fresh):
                    continue
                if (e.online and e.values.get("heater_state") == 1 and plug["state"] == "ON"
                        and plug["power"] is not None and plug["power"] > PLUG_HEATING_W):
                    self._plug_samples.setdefault(e.id, deque(maxlen=PLUG_SAMPLES)).append(plug["power"])
            self._plugs_failed = False
        except Exception:
            if not self._plugs_failed:
                self._plugs_failed = True
                log.exception("Steckdosen: Messen fehlgeschlagen")

    def _plug_view(self, e: HeaterState) -> dict | None:
        """The plug of an AeroFlow for the page: what it reports and what it measured while the heater heated."""
        pid, plug = self._assigned_plug(e)
        if pid is None:
            return None
        samples = list(self._plug_samples.get(e.id) or ())
        zigbee = self._zigbee_plug_id(pid)
        return {"id": pid, "kind": "zigbee" if zigbee else "wifi", "name": self._plug_name(pid),
                "found": plug is not None, "error": None if zigbee else self._wifi_plug_error(pid),
                "state": plug["state"] if plug else None, "power": plug["power"] if plug else None,
                "energy_today": plug["energy_today"] if plug else None,
                "after_outage": plug["power_on_behavior"] if plug else None,
                "heard": self._iso(plug["heard"]) if plug else None,
                "measured_w": round(statistics.median(samples)) if len(samples) >= PLUG_MIN_SAMPLES else None,
                "samples": len(samples)}

    def _plug_words(self, e: HeaterState) -> str:
        """The plug in the line „Stand: …“ of the protocol."""
        _, plug = self._assigned_plug(e)
        if plug is None or plug["state"] is None:
            return ""
        if plug["state"] == "OFF":
            return ", Steckdose aus"
        return f", Steckdose {plug['power']:.0f} W" if plug["power"] is not None else ", Steckdose an"

    def _plug_list(self, data: dict) -> list[dict]:
        """Every plug the app knows, for Strom und Kosten and Geräte › Steckdosen: the Zigbee ones in the network, the
        WLAN ones taken over from the search – with what they report and which heater each one belongs to."""
        owners = {meta["plug"]: hid for hid, meta in data["heaters"].items() if meta.get("plug")}
        known = [(p["ieee"], "zigbee", p["model"], None, None) for p in (self.zigbee.plugs() if self.zigbee else [])]
        known += [(pid, "wifi", meta.get("model"), meta.get("host"), meta.get("device_name"))
                  for pid, meta in (data.get("plugs") or {}).items()]
        out = []
        for pid, kind, model, address, device_name in known:
            v = (self.zigbee.plug(pid) if kind == "zigbee" else self._wifi_plug(pid)) or {}
            out.append({"id": pid, "kind": kind, "name": self._plug_name(pid, data), "model": model, "heater": owners.get(pid),
                        "found": kind == "zigbee" or bool(v), "state": v.get("state"), "power": v.get("power"),
                        "energy_today": v.get("energy_today"), "address": address, "device_name": device_name,
                        "room": self._plug_place(pid, data)["raum"],
                        "error": None if kind == "zigbee" else self._wifi_plug_error(pid)})
        return out

    def plug_on(self, heater_id: str) -> dict:
        """„Einschalten“ on the tile: the plug gives the heater power again. The app never switches a plug off."""
        e = self._entry(heater_id)
        pid, plug = self._assigned_plug(e)
        if pid is None:
            raise ActionError("Diese Heizung hängt an keiner Steckdose der App.", 409)
        if self._zigbee_plug_id(pid):
            if plug is None:
                raise ActionError("Die Steckdose ist nicht (mehr) im Zigbee-Netz.", 409)
            after = self.zigbee.send(pid, {"state": "ON"})
            if after is None:
                raise ActionError(self.zigbee.problem() or "Die Nachricht an die Steckdose ging nicht hinaus.", 503)
            if self.zigbee.wait(pid, after, lambda v: v.get("state") == "ON", PLUG_ANSWER) is None:
                raise ActionError("Die Steckdose hat nicht geantwortet. Bitte noch einmal versuchen – oder an der "
                                  "Steckdose die Taste einmal drücken.", 504)
        else:
            self._wifi_switch_on(pid)
        log.info("%s eingeschaltet – %s hat wieder Strom", self._plug_name(pid), self._who(e))
        return {"message": "Die Steckdose ist wieder an. Bis sich die Heizung meldet, kann es ein, zwei Minuten "
                           "dauern."}

    def _plugs_from(self, body: dict) -> dict[str, str | None]:
        """The plugs of PUT /api/costs ({heater: plug id or ""}), checked: only AeroFlow heaters, only plugs the app
        knows (or the one a heater has already), each plug at one heater at most."""
        wanted = body.get("plugs") or {}
        if not isinstance(wanted, dict):
            raise ValueError("plugs muss ein Objekt sein")
        heaters = self.store.data["heaters"]
        known = {p["ieee"] for p in self.zigbee.plugs()} if self.zigbee else set()
        known |= set(self.store.data.get("plugs") or {})
        clean: dict[str, str | None] = {}
        for heater_id, pid in wanted.items():
            e = self._entry(heater_id)
            if e.zigbee:
                raise ActionError("Ein Thermostat hängt an keiner Steckdose – es hat Batterien.")
            pid = pid or None
            if pid is not None and not (isinstance(pid, str) and (pid in known
                                                                   or pid == heaters.get(e.id, {}).get("plug"))):
                raise ActionError(f"{self._name(e)}: " + ("Diese Steckdose ist nicht (mehr) im Zigbee-Netz."
                                                         if self._zigbee_plug_id(pid)
                                                         else "Diese WLAN-Steckdose ist nicht (mehr) in der App."))
            clean[e.id] = pid
        owners = {hid: meta["plug"] for hid, meta in heaters.items() if meta.get("plug")}
        seen: set[str] = set()
        for pid in {**owners, **clean}.values():
            if pid and pid in seen:
                raise ActionError(f"{self._plug_name(pid)} kann nur zu einer Heizung gehören.")
            seen.add(pid)
        return clean

    def set_plug_heater(self, plug_id: str, body: dict) -> dict:
        """Geräte › Steckdosen: the AeroFlow this plug gives power („gehört zu“; heater "" or None: none). A plug belongs
        to one heater and a heater hangs on one plug – a plug the heater had before no longer belongs to it."""
        heater_id = body.get("heater") or None
        if heater_id is not None and not isinstance(heater_id, str):
            raise ValueError("heater muss eine Kennung sein")
        data = self.store.snapshot()
        known = {p["id"] for p in self._plug_list(data)}
        owners = [hid for hid, meta in data["heaters"].items() if meta.get("plug") == plug_id]
        if plug_id not in known and not owners:
            raise ActionError("Diese Steckdose kennt die App nicht (mehr).", 404)
        e = self._entry(heater_id) if heater_id else None
        if e is not None and e.zigbee:
            raise ActionError("Ein Thermostat hängt an keiner Steckdose – es hat Batterien.")
        if e is not None and plug_id not in known:
            raise ActionError("Diese Steckdose ist nicht (mehr) im Zigbee-Netz." if self._zigbee_plug_id(plug_id)
                              else "Diese WLAN-Steckdose ist nicht (mehr) in der App.", 409)
        before = data["heaters"].get(e.id, {}).get("plug") if e else None
        with self.store.edit() as d:
            for hid, meta in d["heaters"].items():
                if meta.get("plug") == plug_id and (e is None or hid != e.id):
                    meta.pop("plug", None)
            if e is not None:
                d["heaters"].setdefault(e.id, self._new_meta(e))["plug"] = plug_id
                (d.get("plug_rooms") or {}).pop(plug_id, None)   # it is in the heater's room now
        who = {x.id: self._who(x) for x in self.entries}
        for hid in owners:   # what a heater measured belongs to the plug it had
            if e is None or hid != e.id:
                self._plug_samples.pop(hid, None)
                log.info("%s hängt an keiner Steckdose mehr", who.get(hid, hid))
        if e is not None and before != plug_id:
            self._plug_samples.pop(e.id, None)
            log.info("%s hängt jetzt an %s (%s)", who.get(e.id, e.id), self._plug_name(plug_id), plug_id)
        name = self._plug_name(plug_id)
        if e is None:
            return {"message": f"{name} gehört zu keiner Heizung mehr."}
        if self._plugs_on_after_outage({e.id: plug_id}):
            return {"message": f"Gespeichert. {name} war gerade nicht zu erreichen – bitte später noch einmal "
                               "auswählen, damit sie nach einem Stromausfall wieder angeht.", "warning": True}
        return {"message": f"{name} gehört jetzt zu {self._name(e)}."}

    def set_plug_room(self, plug_id: str, body: dict) -> dict:
        """Geräte › Steckdosen: the room of a plug that gives no AeroFlow power – any other device (room "" or None:
        none). A plug at a heater is in the heater's room."""
        room_id = body.get("room") or None
        if room_id is not None and not isinstance(room_id, str):
            raise ValueError("room muss eine Kennung sein")
        data = self.store.snapshot()
        if plug_id not in {p["id"] for p in self._plug_list(data)}:
            raise ActionError("Diese Steckdose kennt die App nicht (mehr).", 404)
        if room_id is not None and room_id not in data["rooms"]:
            raise ActionError("Raum nicht gefunden", 404)
        name = self._plug_name(plug_id, data)
        if self._plug_place(plug_id, data)["heizung"]:
            raise ActionError(f"{name} gehört zu einer Heizung und ist in deren Raum. Für einen eigenen Raum zuerst "
                              "bei „Heizung“ „– keine –“ wählen.", 409)
        with self.store.edit() as d:
            if room_id is None:
                (d.get("plug_rooms") or {}).pop(plug_id, None)
            else:
                d.setdefault("plug_rooms", {})[plug_id] = room_id
        if room_id is None:
            log.info("%s ist in keinem Raum mehr", name)
            return {"message": f"{name} ist in keinem Raum mehr."}
        room = data["rooms"][room_id]["name"]
        log.info("%s ist jetzt im Raum %s", name, room)
        return {"message": f"{name} ist jetzt im Raum „{room}“."}

    def _plugs_on_after_outage(self, plugs: dict[str, str | None]) -> list[str]:
        """Each plug a heater hangs on comes on again after a power cut (Zigbee: power_on_behavior „on“, Shelly:
        initial_state „on“) – the heater must not stay cold. Returns the plugs that could not be told."""
        missed = []
        for pid in filter(None, plugs.values()):
            if self._zigbee_plug_id(pid):
                plug = self.zigbee.plug(pid) if self.zigbee else None
                if plug and plug["power_on_behavior"] != "on" \
                        and self.zigbee.send(pid, {"power_on_behavior": "on"}) is None:
                    missed.append(self._plug_name(pid))
            elif not self._wifi_on_after_outage(pid):
                missed.append(self._plug_name(pid))
        return missed

    # WLAN plugs (Shelly): „Steckdosen suchen“ finds them, „Übernehmen“ keeps them; the app reads them every round
    def _wifi_plug(self, pid: str) -> dict | None:
        """A WLAN plug's last reading in the shape of a Zigbee plug's (heizapp.zigbee_net.ZigbeeNet.plug) – None if it
        has not answered for a minute (or only wants its password)."""
        r = self._wifi_plugs.get(pid)
        if not r or r.get("on") is None or time.monotonic() - r["mono"] > self.WIFI_PLUG_FRESH:
            return None
        return {"state": "ON" if r["on"] else "OFF", "power": r.get("power"), "energy_today": r.get("energy_today"),
                "power_on_behavior": r.get("after_outage"), "heard": r.get("heard")}

    def _wifi_plug_error(self, pid: str) -> str | None:
        """What is wrong with a WLAN plug, in words for the page – None if nothing (or not read yet since the start)."""
        r = self._wifi_plugs.get(pid)
        if r is None:
            return None
        if r.get("protected"):
            return "Die Steckdose ist mit einem Passwort geschützt – die App kann sie nicht lesen."
        if self._wifi_plug(pid) is None:
            return "Die Steckdose antwortet nicht – ist sie eingesteckt und im WLAN?"
        errors = r.get("errors") or []
        if errors:
            return next((self.PLUG_ERRORS[x] for x in errors if x in self.PLUG_ERRORS),
                        f"Die Steckdose meldet einen Fehler ({', '.join(errors)}).")
        return None

    @staticmethod
    def _split_address(address) -> tuple[str, int]:
        host, _, port = str(address or "").partition(":")
        return host, int(port) if port.isdigit() else shelly.PORT

    def _read_wifi_plug(self, pid: str, meta: dict) -> dict | None:
        """The WLAN plug pid at its last address, or None if it does not answer there (or another device does)."""
        host, port = self._split_address(meta.get("host"))
        if not host:
            return None
        r = shelly.plug_reading(host, port, timeout=1.5)
        if r.get("protected"):   # its status wants the password – Shelly's identification never does
            info = shelly.probe(host, port, timeout=1.5, kinds=("plug",))
            return {"protected": True, "address": meta["host"]} if info and str(info.get("id")) == pid else None
        if r.get("offline") or r.get("unsuitable") or not r.get("mac") or r["mac"] != str(meta.get("mac") or "").upper():
            return None
        return {**r, "address": meta["host"]}

    def _read_wifi_plugs(self) -> set:
        """Read every WLAN plug of the app – they never sleep, so every round (about every 10 s). One gone from its
        address is searched for in the home network at most every 15 minutes (the router may have given it a new
        one). Returns the plugs that answered with their values."""
        saved = self.store.snapshot().get("plugs") or {}
        if not saved:
            return set()
        try:   # reading must never hold up the rest of the loop
            readings = dict(zip(saved, self._pool.map(lambda pid: self._read_wifi_plug(pid, saved[pid]), saved)))
            missing = {pid for pid, r in readings.items() if r is None}
            now = time.monotonic()
            if missing and now - self._plugs_searched >= self.SENSOR_SEARCH_EVERY:
                self._plugs_searched = now
                readings.update({p["id"]: p for p in self._locate_plugs(missing, [m.get("host") for m in saved.values()])})
            for pid, r in readings.items():
                if r is None:
                    self._wifi_plug_lost(pid)
                else:
                    self._note_wifi_plug(pid, r)
            self._read_plug_names({pid: r["address"] for pid, r in readings.items() if r is not None})
            self._wifi_failed = False
            return {pid for pid, r in readings.items() if r is not None and not r.get("protected")}
        except Exception:
            if not self._wifi_failed:
                self._wifi_failed = True
                log.exception("WLAN-Steckdosen: Lesen fehlgeschlagen")
            return set()

    PLUG_NAME_EVERY = 10 * 60   # seconds between two readings of a WLAN plug's name (renamed in the Shelly app?)

    def _read_plug_names(self, answered: dict[str, str]) -> None:
        """The name of every WLAN plug that answered, every ten minutes: one renamed in the Shelly app is renamed in
        the app as well. Shelly's identification (/shelly) has it, also with a password."""
        now = time.monotonic()
        for pid, address in answered.items():
            if now - self._plug_names_read.get(pid, -self.PLUG_NAME_EVERY) < self.PLUG_NAME_EVERY:
                continue
            self._plug_names_read[pid] = now   # also when it does not answer: not again every round
            info = shelly.probe(*self._split_address(address), timeout=1.5, kinds=("plug",))
            if info and str(info.get("id")) == pid:
                self._note_plug_name(pid, info.get("name"))

    def _locate_plugs(self, ids: set, addresses: list) -> list[dict]:
        """WLAN plugs that did not answer at their last address: one sweep of the home network for them."""
        nets = discovery.local_networks([e.cfg.host for e in self._aeroflows()]
                                        + [self._split_address(a)[0] for a in addresses if a])
        return [p for p in self._sweep(nets, ("plug",)) if p["id"] in ids and not p["offline"]] if nets else []

    def _note_wifi_plug(self, pid: str, r: dict) -> None:
        """A WLAN plug answered: remember what it reports – and a new address in data.json."""
        old = self._wifi_plugs.get(pid) or {}
        name = self._plug_name(pid)
        if r.get("protected"):
            if not old.get("protected"):
                log.warning("%s (%s) ist mit einem Passwort geschützt – die App kann sie nicht lesen", name, r["address"])
            self._wifi_plugs[pid] = {"protected": True, "on": None, "mono": time.monotonic(), "address": r["address"],
                                     "after_outage": old.get("after_outage")}
        else:
            if not old:   # the first answer since the start
                log.info("%s (%s) erreichbar – %s%s", name, r["address"], "an" if r.get("on") else "aus",
                         f", {r['power']:.0f} W" if r.get("power") is not None else "")
            elif old.get("lost") or old.get("protected"):
                log.info("%s (%s) antwortet wieder", name, r["address"])
            new = [x for x in r.get("errors") or [] if x not in (old.get("errors") or [])]
            if new:
                log.warning("%s meldet: %s", name, ", ".join(new))
            self._wifi_plugs[pid] = {"on": r.get("on"), "power": r.get("power"), "energy_wh": r.get("energy_wh"),
                                     "energy_today": self._energy_today(pid, r.get("energy_wh")),
                                     "errors": r.get("errors") or [], "rssi": r.get("rssi"),
                                     "after_outage": old.get("after_outage"), "address": r["address"],
                                     "mono": time.monotonic(), "heard": time.time()}
        meta = (self.store.data.get("plugs") or {}).get(pid)
        if meta is not None and meta.get("host") != r["address"]:
            with self.store.edit() as d:
                if pid in d.get("plugs") or {}:
                    d["plugs"][pid]["host"] = r["address"]
            log.info("%s hat eine neue Adresse: %s", name, r["address"])

    def _wifi_plug_lost(self, pid: str) -> None:
        old = self._wifi_plugs.get(pid)
        if old is not None and old.get("lost"):
            return
        log.warning("%s antwortet nicht – ist sie eingesteckt und im WLAN?", self._plug_name(pid))
        self._wifi_plugs[pid] = {**(old or {"on": None, "mono": 0.0}), "lost": True, "protected": False}

    def _energy_today(self, pid: str, total_wh) -> float | None:
        """kWh through a WLAN plug today: its counter now minus where it stood at the first reading of the day (kept in
        data.json, so a restart does not lose it). A Shelly counts only up – a smaller value means it was reset."""
        meta = (self.store.data.get("plugs") or {}).get(pid)
        if total_wh is None or meta is None:
            return None
        today, day = self.now().date().isoformat(), meta.get("day") or {}
        start = day.get("wh")
        if day.get("date") != today or not isinstance(start, (int, float)) or total_wh < start:
            with self.store.edit() as d:
                if pid in d.get("plugs") or {}:
                    d["plugs"][pid]["day"] = {"date": today, "wh": total_wh}
            return 0.0
        return round((total_wh - start) / 1000, 3)

    def find_plugs(self, network=None) -> dict:
        """„Steckdosen suchen“: one sweep of the home network for Shelly plugs with power metering (read only)."""
        nets = self._search_networks(network)
        started = time.monotonic()
        plugs = sorted(self._sweep(nets, ("plug",)),
                       key=lambda p: tuple(int(x) for x in p["address"].split(":")[0].split(".")))
        saved = self.store.snapshot().get("plugs") or {}
        for p in plugs:
            self._found_plugs[p["id"]] = {"mono": time.monotonic(), "plug": p}
            if p["id"] in saved:
                self._note_plug_name(p["id"], p["name"])
            if p["id"] in saved and not p["offline"]:
                self._note_wifi_plug(p["id"], p)   # a new address counts at once
        log.info("Steckdosensuche in %s: %s", ", ".join(map(str, nets)),
                 ", ".join(f"{self._plug_name(p['id'])} {p['address']}" for p in plugs) or "keine Steckdose gefunden")
        return {"networks": [str(n) for n in nets], "seconds": round(time.monotonic() - started, 1),
                "plugs": [{"id": p["id"], "name": _device_title(p["name"]) or self._plug_default_name(p["id"]),
                           "device_name": _device_title(p["name"]), "model": p["model"],
                           "address": p["address"], "on": p["on"], "power": p["power"], "protected": p["protected"],
                           "offline": p["offline"], "known": p["id"] in saved} for p in plugs]}

    def add_wifi_plug(self, body: dict) -> dict:
        """„Übernehmen“: a plug from the search becomes a plug of the app (its heater: chosen at the plug)."""
        pid = str(body.get("plug") or "")
        hit = self._found_plugs.get(pid)
        if hit is None or time.monotonic() - hit["mono"] > self.FOUND_KEEP:
            raise ActionError("Diese Steckdose hat die Suche nicht gefunden – bitte erneut suchen.", 404)
        p = hit["plug"]
        if p["protected"]:
            raise ActionError("Die Steckdose ist mit einem Passwort geschützt – die App kann sie nicht lesen und nicht "
                              "schalten. Das Passwort in der Shelly-App ausschalten, dann noch einmal suchen.", 409)
        with self.store.edit() as d:
            new = pid not in d.setdefault("plugs", {})
            d["plugs"].setdefault(pid, {}).update(host=p["address"], model=p["model"], mac=p["mac"],
                                                  device_name=_device_title(p["name"]))
        self._plug_names_read[pid] = time.monotonic()
        if new:
            log.info("%s (%s, %s) ist jetzt in der App", self._plug_name(pid), p["model"], p["address"])
        if not p["offline"]:
            self._note_wifi_plug(pid, p)
            try:
                self._wifi_plugs[pid]["after_outage"] = shelly.after_outage(*self._split_address(p["address"]))
            except shelly.ShellyError:
                pass
        return {"plug": pid, "message": f"{self._plug_name(pid)} ist in der App."}

    def delete_wifi_plug(self, pid: str) -> dict:
        """„Entfernen“: the app forgets a WLAN plug – its heater no longer hangs on it. The plug itself stays as it is."""
        data = self.store.snapshot()
        if pid not in (data.get("plugs") or {}):
            raise ActionError("Diese Steckdose kennt die App nicht.", 404)
        owners = [hid for hid, meta in data["heaters"].items() if meta.get("plug") == pid]
        name = self._plug_name(pid, data)
        with self.store.edit() as d:
            d["plugs"].pop(pid, None)
            (d.get("plug_rooms") or {}).pop(pid, None)
            for hid in owners:
                d["heaters"].get(hid, {}).pop("plug", None)
        for hid in owners:
            self._plug_samples.pop(hid, None)
        self._wifi_plugs.pop(pid, None)
        self._plug_names_read.pop(pid, None)
        log.info("%s aus der App entfernt%s", name,
                 "".join(f" – {self._who(e)} hängt an keiner Steckdose mehr" for e in self.entries if e.id in owners))
        return {"message": "Die Steckdose ist aus der App entfernt. Sie selbst bleibt, wie sie ist – an oder aus."}

    def _wifi_address(self, pid: str) -> tuple[str, int]:
        """Where the WLAN plug is now – checked before anything is written to it (another device may have its address)."""
        meta = (self.store.snapshot().get("plugs") or {}).get(pid)
        if meta is None:
            raise ActionError("Diese WLAN-Steckdose kennt die App nicht (mehr).", 409)
        host, port = self._split_address((self._wifi_plugs.get(pid) or {}).get("address") or meta.get("host"))
        info = shelly.probe(host, port, timeout=2.0, kinds=("plug",)) if host else None
        if not info or str(info.get("id")) != pid:
            raise ActionError("Die Steckdose antwortet nicht unter ihrer letzten Adresse. Ist sie eingesteckt und im "
                              "WLAN?", 504)
        return host, port

    def _wifi_switch_on(self, pid: str) -> None:
        host, port = self._wifi_address(pid)
        try:
            shelly.switch_on(host, port)
        except shelly.ShellyError as exc:
            raise ActionError(str(exc), exc.status) from None
        r = shelly.plug_reading(host, port, timeout=2.0)
        if r.get("on") is not True:
            raise ActionError("Die Steckdose hat nicht eingeschaltet. Bitte noch einmal versuchen – oder an der "
                              "Steckdose die Taste einmal drücken.", 504)
        self._note_wifi_plug(pid, {**r, "address": host if port == shelly.PORT else f"{host}:{port}"})

    def _wifi_on_after_outage(self, pid: str) -> bool:
        """A WLAN plug comes on again after a power cut (Shelly: initial_state „on“). False if it could not be told."""
        try:
            host, port = self._wifi_address(pid)
            if shelly.after_outage(host, port) != "on":
                shelly.set_on_after_outage(host, port)
                log.info("%s geht nach einem Stromausfall jetzt wieder an", self._plug_name(pid))
        except (ActionError, shelly.ShellyError):
            return False
        if pid in self._wifi_plugs:
            self._wifi_plugs[pid]["after_outage"] = "on"
        return True

    def history_view(self, kind: str, ident: str, day_text: str) -> dict:
        """Verlauf of a room (or of a heater without one) on one day: the points of the chart and what it cost."""
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            raise ActionError("Unbekanntes Datum") from None
        data = self.store.snapshot()
        if kind == "room":
            room = data["rooms"].get(ident)
            if room is None:
                raise ActionError("Raum nicht gefunden", 404)
            name = room["name"]
            heaters = [e.id for e in self.entries if data["heaters"].get(e.id, {}).get("room") == ident]
            sensors = [sid for sid, meta in data["sensors"].items() if meta.get("room") == ident]
        else:
            e = self._entry(ident)
            name, heaters, sensors = data["heaters"].get(e.id, {}).get("name") or self._device_name(e), [e.id], []
        today = self.now().date()
        rows = self.history.day_rows(day, set(heaters + sensors)) if day <= today else {}
        points = history.series(rows, heaters, sensors)
        costs = self._costs(data)
        return {"kind": kind, "id": ident, "name": name, "day": day.isoformat(), "label": self.history.label(day, today),
                "tz": getattr(self.tz, "key", None),   # the page shows the times in it (None: the browser's own)
                "start": self.history.moment(day), "end": self.history.moment(day, 24),
                "now": time.time() if day == today else None, "slot": history.SLOT,
                "ticks": [{"t": self.history.moment(day, h), "label": f"{h} Uhr" if h in (0, 24) else str(h)}
                          for h in (0, 6, 12, 18, 24)],
                "points": points, "has_sensor": bool(sensors),
                **history.totals(rows, self._heater_infos(data, heaters), points, costs["price_ct"]),
                "price_ct": costs["price_ct"], "show_costs": costs["show"]}

    def _heater_title(self, heater_id: str | None, data: dict) -> str | None:
        e = next((x for x in self.entries if x.id == heater_id), None)
        return (data["heaters"].get(heater_id, {}).get("name") or self._device_name(e)) if e else None

    def energy_view(self, plug_id: str, day_text: str) -> dict:
        """„Strom“ of one plug on one day: what flowed through it in every quarter hour (heizapp.energy)."""
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            raise ActionError("Unbekanntes Datum") from None
        data = self.store.snapshot()
        plug = next((p for p in self._plug_list(data) if p["id"] == plug_id), None)
        if plug is None:
            raise ActionError("Diese Steckdose kennt die App nicht (mehr).", 404)
        today = self.now().date()
        rows = self.energy.rows_between(day, day, {plug_id}).get(plug_id, {}) if day <= today else {}
        points = [{"t": t, "wh": r["wh"], "w_max": r["w_max"]} for t, r in sorted(rows.items())]
        wh, price = sum(p["wh"] for p in points), self._costs(data)["price_ct"]
        place, first = self._plug_place(plug_id, data), self.energy.first_day()
        return {"kind": "plug", "id": plug_id, "name": plug["name"], "day": day.isoformat(),
                "label": self.history.label(day, today), "tz": getattr(self.tz, "key", None),
                "start": self.history.moment(day), "end": self.history.moment(day, 24),
                "now": time.time() if day == today else None, "slot": energy.SLOT,
                "ticks": [{"t": self.history.moment(day, h), "label": f"{h} Uhr" if h in (0, 24) else str(h)}
                          for h in (0, 6, 12, 18, 24)],
                "points": points, "kwh": round(wh / 1000, 3) if points else None,
                "eur": round(wh / 1000 * price / 100, 2) if points and price else None, "price_ct": price,
                "w_max": max((p["w_max"] for p in points if p["w_max"] is not None), default=None),
                "room": data["rooms"][place["raum"]]["name"] if place["raum"] else None,
                "heater": self._heater_title(place["heizung"], data),
                "first_day": first.isoformat() if first else None, "recording": self.energy.active}

    def energy_table(self, first_text: str, last_text: str) -> tuple[str, bytes, str]:
        """„Als Tabelle“: the quarter hours of every plug from one day to another, for Excel – (file name, bytes,
        type)."""
        try:
            first, last = sorted((date.fromisoformat(first_text), date.fromisoformat(last_text)))
        except ValueError:
            raise ActionError("Unbekanntes Datum") from None
        if (last - first).days >= energy.MAX_DAYS:
            raise ActionError(f"Höchstens {energy.MAX_DAYS} Tage auf einmal.")
        data = self.store.snapshot()
        rows = self.energy.rows_between(first, last)
        names = {pid: self._plug_name(pid, data) for pid in rows}   # also of a plug removed since
        rooms = {rid: room["name"] for rid, room in data["rooms"].items()}
        heaters = {e.id: self._heater_title(e.id, data) for e in self.entries}
        span = first.isoformat() if first == last else f"{first.isoformat()}-bis-{last.isoformat()}"
        return (f"strom-steckdosen-{span}.csv", energy.table(rows, self.energy.local, names, rooms, heaters),
                "text/csv; charset=utf-8")

    # ------------------------------------------------------------------ snapshot for the UI
    def state(self) -> dict:
        self._sync_thermostats()
        data = self.store.snapshot()
        heaters = [self._heater_view(e, data) for e in self.entries]
        today = self.history.day_rows(self.now().date(), files=False)   # Verlauf: heated so far, for the tiles
        for h in heaters:   # None: not known (yet) – rather than „Heute noch nicht geheizt“
            h["today"] = None if today is None else self._today(today, data, [h["id"]], [])
        rooms = []
        for room_id, room in data["rooms"].items():
            plan = data["plans"].get(room.get("plan") or "")
            sent = room.get("sent") or {}
            same_plan = bool(plan) and sent.get("plan") == room.get("plan")
            members = [h["id"] for h in heaters if h["room"] == room_id]
            if "heaters" in sent:   # every current member must have received it
                received = not set(members) - set(sent["heaters"])
            else:                   # older records only know whether all heaters succeeded
                received = bool(sent.get("complete"))
            rooms.append({
                "id": room_id,
                "name": room["name"],
                "plan": room.get("plan") if plan else None,
                "plan_name": plan["name"] if plan else None,
                "plan_sent_at": sent.get("at") if same_plan else None,
                "plan_outdated": bool(plan) and not (same_plan and received
                                                     and sent.get("fingerprint") == plans.fingerprint(plan)),
                "heaters": members,
                "sensors": [sid for sid, meta in data["sensors"].items() if meta.get("room") == room_id],
                "today": None if today is None else self._today(
                    today, data, members, [sid for sid, meta in data["sensors"].items() if meta.get("room") == room_id]),
            })
        plan_list = [{"id": pid, **p, "rooms": [r["id"] for r in rooms if r["plan"] == pid]}
                     for pid, p in data["plans"].items()]
        return {"now": self.now().isoformat(timespec="seconds"), "heaters": heaters,
                "rooms": rooms, "plans": plan_list, "sensors": self._sensor_views(data, heaters),
                "costs": self._costs(data), "plugs": self._plug_list(data),
                "history": {"recording": self.history.active,
                            "first_day": (first := self.history.first_day()) and first.isoformat()},
                "calib_rules": {"settle_minutes": self.CALIB_SETTLE // 60, "min_minutes": self.CALIB_MIN_MINUTES,
                                "hours": self.CALIB_DEFAULT_HOURS, "max_hours": self.CALIB_MAX_HOURS,
                                "spread_warn": self.CALIB_SPREAD_WARN}}

    def _heater_view(self, e: HeaterState, data: dict) -> dict:
        v, meta = e.values, data["heaters"].get(e.id, {})

        def deg(name: str):
            x = v.get(name)
            return x / 10 if isinstance(x, (int, float)) else None
        room = meta.get("room")
        view = {
            "id": e.id,
            "kind": "zigbee" if e.zigbee else "aeroflow",
            "name": meta.get("name") or self._device_name(e),
            "custom_name": meta.get("name") or "",
            "host": e.label,
            "hostname": self._device_name(e),
            "room": room if room in data["rooms"] else None,
            "power_w": None if e.zigbee else meta.get("power_w"),
            "plug": self._plug_view(e),   # the plug (Zigbee or WLAN) an AeroFlow hangs on (None: none)
            "online": e.online,
            "writable": e.device.writable,
            "error": e.error,
            "last_seen": self._iso(e.last_seen),
            "offline_since": self._iso(e.offline_since),
            "room_temp": deg("room_temp"),
            "setpoint": deg("setpoint"),
            "antifrost": deg("antifrost_sp"),
            "heating": v.get("heater_state") == 1,
            "mode": VIEW_MODES.get(v.get("operating_mode")),
            "window_open": v.get("open_window") == 1,
            "boost_remaining": v.get("boost_remaining") if v.get("operating_mode") == 2 else None,
            "plan_slot": v["heating_plan"] + 1 if isinstance(v.get("heating_plan"), int) else None,
            "serial": v.get("idu"),
            "mac": v.get("mac_addr"),
            "firmware": v.get("active_fw_ver"),
            "device_time": v.get("system_time"),
            "clock_drift": round(e.clock_drift) if e.clock_drift is not None else None,
            "last_clock_sync": self._iso(e.last_clock_sync),
            "temp_corr": v["temp_corr"] / 10 if isinstance(v.get("temp_corr"), int) else None,
            "room_temp_avg": round(avg[0] / 10, 2) if (avg := e.room_temp_average()) else None,
            "room_temp_avg_minutes": avg[2] if avg else None,
        }
        if e.zigbee:   # battery, radio, faults; the last sign of life is the thermostat's own, not the last look
            info = e.device.info()
            view.update(host="Zigbee", hostname=info.pop("model"), mac=None,
                        last_seen=self._iso(info.pop("heard")) or view["last_seen"], restore=self._restore_view(e.id),
                        **info)
        return view
