"""A simulated Shelly device (generation 2 or newer) for the tests: answers "/shelly" and "/rpc/Shelly.GetStatus".

Defaults to a Shelly H&T Gen3 on USB power. Every request is recorded in .requests as (method, path).
"""
from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                fake.requests.append((self.command, self.path))
                if self.command == "GET" and self.path == "/shelly":
                    code, body = 200, fake.info
                elif self.command == "GET" and self.path == "/rpc/Shelly.GetStatus":
                    code, body = (401, {"code": 401}) if fake.password else (200, fake.status)
                else:
                    code, body = 404, {"code": 404, "message": "No handler"}
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _answer

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
