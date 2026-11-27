"""Durchgänge: when a washing machine or a dryer runs, and when it is done – from the power its plug reports.

Every poll (about every 10 s) hands in every plug with a washing machine or a dryer: its power and when it reported
it. That power holds until the plug reports again (a Zigbee plug reports only when it changes); the time moves on with
every poll, so „fertig“ also comes from a plug that stays silent at its standby. The values below are start values –
whether they fit, only the real devices show.

- Start: above START_W for START_S. A washing machine turns its drum in short pushes, so its seconds may be pieced
  together within START_WINDOW; a dryer turns it all the time, so its seconds must come in one go – then its short
  pushes after the end (Knitterschutz) never start a run. Waiting for a delayed start (1–3 W) is no start. The minutes
  with power right before the start belong to the run (filling with water, the drum turning before it heats).
- Running: short pauses stay part of it (soaking, pumping out).
- Done: a washing machine after QUIET_S below END_W, a dryer after QUIET_S without lasting power – its pushes shorter
  than BURST_S count as rest. The run ends where the rest began.
- A run shorter than MIN_S is forgotten (a push, a test). A plug silent for STALE ends its run without „fertig“, and so
  does a run longer than MAX_S (the protocol says so: the limits do not fit this device).

Every finished run gets a line in geraete/durchgaenge-<YYYY>.csv next to data.json:

    beginn,ende,id,art,wh,w_max,heiz_min

beginn and ende in seconds since 1970, wh the energy (Wh, power × time), w_max the highest power (W), heiz_min the
minutes above HEAT_W – a hint whether it heated (the temperature the app cannot know). The average power of every
minute goes into geraete/kurven-<YYYY-MM>.json ({"<id>@<beginn>": [W, …]}, about 2 KB a run). A run going on is kept
in geraete/laufend.json every minute: after an update (a restart of the app) within RESUME_GAP it goes on.

The notices „… ist fertig“ (geraete/hinweise.json) stay until someone taps OK on the start page, the device runs again
for MIN_S or NOTICE_S have passed. Nothing about the runs goes into the protocol but faults: when someone washes is
nobody else's business.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable

log = logging.getLogger(__name__)

KINDS = ("waschmaschine", "trockner")   # what hangs on a plug whose runs the app follows
START_W = 15.0        # W: above this the device works …
START_S = 60          # … for this many seconds: it runs
START_WINDOW = 120    # a washing machine: those seconds within this many
END_W = 8.0           # W: below this it rests
QUIET_S = {"waschmaschine": 300, "trockner": 180}   # resting this long: done
BURST_S = 60          # a dryer: power above END_W for less than this is a push (Knitterschutz), no lasting power
HEAT_W = 1000.0       # W: above this it heats (the water, the air)
LEAD_S = 900          # how far back a start looks for the minutes the power began
MIN_S = 300           # shorter runs are forgotten
MAX_S = 8 * 3600      # longer ones end – the limits do not fit this device
STALE = 900           # a plug silent this long: what it draws is not known, its run ends without „fertig“
RESUME_GAP = 300      # back this soon after a restart: the run goes on
NOTICE_S = 12 * 3600  # a notice goes by itself after this time
FIELDS = ("beginn", "ende", "id", "art", "wh", "w_max", "heiz_min")
RUNNING, NOTICES = "laufend.json", "hinweise.json"
YEAR_FILE = re.compile(r"durchgaenge-(\d{4})\.csv")
SAVED = ("kind", "t", "w", "heard", "min_t", "min_e", "min_cov", "min_peak", "min_heat", "run")


def _fresh(kind: str, t: float) -> dict:
    """What the app knows of a plug it starts to follow: nothing yet."""
    return {"kind": kind, "t": t, "w": None, "heard": 0.0,
            "min_t": None, "min_e": 0.0, "min_cov": 0.0, "min_peak": 0.0, "min_heat": 0.0,   # the minute going on
            "lead": deque(maxlen=LEAD_S // 60), "above": deque(), "above_since": None, "blocked": False, "run": None}


class Runs:
    """The runs of every washing machine and dryer (thread-safe): sample() from the poller, the views from the web."""

    def __init__(self, folder: str, tz=None, clock: Callable[[], float] = time.time):
        self.folder, self.tz = folder, tz
        self._lock = threading.Lock()
        self._plugs: dict[str, dict] = {}   # plug id -> what it did lately (_fresh) and the run going on
        self._failed = False
        self._dirty = False                 # a run began or ended: laufend.json is written at the next sample
        self._saved_minute: int | None = None
        notices = self._read_json(os.path.join(folder, NOTICES), list)
        self._notices: list[dict] = [n for n in notices if isinstance(n, dict) and {"id", "plug", "art", "at"} <= set(n)]
        now = clock()
        for pid, saved in self._read_json(os.path.join(folder, RUNNING), dict).items():   # runs before the restart
            try:
                p = _fresh(saved["kind"], float(saved["t"]))
                p.update({k: saved[k] for k in SAVED})
                start = float(p["run"]["start"])
            except (KeyError, TypeError, ValueError):
                continue
            if p["kind"] not in KINDS:
                continue
            if now - p["t"] <= RESUME_GAP:
                self._plugs[pid] = p   # goes on with the next sample
            else:   # the app was away too long: the run as far as it saw it, without „fertig“
                log.warning("Durchgänge: %s lief, als die App aus ging – er endet, wo sie ihn zuletzt sah", pid)
                self._end(pid, p, p["run"]["quiet"] or max(p["t"], start), done=False)
        self._dirty = True

    # ------------------------------------------------------------------ following the plugs
    def sample(self, now: float, plugs: list[dict]) -> None:
        """One poll: plugs = [{id, art, at (when it reported, None: not yet), w (power) or None}] – every plug with a
        washing machine or a dryer; one missing from it is followed no more."""
        with self._lock:
            seen = set()
            for r in plugs:
                if r.get("art") not in KINDS:
                    continue
                pid = r["id"]
                seen.add(pid)
                p = self._plugs.get(pid)
                if p is None or p["kind"] != r["art"]:
                    p = self._plugs[pid] = _fresh(r["art"], now)
                at = r.get("at")
                if isinstance(at, (int, float)) and p["heard"] < at <= now + 60:
                    at = min(float(at), now)
                    self._advance(pid, p, at)   # the power before holds until this report
                    p["heard"] = at
                    w = r.get("w")
                    p["w"] = max(0.0, float(w)) if isinstance(w, (int, float)) else None   # -0,1 W: nothing
                if now - p["heard"] > STALE:   # silent: what it draws is not known
                    if p["run"] is not None:
                        log.warning("Durchgänge: %s meldet sich seit %d Minuten nicht – der Durchgang endet ohne "
                                    "„fertig“", pid, (now - p["heard"]) // 60)
                        self._end(pid, p, p["run"]["quiet"] or max(p["heard"], p["run"]["start"]), done=False)
                    p["w"] = None
                self._advance(pid, p, now)
            for pid in [x for x in self._plugs if x not in seen]:   # gone, or something else hangs on it now
                self._plugs.pop(pid)
                self._dirty = True
            self._save_running(now)

    def _advance(self, pid: str, p: dict, until: float) -> None:
        """The power the plug reported last, from where it was counted to `until` – minute by minute."""
        t = p["t"]
        if until <= t:
            return
        p["t"] = until
        if p["w"] is None:   # not known: nothing to count, a start begins anew
            p["min_t"] = p["above_since"] = None
            p["lead"].clear()
            p["above"].clear()
            return
        w = p["w"]
        while t < until:
            minute = int(t // 60) * 60
            if p["min_t"] != minute:
                self._close_minute(p)
                p.update(min_t=minute, min_e=0.0, min_cov=0.0, min_peak=0.0, min_heat=0.0)
            end = min(until, minute + 60)
            self._segment(pid, p, t, end, w)
            t = end

    @staticmethod
    def _close_minute(p: dict) -> None:   # a finished minute: to the run's curve, or kept for a start
        if p["min_t"] is None or not p["min_cov"]:
            return
        if p["run"] is not None:
            p["run"]["minutes"].append(round(p["min_e"] / p["min_cov"], 1))
        else:
            p["lead"].append((p["min_t"], p["min_e"], p["min_cov"], p["min_peak"], p["min_heat"]))

    def _segment(self, pid: str, p: dict, a: float, b: float, w: float) -> None:
        """w from a to b (within one minute)."""
        dt, run = b - a, p["run"]
        if run is not None:
            self._rest(p, run, a, b, w)   # before this energy: the rest begins with what was counted until then
        p["min_e"] += w * dt
        p["min_cov"] += dt
        p["min_peak"] = max(p["min_peak"], w)
        if w > HEAT_W:
            p["min_heat"] += dt
        if run is not None:
            run["e"] += w * dt
            run["w_max"] = max(run["w_max"], w)
            if w > HEAT_W:
                run["heat"] += dt
            if not run["confirmed"] and b - run["start"] >= MIN_S:   # it really runs: an old „fertig“ goes
                run["confirmed"] = True
                self._drop_notices(pid)
            if run["quiet"] is not None and b - run["quiet"] >= QUIET_S[p["kind"]]:
                self._end(pid, p, run["quiet"], done=True)
            elif b - run["start"] > MAX_S:
                log.warning("Durchgänge: %s läuft seit %d Stunden ohne Pause – die Grenzen passen nicht zu diesem "
                            "Gerät; der Durchgang endet ohne „fertig“", pid, MAX_S // 3600)
                self._end(pid, p, b, done=False)
                p["blocked"] = True   # no new start before the power was down once
            return
        if p["blocked"]:
            p["blocked"] = w >= END_W
            return
        if w <= START_W:
            p["above_since"] = None
            return
        if p["kind"] == "trockner":   # in one go
            if p["above_since"] is None:
                p["above_since"] = a
            started = b - p["above_since"] >= START_S
        else:                         # pieced together within START_WINDOW
            above = p["above"]
            if above and above[-1][1] >= a:
                above[-1] = (above[-1][0], b)
            else:
                above.append((a, b))
            while above and above[0][1] <= b - START_WINDOW:
                above.popleft()
            started = sum(y - max(x, b - START_WINDOW) for x, y in above) >= START_S
        if started:
            self._start(pid, p)

    @staticmethod
    def _rest(p: dict, run: dict, a: float, b: float, w: float) -> None:
        """Does the device rest? A washing machine below END_W; a dryer without lasting power – a push is no power."""
        if w < END_W:
            run["load"] = None
            if run["quiet"] is None:
                run["quiet"], run["at_quiet"] = a, [run["e"], run["w_max"], run["heat"]]
        elif p["kind"] == "trockner":
            if run["load"] is None:
                run["load"] = a
            if b - run["load"] >= BURST_S:
                run["quiet"] = run["at_quiet"] = None
        else:
            run["quiet"] = run["at_quiet"] = None

    def _start(self, pid: str, p: dict) -> None:
        """It runs – since the first of the minutes with power right before (filling, the drum before it heats)."""
        picked, expected = [], p["min_t"] - 60
        for entry in reversed(p["lead"]):
            minute, e, cov, _, _ = entry
            if minute != expected or not cov or e / cov < END_W:
                break
            picked.append(entry)
            expected -= 60
        picked.reverse()
        p["run"] = {"start": float(picked[0][0] if picked else p["min_t"]),
                    "minutes": [round(e / cov, 1) for _, e, cov, _, _ in picked],
                    "e": sum(x[1] for x in picked) + p["min_e"],
                    "w_max": max([x[3] for x in picked] + [p["min_peak"]]),
                    "heat": sum(x[4] for x in picked) + p["min_heat"],
                    "quiet": None, "at_quiet": None, "load": None, "confirmed": False}
        p["lead"].clear()
        p["above"].clear()
        p["above_since"] = None
        self._dirty = True
        log.debug("Durchgänge: %s läuft", pid)

    def _end(self, pid: str, p: dict, end: float, done: bool) -> None:
        """The run is over at `end`: into the files – and with done („fertig“) a notice for the start page."""
        run, p["run"] = p["run"], None
        p["lead"].clear()
        p["above"].clear()
        p["above_since"] = None
        self._dirty = True
        if run["at_quiet"] and run["quiet"] == end:
            e, w_max, heat = run["at_quiet"]   # what it did until it rested
        else:
            e, w_max, heat = run["e"], run["w_max"], run["heat"]
        seconds = end - run["start"]
        if seconds < MIN_S:
            log.debug("Durchgänge: %s – nach %d Sekunden vorbei, kein Durchgang", pid, seconds)
            return
        count = max(1, math.ceil(seconds / 60))
        minutes = run["minutes"][:count]
        if len(minutes) < count and p["min_cov"]:   # the minute it ended in, as far as it went
            minutes.append(round(p["min_e"] / p["min_cov"], 1))
        item = {"beginn": int(run["start"]), "ende": int(end), "id": pid, "art": p["kind"], "wh": round(e / 3600, 1),
                "w_max": round(w_max, 1), "heiz_min": round(heat / 60)}
        self._store(item, minutes)
        log.debug("Durchgänge: %s fertig", pid)
        if done:
            self._notices = [n for n in self._notices if n["plug"] != pid]
            self._notices.append({"id": f"{pid}-{int(end)}", "plug": pid, "art": p["kind"], "at": float(end),
                                  "start": item["beginn"]})
            self._save_notices()

    # ------------------------------------------------------------------ the notices on the start page
    def notices(self, now: float) -> list[dict]:
        """The open notices „… ist fertig“: [{id, plug, art, at (when it was done), start}], oldest first."""
        with self._lock:
            keep = [n for n in self._notices if now - n["at"] < NOTICE_S]
            if len(keep) != len(self._notices):
                self._notices = keep
                self._save_notices()
            return [dict(n) for n in keep]

    def ok(self, notice_id: str) -> bool:
        """„OK“ on the start page: the notice goes. False: there was none (any more)."""
        with self._lock:
            keep = [n for n in self._notices if n["id"] != notice_id]
            if len(keep) == len(self._notices):
                return False
            self._notices = keep
            self._save_notices()
            return True

    def forget(self, pid: str) -> None:
        """The plug is gone, or something else hangs on it now: its run going on and its notices go (the finished runs
        stay in the files)."""
        with self._lock:
            if self._plugs.pop(pid, None) is not None:
                self._dirty = True
            self._drop_notices(pid)

    def _drop_notices(self, pid: str) -> None:
        keep = [n for n in self._notices if n["plug"] != pid]
        if len(keep) != len(self._notices):
            self._notices = keep
            self._save_notices()

    # ------------------------------------------------------------------ views
    def running(self, pid: str, now: float) -> dict | None:
        """The run going on at this plug: {since, minutes, wh} – None: none."""
        with self._lock:
            p = self._plugs.get(pid)
            run = p["run"] if p else None
            if run is None:
                return None
            return {"since": run["start"], "minutes": max(0, int((now - run["start"]) // 60)),
                    "wh": round(run["e"] / 3600, 1)}

    def between(self, start: float, end: float, ids=None) -> list[dict]:
        """The finished runs that began from start to end (seconds since 1970), oldest first."""
        first = datetime.fromtimestamp(start, self.tz).year
        last = datetime.fromtimestamp(max(start, end - 1), self.tz).year
        with self._lock:
            found = [r for year in range(first, last + 1) for r in self._read_year(year)
                     if start <= r["beginn"] < end and (ids is None or r["id"] in ids)]
        return sorted(found, key=lambda r: r["beginn"])

    def of(self, pid: str) -> list[dict]:
        """Every finished run of one plug, oldest first."""
        with self._lock:
            try:
                years = sorted(int(m[1]) for name in os.listdir(self.folder) if (m := YEAR_FILE.fullmatch(name)))
            except OSError:
                years = []
            found = [r for year in years for r in self._read_year(year) if r["id"] == pid]
        return sorted(found, key=lambda r: r["beginn"])

    def curve(self, pid: str, beginn: int) -> list[float] | None:
        """The average power of every minute of a run (W), as it went into the file."""
        local = datetime.fromtimestamp(beginn, self.tz)
        with self._lock:
            curves = self._read_json(os.path.join(self.folder, f"kurven-{local:%Y-%m}.json"), dict)
        values = curves.get(f"{pid}@{int(beginn)}")
        return [float(v) for v in values if isinstance(v, (int, float))] if isinstance(values, list) else None

    def flush(self) -> None:   # the app stops: where the runs going on are
        with self._lock:
            self._dirty = True
            self._save_running(time.time())

    # ------------------------------------------------------------------ files
    def _read_year(self, year: int) -> list[dict]:
        path = os.path.join(self.folder, f"durchgaenge-{year}.csv")
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
        except FileNotFoundError:
            return []
        except (OSError, csv.Error, UnicodeDecodeError) as e:
            log.warning("Durchgänge: %s lässt sich nicht lesen (%s)", path, e)
            return []
        out = []
        for row in rows:
            try:
                out.append({"beginn": int(row["beginn"]), "ende": int(row["ende"]), "id": row["id"], "art": row["art"],
                            "wh": float(row["wh"]), "w_max": float(row["w_max"]), "heiz_min": int(row["heiz_min"])})
            except (KeyError, TypeError, ValueError):
                continue   # a line cut short (power cut while writing)
        return out

    def _store(self, item: dict, minutes: list[float]) -> None:
        local = datetime.fromtimestamp(item["beginn"], self.tz)
        try:
            os.makedirs(self.folder, exist_ok=True)
            path = os.path.join(self.folder, f"durchgaenge-{local:%Y}.csv")
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            with open(path, "a", encoding="utf-8", newline="") as fh:
                out = csv.writer(fh)
                if new:
                    out.writerow(FIELDS)
                out.writerow([item[k] for k in FIELDS])
            curves_path = os.path.join(self.folder, f"kurven-{local:%Y-%m}.json")
            curves = self._read_json(curves_path, dict)
            curves[f"{item['id']}@{item['beginn']}"] = minutes
            self._write_json(curves_path, curves)
            self._failed = False
        except OSError as e:
            self._warn(e)

    def _save_notices(self) -> None:
        try:
            os.makedirs(self.folder, exist_ok=True)
            self._write_json(os.path.join(self.folder, NOTICES), self._notices)
        except OSError as e:
            self._warn(e)

    def _save_running(self, now: float) -> None:
        """laufend.json: every minute while a run goes on, and when one begins or ends."""
        minute = int(now // 60)
        runs = {pid: {k: (list(p[k]) if isinstance(p[k], deque) else p[k]) for k in SAVED}
                for pid, p in self._plugs.items() if p["run"] is not None}
        if not self._dirty and (not runs or minute == self._saved_minute):
            return
        self._dirty, self._saved_minute = False, minute
        path = os.path.join(self.folder, RUNNING)
        try:
            if runs:
                os.makedirs(self.folder, exist_ok=True)
                self._write_json(path, runs)
            elif os.path.exists(path):
                os.remove(path)
        except OSError as e:
            self._warn(e)

    def _warn(self, e: OSError) -> None:   # once, until writing works again
        if not self._failed:
            self._failed = True
            log.warning("Durchgänge: %s lässt sich nicht schreiben (%s)", self.folder, e)

    @staticmethod
    def _read_json(path: str, kind: type):
        try:
            with open(path, encoding="utf-8") as fh:
                value = json.load(fh)
        except FileNotFoundError:
            return kind()
        except (OSError, ValueError) as e:
            log.warning("Durchgänge: %s lässt sich nicht lesen (%s)", path, e)
            return kind()
        return value if isinstance(value, kind) else kind()

    @staticmethod
    def _write_json(path: str, value) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
