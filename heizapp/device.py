"""One FlexiSmart Pro / Thermalink Pro thermostat, driven via its local CoAP JSON-RPC interface.

Behaviour verified on the real device (firmware 00.01.08, 15.11.2026) — partly deviating
from Thermotec's "Lokale Datenschnittstelle" (Ausgabe 04.09.2026):
- resource is /Json (capital J); /json answers 4.04
- reads need no token; writes need the Uri-Query "token=<pop>" (pop = 24 chars from the
  setup QR code). "pop=…", a bare token or a wrong token -> 4.01
- a write must NOT carry a "jsonrpc" member, otherwise it is silently ignored (2.05, empty)
- heating plans: 3 plans x 7 days x 10 windows (doc says 6), value "HH:MM,HH:MM,<0.1 °C>",
  "00:00,00:00,0" = empty, end 24:00 -> error, overlapping windows are silently dropped
- cmd clear_plan answers -31001, so plans are cleared window by window
- modes are switched by writing operating_mode (doc: read-only); auto_mode_en, boost_mode_en,
  holiday_mode_en, standby_mode_en are only the menu's enable switches
- temp_corr (0.1 K, ±99) is writable and room_temp is reported WITH the correction applied
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Callable

from . import coap, logs

EMPTY_WINDOW = "00:00,00:00,0"
WINDOWS_PER_DAY = 10
DAYS = range(1, 8)
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
ERROR_TEXT = {-31000: "Vorgang abgelehnt", -31001: "Vorgang fehlgeschlagen", -31002: "Zeitüberschreitung"}
MODES = {0: "manual", 1: "auto", 2: "boost", 3: "holiday", 4: "standby"}
log = logging.getLogger(__name__)

FAST_FIELDS = ("room_temp", "setpoint", "heater_state", "operating_mode", "open_window", "boost_remaining")
SLOW_FIELDS = ("manual_sp", "antifrost_sp", "sp_limit", "auto_mode_en", "heating_plan", "boost_mode_en",
               "boost_mode_time", "holiday_mode_en", "child_lock_en", "system_time", "active_fw_ver", "temp_corr")
IDENTITY_FIELDS = ("idu", "mac_addr")


class DeviceError(Exception):
    """The heater answered, but refused or failed."""


class AuthError(DeviceError):
    pass


class ReadOnlyError(DeviceError):
    pass


class NotApplied(DeviceError):
    pass


class Heater:
    def __init__(self, host: str, token: str | None = None, port: int = coap.DEFAULT_PORT,
                 timeout: float = 1.5, retries: int = 2):
        self.host = host
        self.port = port
        self.token = token or None
        logs.keep_secret(self.token)   # never in the log, whatever message it may end up in
        self.timeout = timeout
        self.retries = retries
        self._lock = threading.Lock()  # one request at a time per device
        self._mode_before_boost = 0

    @property
    def writable(self) -> bool:
        return self.token is not None

    # ---------------------------------------------------------------- transport
    def _call(self, method: str, name: str, value=None, *, with_value: bool = False) -> dict:
        params = {"name": name}
        if with_value:
            params["value"] = value
        body = json.dumps({"id": 1, "method": method, "params": params}, separators=(",", ":")).encode()
        write = method != "get"
        if write and not self.token:
            raise ReadOnlyError("kein Token hinterlegt – Heizung ist nur lesbar")
        query = [f"token={self.token}"] if write else None
        started = time.monotonic()
        with self._lock:
            code, payload = coap.request(self.host, "PUT" if write else "GET", "Json", body, query=query,
                                         port=self.port, timeout=self.timeout, retries=self.retries)
        if write:   # the detailed log: every write, never the token
            log.debug("%s: %s = %r → %s %s (%.2f s)", self.host, name, value, code,
                      payload[:120].decode("utf-8", "replace") if payload else "(leer)", time.monotonic() - started)
        if code == "4.01":
            raise AuthError("Token abgelehnt – Token aus dem QR-Code prüfen")
        if not code.startswith("2."):
            raise DeviceError(f"{name}: CoAP-Fehler {code}")
        if not payload:
            raise NotApplied(f"{name}: Heizung hat den Befehl ignoriert")
        try:
            answer = json.loads(payload)
        except ValueError as exc:
            raise DeviceError(f"{name}: ungültige Antwort") from exc
        if "error" in answer:
            err = (answer.get("error") or {}).get("code")
            raise DeviceError(f"{name}: {ERROR_TEXT.get(err, 'Fehler')} ({err})")
        return answer.get("result") or {}

    def get(self, name: str):
        return self._call("get", name).get("value")

    def check_token(self) -> None:
        """Raise AuthError if the device rejects the token (a read that carries ?token=…)."""
        body = json.dumps({"id": 1, "method": "get", "params": {"name": "idu"}}, separators=(",", ":")).encode()
        with self._lock:
            code, _payload = coap.request(self.host, "GET", "Json", body, query=[f"token={self.token}"],
                                          port=self.port, timeout=self.timeout, retries=self.retries)
        if code == "4.01":
            raise AuthError("Token abgelehnt")
        if not code.startswith("2."):
            raise DeviceError(f"CoAP-Fehler {code}")

    def read(self, names) -> dict:
        return {n: self.get(n) for n in names}

    def set(self, name: str, value, verify: bool = True) -> None:
        self._call("set", name, value, with_value=True)
        if verify:
            back = self.get(name)
            if back != value:
                raise NotApplied(f"{name}: {value!r} geschrieben, aber {back!r} gelesen")

    def ensure(self, name: str, value) -> None:
        """Write only if the device has a different value (saves writes)."""
        if self.get(name) != value:
            self.set(name, value)

    # ---------------------------------------------------------------- clock
    def set_clock(self, now: Callable[[], datetime]) -> float:
        """Set system_time to local time now(); returns remaining deviation in seconds."""
        self.set("system_time", now().strftime(TIME_FORMAT), verify=False)
        back = datetime.strptime(self.get("system_time"), TIME_FORMAT)
        deviation = (back - now()).total_seconds()
        if abs(deviation) > 10:
            raise NotApplied(f"Uhr nicht übernommen (Heizung zeigt {back:%d.%m.%Y %H:%M:%S})")
        return deviation

    # ---------------------------------------------------------------- heating plans
    def read_day(self, plan: int, day: int) -> list[str]:
        return [self.get(f"heat_win_{plan}_{day}_{w}") for w in range(1, WINDOWS_PER_DAY + 1)]

    def read_plan(self, plan: int) -> dict[int, list[str]]:
        """Non-empty windows of device plan slot 1..3, per weekday (70 reads, ~2–3 s)."""
        return {day: [w for w in self.read_day(plan, day) if w != EMPTY_WINDOW] for day in DAYS}

    def write_plan(self, plan: int, days: dict[int, list[str]]) -> list[int]:
        """Write a whole week into device plan slot 1..3; returns the days that had to change.

        Per day: compare, then clear old windows from last to first (so they can never block
        the new ones) and write the new, already validated windows in ascending order.
        Every changed day is read back, because the device silently drops invalid windows.
        """
        changed = []
        for day in DAYS:
            wanted = list(days.get(day, []))
            if len(wanted) > WINDOWS_PER_DAY:
                raise DeviceError(f"Tag {day}: mehr als {WINDOWS_PER_DAY} Zeitfenster")
            wanted += [EMPTY_WINDOW] * (WINDOWS_PER_DAY - len(wanted))
            current = self.read_day(plan, day)
            if current == wanted:
                continue
            for w in range(WINDOWS_PER_DAY, 0, -1):
                if current[w - 1] != EMPTY_WINDOW:
                    self.set(f"heat_win_{plan}_{day}_{w}", EMPTY_WINDOW, verify=False)
            for w, value in enumerate(wanted, start=1):
                if value != EMPTY_WINDOW:
                    self.set(f"heat_win_{plan}_{day}_{w}", value, verify=False)
            if self.read_day(plan, day) != wanted:
                raise NotApplied(f"Tag {day}: Heizung hat den Plan nicht vollständig übernommen")
            changed.append(day)
        return changed

    # ---------------------------------------------------------------- operating modes
    # Verified on the device: the *_mode_en fields are only the "Freigabe" switches from the menu
    # (they make a mode selectable with the MODUS key). The mode itself is switched by writing
    # operating_mode (0 manual, 1 plan/auto, 2 boost, 3 holiday, 4 standby), which the doc lists
    # as read-only.
    def set_mode(self, mode: str, *, plan: int = 1, boost_minutes: int = 30) -> None:
        current = self.get("operating_mode")
        if mode == "manual":
            self.set("operating_mode", 0)
        elif mode == "auto":
            self.ensure("heating_plan", plan - 1)
            self.ensure("auto_mode_en", 1)
            self.set("operating_mode", 1)
        elif mode == "boost":
            minutes = max(5, min(95, int(boost_minutes) // 5 * 5))
            if current != 2:
                self._mode_before_boost = current if current in (0, 1) else 0
            self.ensure("boost_mode_time", minutes)
            self.ensure("boost_mode_en", 1)
            self.set("operating_mode", 2)
        elif mode == "boost_off":
            if current == 2:
                self.set("operating_mode", self._mode_before_boost)
        elif mode == "off":
            # "Aus" = manual mode on the frost-protection temperature
            self.set("operating_mode", 0)
            self.set("manual_sp", self.get("antifrost_sp"))
        else:
            raise ValueError(f"unbekannter Modus {mode!r}")
