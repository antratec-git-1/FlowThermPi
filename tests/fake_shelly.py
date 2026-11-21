"""Simulated Shelly devices (generation 2 or newer) for the tests.

FakeShelly answers "/shelly" and "/rpc/Shelly.GetStatus" – by default as a Shelly H&T Gen3, which the app no
longer lists. FakePill is a Shelly „The Pill“: it also answers "Pill.GetConfig" and "Shelly.GetComponents" and
takes "Pill.SetConfig" and "Shelly.Reboot" as JSON-RPC frames on "POST /rpc". Every request is recorded in
.requests as (method, path), every POST /rpc call in .calls as (method, params).
"""
from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HT_STATUS = {
    "temperature:0": {"id": 0, "tC": 21.4, "tF": 70.5},
    "humidity:0": {"id": 0, "rh": 47.2},
    "devicepower:0": {"id": 0, "battery": {"V": 5.9, "percent": 88}, "external": {"present": True}},
    "wifi": {"sta_ip": "127.0.0.1", "status": "got ip", "ssid": "Test", "rssi": -58},
}


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):   # skip the reverse DNS lookup of HTTPServer.server_bind (slow on some Macs)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class FakeShelly:
    def __init__(self, ident: str = "shellyhtg3-aabbccddeeff", app: str = "HTG3", model: str = "S3SN-0U12A",
                 status: dict | None = None, password: bool = False, name: str | None = None):
        self.info = {"name": name, "id": ident, "mac": ident.rsplit("-", 1)[-1].upper(), "slot": 1, "model": model,
                     "gen": 3, "fw_id": "20250101-000000/1.5.0-gtest", "ver": "1.5.0", "app": app,
                     "auth_en": password, "auth_domain": ident if password else None}
        self.status = HT_STATUS if status is None else status
        self.password = password
        self.requests: list[tuple[str, str]] = []
        self.calls: list[tuple[str, dict]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                fake.requests.append((self.command, self.path))
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                code, payload = fake.answer(self.command, self.path, body)
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _answer

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, method: str, path: str, body: bytes) -> tuple[int, object]:
        if method == "GET" and path == "/shelly":
            return 200, self.info
        if self.password and path.startswith("/rpc"):
            return 401, {"code": 401}
        if method == "GET" and path == "/rpc/Shelly.GetStatus":
            return 200, self.current_status()
        return 404, {"code": 404, "message": "No handler"}

    def current_status(self) -> dict:
        return self.status

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakePill(FakeShelly):
    """A Shelly Pill. probes: DS18B20 per channel (°C, None = gives no value); dht: (°C, %rh) of a DHT22.
    What it reports depends on the mode, as on the real device. Like the real one (firmware 1.8.99) it lists the
    probes only as dynamic components in "Shelly.GetComponents", page by page (page entries per page);
    in_status=True also puts them into "Shelly.GetStatus". With restart_required, a new mode only takes effect
    after "Shelly.Reboot"."""

    def __init__(self, ident: str = "shellypill-aabbccddeeff", name: str | None = None, mode: str = "onewire",
                 probes: dict | None = None, dht: tuple | None = (22.1, 48.5), password: bool = False,
                 restart_required: bool = False, update: str | None = None, in_status: bool = False, page: int = 20):
        self.mode, self.pending = mode, None
        self.probes = {100: 21.4} if probes is None else dict(probes)
        self.dht = dht
        self.restart_required = restart_required
        self.update = update
        self.in_status, self.page = in_status, page
        self.reboots = 0
        super().__init__(ident=ident, app="Pill", model="S3SN-0U53X", status={}, password=password, name=name)
        self.info.update(fw_id="20251030-160520/g0e480cb", ver="1.8.99-pillprod0")

    def probe_components(self) -> dict:
        """What is plugged in, as the Pill reports it: {"temperature:<id>": status, …}."""
        parts = {}
        if self.mode == "onewire":
            for ch, temp in self.probes.items():
                parts[f"temperature:{ch}"] = {"id": ch, "tC": temp,
                                              "tF": None if temp is None else round(temp * 1.8 + 32, 1),
                                              **({} if temp is not None else {"errors": ["read"]})}
        elif self.mode == "dht22" and self.dht:
            parts["temperature:100"] = {"id": 100, "tC": self.dht[0], "tF": round(self.dht[0] * 1.8 + 32, 1)}
            parts["humidity:100"] = {"id": 100, "rh": self.dht[1]}
        return parts

    def current_status(self) -> dict:
        st = {"ble": {}, "cloud": {"connected": False}, "pill": {},
              "sys": {"mac": self.info["mac"], "restart_required": False,
                      "available_updates": {"stable": {"version": self.update}} if self.update else {}},
              "wifi": {"sta_ip": "127.0.0.1", "status": "got ip", "ssid": "Test", "rssi": -48}}
        if self.in_status:
            st.update(self.probe_components())
        return st

    def components(self, query: str) -> dict:
        """Shelly.GetComponents – only the dynamic part, which is all the app asks for."""
        q = parse_qs(query)
        offset = int((q.get("offset") or ["0"])[0])
        include = json.loads((q.get("include") or ['["status", "config"]'])[0])
        items = [{"key": key, **({"status": st} if "status" in include else {}),
                  **({"config": {"id": st["id"], "name": None, "report_thr_C": 0.1, "offset_C": 0.0}}
                     if "config" in include else {})}
                 for key, st in sorted(self.probe_components().items())]
        return {"components": items[offset:offset + self.page], "cfg_rev": 3, "offset": offset, "total": len(items)}

    def answer(self, method: str, path: str, body: bytes) -> tuple[int, object]:
        if self.password and path.startswith("/rpc"):
            return 401, {"code": 401}
        if method == "GET" and urlsplit(path).path == "/rpc/Shelly.GetComponents":
            return 200, self.components(urlsplit(path).query)
        if method == "GET" and path == "/rpc/Pill.GetConfig":
            return 200, {"mode": self.mode, "pin0_mode": "reserved" if self.mode in ("onewire", "dht22") else "none",
                         "pin1_mode": "none", "pin2_mode": "none"}
        if method == "POST" and path == "/rpc":
            frame = json.loads(body or b"{}")
            name, params = frame.get("method"), frame.get("params") or {}
            self.calls.append((name, params))
            reply = {"id": frame.get("id"), "src": self.info["id"]}
            if name == "Pill.SetConfig":
                mode = (params.get("config") or {}).get("mode")
                if mode not in ("onewire", "dht22", "analog_in", "ssr", "digital_io"):
                    return 200, {**reply, "error": {"code": -103, "message": "Invalid argument 'mode'!"}}
                if self.restart_required:
                    self.pending = mode
                else:
                    self.mode = mode
                return 200, {**reply, "result": {"restart_required": self.restart_required}}
            if name == "Shelly.Reboot":
                self.reboots += 1
                self.mode, self.pending = self.pending or self.mode, None
                return 200, {**reply, "result": None}
            return 200, {**reply, "error": {"code": 404, "message": f"No handler for {name}"}}
        return super().answer(method, path, body)
