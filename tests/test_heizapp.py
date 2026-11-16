"""Tests against the simulated heater — no real device involved.

Run:  python3 -m unittest discover -s tests -v      (from the project folder)
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_heater import EMPTY, FakeHeater  # noqa: E402
from heizapp import coap, discovery, plans  # noqa: E402
from heizapp.device import AuthError, Heater, NotApplied, ReadOnlyError  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import Service  # noqa: E402
from heizapp.store import ConfigError, HeaterConfig, load_config  # noqa: E402


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

WEEKDAY = {"1": [{"start": "06:00", "end": "08:00", "temp": 21.5}, {"start": "08:00", "end": "23:59", "temp": 18}]}


class CoapTest(unittest.TestCase):
    def test_roundtrip_with_long_options(self):
        query = "token=" + "x" * 24  # 30 bytes -> extended option length
        packet = coap.encode(coap.CON, 3, 0x1234, b"\x01\x02",
                             [(coap.OPT_URI_QUERY, query.encode()), (coap.OPT_URI_PATH, b"Json")], b'{"a":1}')
        msg = coap.decode(packet)
        self.assertEqual(msg["mid"], 0x1234)
        self.assertEqual(msg["options"], [(11, b"Json"), (15, query.encode())])
        self.assertEqual(msg["payload"], b'{"a":1}')
        self.assertEqual(coap.code_str(0x45), "2.05")


class PlanTest(unittest.TestCase):
    def test_normalize_sorts_and_converts(self):
        plan = plans.normalize({"name": " Test ", "days": {"2": [{"start": "18:00", "end": "24:00", "temp": "20,5"},
                                                                  {"start": "06:15", "end": "08:00", "temp": 21}]}})
        self.assertEqual(plan["name"], "Test")
        self.assertEqual([w["start"] for w in plan["days"]["2"]], ["06:15", "18:00"])
        self.assertEqual(plan["days"]["2"][1]["end"], "23:59")
        self.assertEqual(plans.to_device(plan)[2], ["06:15,08:00,210", "18:00,23:59,205"])
        self.assertEqual(plans.to_device(plan)[1], [])

    def test_rejects_invalid(self):
        bad = [
            {"name": "", "days": {}},
            {"name": "x", "days": {"1": [{"start": "06:00", "end": "08:00", "temp": 21},
                                         {"start": "07:00", "end": "09:00", "temp": 21}]}},  # overlap
            {"name": "x", "days": {"1": [{"start": "08:00", "end": "07:00", "temp": 21}]}},  # end before start
            {"name": "x", "days": {"1": [{"start": "6:10", "end": "07:00", "temp": 21}]}},  # not HH:MM
            {"name": "x", "days": {"1": [{"start": "06:00", "end": "07:00", "temp": 21.3}]}},  # not 0.5 steps
            {"name": "x", "days": {"1": [{"start": "06:00", "end": "07:00", "temp": 40}]}},  # too hot
            {"name": "x", "days": {"1": [{"start": f"{h:02d}:00", "end": f"{h:02d}:30", "temp": 20} for h in range(11)]}},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(plans.PlanError):
                plans.normalize(raw)

    def test_any_minute_and_device_roundtrip(self):
        plan = plans.normalize({"name": "x", "days": {"3": [{"start": "06:10", "end": "07:05", "temp": 21}]}})
        days = plans.to_device(plan)
        self.assertEqual(days[3], ["06:10,07:05,210"])
        back, skipped = plans.from_device("x", days)
        self.assertEqual((plans.fingerprint(back), skipped), (plans.fingerprint(plan), 0))
        _, skipped = plans.from_device("x", {1: ["kaputt", "06:00,07:00,200"]})
        self.assertEqual(skipped, 1)


class DeviceTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHeater()
        self.dev = Heater("127.0.0.1", self.fake.token, self.fake.port, timeout=0.5, retries=1)

    def tearDown(self):
        self.fake.close()

    def test_read_without_token_write_needs_token(self):
        self.assertEqual(self.dev.get("room_temp"), 215)
        with self.assertRaises(ReadOnlyError):
            Heater("127.0.0.1", None, self.fake.port).set("setpoint", 220)
        with self.assertRaises(AuthError):
            Heater("127.0.0.1", "W" * 24, self.fake.port, timeout=0.5).set("setpoint", 220)

    def test_set_is_applied_and_verified(self):
        self.dev.set("setpoint", 230)
        self.assertEqual(self.fake.values["setpoint"], 230)
        self.assertEqual(self.fake.ignored, 0)  # i.e. no "jsonrpc" member was sent

    def test_write_plan_replaces_old_windows_and_is_idempotent(self):
        self.fake.windows["heat_win_1_1_1"] = "05:00,12:00,190"   # old plan that would block the new one
        self.fake.windows["heat_win_1_1_2"] = "12:00,20:00,200"
        days = plans.to_device(plans.normalize({"name": "x", "days": WEEKDAY}))
        self.assertEqual(self.dev.write_plan(1, days), [1])
        self.assertEqual(self.fake.windows["heat_win_1_1_1"], "06:00,08:00,215")
        self.assertEqual(self.fake.windows["heat_win_1_1_2"], "08:00,23:59,180")
        self.assertEqual(self.fake.windows["heat_win_1_1_3"], EMPTY)
        writes = len(self.fake.writes)
        self.assertEqual(self.dev.write_plan(1, days), [])          # nothing to do the second time
        self.assertEqual(len(self.fake.writes), writes)

    def test_write_plan_detects_dropped_window(self):
        with self.assertRaises(NotApplied):  # bypasses validation on purpose: overlap is silently dropped
            self.dev.write_plan(1, {1: ["06:00,08:00,200", "07:00,09:00,200"]})

    def test_clock_and_modes(self):
        deviation = self.dev.set_clock(datetime.now)
        self.assertLess(abs(deviation), 5)
        self.fake.values["auto_mode_en"] = 1
        self.dev.set_mode("manual")
        self.assertEqual(self.fake.values["operating_mode"], 0)
        self.dev.set_mode("auto")
        self.assertEqual((self.fake.values["auto_mode_en"], self.fake.values["operating_mode"]), (1, 1))
        self.dev.set_mode("boost", boost_minutes=45)
        self.assertEqual((self.fake.values["boost_mode_time"], self.fake.values["operating_mode"]), (45, 2))
        self.assertEqual(self.fake.values["heater_state"], 1)
        self.dev.set_mode("boost_off")                       # back to the mode before the boost
        self.assertEqual(self.fake.values["operating_mode"], 1)
        self.dev.set_mode("off")
        self.assertEqual((self.fake.values["operating_mode"], self.fake.values["manual_sp"]), (0, 50))
        self.assertEqual(self.fake.values["setpoint"], 50)


class ConfigTest(unittest.TestCase):
    def test_parse(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("# Kommentar\n192.168.5.81   " + "A" * 24 + "\n\n192.168.5.152   # ohne Token\nfoo.local:5684\n")
        try:
            cfg = load_config(fh.name)
        finally:
            os.unlink(fh.name)
        self.assertEqual([(c.host, c.port, c.token) for c in cfg],
                         [("192.168.5.81", 5683, "A" * 24), ("192.168.5.152", 5683, None), ("foo.local", 5684, None)])

    def test_errors(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("1.2.3.4 a b\n")
        try:
            with self.assertRaises(ConfigError):
                load_config(fh.name)
        finally:
            os.unlink(fh.name)


class DiscoveryTest(unittest.TestCase):
    def test_parse_network(self):
        for text in ("192.168.5", "192.168.5.0/24", "192.168.5.81", " 192.168.5. "):
            self.assertEqual(str(discovery.parse_network(text)), "192.168.5.0/24", text)
        self.assertEqual(str(discovery.parse_network("10.1.0.0/22")), "10.1.0.0/22")
        for bad in ("8.8.8", "10.0.0.0/8", "127.0.0.1", "hallo", ""):
            with self.assertRaises(discovery.DiscoveryError, msg=bad):
                discovery.parse_network(bad)

    def test_scan_finds_only_heaters(self):
        a, b = FakeHeater(idu="11112222"), FakeHeater(idu="33334444")
        try:
            silent = ("127.0.0.1", free_udp_port())
            found = discovery.scan([("127.0.0.1", a.port), silent, ("127.0.0.1", b.port)], wait=0.3)
            self.assertEqual(found, {("127.0.0.1", a.port): "11112222", ("127.0.0.1", b.port): "33334444"})
        finally:
            a.close()
            b.close()


class ApiTest(unittest.TestCase):
    """End to end: Service + HTTP API against two simulated heaters (one without token)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = None
        self.a = FakeHeater(token="A" * 24, idu="91110001")
        self.b = FakeHeater(token="B" * 24, idu="92220002", room_temp=58)
        conf = Path(self.tmp.name, "heizungen.conf")
        conf.write_text(f"127.0.0.1:{self.a.port} {self.a.token}\n127.0.0.1:{self.b.port}\n")
        self.service = Service(str(conf), str(Path(self.tmp.name, "data.json")), poll_interval=3600)
        for e in self.service.entries:
            e.device.timeout, e.device.retries = 0.5, 1
        self.service.poll_all(full=True)
        self.server = make_server(self.service, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.a.close()
        self.b.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, expect=200, cookie=None):
        """One API request; a session cookie the server sets is kept in self.session."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if cookie:
            headers["Cookie"] = f"heizung_einrichtung={cookie}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                status, payload, set_cookie = res.status, json.loads(res.read() or b"{}"), res.headers.get("Set-Cookie")
        except urllib.error.HTTPError as err:
            with err:
                status, payload, set_cookie = err.code, json.loads(err.read() or b"{}"), err.headers.get("Set-Cookie")
        if set_cookie:
            self.session = set_cookie.split(";")[0].split("=", 1)[1] or None
        self.assertEqual(status, expect, payload)
        return payload

    def test_full_flow(self):
        state = self.call("GET", "/api/state")
        self.assertEqual({h["id"] for h in state["heaters"]}, {"91110001", "92220002"})
        self.assertEqual({h["room_temp"] for h in state["heaters"]}, {21.5, 5.8})

        room = self.call("POST", "/api/rooms", {"name": "Wohnzimmer"})["id"]
        self.call("PATCH", "/api/heaters/91110001", {"room": room, "name": "Fenster"})
        self.call("PATCH", "/api/heaters/92220002", {"room": room})

        res = self.call("POST", f"/api/rooms/{room}/setpoint", {"temp": 22.5})["results"]
        by_id = {r["heater"]: r for r in res}
        self.assertTrue(by_id["91110001"]["ok"])
        self.assertFalse(by_id["92220002"]["ok"])            # no token -> reported, not fatal
        self.assertEqual(self.a.values["setpoint"], 225)

        self.call("POST", "/api/plans", {"name": "kaputt", "days": {"1": [{"start": "08:00", "end": "07:00", "temp": 20}]}},
                  expect=400)
        plan = self.call("POST", "/api/plans", {"name": "Werktag", "days": WEEKDAY})["id"]
        self.call("PATCH", f"/api/rooms/{room}", {"plan": plan})
        self.assertTrue(self.call("GET", "/api/state")["rooms"][0]["plan_outdated"])
        self.call("POST", f"/api/rooms/{room}/send-plan")
        self.assertEqual(self.a.windows["heat_win_1_1_2"], "08:00,23:59,180")
        room_state = self.call("GET", "/api/state")["rooms"][0]
        self.assertTrue(room_state["plan_outdated"])          # heater b could not receive it
        self.call("PATCH", "/api/heaters/92220002", {"room": None})
        self.call("POST", f"/api/rooms/{room}/send-plan")
        self.assertFalse(self.call("GET", "/api/state")["rooms"][0]["plan_outdated"])

        self.call("PUT", f"/api/plans/{plan}", {"name": "Werktag", "days": {"1": [{"start": "07:00", "end": "09:00", "temp": 20}]}})
        self.assertTrue(self.call("GET", "/api/state")["rooms"][0]["plan_outdated"])  # edited after sending

        self.call("POST", f"/api/rooms/{room}/mode", {"mode": "auto"})
        self.assertEqual(self.a.values["operating_mode"], 1)
        self.call("POST", "/api/heaters/91110001/clock")
        heater = next(h for h in self.call("GET", "/api/state")["heaters"] if h["id"] == "91110001")
        self.assertLess(abs(heater["clock_drift"]), 5)
        self.assertEqual(heater["name"], "Fenster")

        self.call("DELETE", f"/api/plans/{plan}")
        self.assertIsNone(self.call("GET", "/api/state")["rooms"][0]["plan"])

    def test_import_plans_from_heater(self):
        self.a.windows.update({"heat_win_1_1_1": "06:10,08:00,215", "heat_win_1_1_2": "08:00,23:59,170",
                               "heat_win_3_7_1": "00:00,23:59,190"})
        self.a.values["heating_plan"] = 2
        res = self.call("POST", "/api/heaters/91110001/import-plans")
        self.assertEqual([c["slot"] for c in res["created"]], [1, 3])
        self.assertEqual((res["empty"], res["active_slot"]), ([2], 3))
        state = self.call("GET", "/api/state")
        imported = {p["name"]: p for p in state["plans"]}
        self.assertEqual(imported["Heat4U-911001 – Plan 1"]["days"]["1"][0],
                         {"start": "06:10", "end": "08:00", "temp": 21.5})
        self.assertEqual(imported["Heat4U-911001 – Plan 3"]["source"]["slot"], 3)

        again = self.call("POST", "/api/heaters/91110001/import-plans")     # no duplicates
        self.assertEqual((again["created"], len(again["existing"])), ([], 2))

        none = self.call("POST", "/api/heaters/92220002/import-plans")      # works without token, too
        self.assertEqual((none["created"], none["empty"]), ([], [1, 2, 3]))
        self.assertIn("kein Heizplan", none["message"])

        # an imported plan (minute-exact times) can be edited, saved and sent back unchanged
        pid = imported["Heat4U-911001 – Plan 1"]["id"]
        self.call("PUT", f"/api/plans/{pid}", {"name": "Wohnzimmer", "days": imported["Heat4U-911001 – Plan 1"]["days"]})
        room = self.call("POST", "/api/rooms", {"name": "Wohnzimmer"})["id"]
        self.call("PATCH", "/api/heaters/91110001", {"room": room})
        self.call("PATCH", f"/api/rooms/{room}", {"plan": pid})
        res = self.call("POST", f"/api/rooms/{room}/send-plan")["results"]
        self.assertEqual(res[0]["message"], "war bereits aktuell")

    def test_calibration(self):
        url = "/api/heaters/91110001/calibrate"
        res = self.call("POST", url, {"measured": 20.0})                 # heater shows 21.5 °C, correction 0
        self.assertEqual((res["old"], res["new"], self.a.values["temp_corr"]), (0.0, -1.5, -15))
        self.assertIn("-1,5 °C", res["message"])
        self.assertIn("Heizung zeigte 21,5 °C", res["message"])
        heater = next(h for h in self.call("GET", "/api/state")["heaters"] if h["id"] == "91110001")
        self.assertEqual((heater["room_temp"], heater["temp_corr"]), (20.0, -1.5))

        res = self.call("POST", url, {"measured": "20,5"})               # builds on the current correction
        self.assertEqual(res["new"], -1.0)

        e = next(x for x in self.service.entries if x.id == "91110001")  # 10 minutes of readings at 19.0 °C
        now = time.time()
        e.temps.clear()
        e.temps.extend((now - 600 + i * 60, 190, -10) for i in range(10))
        res = self.call("POST", url, {"measured": 20.0})
        self.assertEqual(res["new"], 0.0)                                  # 30-min average used, not the current 20.5
        self.assertIn("im Mittel", res["message"])

        self.call("POST", url, {"measured": 35}, expect=400)              # would need +13.5 °C
        self.call("POST", url, {"measured": "abc"}, expect=400)
        self.call("POST", "/api/heaters/92220002/calibrate", {"measured": 20}, expect=400)  # no token
        self.a.values["temp_corr"] = 7
        self.assertEqual(self.call("POST", url, {"correction": 0})["new"], 0.0)
        self.assertEqual(self.a.values["temp_corr"], 0)

    def test_room_form_with_heaters(self):
        r1 = self.call("POST", "/api/rooms", {"name": "Salon", "heaters": ["91110001"]})["id"]
        r2 = self.call("POST", "/api/rooms", {"name": "Schuppen", "heaters": ["92220002"]})["id"]
        rooms = {r["id"]: r for r in self.call("GET", "/api/state")["rooms"]}
        self.assertEqual((rooms[r1]["heaters"], rooms[r2]["heaters"]), (["91110001"], ["92220002"]))
        self.call("POST", "/api/rooms", {"name": "x", "heaters": ["gibtsnicht"]}, expect=404)
        self.call("POST", "/api/rooms", {"name": "x", "heaters": "91110001"}, expect=400)

        plan = self.call("POST", "/api/plans", {"name": "Werktag", "days": WEEKDAY})["id"]
        self.call("PATCH", f"/api/rooms/{r1}", {"plan": plan})
        self.call("POST", f"/api/rooms/{r1}/send-plan")
        rooms = {r["id"]: r for r in self.call("GET", "/api/state")["rooms"]}
        self.assertFalse(rooms[r1]["plan_outdated"])

        # moving the other heater into the room: it leaves its old room, and it lacks the plan
        self.call("PATCH", f"/api/rooms/{r1}", {"name": "Wohnzimmer", "heaters": ["91110001", "92220002"]})
        rooms = {r["id"]: r for r in self.call("GET", "/api/state")["rooms"]}
        self.assertEqual(rooms[r1]["name"], "Wohnzimmer")
        self.assertEqual(sorted(rooms[r1]["heaters"]), ["91110001", "92220002"])
        self.assertEqual(rooms[r2]["heaters"], [])
        self.assertTrue(rooms[r1]["plan_outdated"])

        self.call("PATCH", f"/api/rooms/{r1}", {"heaters": ["91110001"]})
        state = self.call("GET", "/api/state")
        self.assertFalse(next(r for r in state["rooms"] if r["id"] == r1)["plan_outdated"])
        self.assertIsNone(next(h for h in state["heaters"] if h["id"] == "92220002")["room"])

    def test_add_edit_delete_heater(self):
        conf = Path(self.service.config_path)
        c = FakeHeater(idu="97000001")
        try:
            host_c = f"127.0.0.1:{c.port}"
            # wrong token, invalid addresses, QR code of another heater -> refused before anything is saved
            self.assertIn("lehnt", self.call("POST", "/api/heaters", {"host": host_c, "token": "W" * 24}, expect=400)["error"])
            for bad in ("", "999.1.1.1", "foo bar", "1.2.3.4:99999"):
                self.call("POST", "/api/heaters", {"host": bad}, expect=400)
            qr_other = json.dumps({"ver": "v1", "name": "000000_12345678", "pop": c.token, "transport": "ble"})
            self.assertIn("12345678", self.call("POST", "/api/heaters", {"host": host_c, "token": qr_other}, expect=400)["error"])
            self.call("POST", "/api/heaters", {"host": f"127.0.0.1:{self.a.port}"}, expect=409)   # duplicate

            res = self.call("POST", "/api/heaters", {"name": "Bad", "host": host_c, "token": c.token})
            self.assertEqual((res["id"], res["warning"]), ("97000001", False))
            self.assertIn([host_c, c.token], [line.split() for line in conf.read_text().splitlines()])
            self.assertEqual(oct(conf.stat().st_mode & 0o777), "0o600")
            heater = next(h for h in self.call("GET", "/api/state")["heaters"] if h["id"] == "97000001")
            self.assertEqual((heater["name"], heater["writable"], heater["online"]), ("Bad", True, True))

            # heater b gets a token by pasting the whole QR content, then loses it again
            qr_b = json.dumps({"ver": "v1", "name": "000000_92220002", "pop": self.b.token, "transport": "ble"})
            self.call("PATCH", "/api/heaters/92220002", {"name": "Schuppen", "token": qr_b})
            self.assertTrue(next(h for h in self.call("GET", "/api/state")["heaters"] if h["id"] == "92220002")["writable"])
            self.assertIn(self.b.token, conf.read_text())
            self.call("PATCH", "/api/heaters/92220002", {"remove_token": True})
            self.assertFalse(next(h for h in self.call("GET", "/api/state")["heaters"] if h["id"] == "92220002")["writable"])
            self.assertNotIn(self.b.token, conf.read_text())

            # a heater that does not answer is added with a warning (it may just be switched off)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind(("127.0.0.1", 0))
                dead_port = s.getsockname()[1]
            res = self.call("POST", "/api/heaters", {"host": f"127.0.0.1:{dead_port}"})
            self.assertTrue(res["warning"])
            self.call("DELETE", f"/api/heaters/{res['id']}")

            self.call("DELETE", "/api/heaters/97000001")
            ids = {h["id"] for h in self.call("GET", "/api/state")["heaters"]}
            self.assertEqual(ids, {"91110001", "92220002"})
            self.assertNotIn(host_c, conf.read_text())
            self.assertEqual([c.host for c in load_config(str(conf))], ["127.0.0.1", "127.0.0.1"])  # still parseable
        finally:
            c.close()

    def test_pin_protects_only_the_setup(self):
        self.assertEqual(self.call("GET", "/api/state")["setup"], {"pin": False, "unlocked": True})
        self.call("POST", "/api/rooms", {"name": "Bad"})                    # no PIN yet: setup is open
        self.call("PUT", "/api/setup/pin", {"pin": "12"}, expect=400)
        self.call("PUT", "/api/setup/pin", {"pin": "12ab"}, expect=400)
        self.call("PUT", "/api/setup/pin", {"pin": "1234"})
        mine = self.session                                                 # whoever set it stays unlocked
        self.assertTrue(mine)
        self.assertNotIn("1234", Path(self.tmp.name, "data.json").read_text())   # only a salted hash

        locked = self.call("POST", "/api/rooms", {"name": "Flur"}, expect=403)
        self.assertTrue(locked["pin_required"])
        self.call("PATCH", "/api/heaters/91110001", {"name": "x"}, expect=403)
        self.call("POST", "/api/rooms", {"name": "Flur"}, cookie=mine)
        self.assertEqual(self.call("GET", "/api/state")["setup"], {"pin": True, "unlocked": False})

        # the simple view keeps working without PIN
        self.call("POST", "/api/heaters/91110001/setpoint", {"temp": 21})
        self.call("POST", "/api/heaters/91110001/mode", {"mode": "auto"})
        self.assertEqual((self.a.values["setpoint"], self.a.values["operating_mode"]), (210, 1))

        self.call("POST", "/api/setup/unlock", {"pin": "9999"}, expect=403)
        self.call("POST", "/api/setup/unlock", {"pin": "1234"})
        other = self.session
        self.assertNotEqual(other, mine)
        self.assertTrue(self.call("GET", "/api/state", cookie=other)["setup"]["unlocked"])
        self.call("POST", "/api/setup/lock", cookie=other)
        self.assertIsNone(self.session)                                    # cookie cleared
        self.call("POST", "/api/rooms", {"name": "Keller"}, cookie=other, expect=403)

        self.call("PUT", "/api/setup/pin", {"pin": "567890"}, cookie=mine)     # change: other sessions end
        self.call("POST", "/api/setup/unlock", {"pin": "1234"}, expect=403)
        self.call("DELETE", "/api/setup/pin", cookie=self.session)
        self.assertEqual(self.call("GET", "/api/state")["setup"], {"pin": False, "unlocked": True})

    def test_pin_pause_after_wrong_attempts(self):
        self.call("PUT", "/api/setup/pin", {"pin": "2468"})
        for _ in range(5):
            self.call("POST", "/api/setup/unlock", {"pin": "0000"}, expect=403)
        self.call("POST", "/api/setup/unlock", {"pin": "2468"}, expect=429)   # paused, even with the right PIN

    def test_off_and_on_restore_what_was_before(self):
        heater = "/api/heaters/91110001"
        self.call("POST", heater + "/setpoint", {"temp": 22.5})            # manual 22.5 °C
        self.call("POST", heater + "/mode", {"mode": "off"})
        self.assertEqual((self.a.values["operating_mode"], self.a.values["setpoint"]), (0, 50))
        self.call("POST", heater + "/mode", {"mode": "off"})               # twice: 22.5 stays remembered
        res = self.call("POST", heater + "/mode", {"mode": "on"})["results"][0]
        self.assertEqual(res["message"], "Eingeschaltet – 22,5 °C")
        self.assertEqual((self.a.values["operating_mode"], self.a.values["setpoint"]), (0, 225))

        self.call("POST", heater + "/mode", {"mode": "auto"})
        self.call("POST", heater + "/mode", {"mode": "off"})
        self.call("POST", heater + "/mode", {"mode": "on"})
        self.assertEqual(self.a.values["operating_mode"], 1)               # back to the plan

        self.a.values.update(operating_mode=0, manual_sp=50, setpoint=50)  # switched off at the heater itself
        self.call("POST", heater + "/mode", {"mode": "on"})
        self.assertEqual((self.a.values["operating_mode"], self.a.values["setpoint"]), (0, 200))

    def test_discover_lists_heaters_and_adopts_new_address(self):
        c = FakeHeater(token="C" * 24, idu="90000077", room_temp=193)   # in the network, not in the app
        try:
            targets = [("127.0.0.1", self.a.port), ("127.0.0.1", self.b.port), ("127.0.0.1", c.port),
                       ("127.0.0.1", free_udp_port())]
            self.service._scan_targets = lambda nets: targets
            # heater a got a new address from the router: the app still knows the old one
            ea = self.service._entry("91110001")
            gone = free_udp_port()
            ea.cfg = HeaterConfig("127.0.0.1", gone, ea.cfg.token, ea.cfg.line)
            ea.device = Heater("127.0.0.1", ea.cfg.token, gone, timeout=0.3, retries=0)
            self.service.refresh(ea)
            self.assertFalse(ea.online)

            res = self.call("POST", "/api/discover", {"network": "192.168.5"})
            self.assertEqual(res["networks"], ["192.168.5.0/24"])
            by_serial = {h["serial"]: h for h in res["heaters"]}
            self.assertEqual(set(by_serial), {"91110001", "92220002", "90000077"})
            self.assertEqual((by_serial["90000077"]["hostname"], by_serial["90000077"]["room_temp"]),
                             ("Heat4U-900077", 19.3))
            self.assertEqual([m["heater"] for m in res["moved"]], ["91110001"])
            self.assertEqual(res["moved"][0]["new"], f"127.0.0.1:{self.a.port}")
            self.assertTrue(ea.online)
            saved = [(c.host, c.port, c.token) for c in load_config(str(Path(self.tmp.name, "heizungen.conf")))]
            self.assertIn(("127.0.0.1", self.a.port, "A" * 24), saved)

            self.call("POST", "/api/discover", {"network": "8.8.8"}, expect=400)       # not a home network
            self.call("POST", "/api/discover", {"network": "10.0.0.0/8"}, expect=400)  # too large
        finally:
            c.close()

    def test_automatic_search_for_lost_heater(self):
        calls = []
        self.service._scan_targets = lambda nets: calls.append(1) or [("127.0.0.1", self.a.port)]
        ea = self.service._entry("91110001")
        gone = free_udp_port()
        ea.cfg = HeaterConfig("127.0.0.1", gone, ea.cfg.token, ea.cfg.line)
        ea.device = Heater("127.0.0.1", ea.cfg.token, gone, timeout=0.3, retries=0)
        self.service.refresh(ea)
        self.service._heal()
        self.assertEqual(calls, [])                         # offline only just now: no search yet
        ea.offline_since -= 3600
        self.service._heal()
        self.assertEqual((len(calls), ea.cfg.port, ea.online), (1, self.a.port, True))
        ea.online, ea.offline_since = False, time.time() - 3600
        self.service._heal()
        self.assertEqual(len(calls), 1)                     # at most one search per 15 minutes

    def test_other_heater_at_known_address_is_recognised(self):
        e = self.service._entry("91110001")
        self.a.values["idu"] = "55556666"                   # router gave this address to another heater
        self.service.refresh(e, full=True)
        self.assertEqual(e.key, "55556666")
        self.assertEqual(self.service.store.data["heaters"]["55556666"]["last_host"], e.label)

    def test_offline_heater_and_static_files(self):
        self.b.online = False
        self.service.poll_all()
        heater = next(h for h in self.call("GET", "/api/state")["heaters"] if h["id"] == "92220002")
        self.assertFalse(heater["online"])
        with urllib.request.urlopen(self.base + "/", timeout=5) as res:
            self.assertIn(b"<title>FlowThermPi</title>", res.read())
            self.assertTrue(res.headers["Server"].startswith("FlowThermPi/"))
        self.call("GET", "/api/nope", expect=404)

    def test_heater_clocks_wait_until_own_clock_is_right(self):
        from heizapp import service as service_module   # e.g. a Raspberry Pi right after a power cut
        original = service_module.host_clock_synced
        service_module.host_clock_synced = lambda: False
        try:
            self.service._clock_trusted = False
            self.assertEqual(self.service.sync_clocks()["results"], [])
            self.call("POST", "/api/heaters/91110001/clock", expect=409)
        finally:
            service_module.host_clock_synced = original
        self.call("POST", "/api/heaters/91110001/clock")                   # fine once it is synchronised

    def test_state_names_the_app(self):
        app = self.call("GET", "/api/state")["app"]
        self.assertEqual((app["name"], app["license"]), ("FlowThermPi", "GPL-3.0"))
        self.assertRegex(app["version"], r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
