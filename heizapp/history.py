"""Verlauf: what every heater, thermostat and sensor showed, in steps of five minutes – and how long each one heated.

The heaters keep no history (Thermotec's Data Act sheet: no time series, neither in the device nor in the cloud), so
the app writes down what it reads anyway. Every poll (10 s) adds to the current five-minute entry of each device;
finished entries go into a monthly file history/<YYYY-MM>.csv next to data.json – every 15 minutes in one go, to spare
the memory card (a power cut loses at most that much). Nothing is recorded while the clock is not trusted: a Pi
without battery clock would file the readings under the wrong time (the service checks that before every sample).

One line per device and five minutes, temperatures in 0.1 °C:

    t,id,ist,soll,heizt,fenster,da,feuchte

t is the start of the five minutes (seconds since 1970, so summer and winter time cannot mix it up); heizt, fenster
and da are the seconds the device heated, had its window open and answered. A sensor has only ist and feuchte
(humidity in 0.1 %). Energy and costs are not stored: they are heating seconds × the power set for the heater × the
price, reckoned when shown – a corrected power or price counts for the past days as well.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable

log = logging.getLogger(__name__)

SLOT = 300            # seconds per entry
FLUSH_EVERY = 900     # seconds between two writes to the memory card
MAX_STEP = 30         # seconds one poll counts for at most: a stalled loop or a gap adds no heating time
FIELDS = ("t", "id", "ist", "soll", "heizt", "fenster", "da", "feuchte")
NUMBERS = ("ist", "soll", "heizt", "fenster", "da", "feuchte")
MAX_WAITING = 20000   # entries kept while the file cannot be written (about two days of four devices), then dropped
FILE_NAME = re.compile(r"(\d{4})-(\d{2})\.csv")
DAY_NAMES = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
MONTH_NAMES = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
               "November", "Dezember")


class History:
    """The recording (thread-safe): sample() from the poller every 10 s, the views from the web server."""

    def __init__(self, folder: str, tz=None, clock: Callable[[], float] = time.monotonic):
        self.folder = folder
        self.tz = tz
        self._clock = clock                     # monotonic, only for when to write next (tests bring their own)
        self._lock = threading.Lock()
        self._open: dict[str, dict] = {}        # id -> the entry being filled now
        self._waiting: list[dict] = []          # finished entries not yet in the file
        self._day: date | None = None           # the local date of _today
        self._today: dict[str, dict[int, dict]] = {}   # id -> t -> finished entry of that day (also from the file)
        self._seen: dict[str, str] = {}         # sensor id -> time of the reading counted last
        self._last: float | None = None         # time of the last sample
        self._written = clock()
        self._warned = False
        self._first: date | None = self._first_in_files()

    # ------------------------------------------------------------------ time
    def local(self, t: float) -> datetime:
        return datetime.fromtimestamp(t, self.tz)

    def day_of(self, t: float) -> date:
        return self.local(t).date()

    def moment(self, day: date, hour: int = 0) -> float:
        """Seconds since 1970 of a local clock time – also right on the days the clocks change."""
        if hour >= 24:
            day, hour = day + timedelta(days=1), hour - 24
        return datetime(day.year, day.month, day.day, hour, tzinfo=self.tz).timestamp()

    @staticmethod
    def label(day: date, today: date) -> str:
        """„Heute“, „Gestern“ or „Dienstag, 24. November“ (with the year when it is not this one)."""
        if day == today:
            return "Heute"
        if day == today - timedelta(days=1):
            return "Gestern"
        text = f"{DAY_NAMES[day.weekday()]}, {day.day}. {MONTH_NAMES[day.month - 1]}"
        return text if day.year == today.year else f"{text} {day.year}"

    # ------------------------------------------------------------------ recording
    def sample(self, now: float, devices: list[dict], sensors: list[dict]) -> None:
        """One poll: devices = [{id, online, temp, soll (0.1 °C), heating, window}], sensors = [{id, at, temp,
        humidity}] with the latest reading of each (counted once, however often it is handed in)."""
        with self._lock:
            step = 0.0 if self._last is None else max(0.0, min(MAX_STEP, now - self._last))
            self._last = now
            slot = int(now // SLOT * SLOT)
            self._close(slot)
            for d in devices:
                entry = self._entry(d["id"], slot, sensor=False)
                if not d.get("online"):
                    continue
                entry["da"] += step
                if _tenths(d.get("temp")) is not None:
                    entry["sum"] += d["temp"]
                    entry["n"] += 1
                if _tenths(d.get("soll")) is not None:
                    entry["soll"] = d["soll"]
                if d.get("heating"):
                    entry["heizt"] += step
                if d.get("window"):
                    entry["fenster"] += step
            for s in sensors:
                if not s.get("at") or s["at"] == self._seen.get(s["id"]) or not isinstance(s.get("temp"), (int, float)):
                    continue
                self._seen[s["id"]] = s["at"]
                entry = self._entry(s["id"], slot, sensor=True)
                entry["sum"] += round(s["temp"] * 10)
                entry["n"] += 1
                if isinstance(s.get("humidity"), (int, float)):
                    entry["hsum"] += round(s["humidity"] * 10)
                    entry["hn"] += 1
            if self._clock() - self._written >= FLUSH_EVERY:
                self._write()

    def _entry(self, ident: str, slot: int, sensor: bool) -> dict:
        entry = self._open.get(ident)
        if entry is None or entry["t"] != slot:
            if entry is not None:
                self._finish(entry)
            entry = self._open[ident] = {"t": slot, "id": ident, "sensor": sensor, "sum": 0, "n": 0, "soll": None,
                                         "heizt": 0.0, "fenster": 0.0, "da": 0.0, "hsum": 0, "hn": 0}
        return entry

    def _close(self, slot: int) -> None:
        """Finish every entry of an earlier five minutes (also of a device that is gone)."""
        for ident, entry in list(self._open.items()):
            if entry["t"] != slot:
                self._finish(entry)
                del self._open[ident]
        day = self.day_of(slot)
        if day != self._day:   # a new day: the entries of the old one are in the file or about to be
            self._day, self._today = day, self._read_day(day)

    def _finish(self, entry: dict) -> None:
        row = _row(entry)
        if row is None:   # a sensor without a reading in these five minutes
            return
        if len(self._waiting) < MAX_WAITING:
            self._waiting.append(row)
        if self._day is not None and self.day_of(row["t"]) == self._day:
            _merge(self._today.setdefault(row["id"], {}), row)

    def flush(self) -> None:
        """Write what is finished (when the app stops; otherwise every 15 minutes by itself)."""
        with self._lock:
            self._write()

    def _write(self) -> None:
        self._written = self._clock()
        if not self._waiting:
            return
        by_month: dict[str, list[dict]] = {}
        for row in self._waiting:
            by_month.setdefault(self.local(row["t"]).strftime("%Y-%m"), []).append(row)
        try:
            os.makedirs(self.folder, exist_ok=True)
            for month, rows in sorted(by_month.items()):
                path = os.path.join(self.folder, f"{month}.csv")
                new = not os.path.exists(path)
                with open(path, "a", encoding="utf-8", newline="") as fh:
                    out = csv.writer(fh)
                    if new:
                        out.writerow(FIELDS)
                    out.writerows([[("" if row[f] is None else row[f]) for f in FIELDS] for row in rows])
        except OSError as exc:
            if not self._warned:
                self._warned = True
                log.warning("Verlauf: %s lässt sich nicht schreiben (%s) – die Werte bleiben vorerst im Speicher",
                            self.folder, exc)
            return
        if self._warned:
            self._warned = False
            log.info("Verlauf: wird wieder geschrieben")
        first = self.day_of(min(row["t"] for row in self._waiting))
        self._first = min(self._first, first) if self._first else first
        self._waiting = []

    # ------------------------------------------------------------------ reading
    @property
    def active(self) -> bool:
        """Has the app recorded in the last minute (its clock is trusted and the poller runs)?"""
        return self._last is not None and time.time() - self._last < 60

    def first_day(self) -> date | None:
        """The first day with entries, in the files or – before the first write – today."""
        with self._lock:
            days = [d for d in (self._first, self._day if (self._today or self._open) else None) if d]
            return min(days) if days else None

    def day_rows(self, day: date, ids=None, files: bool = True) -> dict[str, dict[int, dict]] | None:
        """id -> t -> entry of a local day; today with the five minutes being filled right now, an earlier day with
        what is not in the file yet (just after midnight, or while the card cannot be written). files=False: only
        what is in memory, for the page's refresh every few seconds, which must not read the memory card – None
        if the day is not there (no poll of that day yet, e.g. while the clock is not set)."""
        with self._lock:   # also while reading the file: nothing moves from _waiting into it meanwhile
            if day == self._day:
                rows = {i: dict(by_t) for i, by_t in self._today.items() if ids is None or i in ids}
                more = [row for row in map(_row, self._open.values()) if row is not None]
            elif not files:
                return None
            else:
                rows, more = self._read_day(day, ids), self._waiting
            for row in more:
                if (ids is None or row["id"] in ids) and self.day_of(row["t"]) == day:
                    _merge(rows.setdefault(row["id"], {}), row)
            return rows

    def _read_day(self, day: date, ids=None) -> dict[str, dict[int, dict]]:
        start, end = self.moment(day), self.moment(day, 24)
        rows: dict[str, dict[int, dict]] = {}
        path = os.path.join(self.folder, f"{day:%Y-%m}.csv")
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                for line in csv.reader(fh):
                    row = _parse(line)
                    if row and start <= row["t"] < end and (ids is None or row["id"] in ids):
                        _merge(rows.setdefault(row["id"], {}), row)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Verlauf: %s nicht lesbar: %s", path, exc)
        return rows

    def _first_in_files(self) -> date | None:
        try:
            months = sorted(name for name in os.listdir(self.folder) if FILE_NAME.fullmatch(name))
        except OSError:
            return None
        for name in months:
            try:
                with open(os.path.join(self.folder, name), encoding="utf-8", newline="") as fh:
                    first = min((row["t"] for row in map(_parse, csv.reader(fh)) if row), default=None)
            except OSError:
                continue
            if first is not None:
                return self.day_of(first)
        return None


def _tenths(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _row(entry: dict) -> dict | None:
    if entry["sensor"]:
        if not entry["n"]:
            return None
        return {"t": entry["t"], "id": entry["id"], "ist": round(entry["sum"] / entry["n"]), "soll": None, "heizt": None,
                "fenster": None, "da": None, "feuchte": round(entry["hsum"] / entry["hn"]) if entry["hn"] else None}
    return {"t": entry["t"], "id": entry["id"], "ist": round(entry["sum"] / entry["n"]) if entry["n"] else None,
            "soll": entry["soll"], "heizt": round(entry["heizt"]), "fenster": round(entry["fenster"]),
            "da": round(entry["da"]), "feuchte": None}


def _parse(line: list[str]) -> dict | None:
    if len(line) != len(FIELDS) or not line[0].isdigit():
        return None   # the header, or a line cut off by a power cut
    try:
        row = {"t": int(line[0]), "id": line[1]}
        row.update({f: int(v) if v != "" else None for f, v in zip(NUMBERS, line[2:])})
    except ValueError:
        return None
    return row


def _merge(by_t: dict[int, dict], row: dict) -> None:
    """The same five minutes twice (the clock was set back): seconds add up, temperatures are averaged."""
    old = by_t.get(row["t"])
    if old is None:
        by_t[row["t"]] = row
        return
    new = dict(old)   # a new entry: a copy of the day handed out (day_rows) shares the old one
    for f in ("heizt", "fenster", "da"):
        if row[f] is not None:
            new[f] = min(SLOT, (old[f] or 0) + row[f])
    for f in ("ist", "feuchte"):
        if row[f] is not None:
            new[f] = row[f] if old[f] is None else round((old[f] + row[f]) / 2)
    if row["soll"] is not None:
        new["soll"] = row["soll"]
    by_t[row["t"]] = new


# ---------------------------------------------------------------------- what a room or heater shows
def series(rows: dict[str, dict[int, dict]], heaters: list[str], sensors: list[str]) -> list[dict]:
    """One point per five minutes, as the home page shows the room: the temperature of its sensors when one
    measured, else of its heaters that answered; the setpoint of the first heater that answered; and – like „heizt“ on
    the page – the longest any heater heated (heaters side by side heat mostly at the same time)."""
    points = []
    for t in sorted({t for i in heaters + sensors for t in rows.get(i, {})}):
        own = [rows[i][t] for i in heaters if t in rows.get(i, {})]
        answered = [r for r in own if r["da"]]
        temps = [rows[i][t]["ist"] for i in sensors if t in rows.get(i, {}) and rows[i][t]["ist"] is not None] \
            or [r["ist"] for r in answered if r["ist"] is not None]
        soll = next((r["soll"] for r in answered if r["soll"] is not None), None)
        points.append({"t": t, "ist": round(sum(temps) / len(temps) / 10, 1) if temps else None,
                       "soll": soll / 10 if soll is not None else None,
                       "heizt": max((r["heizt"] or 0 for r in own), default=0),
                       "fenster": max((r["fenster"] or 0 for r in own), default=0),
                       "da": max((r["da"] or 0 for r in own), default=0) if own else None})
    return points


def totals(rows: dict[str, dict[int, dict]], heaters: list[dict], points: list[dict], price_ct) -> dict:
    """Heating time of the room (from the points) and of every heater; energy only of the AeroFlow heaters with a
    power, costs only with a price. heaters = [{id, name, kind, power_w}]."""
    devices, kwh, known, missing = [], 0.0, False, []
    for h in heaters:
        seconds = sum(r["heizt"] or 0 for r in rows.get(h["id"], {}).values())
        energy = seconds * h["power_w"] / 3.6e6 if h["kind"] == "aeroflow" and h.get("power_w") else None
        if h["kind"] == "aeroflow" and energy is None:
            missing.append(h["name"])
        if energy is not None:
            kwh, known = kwh + energy, True
        devices.append({"id": h["id"], "name": h["name"], "kind": h["kind"], "power_w": h.get("power_w"),
                        "heat_s": seconds, "kwh": round(energy, 2) if energy is not None else None})
    return {"heat_s": sum(p["heizt"] for p in points), "kwh": round(kwh, 2) if known else None,
            "eur": round(kwh * price_ct / 100, 2) if known and price_ct else None,
            "power_missing": missing, "devices": devices}
