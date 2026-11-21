"""Background polling, clock keeping and every action the web UI can trigger."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable

from . import coap, discovery, plans, shelly
from .device import FAST_FIELDS, IDENTITY_FIELDS, MODES, SLOW_FIELDS, TIME_FORMAT, DeviceError, Heater
from .store import DataStore, HeaterConfig, load_config, new_id

log = logging.getLogger("heizapp")

DEVICE_PLAN_SLOT = 1   # the app always writes its plan into plan 1 of a heater
AVG_WINDOW = 1800      # seconds of heater readings averaged for calibration
MAX_CORRECTION = 99    # temp_corr limit on the device: ±9.9 K
SLOW_EVERY = 6         # slow fields (incl. clock) every 6th poll, ~1 min at 10 s
DRIFT_LIMIT = 60       # seconds of clock deviation before the app corrects it
DAILY_SYNC_HOUR = 3    # daily clock sync; also covers summer/winter time changes
DEFAULT_ON_TEMP = 200  # 0.1 °C: "Einschalten" without a remembered temperature and without a plan
LOST_AFTER = 180       # seconds offline before the app looks for the heater at another address
HEAL_EVERY = 900       # at most one automatic network search per 15 minutes
MODE_LABELS = {"manual": "Manuell", "auto": "Automatik (Heizplan)", "boost": "Boost",
               "boost_off": "Boost beendet", "off": "Aus (Frostschutz)", "on": "Eingeschaltet"}


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

    def __init__(self, cfg: HeaterConfig):
        self.cfg = cfg
        self.device = Heater(cfg.host, cfg.token, cfg.port)
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


class Service:
    def __init__(self, config_path: str, data_path: str, poll_interval: float = 10.0):
        self.config_path = config_path
        self.entries = [HeaterState(c) for c in load_config(config_path)]
        self.store = DataStore(data_path)
        self.poll_interval = poll_interval
        self.tz = _local_tz()
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
        self._last_daily_sync = None
        self._clock_trusted = False
        self._clock_warned = False
        self._drop_old_sensors()
        known = {m.get("last_host"): k for k, m in self.store.data["heaters"].items() if not k.startswith("host:")}
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
        return self._clock_trusted

    def _iso(self, ts: float | None) -> str | None:
        return datetime.fromtimestamp(ts, self.tz).isoformat(timespec="seconds") if ts else None

    # ------------------------------------------------------------------ polling
    def start(self) -> None:
        threading.Thread(target=self._run, name="heizung-poller", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        self.poll_all(full=True)
        self.sync_clocks(only_drifted=True)
        while not self._stop.wait(self.poll_interval):
            try:
                self.poll_all()
                self._heal()
                self.poll_sensors()
                now = self.now()
                if now.hour == DAILY_SYNC_HOUR and self._last_daily_sync != now.date():
                    self._last_daily_sync = now.date()
                    self.sync_clocks()
                else:
                    self.sync_clocks(only_drifted=True)
            except Exception:  # the poller must never die
                log.exception("Fehler im Abfrage-Thread")

    def poll_all(self, full: bool = False) -> None:
        list(self._pool.map(lambda e: self.refresh(e, full), self.entries))

    def refresh(self, e: HeaterState, full: bool = False) -> None:
        with e.refresh_lock:
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
            except (DeviceError, ValueError, KeyError, TypeError) as exc:
                e.online, e.error = True, f"unerwartete Antwort: {exc}"
            else:
                e.values.update(vals)
                e.online, e.error, e.last_seen, e.offline_since = True, None, time.time(), None
                if isinstance(vals.get("room_temp"), int):
                    e.temps.append((e.last_seen, vals["room_temp"], e.values.get("temp_corr")))
            finally:
                e.polls += 1

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
        last three digits of the serial number. Derived locally, because reverse DNS can hang."""
        idu = str(e.values.get("idu") or "")
        return f"Heat4U-{idu[:3]}{idu[-3:]}" if len(idu) >= 6 else e.label

    # ------------------------------------------------------------------ helpers
    def _run_parallel(self, entries: list[HeaterState], action: Callable[[HeaterState], str]) -> list[dict]:
        def one(e: HeaterState) -> dict:
            try:
                message, ok = action(e), True
            except (DeviceError, coap.CoapError, ValueError) as exc:
                message, ok = str(exc), False
            self.refresh(e, full=True)
            return {"heater": e.id, "name": self._name(e), "ok": ok, "message": message}
        return list(self._pool.map(one, entries))

    def _name(self, e: HeaterState) -> str:
        return self.store.data["heaters"].get(e.id, {}).get("name") or self._device_name(e)

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
        return {"results": self._run_parallel(self._targets(kind, ident), apply)}

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
        return {"results": self._run_parallel(self._targets(kind, ident), apply)}

    def _mode_before_off(self, e: HeaterState) -> dict | None:
        """What "on" should restore later; None if the heater is already off (manual at frost level)."""
        mode, manual_sp, frost = (e.device.get(n) for n in ("operating_mode", "manual_sp", "antifrost_sp"))
        if mode in (0, 4) and isinstance(manual_sp, int) and isinstance(frost, int) and manual_sp <= frost:
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
            meta = d["heaters"].setdefault(e.id, {"last_host": e.label})
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
        results = self._run_parallel(self._targets("room", room_id), write)
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
        return {"created": created, "existing": existing, "empty": empty, "skipped": skipped,
                "active_slot": active_slot, "message": " ".join(parts)}

    def calibrate(self, heater_id: str, body: dict) -> dict:
        """Set the heater's temperature correction from a reference measurement ("measured", °C) or directly
        ("correction", K). The heater reports room_temp WITH its correction, so:
        new correction = old correction + (measured − shown), shown = 30-min average if available."""
        e = self._entry(heater_id)
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
        if abs(new) > MAX_CORRECTION:
            raise ActionError(f"Die nötige Korrektur ({_num(new / 10, '+.1f')} °C) liegt außerhalb von ±9,9 °C – "
                              "bitte die Messung prüfen.")
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
        entries = [self._entry(heater_id)] if heater_id else self.entries
        targets = [e for e in entries if e.online and e.device.writable and
                   (not only_drifted or (e.clock_drift is not None and abs(e.clock_drift) > DRIFT_LIMIT))]
        if heater_id and not targets:
            raise ActionError("Heizung ist offline oder nur lesbar (Token fehlt).")

        def apply(e: HeaterState) -> str:
            e.clock_drift = e.device.set_clock(self.now)
            e.last_clock_sync = time.time()
            log.info("Uhr gestellt: %s", e.cfg.host)
            return "Uhr gestellt"
        return {"results": self._run_parallel(targets, apply) if targets else []}

    # ------------------------------------------------------------------ network search
    def _scan_targets(self, networks) -> list[tuple[str, int]]:
        return discovery.targets_of(networks)

    def discover(self, network=None) -> dict:
        """Search the home network (or the given one) for heaters. Heaters the app knows by serial number but
        cannot reach any more are moved to the address where they answer now."""
        try:
            nets = [discovery.parse_network(network)] if str(network or "").strip() else \
                discovery.local_networks([e.cfg.host for e in self.entries])
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

    def _sweep(self, nets) -> list[dict]:
        with self._sensor_lock:
            return shelly.scan(self._sensor_targets(nets))

    def find_sensors(self, network=None) -> dict:
        """„Fühler suchen“: one sweep of the home network for Shelly Pills (read only)."""
        try:
            nets = [discovery.parse_network(network)] if str(network or "").strip() else \
                discovery.local_networks([e.cfg.host for e in self.entries])
        except discovery.DiscoveryError as exc:
            raise ActionError(str(exc)) from None
        if not nets:
            raise ActionError("Das Heimnetz wurde nicht erkannt – bitte das Netz angeben, z. B. 192.168.178.")
        started = time.monotonic()
        pills = sorted(self._sweep(nets), key=lambda p: tuple(int(x) for x in p["address"].split(":")[0].split(".")))
        at, heater_temps = self.now().isoformat(timespec="seconds"), self._heater_temps()
        for p in pills:
            self._found_pills[p["id"]] = {"mono": time.monotonic(), "at": at, "heaters": heater_temps, "pill": p}
            self._note_pill(p, at, heater_temps, save=True)
        if pills:
            log.info("Fühlersuche in %s: %s", ", ".join(map(str, nets)),
                     ", ".join(f"{p['name'] or p['model']} {p['address']}" for p in pills))
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

    def _note_pill(self, pill: dict, at: str, heater_temps: dict | None = None, save: bool = False) -> None:
        """A Pill answered: remember what it measures for its saved sensors, together with the room temperature
        of the heaters in their rooms at that moment (the pair the calibration compares). With save, the
        readings also go to data.json; a new address always does."""
        probes = {p["channel"]: p for p in pill["probes"]}
        self._pill_status[pill["id"]] = {
            "online": True, "since": None, "mode": pill["mode"], "firmware": pill["firmware"], "update": pill["update"],
            "probes": probes, "error": "Die Pill ist mit einem Passwort geschützt – ihre Messwerte lassen sich nicht "
                                       "lesen." if pill["protected"] else None}
        data = self.store.snapshot()
        saved = {sid: m for sid, m in data["sensors"].items() if m.get("pill") == pill["id"]}
        if not saved:
            return
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

    def _pill_offline(self, pill_id: str) -> None:
        old = self._pill_status.get(pill_id) or {}
        since = old.get("since") if old.get("online") is False else self.now().isoformat(timespec="seconds")
        self._pill_status[pill_id] = {**old, "online": False, "since": since, "error": self.NOT_ANSWERING}

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
        nets = discovery.local_networks([e.cfg.host for e in self.entries]
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
        for pid, p in found.items():
            if p is None:
                self._pill_offline(pid)
            else:
                self._note_pill(p, at, heater_temps, save=save)
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
            raise ActionError("Diese Pill hat die Suche nicht gefunden – bitte zuerst „Fühler suchen“.", 404)
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
        self._sensor(sensor_id)
        name = self._clean_name(body.get("name"), allow_empty=True)
        room_id = self._room_or_none(body.get("room")) if "room" in body else None
        with self.store.edit() as d:
            meta = d["sensors"][sensor_id]
            if "name" in body:
                meta["name"] = name
            if "room" in body:
                meta["room"] = room_id
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
            raise ActionError("Diese Pill kennt die App nicht – bitte zuerst „Fühler suchen“.", 404)
        host, _, port = address.partition(":")
        port = int(port) if port.isdigit() else shelly.PORT
        info = shelly.probe(host, port, timeout=2.0)
        if not info or str(info.get("id")) != pill_id:   # gone, or another device has taken the address
            raise ActionError("Die Pill antwortet nicht unter ihrer letzten Adresse – bitte erneut „Fühler suchen“.", 504)
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
                          "compare": compare, "tolerance": self.CALIB_TOLERANCE})
        return sorted(views, key=lambda v: v["name"].lower())

    def _adopt(self, found: dict) -> list[dict]:
        """Known heaters (by serial number) that are offline but answered at another address: take that address."""
        moved = []
        for (ip, port), serial in found.items():
            e = next((x for x in self.entries if x.key == serial), None)
            if e is None or e.online or (e.cfg.host, e.cfg.port) == (ip, port):
                continue
            old = e.label
            if self._move(e, ip, port):
                moved.append({"heater": e.id, "name": self._name(e), "old": old, "new": e.label})
        return moved

    def _move(self, e: HeaterState, host: str, port: int) -> bool:
        with self._entries_lock:
            if any(x is not e and (x.cfg.host, x.cfg.port) == (host, port) for x in self.entries):
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
        lost = [e for e in self.entries if e.key and not e.online and e.offline_since
                and now - e.offline_since >= LOST_AFTER]
        if not lost or now - self._last_heal < HEAL_EVERY:
            return
        self._last_heal = now
        try:
            nets = discovery.local_networks([e.cfg.host for e in self.entries])
            with self._scan_lock:
                found = discovery.scan(self._scan_targets(nets))
            self._adopt(found)
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
            if name is not None:
                room["name"] = name
            if "plan" in body:
                room["plan"] = plan_id
            if members is not None:
                self._assign(d, room_id, members)
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
            data["heaters"].setdefault(e.id, {"last_host": e.label})["room"] = room_id
        for heater_id, meta in data["heaters"].items():
            if meta.get("room") == room_id and heater_id not in wanted:
                meta["room"] = None

    def delete_room(self, room_id: str) -> dict:
        self._room(room_id)
        with self.store.edit() as d:
            d["rooms"].pop(room_id)
            for meta in list(d["heaters"].values()) + list(d["sensors"].values()):
                if meta.get("room") == room_id:
                    meta["room"] = None
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
            warning = self._reconfigure(e, body)
        with self.store.edit() as d:  # after _reconfigure: the heater may now be known by its serial number
            meta = d["heaters"].setdefault(e.id, {"last_host": e.label})
            if "name" in body:
                meta["name"] = name
            if "room" in body:
                meta["room"] = room_id
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
            self.entries = self.entries + [e]
            self._save_config()
        if online:
            self.refresh(e, full=True)
        if name:
            with self.store.edit() as d:
                d["heaters"].setdefault(e.id, {"last_host": e.label})["name"] = name
        log.info("Heizung hinzugefügt: %s", e.label)
        return {"id": e.id, "warning": bool(warning),
                "message": ("Heizung hinzugefügt." if token else "Heizung hinzugefügt (ohne Token: nur Anzeige).") + warning}

    def delete_heater(self, heater_id: str) -> dict:
        e = self._entry(heater_id)
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
        for x in self.entries:
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
        lines = header + [f"{x.label:<19} {x.cfg.token or ''}".rstrip() for x in self.entries]
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
        return {"id": plan_id}

    def update_plan(self, plan_id: str, body: dict) -> dict:
        if plan_id not in self.store.data["plans"]:
            raise ActionError("Heizplan nicht gefunden", 404)
        plan = self._plan_from(body)
        with self.store.edit() as d:
            d["plans"][plan_id] = plan
        return {}

    def delete_plan(self, plan_id: str) -> dict:
        if plan_id not in self.store.data["plans"]:
            raise ActionError("Heizplan nicht gefunden", 404)
        with self.store.edit() as d:
            d["plans"].pop(plan_id)
            for room in d["rooms"].values():
                if room.get("plan") == plan_id:
                    room["plan"] = None
        return {}

    # ------------------------------------------------------------------ snapshot for the UI
    def state(self) -> dict:
        data = self.store.snapshot()
        heaters = [self._heater_view(e, data) for e in self.entries]
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
            })
        plan_list = [{"id": pid, **p, "rooms": [r["id"] for r in rooms if r["plan"] == pid]}
                     for pid, p in data["plans"].items()]
        return {"now": self.now().isoformat(timespec="seconds"), "heaters": heaters,
                "rooms": rooms, "plans": plan_list, "sensors": self._sensor_views(data, heaters)}

    def _heater_view(self, e: HeaterState, data: dict) -> dict:
        v, meta = e.values, data["heaters"].get(e.id, {})

        def deg(name: str):
            x = v.get(name)
            return x / 10 if isinstance(x, (int, float)) else None
        room = meta.get("room")
        return {
            "id": e.id,
            "name": meta.get("name") or self._device_name(e),
            "custom_name": meta.get("name") or "",
            "host": e.label,
            "hostname": self._device_name(e),
            "room": room if room in data["rooms"] else None,
            "online": e.online,
            "writable": e.device.writable,
            "error": e.error,
            "last_seen": self._iso(e.last_seen),
            "room_temp": deg("room_temp"),
            "setpoint": deg("setpoint"),
            "antifrost": deg("antifrost_sp"),
            "heating": v.get("heater_state") == 1,
            "mode": MODES.get(v.get("operating_mode")),
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
