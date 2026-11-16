"""Heating plans as kept in the app: validation and conversion from/to device windows.

A plan has a name and up to 10 windows per weekday (1 = Monday … 7 = Sunday).
Each window: start, end ("HH:MM", end at most 23:59) and a temperature in 0.5 °C steps.
The editor offers a 15-minute grid; plans read from a heater may use any minute.
Between windows the heater holds its frost-protection temperature, so a "reduced"
night temperature has to be an explicit window.
"""
from __future__ import annotations

import hashlib
import json
import re

DAY_NAMES = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MAX_WINDOWS = 10
MIN_TEMP, MAX_TEMP = 5.0, 30.0
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class PlanError(ValueError):
    pass


def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _check_time(value: str, day: str, *, is_end: bool) -> str:
    value = str(value or "").strip()
    if is_end and value == "24:00":
        value = "23:59"
    if not _TIME.match(value):
        raise PlanError(f"{day}: Uhrzeit „{value}“ bitte als HH:MM angeben.")
    return value


def normalize(raw: dict) -> dict:
    """Validate a plan sent by the UI and return a clean copy (raises PlanError in German)."""
    name = str(raw.get("name") or "").strip()
    if not name:
        raise PlanError("Der Heizplan braucht einen Namen.")
    days_in = raw.get("days") or {}
    days: dict[str, list[dict]] = {}
    for d in range(1, 8):
        label = DAY_NAMES[d - 1]
        windows = []
        for w in days_in.get(str(d)) or days_in.get(d) or []:
            start = _check_time(w.get("start"), label, is_end=False)
            end = _check_time(w.get("end"), label, is_end=True)
            try:
                temp = float(str(w.get("temp")).replace(",", "."))
            except ValueError:
                raise PlanError(f"{label}: Temperatur fehlt oder ist keine Zahl.") from None
            if not MIN_TEMP <= temp <= MAX_TEMP:
                raise PlanError(f"{label}: Temperatur {temp:g} °C liegt außerhalb {MIN_TEMP:g}–{MAX_TEMP:g} °C.")
            if temp * 2 != round(temp * 2):
                raise PlanError(f"{label}: Temperatur bitte in 0,5-°C-Schritten.")
            if to_minutes(end) <= to_minutes(start):
                raise PlanError(f"{label}: Ende {end} muss nach dem Start {start} liegen.")
            windows.append({"start": start, "end": end, "temp": temp})
        windows.sort(key=lambda w: to_minutes(w["start"]))
        for a, b in zip(windows, windows[1:]):
            if to_minutes(b["start"]) < to_minutes(a["end"]):
                raise PlanError(f"{label}: {a['start']}–{a['end']} und {b['start']}–{b['end']} überschneiden sich.")
        if len(windows) > MAX_WINDOWS:
            raise PlanError(f"{label}: höchstens {MAX_WINDOWS} Zeitfenster pro Tag.")
        days[str(d)] = windows
    return {"name": name, "days": days}


def to_device(plan: dict) -> dict[int, list[str]]:
    """Convert to the device format "HH:MM,HH:MM,<temperature in 0.1 °C>" per weekday."""
    return {
        d: [f"{w['start']},{w['end']},{round(w['temp'] * 10)}" for w in plan["days"].get(str(d), [])]
        for d in range(1, 8)
    }


def from_device(name: str, days: dict[int, list[str]]) -> tuple[dict, int]:
    """Build an app plan from device windows ("HH:MM,HH:MM,<0.1 °C>").

    Values are taken over as they are (no range check), so nothing is changed silently;
    returns (plan, number of unreadable windows that were skipped).
    """
    skipped = 0
    out: dict[str, list[dict]] = {}
    for d in range(1, 8):
        windows = []
        for raw in days.get(d, []):
            try:
                start, end, temp = str(raw).split(",")
                if not (_TIME.match(start) and _TIME.match(end)):
                    raise ValueError(raw)
                windows.append({"start": start, "end": end, "temp": int(temp) / 10})
            except ValueError:
                skipped += 1
        out[str(d)] = sorted(windows, key=lambda w: to_minutes(w["start"]))
    return {"name": name, "days": out}, skipped


def fingerprint(plan: dict) -> str:
    """Stable hash of the windows, to tell whether a sent plan was edited afterwards."""
    return hashlib.sha256(json.dumps(plan["days"], sort_keys=True).encode()).hexdigest()[:16]
