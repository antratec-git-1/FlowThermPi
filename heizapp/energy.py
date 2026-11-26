"""Strom der Steckdosen: what flowed through every plug with power metering, in quarter hours.

Every poll (about every 10 s) hands in what each plug reported last: its meter (Wh, counting up), its power and when
it reported. The meter's step since the reading before is spread evenly over the time between the two readings and
split at the quarter hours – so a plug that reports only every few minutes (Zigbee) still lands in the right quarter
hours. A meter that went back (the plug was reset) counts from zero; a step the plug cannot have drawn (more than
MAX_W all the time – Zigbee2MQTT has a bug that multiplies the meter by 1000) counts as power × time instead, like a
plug without a meter. Readings more than MAX_GAP apart: what flowed in between is not spread over hours nobody saw – it
is left out (the protocol says so).

Finished quarter hours go into a monthly file energy/<YYYY-MM>.csv next to data.json – every 15 minutes in one go,
like the Verlauf. With them goes the last meter reading of every plug (energy/zaehler.json), so a restart (an update)
loses nothing: the next reading spreads what flowed meanwhile over the minutes the app was away. Nothing is recorded
while the clock is not trusted (the service checks that before every sample).

One line per plug and quarter hour:

    t,id,wh,w_max,raum,heizung

t is the start of the quarter hour (seconds since 1970), wh the energy in it (Wh, one decimal), w_max the highest power
in it (W): what the plug reported there, what it had reported last before (a Zigbee plug reports only when the power
changes), and at least the average of the quarter hour. raum and heizung: the room and the heater the plug belonged to
then (empty: none) – a later change does not rewrite the past. A quarter hour only gets a line once energy was counted
for it: the very first reading of a plug only marks where its meter stands.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable

log = logging.getLogger(__name__)

SLOT = 900             # seconds per entry: a quarter hour
FLUSH_EVERY = 900      # seconds between two writes to the memory card
MAX_GAP = 6 * 3600     # readings further apart: what flowed in between is left out
POWER_GAP = 1200       # without a meter: power × time only over at most this many seconds
MAX_W = 4000           # W a plug can draw (16 A × 230 V = 3680 W) – a bigger step of the meter is a fault
SLACK_WH = 5.0         # Wh a meter may step more than MAX_W allows (rounding, a reading a little late)
FULL = SLOT * 9 // 10  # seconds of a quarter hour with values before its average counts as a floor for the highest power
FIELDS = ("t", "id", "wh", "w_max", "raum", "heizung")
MAX_WAITING = 50000    # entries kept while the file cannot be written (about a week of five plugs), then dropped
MAX_DAYS = 366         # days one download may span
FILE_NAME = re.compile(r"(\d{4})-(\d{2})\.csv")
MARKS = "zaehler.json"


class PlugEnergy:
    """The recording (thread-safe): sample() from the poller every 10 s, the views and downloads from the web server."""

    def __init__(self, folder: str, tz=None, clock: Callable[[], float] = time.monotonic):
        self.folder = folder
        self.tz = tz
        self._clock = clock                       # monotonic, only for when to write next (tests bring their own)
        self._lock = threading.Lock()
        self._marks: dict[str, dict] = self._load_marks()   # id -> the last reading counted: {at, wh, w}
        self._open: dict[tuple[str, int], dict] = {}        # (id, t) -> the entry of the quarter hour being filled
        self._waiting: list[dict] = []            # finished entries not yet in the file
        self._last: float | None = None           # time of the last sample
        self._written = clock()
        self._warned = False
        self._said: dict[str, str] = {}           # id -> what the protocol said last about this plug's meter
        self._first: date | None = self._first_in_files()

    # ------------------------------------------------------------------ time
    def local(self, t: float) -> datetime:
        return datetime.fromtimestamp(t, self.tz)

    def moment(self, day: date) -> float:
        """Seconds since 1970 of the local midnight starting this day."""
        return datetime(day.year, day.month, day.day, tzinfo=self.tz).timestamp()

    # ------------------------------------------------------------------ recording
    def sample(self, now: float, plugs: list[dict]) -> None:
        """One poll: plugs = [{id, at (when it reported), wh (meter) or None, w (power) or None, raum, heizung}]."""
        with self._lock:
            self._last = now
            for r in plugs:
                at = r.get("at")
                if not isinstance(at, (int, float)) or at > now + 60:
                    continue
                at = min(float(at), now)
                wh, w = _number(r.get("wh")), _number(r.get("w"))
                place = {"raum": r.get("raum") or None, "heizung": r.get("heizung") or None}
                mark = self._marks.get(r["id"])
                if mark is not None and at <= mark["at"]:
                    continue   # the same reading as last time
                step = self._step(r["id"], mark, at, wh, w) if mark is not None else None
                if step is not None:
                    carry = mark["w"] if at - mark["at"] <= POWER_GAP else None   # the power until this reading
                    self._spread(r["id"], mark["at"], at, step, place, carry)
                    entry = self._open.get((r["id"], int(at // SLOT * SLOT)))
                    if entry is not None and w is not None:   # the power now, in the quarter hour it reached
                        entry["w_max"] = w if entry["w_max"] is None else max(entry["w_max"], w)
                self._marks[r["id"]] = {"at": at, "wh": wh, "w": w}
            current = int(now // SLOT * SLOT)
            for key in [k for k in self._open if k[1] < current]:   # finished quarter hours
                self._finish(self._open.pop(key))
            if self._clock() - self._written >= FLUSH_EVERY:
                self._write()

    def _step(self, ident: str, mark: dict, at: float, wh: float | None, w: float | None) -> float | None:
        """Wh between the reading before (mark) and this one – None if it cannot be told."""
        seconds = at - mark["at"]
        if seconds > MAX_GAP:
            self._say(ident, "gap", "Strom der Steckdosen: %s hat %d Stunden nichts gemeldet – was in der Zeit floss, "
                                    "fehlt in der Aufzeichnung", ident, round(seconds / 3600))
            return None
        if wh is not None and mark["wh"] is not None:
            step = wh - mark["wh"] if wh >= mark["wh"] else wh   # went back: the plug was reset and counts from zero
            if step <= MAX_W * seconds / 3600 + SLACK_WH:
                self._said.pop(ident, None)
                return step
            self._say(ident, "jump", "Strom der Steckdosen: der Zähler von %s springt um %.0f Wh in %d s – gezählt "
                                     "wird stattdessen die Leistung", ident, step, round(seconds))
        power = mark["w"] if mark["w"] is not None else w
        if power is None or seconds > POWER_GAP:
            return None
        return power * seconds / 3600

    def _say(self, ident: str, what: str, *args) -> None:   # once, until the meter counts normally again
        if self._said.get(ident) != what:
            self._said[ident] = what
            log.warning(*args)

    def _spread(self, ident: str, start: float, end: float, wh: float, place: dict, carry: float | None) -> None:
        """wh evenly over start … end, quarter hour by quarter hour (also 0 Wh: the plug was measured, nothing flowed);
        carry: the power the plug reported at start, for the highest power of each quarter hour."""
        span = end - start
        t = int(start // SLOT * SLOT)
        while t < end:
            seconds = min(end, t + SLOT) - max(start, t)
            entry = self._entry(ident, t, place)
            entry["wh"] += wh * seconds / span
            entry["s"] += seconds
            if carry is not None:
                entry["w_max"] = carry if entry["w_max"] is None else max(entry["w_max"], carry)
            t += SLOT

    def _entry(self, ident: str, t: int, place: dict) -> dict:
        entry = self._open.get((ident, t))
        if entry is None:
            entry = self._open[(ident, t)] = {"t": t, "id": ident, "wh": 0.0, "s": 0.0, "w_max": None, **place}
        else:
            entry.update(place)   # the place at the latest reading counts
        return entry

    def _finish(self, entry: dict) -> None:
        if len(self._waiting) < MAX_WAITING:
            self._waiting.append(_row(entry))

    def flush(self) -> None:
        """Write what is finished (when the app stops; otherwise every 15 minutes by itself)."""
        with self._lock:
            self._write()

    def _write(self) -> None:
        self._written = self._clock()
        if not self._waiting and not any(m["wh"] is not None for m in self._marks.values()):
            return   # no plug with a meter: nothing to keep
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
            self._save_marks()
        except OSError as exc:
            if not self._warned:
                self._warned = True
                log.warning("Strom der Steckdosen: %s lässt sich nicht schreiben (%s) – die Werte bleiben vorerst im "
                            "Speicher", self.folder, exc)
            return
        if self._warned:
            self._warned = False
            log.info("Strom der Steckdosen: wird wieder geschrieben")
        if self._waiting:
            first = self.local(min(row["t"] for row in self._waiting)).date()
            self._first = min(self._first, first) if self._first else first
        self._waiting = []

    def _save_marks(self) -> None:
        """The meter of every plug where the file ends: the quarter hour being filled is not in it yet, so its start
        and the meter less what it holds – after a restart the next reading counts that part again."""
        marks = {}
        for ident, mark in self._marks.items():
            if mark["wh"] is None:
                continue
            held = [e for (i, _), e in self._open.items() if i == ident]
            marks[ident] = ({"at": min(e["t"] for e in held), "wh": round(mark["wh"] - sum(e["wh"] for e in held), 3),
                             "w": mark["w"]} if held else dict(mark))
        tmp = os.path.join(self.folder, MARKS + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(marks, fh)
        os.replace(tmp, os.path.join(self.folder, MARKS))

    def _load_marks(self) -> dict[str, dict]:
        try:
            with open(os.path.join(self.folder, MARKS), encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("Strom der Steckdosen: %s nicht lesbar (%s) – die Zähler fangen neu an", MARKS, exc)
            return {}
        marks = {}
        for ident, m in (raw.items() if isinstance(raw, dict) else []):
            if isinstance(m, dict) and isinstance(m.get("at"), (int, float)) and _number(m.get("wh")) is not None:
                marks[str(ident)] = {"at": float(m["at"]), "wh": float(m["wh"]), "w": _number(m.get("w"))}
        return marks

    # ------------------------------------------------------------------ reading
    @property
    def active(self) -> bool:
        """Has the app recorded in the last minute (its clock is trusted and the poller runs)?"""
        return self._last is not None and time.time() - self._last < 60

    def first_day(self) -> date | None:
        """The first day with entries, in the files or – before the first write – in memory."""
        with self._lock:
            ts = [row["t"] for row in self._waiting] + [e["t"] for e in self._open.values()]
            days = [d for d in (self._first, self.local(min(ts)).date() if ts else None) if d]
            return min(days) if days else None

    def rows_between(self, first: date, last: date, ids=None) -> dict[str, dict[int, dict]]:
        """id -> t -> entry of the local days first … last: from the files, the entries not written yet and the
        quarter hour being filled right now."""
        start, end = self.moment(first), self.moment(last + timedelta(days=1))
        keep = (lambda row: start <= row["t"] < end and (ids is None or row["id"] in ids))
        rows: dict[str, dict[int, dict]] = {}
        with self._lock:   # also while reading the files: nothing moves from _waiting into them meanwhile
            month = date(first.year, first.month, 1)
            while month <= last:
                path = os.path.join(self.folder, f"{month:%Y-%m}.csv")
                try:
                    with open(path, encoding="utf-8", newline="") as fh:
                        for row in filter(None, map(_parse, csv.reader(fh))):
                            if keep(row):
                                _merge(rows.setdefault(row["id"], {}), row)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    log.warning("Strom der Steckdosen: %s nicht lesbar: %s", path, exc)
                month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
            for row in self._waiting + [_row(e) for e in self._open.values()]:
                if keep(row):
                    _merge(rows.setdefault(row["id"], {}), row)
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
                return self.local(first).date()
        return None


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _row(entry: dict) -> dict:
    """An entry as it goes into the file: the highest power at least the average of its quarter hour – only once that has
    values (nearly) throughout: over a few minutes a meter that counts in 10-Wh steps (Zigbee) would make it much too high."""
    w_max = [v for v in (entry["w_max"], entry["wh"] * 3600 / entry["s"] if entry["s"] >= FULL else None) if v is not None]
    return {"t": entry["t"], "id": entry["id"], "wh": round(entry["wh"], 1), "w_max": round(max(w_max)) if w_max else None,
            "raum": entry.get("raum"), "heizung": entry.get("heizung")}


def _parse(line: list[str]) -> dict | None:
    if len(line) != len(FIELDS) or not line[0].isdigit():
        return None   # the header, or a line cut off by a power cut
    try:
        return {"t": int(line[0]), "id": line[1], "wh": float(line[2]), "w_max": int(line[3]) if line[3] else None,
                "raum": line[4] or None, "heizung": line[5] or None}
    except ValueError:
        return None


def _merge(by_t: dict[int, dict], row: dict) -> None:
    """The same quarter hour twice (a late reading, the clock set back): the energy adds up, the highest power counts."""
    old = by_t.get(row["t"])
    if old is None:
        by_t[row["t"]] = row
        return
    new = dict(old)
    new["wh"] = round(old["wh"] + row["wh"], 1)
    if row["w_max"] is not None:
        new["w_max"] = row["w_max"] if old["w_max"] is None else max(old["w_max"], row["w_max"])
    for f in ("raum", "heizung"):
        new[f] = row[f] if row[f] is not None or old[f] is None else old[f]
    by_t[row["t"]] = new


# ---------------------------------------------------------------------- the download („Als Tabelle“)
def table(rows: dict[str, dict[int, dict]], local: Callable[[float], datetime], names: dict[str, str],
          rooms: dict[str, str], heaters: dict[str, str]) -> bytes:
    """The quarter hours as a table for Excel or Numbers: German style (semicolons, decimal commas), UTF-8 with BOM so
    the umlauts survive. names/rooms/heaters: id -> name now (an id no longer known stays empty)."""
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(["Tag", "Beginn", "Ende", "Steckdose", "Raum", "Heizung", "Strom (Wh)", "Höchste Leistung (W)"])
    lines = [(t, names.get(ident, ident), row) for ident, by_t in rows.items() for t, row in by_t.items()]
    for t, name, row in sorted(lines, key=lambda x: (x[0], x[1])):
        begin, end = local(t), local(t + SLOT)
        w.writerow([f"{begin:%d.%m.%Y}", f"{begin:%H:%M}", f"{end:%H:%M}", name, rooms.get(row["raum"] or "", ""),
                    heaters.get(row["heizung"] or "", ""), f"{row['wh']:.1f}".replace(".", ","),
                    "" if row["w_max"] is None else row["w_max"]])
    return ("﻿" + out.getvalue()).encode("utf-8")
