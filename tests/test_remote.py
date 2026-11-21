"""Tests for Einrichtung › Fernwartung: heizapp.remote.Remote and its API routes.

The root service is played by a thread that runs the real root part (install.py --remote) against the simulated
Tailscale from test_install – so both sides of the request file are tested together.
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
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import install  # noqa: E402
from heizapp import remote as remote_module  # noqa: E402
from heizapp.remote import Remote  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import ActionError, Service  # noqa: E402
from test_install import KEY, QR, FakeTS, pi_root  # noqa: E402


class SlowTS(FakeTS):
    """Installing and asking take a moment, as on the real Pi."""

    def __init__(self):
        super().__init__()
        self.apt_seconds = self.status_seconds = 0.0

    def execute(self, cmd, env, timeout=None):
        if cmd[0] == "apt-get" and "install" in cmd:
            time.sleep(self.apt_seconds)
        if cmd == ("tailscale", "status", "--json"):
            time.sleep(self.status_seconds)
        return super().execute(cmd, env, timeout)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.data = self.root / install.DATA_DIR.lstrip("/")
        self.data.mkdir(parents=True)
        pi_root(self.root)
        self.ts = SlowTS()
        for patcher in (mock.patch.object(install, "say", lambda *args: None),   # quiet root part
                        mock.patch.object(install, "fetch", lambda url, missing, limit=0: KEY)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.remote = self.make_remote()

    def make_remote(self):
        remote = Remote(self.data, poll=0.01, root=self.root)
        remote.watch_every = remote.guard_every = 0.02
        return remote

    def guard(self, remote=None):
        """remote.start() as when the app starts: one look at the Pi, and the guard of the Fernhilfe."""
        remote = remote or self.remote
        remote.start()
        self.addCleanup(remote.stop)
        return remote

    def start_service(self):
        """flowthermpi-remote.path + .service: run install.py --remote whenever a request appears."""
        stop = threading.Event()
        args = install.parse_args(["--root", str(self.root), "--remote"])

        def service():
            while not stop.is_set():
                if (self.data / install.REMOTE_REQUEST).exists():
                    try:
                        install.remote_from_app(args, self.ts)
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

    def view_when(self, check, local=None):
        return self.until(lambda: (lambda v: v if check(v) else None)(self.remote.view(local)))

    def setup_done(self):
        return self.view_when(lambda v: v["job"] and v["job"]["state"] in ("done", "failed") and not v["busy"])


class RemoteTest(Base):
    def test_device_number_is_the_installers(self):
        self.assertEqual(self.remote.device, install.device_number(self.root))
        self.assertEqual(self.remote.view()["hostname"], "flowthermpi-a1b2c3d4")
        for serial in (b"100000009f8e7d6c\x00", b"", b"0000000000000000\x00", None):
            other = self.tmp / f"pi-{serial!r}"
            pi_root(other, serial=serial)
            (other / "etc/machine-id").write_text("fedcba9876543210fedcba9876543210\n")
            self.assertEqual(remote_module.device_number(other), install.device_number(other), serial)

    def test_setup_runs_in_the_background_until_the_login_link(self):
        self.ts.apt_seconds = 0.3
        self.start_service()
        view = self.remote.up()["remote"]
        self.assertEqual((view["job"]["state"], view["busy"]), ("waiting", "up"))    # answers at once
        with self.assertRaises(ActionError) as ctx:
            self.remote.down()                                                       # one thing at a time
        self.assertEqual(ctx.exception.status, 409)
        self.view_when(lambda v: v["job"]["phase"] == "installing")                 # the page can say what happens
        view = self.setup_done()
        ts = view["tailscale"]
        self.assertEqual((view["job"]["state"], ts["state"]), ("done", "NeedsLogin"))
        self.assertEqual((ts["auth_url"], ts["qr"]), ("https://login.tailscale.com/a/000000000001", QR))
        self.assertTrue(view["watching"])                                           # looks until the login is done
        self.assertFalse(self.remote.summary()["on"])

    def test_the_app_sees_when_the_login_is_done(self):
        self.start_service()
        self.remote.up()
        self.setup_done()
        self.ts.sign_in()                                                            # on the helper's phone
        view = self.view_when(lambda v: v["tailscale"]["state"] == "Running" and not v["watching"], local="100.101.102.103")
        ts = view["tailscale"]
        self.assertEqual((ts["name"], ts["ipv4"], ts["tags"], ts["auth_url"], ts["qr"]),
                         ("flowthermpi-a1b2c3d4", "100.101.102.103", ["tag:flowthermpi"], None, None))
        self.assertTrue(view["via_tailscale"])                                      # this browser comes from afar
        self.assertFalse(self.remote.view(local="192.168.1.20")["via_tailscale"])
        self.assertTrue(self.remote.summary()["on"])

    def test_a_login_started_by_hand_is_not_offered(self):
        # „sudo tailscale up“ by hand before the app: that link would sign the Pi in under the plain name „flowthermpi“
        self.ts.installed = True
        self.ts.tailscale(["up"])
        self.start_service()
        self.remote.refresh()
        view = self.remote.view()
        ts = view["tailscale"]
        self.assertEqual((ts["state"], ts["auth_url"], ts["qr"], ts["other_login"], view["watching"]),
                         ("NeedsLogin", None, None, True, False))
        self.remote.up()                                                             # „Fernwartung einrichten“
        ts = self.setup_done()["tailscale"]
        self.assertEqual((ts["auth_url"], ts["qr"], ts["other_login"]), ("https://login.tailscale.com/a/000000000002", QR, False))
        self.assertEqual(self.ts.prefs["hostname"], "flowthermpi-a1b2c3d4")

    def test_cancelling_closes_the_login(self):
        self.start_service()
        self.remote.up()
        self.setup_done()
        self.remote.logout()                                                         # „Abbrechen“ on the login card
        view = self.remote.view()
        ts = view["tailscale"]
        self.assertEqual((ts["state"], ts["auth_url"], ts["other_login"]), ("NeedsLogin", None, False))
        self.assertIsNone(self.ts.login)                                             # the link cannot be used any more
        self.until(lambda: not self.remote.view()["watching"])

    def test_switch_off_on_and_log_out(self):
        self.start_service()
        self.remote.up()
        self.setup_done()
        self.ts.sign_in()
        self.view_when(lambda v: v["tailscale"]["state"] == "Running" and not v["watching"])
        res = self.remote.down()
        self.assertEqual((res["message"], res["remote"]["tailscale"]["state"]), ("Die Fernwartung ist aus.", "Stopped"))
        self.assertFalse(self.remote.summary()["on"])
        self.remote.up()
        view = self.setup_done()
        self.assertEqual((view["tailscale"]["state"], self.ts.logins), ("Running", 1))   # on again without a new login
        res = self.remote.logout()
        self.assertEqual(res["remote"]["tailscale"]["state"], "NeedsLogin")
        self.assertIsNone(res["remote"]["tailscale"]["auth_url"])

    def test_a_quick_look_does_not_block_a_button(self):
        self.start_service()
        self.remote.up()
        self.setup_done()                                                            # a login is open: the app looks often
        self.ts.status_seconds = 0.1                                                 # and each look takes a moment
        for _ in range(3):
            self.until(lambda: self.remote.view()["busy"] == "status")             # the watcher is asking right now …
            self.remote.refresh()                                                    # … a button waits instead of „busy“
        self.until(lambda: self.remote.view()["busy"] == "status")
        self.assertIsNone(self.remote.logout()["remote"]["tailscale"]["auth_url"])
        self.until(lambda: not self.remote.view()["watching"])

    def test_request_nobody_picks_up_is_taken_back(self):
        self.remote.pickup = 0.2                                                     # no service running
        with self.assertRaises(ActionError) as ctx:
            self.remote.refresh()
        self.assertEqual(ctx.exception.status, 503)
        self.assertFalse((self.data / install.REMOTE_REQUEST).exists())
        self.assertIn("reagiert nicht", self.remote.view()["error"])
        self.remote.up()
        job = self.setup_done()["job"]
        self.assertEqual(job["state"], "failed")
        self.assertIn("reagiert nicht", job["message"])

    def test_request_holds_nothing_but_the_action(self):
        self.remote.pickup = 0.2
        self.remote.up()
        request = json.loads((self.data / install.REMOTE_REQUEST).read_text())
        self.assertEqual(set(request), {"id", "action"})
        self.assertEqual(oct((self.data / install.REMOTE_REQUEST).stat().st_mode & 0o777), "0o600")
        self.setup_done()

    def test_failure_is_shown_in_the_job(self):
        self.ts.apt_error = "E: Unable to locate package tailscale\n"
        self.start_service()
        self.remote.up()
        view = self.setup_done()
        self.assertEqual(view["job"]["state"], "failed")
        self.assertIn("nicht installieren", view["job"]["message"])
        self.assertEqual(view["tailscale"], {"installed": False})
        self.assertFalse(view["watching"])

    def test_state_survives_a_restart_of_the_app(self):
        self.start_service()
        self.remote.up()
        self.setup_done()
        again = self.make_remote().view()
        self.assertEqual((again["tailscale"]["state"], again["tailscale"]["qr"], again["job"]), ("NeedsLogin", QR, None))
        self.assertEqual(again["device"], "a1b2c3d4")

    def test_start_looks_once_and_follows_an_open_login(self):
        self.start_service()
        self.remote.up()
        self.setup_done()
        restarted = self.make_remote()
        restarted.start()
        self.until(lambda: restarted.view()["watching"])                          # the login is still open
        self.ts.sign_in()
        self.until(lambda: restarted.view()["tailscale"]["state"] == "Running")

    def test_reported_values_are_checked(self):
        bad = {"id": "x", "state": "done", "device": "../../etc", "qr": "javascript:alert(1)",
               "tailscale": {"installed": True, "daemon": True, "state": "Hacked", "auth_url": "javascript:alert(1)",
                             "dns_name": "<b>x</b>", "ips": ["100.1.2.3", "<script>"], "version": "1.2<"}}
        (self.data / install.REMOTE_STATUS).write_text(json.dumps(bad))
        view = self.make_remote().view()
        ts = view["tailscale"]
        self.assertEqual((ts["state"], ts["auth_url"], ts["dns_name"], ts["ips"], ts["version"], ts["qr"]),
                         (None, None, None, ["100.1.2.3"], None, None))
        self.assertEqual(view["device"], "a1b2c3d4")

    def test_tags_do_not_hide_the_key_expiry(self):
        # a tag given in Tailscale's admin console keeps the key's expiry – the page must still show the date
        state = {"id": "x", "state": "done", "tailscale": {"installed": True, "daemon": True, "state": "Running",
                                                            "tags": ["tag:flowthermpi", "<b>x</b>", 5],
                                                            "key_expiry": "2027-05-20T10:00:00Z"}}
        (self.data / install.REMOTE_STATUS).write_text(json.dumps(state))
        ts = self.make_remote().view()["tailscale"]
        self.assertEqual((ts["tags"], ts["key_expiry"]), (["tag:flowthermpi"], "2027-05-20T10:00:00Z"))

    def test_without_folder_there_is_no_remote_maintenance(self):
        off = Remote(None)
        self.assertFalse(off.supported)
        self.assertEqual((off.view(), off.summary(), off.device), ({"supported": False}, {"supported": False}, None))
        for action in (off.up, off.help, off.end, lambda: off.set_mode("always")):
            with self.assertRaises(ActionError) as ctx:
                action()
            self.assertEqual(ctx.exception.status, 409)
        self.assertIn("Installationsbefehl", str(ctx.exception))

    def test_fernhilfe_needs_the_setup(self):
        self.start_service()
        self.remote.refresh()
        with self.assertRaises(ActionError) as ctx:
            self.remote.help("192.168.1.20")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("noch nicht eingerichtet", str(ctx.exception))
        self.assertEqual((self.ts.installed, self.remote.summary()["until"]), (False, None))   # nothing installed


class FernhilfeTest(Base):
    """„Fernhilfe erlauben“: in the default mode the Pi is reachable only while the residents allow it."""

    LAN, AFAR = "192.168.1.20", "100.101.102.103"   # the Pi's address the browser talks to: home network | Tailscale

    def setUp(self):
        super().setUp()
        self.start_service()
        self.remote.up(self.LAN)                                                     # „Fernwartung einrichten“
        self.setup_done()
        self.ts.sign_in()
        self.view_when(lambda v: v["tailscale"]["state"] == "Running" and not v["watching"])

    def state(self, remote=None):
        return (remote or self.remote).view()["tailscale"]["state"]

    def saved(self):
        path = self.data / remote_module.SETTINGS
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        return json.loads(path.read_text())

    def refused(self, action, status=403):
        with self.assertRaises(ActionError) as ctx:
            action()
        self.assertEqual(ctx.exception.status, status)
        return str(ctx.exception)

    def test_the_setup_is_a_fernhilfe_too(self):
        # the helper can try it right after the login – and later it switches off by itself
        summary = self.remote.summary(self.LAN)
        self.assertEqual((summary["mode"], summary["on"], summary["ready"], summary["afar"]), ("ask", True, True, False))
        left = (datetime.fromisoformat(summary["until"]) - datetime.now().astimezone()).total_seconds()
        self.assertTrue(7195 < left <= 7201, left)
        self.assertEqual(self.saved(), {"mode": "ask", "until": summary["until"]})

    def test_allowed_for_two_hours_then_off_by_itself(self):
        self.remote.end(self.LAN)
        self.assertEqual((self.state(), self.remote.summary()["until"]), ("Stopped", None))
        self.remote.help_time = 0.4
        self.guard()
        before = len(self.ts.commands)
        res = self.remote.help(self.LAN)                                             # „Fernhilfe erlauben“
        self.assertTrue(res["remote"]["on"] and res["remote"]["until"])
        self.assertRegex(res["message"], r"^Die Fernhilfe ist an – bis \d\d:\d\d Uhr\.$")
        ups = [c for c in self.ts.commands[before:] if c[:2] == ("tailscale", "up")]
        self.assertEqual((ups, self.ts.logins), ([("tailscale", "up")], 1))         # plain „up“ – no new login
        self.until(lambda: self.state() == "Stopped")                               # the guard switched it off
        self.until(lambda: self.saved()["until"] is None)
        self.assertEqual((self.remote.summary()["until"], self.remote.summary()["on"]), (None, False))

    def test_only_the_residents_allow_it(self):
        self.remote.end(self.LAN)
        for afar in (self.AFAR, "100.64.0.7", "fd7a:115c:a1e0::5"):                 # through Tailscale: the helper
            self.assertIn("nur die Bewohner", self.refused(lambda: self.remote.help(afar)))   # cannot allow it
        self.assertEqual((self.state(), self.saved()["until"]), ("Stopped", None))
        self.remote.help(self.LAN)
        until = self.saved()["until"]
        self.refused(lambda: self.remote.help(self.AFAR))                            # … nor make it longer
        self.refused(lambda: self.remote.up(self.AFAR))
        self.assertIn("nur hier im Heimnetz", self.refused(lambda: self.remote.set_mode("always", self.AFAR)))
        self.assertEqual((self.saved(), self.state()), ({"mode": "ask", "until": until}, "Running"))
        res = self.remote.end(self.AFAR)                                             # ending works from afar too:
        self.assertEqual((res["message"], self.state()), ("Die Fernhilfe ist beendet.", "Stopped"))   # helper is done

    def test_switching_off_ends_it(self):
        self.remote.down(self.LAN)                                                   # „Ausschalten“ (Einrichtung)
        self.assertEqual((self.state(), self.saved()["until"]), ("Stopped", None))
        self.remote.help(self.LAN)
        self.remote.logout(self.LAN)                                                 # „Abmelden“
        self.assertEqual((self.state(), self.saved()["until"]), ("NeedsLogin", None))
        self.assertIn("noch nicht eingerichtet", self.refused(lambda: self.remote.help(self.LAN), 409))

    def test_on_without_a_fernhilfe_is_switched_off(self):
        # e.g. switched on by hand on the Pi, or the app was not running when the Fernhilfe ended
        self.remote.end(self.LAN)
        self.ts.tailscale(["up"])                                                    # „sudo tailscale up“ by hand
        self.remote.refresh()
        self.assertEqual(self.state(), "Running")
        self.guard()
        self.until(lambda: self.state() == "Stopped")

    def test_a_restart_keeps_the_fernhilfe(self):
        until = self.saved()["until"]
        again = self.guard(self.make_remote())
        time.sleep(0.3)
        self.assertEqual((again.summary()["until"], self.state(again)), (until, "Running"))

    def test_an_end_missed_while_the_app_was_down(self):
        (self.data / remote_module.SETTINGS).write_text(json.dumps({"mode": "ask", "until": "2026-01-01T10:00:00+01:00"}))
        again = self.guard(self.make_remote())
        self.until(lambda: self.state(again) == "Stopped")
        self.until(lambda: self.saved()["until"] is None)

    def test_never_longer_than_one_fernhilfe(self):
        # the Pi has no clock of its own: if it was ahead when the Fernhilfe began, it must not last a day
        later = (datetime.now().astimezone() + timedelta(days=1)).isoformat(timespec="seconds")
        (self.data / remote_module.SETTINGS).write_text(json.dumps({"mode": "ask", "until": later}))
        again = self.make_remote()
        again.help_time = 0.3
        self.guard(again)
        self.until(lambda: self.state(again) == "Stopped")

    def test_a_failed_switch_on_leaves_nothing_open(self):
        self.remote.end(self.LAN)
        self.ts.account, self.ts.backend = None, "NeedsLogin"                        # removed from the tailnet meanwhile
        self.assertIn("nicht angemeldet", self.refused(lambda: self.remote.help(self.LAN), 502))
        self.assertEqual((self.saved()["until"], self.remote.summary()["until"]), (None, None))
        self.assertEqual((self.ts.logins, self.ts.login), (1, None))                 # „on“ never opens a login

    def test_always_reachable(self):
        self.remote.end(self.LAN)
        res = self.remote.set_mode("always", self.LAN)
        self.assertEqual((res["remote"]["mode"], res["remote"]["until"], self.state()), ("always", None, "Running"))
        self.assertIn("immer an", res["message"])                                    # on at once
        self.guard()
        time.sleep(0.3)
        self.assertEqual(self.state(), "Running")                                    # the guard leaves it on
        self.assertIn("dauerhaft", self.refused(lambda: self.remote.help(self.LAN), 409))
        self.assertEqual(self.make_remote().summary()["mode"], "always")             # saved
        res = self.remote.set_mode("ask", self.LAN)
        self.assertEqual((res["remote"]["tailscale"]["state"], self.saved()), ("Stopped", {"mode": "ask", "until": None}))

    def test_choosing_the_mode_again_changes_nothing(self):
        until = self.saved()["until"]
        self.assertIsNone(self.remote.set_mode("ask", self.LAN)["message"])
        self.assertEqual((self.saved()["until"], self.state()), (until, "Running"))
        self.refused(lambda: self.remote.set_mode("immer", self.LAN), 400)

    def test_bad_settings_mean_ask(self):
        for text in ("kaputt", '["always"]', '{"mode": "immer", "until": "morgen"}'):
            (self.data / remote_module.SETTINGS).write_text(text)
            self.assertEqual(self.make_remote().summary(), {**self.make_remote().summary(), "mode": "ask", "until": None})


class RemoteApiTest(Base):
    def setUp(self):
        super().setUp()
        self.start_service()
        conf = self.tmp / "heizungen.conf"
        conf.write_text("")
        service = Service(str(conf), str(self.tmp / "data.json"), poll_interval=3600)
        self.server = make_server(service, "127.0.0.1", 0, remote=self.remote)
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
                status, raw, cookie = res.status, res.read(), res.headers.get("Set-Cookie")
        except urllib.error.HTTPError as err:
            with err:
                status, raw, cookie = err.code, err.read(), err.headers.get("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0].split("=", 1)[1] or None
        payload = json.loads(raw or b"{}")
        self.assertEqual(status, expect, payload)
        return payload

    def test_api_flow(self):
        state = self.call("GET", "/api/state")
        self.assertEqual((state["app"]["remote"], state["app"]["device"], state["remote"]),
                         (True, "a1b2c3d4", {"supported": True, "on": False, "ready": False, "afar": False, "mode": "ask",
                                             "until": None, "help_minutes": 120}))
        self.assertEqual(self.call("POST", "/api/remote/refresh")["remote"]["tailscale"], {"installed": False})
        self.call("POST", "/api/remote/up")
        view = self.until(lambda: (lambda v: v if v["job"]["state"] == "done" else None)(self.call("GET", "/api/remote")))
        self.assertEqual(view["tailscale"]["auth_url"], "https://login.tailscale.com/a/000000000001")
        self.ts.sign_in()
        self.until(lambda: self.call("GET", "/api/state")["remote"]["on"])
        self.assertEqual(self.call("POST", "/api/remote/down")["message"], "Die Fernwartung ist aus.")
        self.assertFalse(self.call("GET", "/api/state")["remote"]["on"])

    def test_fernhilfe_in_the_residents_view(self):
        self.call("POST", "/api/remote/up")
        self.until(lambda: self.call("GET", "/api/remote")["job"]["state"] == "done")
        self.ts.sign_in()
        self.until(lambda: self.call("GET", "/api/state")["remote"]["on"])
        self.call("PUT", "/api/setup/pin", {"pin": "4711"})
        self.cookie = None                                                           # the residents have no PIN
        self.assertEqual(self.call("POST", "/api/remote/help/end")["message"], "Die Fernhilfe ist beendet.")
        state = self.call("GET", "/api/state")["remote"]
        self.assertEqual((state["on"], state["until"], state["mode"], state["ready"]), (False, None, "ask", True))
        res = self.call("POST", "/api/remote/help")
        self.assertTrue(res["remote"]["on"] and res["remote"]["until"])
        self.assertTrue(self.call("GET", "/api/state")["remote"]["until"])
        self.assertTrue(self.call("PUT", "/api/remote/mode", {"mode": "always"}, expect=403)["pin_required"])

    def test_the_login_link_is_never_public(self):
        self.call("POST", "/api/remote/up")
        self.until(lambda: self.call("GET", "/api/remote")["job"]["state"] == "done")
        req = urllib.request.Request(self.base + "/api/state")
        with urllib.request.urlopen(req, timeout=10) as res:
            text = res.read().decode()
        self.assertNotIn("login.tailscale.com", text)
        self.assertNotIn("data:image", text)

    def test_remote_maintenance_needs_the_pin(self):
        self.call("PUT", "/api/setup/pin", {"pin": "4711"})
        self.cookie = None
        self.assertTrue(self.call("GET", "/api/remote", expect=403)["pin_required"])
        for path in ("/api/remote/refresh", "/api/remote/up", "/api/remote/down", "/api/remote/logout"):
            self.call("POST", path, {}, expect=403)
        self.call("PUT", "/api/remote/mode", {"mode": "always"}, expect=403)
        self.assertEqual(self.ts.commands, [])
        self.assertFalse((self.data / install.REMOTE_REQUEST).exists())


if __name__ == "__main__":
    unittest.main()
