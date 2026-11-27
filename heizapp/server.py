"""HTTP server: JSON API for the web UI plus the static files in web/. Standard library only."""
from __future__ import annotations

import base64
import hmac
import json
import logging
import mimetypes
import re
import socketserver
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import APP_NAME, COPYRIGHT, LICENSE, REPO_URL, __version__
from .access import COOKIE, SetupAccess
from .remote import Remote
from .service import ActionError, Service
from .support import Download, Support
from .update import Updater
from .wifi import Wifi
from .zigbee import Zigbee

log = logging.getLogger(__name__)
WEB_DIR = (Path(__file__).resolve().parent.parent / "web").resolve()
mimetypes.add_type("font/woff2", ".woff2")   # the font in web/fonts – older Pythons and slim images do not know it
MAX_BODY = 256 * 1024
PUBLIC, SETUP = True, False   # PUBLIC: usable from the simple view without PIN


def _routes(s: Service, u: Updater, w: Wifi, t: Remote, z: Zigbee, p: Support):
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
        ("POST", rf"/api/heaters/{h}/plug/on", lambda m, b, r: s.plug_on(m[1]), PUBLIC),   # its plug was switched off
        ("POST", rf"/api/notices/{i}/ok", lambda m, b, r: s.notice_ok(m[1]), PUBLIC),   # „Die Waschmaschine ist fertig“
        ("GET", rf"/api/rooms/{i}/history/(\d{{4}}-\d{{2}}-\d{{2}})",   # Verlauf of one day
         lambda m, b, r: s.history_view("room", m[1], m[2]), PUBLIC),
        ("GET", rf"/api/heaters/{h}/history/(\d{{4}}-\d{{2}}-\d{{2}})",
         lambda m, b, r: s.history_view("heater", m[1], m[2]), PUBLIC),
        ("POST", r"/api/setup/unlock", lambda m, b, r: r.unlock(b.get("pin")), PUBLIC),
        ("POST", r"/api/setup/lock", lambda m, b, r: r.lock(), PUBLIC),
        # „Protokoll speichern“ – residents send it to their helper when something goes wrong
        ("POST", r"/api/logs/prepare", lambda m, b, r: p.prepare(r.local_ip()), PUBLIC),
        ("GET", r"/api/logs/([0-9a-f]{16})", lambda m, b, r: p.download(m[1]), PUBLIC),
        # setup ("Einrichtung") – needs the PIN once one is set
        ("PUT", r"/api/setup/pin", lambda m, b, r: r.set_pin(b.get("pin")), SETUP),
        ("DELETE", r"/api/setup/pin", lambda m, b, r: r.remove_pin(), SETUP),
        ("PUT", r"/api/costs", lambda m, b, r: s.set_costs(b), SETUP),   # Strom und Kosten
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
        ("POST", rf"/api/heaters/{h}/regulation", lambda m, b, r: s.set_regulation(m[1], b), SETUP),   # thermostat
        ("POST", rf"/api/heaters/{h}/firmware", lambda m, b, r: s.update_firmware(m[1]), SETUP),       # thermostat
        ("POST", r"/api/clock-sync", lambda m, b, r: s.sync_clocks(), SETUP),
        ("POST", r"/api/discover", lambda m, b, r: s.discover(b.get("network")), SETUP),
        ("POST", r"/api/sensors/discover", lambda m, b, r: s.find_sensors(b.get("network")), SETUP),
        ("POST", r"/api/sensors", lambda m, b, r: s.add_sensor(b), SETUP),
        ("PATCH", rf"/api/sensors/{i}", lambda m, b, r: s.update_sensor(m[1], b), SETUP),
        ("DELETE", rf"/api/sensors/{i}", lambda m, b, r: s.delete_sensor(m[1]), SETUP),
        ("POST", rf"/api/sensors/{i}/refresh", lambda m, b, r: s.refresh_sensor(m[1]), SETUP),
        ("POST", rf"/api/pills/{i}/mode", lambda m, b, r: s.set_pill_mode(m[1], b.get("mode")), SETUP),
        ("POST", rf"/api/pills/{i}/restart", lambda m, b, r: s.restart_pill(m[1]), SETUP),
        ("POST", r"/api/plugs/discover", lambda m, b, r: s.find_plugs(b.get("network")), SETUP),   # WLAN plugs
        ("POST", r"/api/plugs", lambda m, b, r: s.add_wifi_plug(b), SETUP),
        ("DELETE", rf"/api/plugs/{i}", lambda m, b, r: s.delete_wifi_plug(m[1]), SETUP),
        ("PUT", rf"/api/plugs/{i}/heater", lambda m, b, r: s.set_plug_heater(m[1], b), SETUP),   # „gehört zu“
        ("PUT", rf"/api/plugs/{i}/room", lambda m, b, r: s.set_plug_room(m[1], b), SETUP),   # a room, no heater
        ("PUT", rf"/api/plugs/{i}/name", lambda m, b, r: s.rename_plug(m[1], b), SETUP),
        ("PUT", rf"/api/plugs/{i}/appliance", lambda m, b, r: s.set_plug_appliance(m[1], b), SETUP),   # „Angeschlossen“
        ("POST", rf"/api/plugs/{i}/switch", lambda m, b, r: s.switch_plug(m[1], b), SETUP),   # on and off, to test
        ("GET", rf"/api/plugs/{i}/energy/(\d{{4}}-\d{{2}}-\d{{2}})",   # „Strom“: one day in quarter hours
         lambda m, b, r: s.energy_view(m[1], m[2]), SETUP),
        ("GET", r"/api/energy/(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})",   # „Als Tabelle“: all plugs, for Excel
         lambda m, b, r: Download(*s.energy_table(m[1], m[2])), SETUP),
        ("GET", r"/api/power/(day|week|month)/(\d{4}-\d{2}-\d{2})",   # Strom: every plug in a day, week, month
         lambda m, b, r: s.power_view(m[1], m[2]), SETUP),
        ("GET", rf"/api/plugs/{i}/runs/(\d+)", lambda m, b, r: s.run_view(m[1], m[2]), SETUP),   # one wash, minute by minute
        ("POST", rf"/api/sensors/{i}/calibration", lambda m, b, r: s.start_calibration(m[1], b), SETUP),
        ("POST", rf"/api/sensors/{i}/calibration/finish", lambda m, b, r: s.finish_calibration(m[1]), SETUP),
        ("POST", rf"/api/sensors/{i}/calibration/apply", lambda m, b, r: s.apply_calibration(m[1], b), SETUP),
        ("DELETE", rf"/api/sensors/{i}/calibration", lambda m, b, r: s.discard_calibration(m[1]), SETUP),
        ("POST", r"/api/plans", lambda m, b, r: s.create_plan(b), SETUP),
        ("PUT", rf"/api/plans/{i}", lambda m, b, r: s.update_plan(m[1], b), SETUP),
        ("DELETE", rf"/api/plans/{i}", lambda m, b, r: s.delete_plan(m[1]), SETUP),
        ("POST", r"/api/update/check", lambda m, b, r: u.check(), SETUP),
        ("POST", r"/api/update/start", lambda m, b, r: u.start(), SETUP),
        ("GET", r"/api/wifi", lambda m, b, r: w.view(r.local_ip()), SETUP),
        ("POST", r"/api/wifi/scan", lambda m, b, r: w.scan(b, r.local_ip()), SETUP),
        ("POST", r"/api/wifi/connect", lambda m, b, r: w.connect(b, r.local_ip()), SETUP),
        ("POST", r"/api/wifi/forget", lambda m, b, r: w.forget(b, r.local_ip()), SETUP),
        ("GET", r"/api/remote", lambda m, b, r: t.view(r.local_ip()), SETUP),   # holds the login link: setup only
        ("POST", r"/api/remote/refresh", lambda m, b, r: t.refresh(r.local_ip()), SETUP),
        ("POST", r"/api/remote/up", lambda m, b, r: t.up(r.local_ip()), SETUP),
        ("POST", r"/api/remote/down", lambda m, b, r: t.down(r.local_ip()), SETUP),
        ("POST", r"/api/remote/logout", lambda m, b, r: t.logout(r.local_ip()), SETUP),
        ("PUT", r"/api/remote/mode", lambda m, b, r: t.set_mode(b.get("mode"), r.local_ip()), SETUP),
        ("GET", r"/api/zigbee", lambda m, b, r: z.view(), SETUP),
        ("POST", r"/api/zigbee/refresh", lambda m, b, r: z.refresh(), SETUP),
        ("POST", r"/api/zigbee/install", lambda m, b, r: z.install(), SETUP),
        ("POST", r"/api/zigbee/remove", lambda m, b, r: z.remove(), SETUP),
        ("POST", r"/api/zigbee/setup", lambda m, b, r: z.setup(b.get("channel"), b.get("port")), SETUP),
        ("POST", r"/api/zigbee/service", lambda m, b, r: z.service(b.get("on")), SETUP),
        ("POST", r"/api/zigbee/search", lambda m, b, r: z.search(b.get("on")), SETUP),
        ("POST", r"/api/zigbee/devices/(0x[0-9a-f]{16})/remove",   # a thermostat leaves the app, a plug its heater
         lambda m, b, r: s.forget_thermostat(m[1], z.remove_device(m[1], b.get("force"))),
         SETUP),
        ("PUT", r"/api/logs/settings", lambda m, b, r: p.change(b), SETUP),
        # Fernhilfe in the residents' view, without PIN: allowing works only in the home network, ending everywhere
        ("POST", r"/api/remote/help", lambda m, b, r: t.help(r.local_ip()), PUBLIC),
        ("POST", r"/api/remote/help/end", lambda m, b, r: t.end(r.local_ip()), PUBLIC),
    ]


def make_handler(service: Service, password: str | None = None, access: SetupAccess | None = None,
                 updater: Updater | None = None, wifi: Wifi | None = None, remote: Remote | None = None,
                 zigbee: Zigbee | None = None, support: Support | None = None):
    updater = updater or Updater(None)   # without it: no update button (e.g. on the Mac or in Docker)
    wifi = wifi or Wifi(None)            # without it: no WLAN setup
    remote = remote or Remote(None)      # without it: no remote maintenance
    zigbee = zigbee or Zigbee(None)      # without it: no Zigbee programs
    support = support or Support(service, updater, wifi, remote, zigbee, password=bool(password))   # without system logs
    routes = _routes(service, updater, wifi, remote, zigbee, support)
    access = access or SetupAccess(service.store)
    expected_auth = ("Basic " + base64.b64encode(f"heizung:{password}".encode()).decode()) if password else None

    class Handler(BaseHTTPRequestHandler):
        server_version = f"{APP_NAME}/{__version__}"

        def log_message(self, fmt, *args):  # every request is logged by _dispatch, with its result
            pass

        def log_error(self, fmt, *args):  # a request that could not be read (or timed out)
            log.warning("Anfrage von %s nicht lesbar: %s", self._client(), fmt % args)

        def _client(self) -> str:
            try:
                return str(self.client_address[0]).removeprefix("::ffff:")
            except (AttributeError, IndexError, TypeError):
                return "?"

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self._status = status
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
            result["app"] = {"name": APP_NAME, "version": __version__, "license": LICENSE, "copyright": COPYRIGHT,
                             "repo": REPO_URL, "commit": updater.installed["commit"],
                             "installed": updater.installed["installed"], "updates": updater.supported,
                             "wifi": wifi.supported, "remote": remote.supported, "zigbee": zigbee.supported,
                             "device": remote.device}
            result["update"] = updater.status()   # also shown to residents: "the app restarts in a moment"
            result["remote"] = remote.summary(self.local_ip())   # residents see when remote maintenance is on
            result["logs"] = support.view()   # the helper's e-mail address for „Protokoll speichern“
            return result

        def local_ip(self) -> str | None:
            """The Pi's own address this browser talks to – on the cable or in the WLAN."""
            try:
                return str(self.connection.getsockname()[0]).removeprefix("::ffff:")
            except (OSError, AttributeError, IndexError):
                return None

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
            started, self._status = time.monotonic(), None
            try:
                self._handle(method)
            finally:
                self._log(method, time.monotonic() - started)

        def _log(self, method: str, seconds: float) -> None:
            """One entry per request: changes and failures always, the polling of the page only in the detailed log."""
            status = self._status
            level = logging.WARNING if status is None or status >= 500 else \
                logging.DEBUG if method in ("GET", "HEAD") and status < 400 else logging.INFO
            if log.isEnabledFor(level):
                log.log(level, "%s %s → %s (%d ms, von %s)", method, urlparse(self.path).path[:200], status or "abgebrochen",
                        round(seconds * 1000), self._client())

        def _handle(self, method: str) -> None:
            self._set_cookies = []
            if not self._authorized():
                self._send(401, b"Anmeldung erforderlich", "text/plain; charset=utf-8",
                           {"WWW-Authenticate": f'Basic realm="{APP_NAME}", charset="UTF-8"'})
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
                    if isinstance(result, Download):   # a file to save, e.g. „Protokoll speichern“
                        self._send(200, result.data, result.ctype,
                                   {"Content-Disposition": f'attachment; filename="{result.name}"'})
                    else:
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


def make_server(service: Service, host: str, port: int, password: str | None = None,
                updater: Updater | None = None, wifi: Wifi | None = None,
                remote: Remote | None = None, zigbee: Zigbee | None = None,
                support: Support | None = None) -> ThreadingHTTPServer:
    return _Server((host, port), make_handler(service, password, SetupAccess(service.store), updater, wifi, remote, zigbee,
                                              support))
