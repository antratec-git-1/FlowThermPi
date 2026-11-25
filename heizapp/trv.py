"""Ein SONOFF-Thermostat (TRV Gen2, Modell TRV-ZBT) über Zigbee2MQTT – mit derselben Schnittstelle wie eine AeroFlow
(heizapp.device.Heater), damit Räume, Knöpfe, Heizpläne und Kalibrierung für beide gleich gehen.

Die Werte kommen aus dem, was Zigbee2MQTT über das Thermostat meldet (heizapp.zigbee_net hält sie). Befehle gehen als
zigbee2mqtt/<IEEE-Adresse>/set hinaus; ein Befehl gilt erst als angekommen, wenn Zigbee2MQTT danach den neuen Wert
meldet. Das Thermostat fragt alle 2 Sekunden beim Stick nach, ob etwas für es da ist – Befehle brauchen also ein paar
Sekunden. Die Namen der Werte stehen in der Gerätedefinition (zigbee-herdsman-converters, src/devices/sonoff.ts,
gelesen am 25.11.2026):

  AeroFlow (App)            Zigbee2MQTT beim TRV-ZBT
  room_temp                 local_temperature (°C)
  setpoint, manual_sp       occupied_heating_setpoint (5–30 °C, Schritt 0,5)
  operating_mode 0 / 1      system_mode "heat" / "auto" (dazu schedule_active_group "1" … "3")
  operating_mode 2 (Boost)  temporary_mode_mode "boost", temporary_mode_duration (Minuten, höchstens 180)
  Aus                       system_mode "off" (das Thermostat hält dann seinen Frostschutz) – hier operating_mode 5
  antifrost_sp              frost_protection_temperature (5–15 °C)
  heater_state              running_state "heat" / "idle"
  open_window               open_window_detected (true / false)
  temp_corr                 local_temperature_calibration (−10 … +10 °C, Schritt 0,2)
  heating_plan              schedule_active_group "1" … "3"
  Heizplan                  schedule_group_to_edit + weekly_schedule_monday … _sunday: "HH:mm/Temperatur …",
                            erster Punkt 00:00, höchstens 12 Punkte pro Tag, 5–30 °C in 0,5-°C-Schritten

Erreichbar ist ein Thermostat, solange es sich in den letzten 2 Stunden gemeldet hat (Zigbee2MQTT schreibt die Zeit
als last_seen dazu). Ist es 30 Minuten still, fragt die App einmal nach seiner Temperatur (…/get) – höchstens alle
30 Minuten. Die Uhr stellt Zigbee2MQTT selbst; Heizpläne aus dem Thermostat auslesen kann die App noch nicht.
"""
from __future__ import annotations

import logging
import threading
import time

from . import coap
from .device import DeviceError

log = logging.getLogger("heizapp.zigbee")
MODEL = "TRV-ZBT"
OFF = 5                    # operating_mode of a thermostat that is switched off (the AeroFlow has no such mode)
DAY_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")   # day 1 … 7 of the app
DAY_NAMES = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
MAX_POINTS = 12            # change points per day in a plan of the thermostat
END_OF_DAY = 23 * 60 + 59  # a window of the app ending at 23:59 lasts until midnight
DEFAULT_FROST = 50         # 0.1 °C: the frost protection in a plan's gaps while the thermostat has not said its own
QUERY_EVERY = 30 * 60      # seconds of silence after which the app asks the thermostat – and at most this often
LOST_AFTER = 2 * 3600      # seconds without a sign of life after which it counts as not reachable
ANSWER = 20.0              # seconds a command may take until Zigbee2MQTT reports the new value
PLAN_ANSWER = 60.0         # … a heating plan (one radio message per different day)
BOOST_MAX = 180            # minutes
BATTERY_LOW = 20           # per cent: „Batterie bald wechseln“
CORR_STEP, CORR_MAX = 2, 100   # 0.1 °C: the correction goes in 0.2 °C steps up to ±10 °C
FAULTS = {"temperature_sensor_issue_detected": "Temperaturfühler gestört",
          "valve_adjustment_issue_detected": "Ventil lässt sich nicht richtig stellen",
          "battery_too_low_please_replace_the_batteries": "Batterie fast leer",
          "battery_too_low_for_firmware_upgrade": "Batterie zu schwach für ein Update",
          "battery_status_abnormal": "Batterie auffällig",
          "external_temperature_sensor_connection_issue": "externer Fühler meldet sich nicht",
          "unknown": "unbekannte Störung"}
BATTERY_FAULTS = ("battery_too_low_please_replace_the_batteries", "battery_status_abnormal")
NOT_ANSWERED = ("Das Thermostat hat nicht rechtzeitig geantwortet – vielleicht ist es zu weit vom Pi weg. Am Thermostat "
                "einmal die mittlere Taste drücken und noch einmal versuchen.")
# what a field of the AeroFlow is called at the thermostat when the app writes it, and its range and step (0.1 °C)
WRITABLE = {"setpoint": ("occupied_heating_setpoint", 50, 300, 5), "manual_sp": ("occupied_heating_setpoint", 50, 300, 5),
            "temp_corr": ("local_temperature_calibration", -CORR_MAX, CORR_MAX, CORR_STEP),
            "antifrost_sp": ("frost_protection_temperature", 50, 150, 5)}


class NoAnswer(coap.CoapError):
    """The thermostat (or Zigbee2MQTT) cannot be reached – counts like a heater that does not answer."""


def _number(value) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _tenths(value) -> int | None:
    x = _number(value)
    return round(x * 10) if x is not None else None


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def day_points(windows: list[str], frost: int | None) -> list[tuple[int, int]]:
    """One day of an app plan, in the AeroFlow's format ("HH:MM,HH:MM,<0.1 °C>", see plans.to_device), as the
    thermostat's change points: (minute of the day, temperature in 0.1 °C). The first is at 00:00; a gap between
    windows becomes a point at the frost protection – what the AeroFlow holds between its windows; neighbours with
    the same temperature are merged."""
    frost = frost if isinstance(frost, int) else DEFAULT_FROST
    points, at = [], 0
    for start, end, temp in sorted((_minutes(s), _minutes(e), int(t)) for s, e, t in (w.split(",") for w in windows)):
        if start > at:
            points.append((at, frost))
        points.append((start, temp))
        at = end
    if at < END_OF_DAY:
        points.append((at, frost))
    merged: list[tuple[int, int]] = []
    for minute, temp in points:
        if not merged or merged[-1][1] != temp:
            merged.append((minute, temp))
    return merged


def schedule_text(points: list[tuple[int, int]]) -> str:
    """[(0, 170), (360, 220)] -> "00:00/17 06:00/22" – as Zigbee2MQTT wants weekly_schedule_<day>."""
    return " ".join(f"{m // 60:02d}:{m % 60:02d}/{t / 10:g}" for m, t in points)


def plan_problems(days: dict[int, list[str]], frost: int | None) -> list[str]:
    """Days of a plan with more change points than the thermostat can keep, in words – empty if it fits."""
    return [f"{DAY_NAMES[d - 1]} hätte {n} Wechsel, das Thermostat kann höchstens {MAX_POINTS}"
            for d in range(1, 8) if (n := len(day_points(days.get(d, []), frost))) > MAX_POINTS]


def _same(have, want) -> bool:
    if _number(want) is not None:
        return _number(have) is not None and abs(have - want) < 0.05
    return have == want


def _matches(values: dict, want: dict) -> bool:
    return all(_same(values.get(k), x) for k, x in want.items())


class Thermostat:
    """Like heizapp.device.Heater – the same reads, writes and modes, in the AeroFlow's units (0.1 °C) – but over
    Zigbee2MQTT. net is the app's connection to it (heizapp.zigbee_net.ZigbeeNet)."""

    writable = True   # needs no token
    answer, plan_answer = ANSWER, PLAN_ANSWER   # seconds to wait for Zigbee2MQTT's report of the new values

    def __init__(self, ieee: str, net):
        self.ieee, self.net = ieee, net
        self._lock = threading.Lock()      # one command at a time per thermostat
        self._asked = 0.0                  # time.time() of the last question „are you there?“
        self._boost_started: float | None = None   # when the app started the boost that runs now
        self._boost_asked = False          # the boost should be over: asked once whether it is

    # ---------------------------------------------------------------- what Zigbee2MQTT knows
    def _state(self) -> dict:
        """The thermostat's latest values and when it was last heard; NoAnswer if it is out of reach."""
        problem = self.net.problem()
        if problem:
            raise NoAnswer(problem)
        st = self.net.thermostat(self.ieee)
        if st is None or st["device"] is None:
            raise NoAnswer("Das Thermostat ist nicht (mehr) im Zigbee-Netz angelernt – unter „Zigbee“ neu anlernen "
                           "oder hier entfernen.")
        if not st["values"]:
            raise NoAnswer("Das Thermostat hat noch keine Werte gemeldet.")
        silent = time.time() - st["heard"]
        if silent >= QUERY_EVERY and time.time() - self._asked >= QUERY_EVERY:
            self._asked = time.time()
            self.net.send(self.ieee, {"local_temperature": ""}, "get")   # its answer brings it back, if it hears us
        if silent >= LOST_AFTER:
            raise NoAnswer(f"Das Thermostat hat sich seit {int(silent // 3600)} Stunden nicht gemeldet – am Thermostat "
                           "einmal die mittlere Taste drücken.")
        return st

    def _boost_left(self, st: dict) -> int | None:
        """Seconds of the running boost still to go – None without one. The thermostat does not count down where
        Zigbee2MQTT can see it, so the app counts from the start of the boost."""
        v = st["values"]
        minutes = _number(v.get("temporary_mode_duration"))
        if v.get("temporary_mode_mode") != "boost" or minutes is None:
            self._boost_started, self._boost_asked = None, False
            return None
        since = self._boost_started or st.get("boost_since") or st["heard"]
        left = round(minutes * 60 - (time.time() - since))
        if left > 0:
            return left
        if not self._boost_asked:   # it should have ended by itself: ask, its answer shows
            self._boost_asked = True
            self.net.send(self.ieee, {"temporary_mode_mode": ""}, "get")
        return None

    def _translate(self, st: dict) -> dict:
        v, device = st["values"], st["device"] or {}
        mode, left, group = v.get("system_mode"), self._boost_left(st), v.get("schedule_active_group")
        return {
            "room_temp": _tenths(v.get("local_temperature")),
            "setpoint": _tenths(v.get("occupied_heating_setpoint")),
            "manual_sp": _tenths(v.get("occupied_heating_setpoint")),
            "heater_state": 1 if v.get("running_state") == "heat" else 0,
            "operating_mode": OFF if mode == "off" else 2 if left else {"auto": 1, "heat": 0}.get(mode),
            "open_window": 1 if v.get("open_window_detected") is True else 0,
            "boost_remaining": f"{left // 60}:{left % 60:02d}" if left else None,
            "antifrost_sp": _tenths(v.get("frost_protection_temperature")),
            "heating_plan": int(group) - 1 if group in ("1", "2", "3") else None,
            "temp_corr": _tenths(v.get("local_temperature_calibration")),
            "child_lock_en": {"LOCK": 1, "UNLOCK": 0}.get(v.get("child_lock")),
            "active_fw_ver": device.get("firmware"),
            "idu": self.ieee,
        }

    def read(self, names) -> dict:
        values = self._translate(self._state())
        return {n: values.get(n) for n in names}

    def get(self, name: str):
        return self.read((name,))[name]

    def info(self) -> dict:
        """What the setup shows beyond the AeroFlow's values: battery, radio, faults, firmware update."""
        st = self.net.thermostat(self.ieee) or {}
        v, device = st.get("values") or {}, st.get("device")
        battery = _number(v.get("battery"))
        codes = [c.strip() for c in str(v.get("fault_status") or "").split(",") if c.strip() not in ("", "none")]
        update = v.get("update") if isinstance(v.get("update"), dict) else {}
        return {"battery": round(battery) if battery is not None else None,
                "battery_low": (battery is not None and battery <= BATTERY_LOW) or any(c in BATTERY_FAULTS for c in codes),
                "signal": _number(v.get("linkquality")), "faults": [FAULTS.get(c, FAULTS["unknown"]) for c in codes],
                "update": update.get("state") == "available", "paired": device is not None,
                "model": " ".join(filter(None, ((device or {}).get("vendor"), (device or {}).get("model")))) or MODEL,
                "heard": st.get("heard") or None}

    # ---------------------------------------------------------------- commands
    def _send(self, payload: dict, timeout: float | None = None, force: bool = False) -> None:
        """Send payload as one command and wait until Zigbee2MQTT reports exactly these values. Nothing is sent if
        they are so already (force: send anyway, e.g. a boost that starts again)."""
        with self._lock:
            st = self.net.thermostat(self.ieee)
            if not force and st and st["values"] and _matches(st["values"], payload):
                return
            seq = self.net.send(self.ieee, payload)
            if seq is None:
                raise NoAnswer(self.net.problem() or "Keine Verbindung zu Zigbee2MQTT.")
            if self.net.wait(self.ieee, seq, lambda values: _matches(values, payload), timeout or self.answer) is None:
                log.warning("Zigbee: Thermostat %s hat %s nicht bestätigt", self.ieee, ", ".join(payload))
                raise NoAnswer(NOT_ANSWERED)
        log.debug("Zigbee: Thermostat %s: %s bestätigt", self.ieee, payload)

    def set(self, name: str, value, verify: bool = True) -> None:
        """Write one field (in the AeroFlow's units), rounded to the thermostat's step."""
        if name not in WRITABLE:
            raise DeviceError(f"{name}: beim Thermostat nicht möglich")
        key, low, high, step = WRITABLE[name]
        tenths = int(round(int(value) / step)) * step
        if not low <= tenths <= high:
            raise DeviceError(f"{name}: {value} liegt außerhalb von {low / 10:g} … {high / 10:g} °C")
        self._send({key: tenths / 10})

    def ensure(self, name: str, value) -> None:
        self.set(name, value)   # set() sends nothing if the thermostat has the value already

    def set_mode(self, mode: str, *, plan: int = 1, boost_minutes: int = 30) -> None:
        """manual, auto (with plan 1 … 3 of the thermostat), boost, boost_off, off – as Heater.set_mode. Every mode but
        the boost also ends a running boost, as on the AeroFlow."""
        values = (self.net.thermostat(self.ieee) or {}).get("values") or {}
        temporary = values.get("temporary_mode_mode") in ("boost", "timer")
        end = {"temporary_mode_mode": "disabled"} if temporary else {}
        if mode == "manual":
            self._send({"system_mode": "heat", **end})
        elif mode == "auto":
            self._send({"system_mode": "auto", "schedule_active_group": str(plan), **end})
        elif mode == "boost":
            self._send({"temporary_mode_mode": "boost",
                        "temporary_mode_duration": max(1, min(BOOST_MAX, int(boost_minutes)))}, force=True)
            self._boost_started, self._boost_asked = time.time(), False
        elif mode == "boost_off":
            if temporary:
                self._send({"temporary_mode_mode": "disabled"})
        elif mode == "off":
            self._send({"system_mode": "off", **end})
        else:
            raise ValueError(f"unbekannter Modus {mode!r}")

    # ---------------------------------------------------------------- heating plans
    def write_plan(self, plan: int, days: dict[int, list[str]]) -> list[int]:
        """Write a week (the AeroFlow's windows per day, see plans.to_device) into plan 1 … 3 of the thermostat;
        returns the days that had to change. Raises DeviceError if a day has more than 12 change points."""
        st = self._state()
        frost = self._translate(st)["antifrost_sp"]
        problems = plan_problems(days, frost)
        if problems:
            raise DeviceError("; ".join(problems))
        texts = {d: schedule_text(day_points(days.get(d, []), frost)) for d in range(1, 8)}
        values = st["values"]
        known = values.get("schedule_group_to_edit") == str(plan)   # the days in its state belong to this plan
        changed = [d for d in range(1, 8) if not (known and values.get(f"weekly_schedule_{DAY_KEYS[d - 1]}") == texts[d])]
        if changed:
            self._send({"schedule_group_to_edit": str(plan),
                        **{f"weekly_schedule_{DAY_KEYS[d - 1]}": texts[d] for d in changed}},
                       timeout=self.plan_answer, force=True)
        return changed

    def read_plan(self, plan: int) -> dict[int, list[str]]:
        raise DeviceError("Beim Thermostat kann die App den Heizplan noch nicht auslesen.")

    def set_clock(self, now) -> float:
        raise DeviceError("Das Thermostat bekommt die Uhrzeit über Zigbee.")
