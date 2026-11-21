"""Tests for Einrichtung › WLAN: heizapp.wifi.Wifi and its API routes.

The WLAN service is played by a thread that runs the real root part (install.py --wifi) against the
simulated NetworkManager from test_install – so both sides of the request file are tested together.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import install  # noqa: E402
from heizapp import wifi as wifi_module  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import ActionError, Service  # noqa: E402
from heizapp.wifi import Wifi  # noqa: E402
from test_install import FakeNM  # noqa: E402


class SlowNM(FakeNM):
    """Connecting takes a moment, as on the real Pi."""

    def __init__(self, connect_seconds=0.0):
        super().__init__()
        self.connect_seconds = connect_seconds

    def execute(self, cmd, env):
        if cmd[1:5] == ("--wait", "45", "connection", "up"):
            time.sleep(self.connect_seconds)
        return super().execute(cmd, env)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.data = self.root / install.DATA_DIR.lstrip("/")
        self.data.mkdir(parents=True)
        self.nm = SlowNM()
        patcher = mock.patch.object(install, "say", lambda *args: None)   # quiet root part
        patcher.start()
        self.addCleanup(patcher.stop)
        self.wifi = Wifi(self.data, poll=0.01)

    def start_service(self):
        """flowthermpi-wifi.path + .service: run install.py --wifi whenever a request appears."""
        stop = threading.Event()
        args = install.parse_args(["--root", str(self.root), "--wifi"])

        def service():
            while not stop.is_set():
                if (self.data / install.WIFI_REQUEST).exists():
                    try:
                        install.wifi_from_app(args, self.nm)
                    except install.InstallError:
                        pass
                time.sleep(0.005)

        thread = threading.Thread(target=service, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(stop.set)

    def until(self, check, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = check()
            if result:
                return result
            time.sleep(0.01)
        self.fail("timed out")

    def finished_job(self):
        return self.until(lambda: (lambda j: j if j and j["state"] in ("done", "failed") else None)(self.wifi.view()["job"]))


class WifiTest(Base):
    def test_countries_and_texts_match_the_installer(self):
        self.assertEqual(tuple(wifi_module.COUNTRIES), install.WIFI_COUNTRIES)
        self.assertEqual(wifi_module.ONLY_LINK, install.WIFI_ONLY_LINK)

    def test_scan_shows_networks_and_the_state_of_the_pi(self):
        self.start_service()
        view = self.wifi.scan({}, local="192.168.1.20")["wifi"]
        self.assertEqual([n["ssid"] for n in view["networks"]], ["Zuhause", "Neu-WPA3", "Nachbar:5G", "Firma", "Café"])
        self.assertEqual((view["country"], view["via"], view["busy"], view["job"]), ("Deutschland", "ethernet", None, None))
        self.assertTrue(view["scanned"])
        cable, air = view["net"]["interfaces"]
        self.assertEqual((cable["connected"], cable["ipv4"], air["connected"]), (True, ["192.168.1.20"], False))
        self.assertEqual(self.nm.country, "DE")
        self.assertFalse((self.data / install.WIFI_REQUEST).exists())

    def test_connect_runs_in_the_background(self):
        self.nm.connect_seconds = 0.3
        self.start_service()
        res = self.wifi.connect({"ssid": "Zuhause", "password": "richtig-geheim"})
        self.assertEqual(res["wifi"]["job"]["state"], "waiting")              # answers at once
        self.assertEqual(res["wifi"]["busy"], "connect")
        with self.assertRaises(ActionError) as ctx:
            self.wifi.scan({})                                                # one thing at a time
        self.assertEqual(ctx.exception.status, 409)
        self.until(lambda: self.wifi.view()["job"]["state"] == "connecting")  # the service has started
        job = self.finished_job()
        self.assertEqual((job["state"], job["ssid"], job["message"]), ("done", "Zuhause", "Verbunden mit „Zuhause“."))
        self.assertTrue(job["finished"])
        view = self.until(lambda: (lambda v: v if v["busy"] is None else None)(self.wifi.view(local="192.168.1.21")))
        air = view["net"]["interfaces"][1]
        self.assertEqual((air["connected"], air["ssid"], air["ipv4"], view["via"]), (True, "Zuhause", ["192.168.1.21"], "wifi"))
        self.assertTrue(next(n for n in view["networks"] if n["ssid"] == "Zuhause")["active"])
        self.assertNotIn("richtig-geheim", (self.data / install.WIFI_STATUS).read_text())

    def test_wrong_password_ends_the_job_with_the_reason(self):
        self.start_service()
        self.wifi.connect({"ssid": "Zuhause", "password": "falsch-geraten"})
        job = self.finished_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("Passwort passt nicht", job["message"])

    def test_request_nobody_picks_up_is_taken_back(self):
        self.wifi.pickup = 0.2                                                # no service running
        self.wifi.connect({"ssid": "Zuhause", "password": "richtig-geheim"})
        request = self.data / install.WIFI_REQUEST
        self.assertEqual(oct(request.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(request.read_text())["password"], "richtig-geheim")
        job = self.finished_job()
        self.assertIn("reagiert nicht", job["message"])
        self.assertFalse(request.exists())                                    # the password does not stay on disk
        self.until(lambda: self.wifi.view()["busy"] is None)
        with self.assertRaises(ActionError) as ctx:
            self.wifi.scan({})
        self.assertEqual(ctx.exception.status, 503)
        self.assertFalse(request.exists())

    def test_wrong_input_is_refused_before_anything_is_asked(self):
        for body in ({"ssid": ""}, {"ssid": "x" * 33}, {"ssid": "Zuhause", "password": "kurz"},
                     {"ssid": "Zuhause", "password": "Grüße-an-alle"}, {"ssid": "Zuhause", "password": 12345678},
                     {"ssid": "Zuhause", "password": "richtig-geheim", "country": "XX"}, {"ssid": "a\nb"}):
            with self.assertRaises(ActionError) as ctx:
                self.wifi.connect(body)
            self.assertEqual(ctx.exception.status, 400, body)
        self.assertEqual(wifi_module.check_password("A" * 64), "A" * 64)     # a key in hex digits
        self.assertEqual(wifi_module.check_password(""), "")                   # open network
        self.assertEqual(list(self.data.iterdir()), [])

    def test_forget(self):
        self.start_service()
        self.nm.country = "DE"
        self.nm.add_profile("Café", "")
        res = self.wifi.forget({"ssid": "Café"})
        self.assertEqual(res["message"], "„Café“ ist vergessen.")
        self.assertEqual(self.nm.profiles, {})

    def test_forget_keeps_the_only_way_to_the_pi(self):
        self.start_service()
        self.nm.country, self.nm.cable = "DE", False
        self.nm.active = self.nm.add_profile("Zuhause", "richtig-geheim")
        self.wifi.scan({})
        # via that WLAN, from afar (Tailscale runs over the same WLAN) or on the Pi itself: always refused
        for local in ("192.168.1.21", "100.101.102.103", "127.0.0.1", None):
            with self.assertRaises(ActionError) as ctx:
                self.wifi.forget({"ssid": "Zuhause"}, local=local)
            self.assertEqual(ctx.exception.status, 409, local)
            self.assertIn("Netzwerkkabel", str(ctx.exception))
        self.assertEqual(len(self.nm.profiles), 1)
        self.assertFalse([c for c in self.nm.commands if "delete" in c])     # the Pi was not even asked
        self.nm.cable = True                                                  # with the cable in, it may go
        self.wifi.scan({})
        self.wifi.forget({"ssid": "Zuhause"}, local="100.101.102.103")
        self.assertEqual(self.nm.profiles, {})

    def test_forget_asks_the_pi_about_the_cable_again(self):
        self.start_service()
        self.nm.country = "DE"
        self.nm.active = self.nm.add_profile("Zuhause", "richtig-geheim")
        self.wifi.scan({})                                                    # the app last saw the cable in …
        self.nm.cable = False                                                 # … but it has been pulled since
        with self.assertRaises(ActionError) as ctx:
            self.wifi.forget({"ssid": "Zuhause"}, local="100.101.102.103")
        self.assertIn("Netzwerkkabel", str(ctx.exception))
        self.assertEqual(len(self.nm.profiles), 1)
        cable = self.wifi.view()["net"]["interfaces"][0]
        self.assertEqual((cable["type"], cable["connected"]), ("ethernet", False))   # and the app knows it now

    def test_state_survives_a_restart_of_the_app(self):
        self.start_service()
        self.wifi.scan({})
        again = Wifi(self.data).view()
        self.assertEqual(again["net"]["country"], "DE")
        self.assertIsNone(again["job"])

    def test_without_folder_there_is_no_wlan_setup(self):
        off = Wifi(None)
        self.assertFalse(off.supported)
        self.assertEqual(off.view(), {"supported": False})
        with self.assertRaises(ActionError) as ctx:
            off.scan({})
        self.assertEqual(ctx.exception.status, 409)


class WifiApiTest(Base):
    def setUp(self):
        super().setUp()
        self.start_service()
        conf = self.tmp / "heizungen.conf"
        conf.write_text("")
        service = Service(str(conf), str(self.tmp / "data.json"), poll_interval=3600)
        self.server = make_server(service, "127.0.0.1", 0, wifi=self.wifi)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cookie = None

    def call(self, method, path, body=None, expect=200):
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = f"heizung_einrichtung={self.cookie}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                status, payload, cookie = res.status, json.loads(res.read() or b"{}"), res.headers.get("Set-Cookie")
        except urllib.error.HTTPError as err:
            with err:
                status, payload, cookie = err.code, json.loads(err.read() or b"{}"), err.headers.get("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0].split("=", 1)[1] or None
        self.assertEqual(status, expect, payload)
        return payload

    def test_api_flow(self):
        self.assertTrue(self.call("GET", "/api/state")["app"]["wifi"])
        view = self.call("POST", "/api/wifi/scan", {})["wifi"]
        self.assertEqual(view["via"], None)                                   # 127.0.0.1 is neither cable nor WLAN
        self.assertIn("Zuhause", [n["ssid"] for n in view["networks"]])
        self.call("POST", "/api/wifi/connect", {"ssid": "Zuhause", "password": "kurz"}, expect=400)
        self.call("POST", "/api/wifi/connect", {"ssid": "Zuhause", "password": "richtig-geheim"})
        view = self.until(lambda: (lambda v: v if v["job"]["state"] == "done" else None)(self.call("GET", "/api/wifi")))
        self.assertEqual((view["job"]["ssid"], view["busy"]), ("Zuhause", None))   # done means: free for the next step
        self.assertEqual(self.call("POST", "/api/wifi/forget", {"ssid": "Zuhause"})["message"], "„Zuhause“ ist vergessen.")

    def test_wlan_needs_the_pin(self):
        self.call("PUT", "/api/setup/pin", {"pin": "4711"})
        self.cookie = None
        self.assertTrue(self.call("GET", "/api/wifi", expect=403)["pin_required"])
        for path, body in (("/api/wifi/scan", {}), ("/api/wifi/connect", {"ssid": "Zuhause", "password": "richtig-geheim"}),
                           ("/api/wifi/forget", {"ssid": "Zuhause"})):
            self.call("POST", path, body, expect=403)
        self.assertEqual(self.nm.commands, [])


if __name__ == "__main__":
    unittest.main()
