"""HTTP server: JSON API for the web UI plus the static files in web/. Standard library only."""
from __future__ import annotations

import base64
import hmac
import json
import logging
import mimetypes
import re
import socketserver
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .access import COOKIE, SetupAccess
from .service import ActionError, Service

log = logging.getLogger("heizapp")
WEB_DIR = (Path(__file__).resolve().parent.parent / "web").resolve()
MAX_BODY = 256 * 1024
PUBLIC, SETUP = True, False   # PUBLIC: usable from the simple view without PIN


def _routes(s: Service):
    h = r"([\w:.\-]+)"  # heater id (serial number or host:<ip>)
    i = r"([\w\-]+)"    # room / plan id
    return [
        # simple view (residents) – never needs the PIN
        ("GET", r"/api/state", lambda m, b, r: r.state(), PUBLIC),
        ("POST", rf"/api/rooms/{i}/setpoint", lambda m, b, r: s.set_setpoint("room", m[1], b.get("temp")), PUBLIC),
        ("POST", rf"/api/rooms/{i}/mode",
         lambda m, b, r: s.set_mode("room", m[1], b.get("mode"), b.get("minutes")), PUBLIC),
        ("POST", rf"/api/heaters/{h}/setpoint", lambda m, b, r: s.set_setpoint("heater", m[1], b.get("temp")), PUBLIC),
        ("POST", rf"/api/heaters/{h}/mode",
         lambda m, b, r: s.set_mode("heater", m[1], b.get("mode"), b.get("minutes")), PUBLIC),
        ("POST", r"/api/setup/unlock", lambda m, b, r: r.unlock(b.get("pin")), PUBLIC),
        ("POST", r"/api/setup/lock", lambda m, b, r: r.lock(), PUBLIC),
        # setup ("Einrichtung") – needs the PIN once one is set
        ("PUT", r"/api/setup/pin", lambda m, b, r: r.set_pin(b.get("pin")), SETUP),
        ("DELETE", r"/api/setup/pin", lambda m, b, r: r.remove_pin(), SETUP),
        ("POST", r"/api/rooms", lambda m, b, r: s.create_room(b), SETUP),
        ("PATCH", rf"/api/rooms/{i}", lambda m, b, r: s.update_room(m[1], b), SETUP),
        ("DELETE", rf"/api/rooms/{i}", lambda m, b, r: s.delete_room(m[1]), SETUP),
        ("POST", rf"/api/rooms/{i}/send-plan", lambda m, b, r: s.send_plan(m[1]), SETUP),
        ("POST", r"/api/heaters", lambda m, b, r: s.add_heater(b), SETUP),
        ("PATCH", rf"/api/heaters/{h}", lambda m, b, r: s.update_heater(m[1], b), SETUP),
        ("DELETE", rf"/api/heaters/{h}", lambda m, b, r: s.delete_heater(m[1]), SETUP),
        ("POST", rf"/api/heaters/{h}/clock", lambda m, b, r: s.sync_clocks(heater_id=m[1]), SETUP),
        ("POST", rf"/api/heaters/{h}/import-plans", lambda m, b, r: s.import_plans(m[1]), SETUP),
        ("POST", rf"/api/heaters/{h}/calibrate", lambda m, b, r: s.calibrate(m[1], b), SETUP),
        ("POST", r"/api/clock-sync", lambda m, b, r: s.sync_clocks(), SETUP),
        ("POST", r"/api/discover", lambda m, b, r: s.discover(b.get("network")), SETUP),
        ("POST", r"/api/plans", lambda m, b, r: s.create_plan(b), SETUP),
        ("PUT", rf"/api/plans/{i}", lambda m, b, r: s.update_plan(m[1], b), SETUP),
        ("DELETE", rf"/api/plans/{i}", lambda m, b, r: s.delete_plan(m[1]), SETUP),
    ]


def make_handler(service: Service, password: str | None = None, access: SetupAccess | None = None):
    routes = _routes(service)
    access = access or SetupAccess(service.store)
    expected_auth = ("Basic " + base64.b64encode(f"heizung:{password}".encode()).decode()) if password else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "Heizung/1.0"

        def log_message(self, fmt, *args):  # only log changes, not the 5-second polling
            if self.command != "GET":
                log.info("%s %s", self.command, self.path)

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            for cookie in getattr(self, "_set_cookies", []):
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _authorized(self) -> bool:
            if not expected_auth:
                return True
            return hmac.compare_digest(self.headers.get("Authorization", ""), expected_auth)

        # ---------------------------------------------------------- setup session (cookie)
        def _token(self) -> str | None:
            try:
                jar = SimpleCookie(self.headers.get("Cookie") or "")
            except CookieError:
                return None
            return jar[COOKIE].value if COOKIE in jar else None

        def _cookie(self, token: str | None) -> None:
            value = f"{COOKIE}={token}" if token else f"{COOKIE}=; Max-Age=0"
            self._set_cookies.append(f"{value}; Path=/; HttpOnly; SameSite=Strict")

        def state(self) -> dict:
            result = service.state()
            result["setup"] = {"pin": access.pin_set, "unlocked": access.allowed(self._token(), touch=False)}
            return result

        def unlock(self, pin) -> dict:
            self._cookie(access.unlock(pin))
            return {}

        def lock(self) -> dict:
            access.lock(self._token())
            self._cookie(None)
            return {}

        def set_pin(self, pin) -> dict:
            self._cookie(access.set_pin(pin, self._token()))
            return {"message": "PIN gespeichert."}

        def remove_pin(self) -> dict:
            access.remove_pin()
            self._cookie(None)
            return {"message": "PIN entfernt – die Einrichtung ist jetzt ohne PIN zugänglich."}

        # ---------------------------------------------------------- dispatch
        def _dispatch(self, method: str) -> None:
            self._set_cookies = []
            if not self._authorized():
                self._send(401, b"Anmeldung erforderlich", "text/plain; charset=utf-8",
                           {"WWW-Authenticate": 'Basic realm="Heizung", charset="UTF-8"'})
                return
            path = urlparse(self.path).path
            if not path.startswith("/api/"):
                if method in ("GET", "HEAD"):
                    self._static(path)
                else:
                    self._json(405, {"error": "nicht erlaubt"})
                return
            for verb, pattern, fn, public in routes:
                match = re.fullmatch(pattern, path)
                if verb != method or not match:
                    continue
                if not public and not access.allowed(self._token()):
                    self._json(403, {"error": "Für die Einrichtung bitte zuerst die PIN eingeben.", "pin_required": True})
                    return
                try:
                    body = self._read_body()
                    result = fn(match, body, self)
                    self._json(200, result if result is not None else {})
                except ActionError as exc:
                    self._json(exc.status, {"error": str(exc)})
                except ValueError as exc:
                    self._json(400, {"error": f"ungültige Anfrage: {exc}"})
                except Exception as exc:  # report, but keep the server alive
                    log.exception("Fehler bei %s %s", method, path)
                    self._json(500, {"error": f"interner Fehler: {exc}"})
                return
            self._json(404, {"error": "unbekannte Adresse"})

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("Anfrage zu groß")
            if not length:
                return {}
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("JSON-Objekt erwartet")
            return body

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            file = (WEB_DIR / name).resolve()
            if WEB_DIR not in file.parents or not file.is_file():
                self._send(404, b"nicht gefunden", "text/plain; charset=utf-8")
                return
            ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            self._send(200, file.read_bytes(), ctype)

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("HEAD")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind() calls socket.getfqdn(), a reverse DNS lookup that can block
        # for half a minute on some networks. The name is not needed, so skip it.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_server(service: Service, host: str, port: int, password: str | None = None) -> ThreadingHTTPServer:
    return _Server((host, port), make_handler(service, password, SetupAccess(service.store)))
