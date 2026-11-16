"""A simulated FlexiSmart Pro heater (CoAP over UDP on 127.0.0.1) for tests.

Mimics the behaviour measured on the real device on 15.11.2026:
token as Uri-Query "token=…", writes with a "jsonrpc" member silently ignored,
10 windows per day, "24:00" rejected (-31001), windows overlapping the previous
one silently dropped, cmd clear_plan failing with -31001.
"""
from __future__ import annotations

import json
import socket
import threading
from datetime import datetime, timedelta

from heizapp import coap

EMPTY = "00:00,00:00,0"
FMT = "%Y-%m-%d %H:%M:%S"


def _minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


class FakeHeater:
    def __init__(self, token: str = "T" * 24, idu: str = "11111111", room_temp: int = 215):
        self.token = token
        self.values = {
            "room_temp": room_temp, "setpoint": 200, "manual_sp": 200, "heater_state": 0, "operating_mode": 0,
            "open_window": 0, "boost_remaining": "30:00", "antifrost_sp": 50, "sp_limit": 350, "auto_mode_en": 0,
            "heating_plan": 0, "boost_mode_en": 0, "boost_mode_time": 30, "holiday_mode_en": 0, "child_lock_en": 0,
            "active_fw_ver": "00.01.08", "idu": idu, "mac_addr": "aa:bb:cc:dd:ee:ff", "standby_mode_en": 1,
            "heat_algo_hyst": 10, "system_id": "000000", "wifi_en": 1, "temp_corr": 0,
        }
        self.windows = {f"heat_win_{p}_{t}_{f}": EMPTY for p in (1, 2, 3) for t in range(1, 8) for f in range(1, 11)}
        self.clock_offset = timedelta(days=-5000)  # clock wrong like on the real device
        self.writes: list[tuple[str, object]] = []
        self.ignored = 0
        self.online = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        self._stop = True
        self.sock.close()

    # ---------------------------------------------------------------- server loop
    def _serve(self) -> None:
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                return
            if not self.online:
                continue
            try:
                reply = self._handle(coap.decode(data))
            except Exception:  # never crash the fake
                continue
            self.sock.sendto(reply, addr)

    def _reply(self, req: dict, code: int, payload: bytes = b"") -> bytes:
        return coap.encode(coap.ACK, code, req["mid"], req["token"], [], payload)

    def _result(self, req: dict, obj: dict) -> bytes:
        return self._reply(req, 0x45, json.dumps(obj).encode())

    def _handle(self, req: dict) -> bytes:
        path = "/".join(v.decode() for n, v in req["options"] if n == coap.OPT_URI_PATH)
        queries = [v.decode() for n, v in req["options"] if n == coap.OPT_URI_QUERY]
        if path == ".well-known/core":
            return self._reply(req, 0x45, b"</Json>;obs")
        if path != "Json":
            return self._reply(req, 0x84, b"Not Found")
        if queries and queries != [f"token={self.token}"]:
            return self._reply(req, 0x81)
        authed = bool(queries)
        if not req["payload"]:
            return self._reply(req, 0x45)
        body = json.loads(req["payload"])
        method, params = body.get("method"), body.get("params") or {}
        name, rid = params.get("name"), body.get("id")
        if method == "get":
            value = self._get(name)
            if value is None:
                return self._result(req, {"id": rid, "error": {"code": -31001}})
            return self._result(req, {"id": rid, "result": {"name": name, "value": value}})
        if not authed or "jsonrpc" in body:   # like the real device: silently ignored
            self.ignored += 1
            return self._reply(req, 0x45)
        if method == "cmd":
            return self._result(req, {"id": rid, "error": {"code": -31001}})
        if method == "set":
            ok = self._set(name, params.get("value"))
            if ok is False:
                return self._result(req, {"id": rid, "error": {"code": -31001}})
            return self._result(req, {"id": rid, "result": {}})
        return self._result(req, {"id": rid, "error": {"code": -31000}})

    # ---------------------------------------------------------------- data points
    def _get(self, name: str):
        if name == "system_time":
            return (datetime.now() + self.clock_offset).strftime(FMT)
        if name == "room_temp":  # like the real device: reported WITH the correction
            return self.values["room_temp"] + self.values["temp_corr"]
        if name in self.windows:
            return self.windows[name]
        return self.values.get(name)

    def _set(self, name: str, value):
        self.writes.append((name, value))
        if name == "system_time":
            self.clock_offset = datetime.strptime(value, FMT) - datetime.now()
            return True
        if name in self.windows:
            return self._set_window(name, value)
        if name not in self.values:
            return False
        if name == "temp_corr" and not -99 <= value <= 99:
            return False
        if name in ("setpoint", "manual_sp"):
            if name == "setpoint" or self.values["operating_mode"] == 0:
                self.values["setpoint"] = value
            if self.values["operating_mode"] == 0:
                self.values["manual_sp"] = value
        elif name == "operating_mode":
            # like the real device: the mode is switched here; the *_mode_en fields are only enables
            self.values["operating_mode"] = value
            if value == 0:
                self.values["setpoint"] = self.values["manual_sp"]
        else:
            self.values[name] = value
        mode = self.values["operating_mode"]
        shown = self.values["room_temp"] + self.values["temp_corr"]
        self.values["heater_state"] = int(mode == 2 or shown < self.values["setpoint"] - 10)
        return True

    def _set_window(self, name: str, value: str):
        if value == EMPTY:
            self.windows[name] = EMPTY
            return True
        try:
            start, end, temp = value.split(",")
            int(temp)
            if _minutes(end) >= 24 * 60 or _minutes(start) >= 24 * 60:
                return False
        except ValueError:
            return False
        if _minutes(end) <= _minutes(start):
            return True  # accepted but not stored, like overlaps
        _, _, p, t, f = name.split("_")
        for prev in range(int(f) - 1, 0, -1):  # nearest previous non-empty window
            other = self.windows[f"heat_win_{p}_{t}_{prev}"]
            if other != EMPTY:
                if _minutes(start) < _minutes(other.split(",")[1]):
                    return True  # silently dropped
                break
        self.windows[name] = value
        return True
