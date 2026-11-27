"""Verlauf und Kosten: heizapp.history (recording, files, the points of a day) and the service's part (costs, API).

Only simulated: made-up times and values, heaters from tests/fake_heater.py – no real device is involved."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_heater import FakeHeater  # noqa: E402
from heizapp import history  # noqa: E402
from heizapp import service as service_module  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import Service  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
HEADER = ",".join(history.FIELDS)


def heater(ident="h1", online=True, temp=205, soll=210, heating=False, window=False):
    return {"id": ident, "online": online, "temp": temp, "soll": soll, "heating": heating, "window": window}


def row(t, ident, ist=None, soll=None, heizt=None, fenster=None, da=None, feuchte=None):
    return {"t": t, "id": ident, "ist": ist, "soll": soll, "heizt": heizt, "fenster": fenster, "da": da, "feuchte": feuchte}


class RecordingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "history")
        self.mono = [0.0]   # the monotonic clock that decides when to write
        self.h = history.History(self.folder, BERLIN, clock=lambda: self.mono[0])
        self.day = date(2026, 11, 24)
        self.t0 = int(self.h.moment(self.day, 8))   # 8:00 local time

    def tearDown(self):
        self.tmp.cleanup()

    def polls(self, start, seconds, **values):
        """A poll every 10 s from start on, for `seconds` seconds, always with the same values."""
        for k in range(0, seconds, 10):
            self.h.sample(start + k, [heater(**values)], [])

    def lines(self, month="2026-11"):
        return Path(self.folder, f"{month}.csv").read_text(encoding="utf-8").splitlines()

    def test_heating_seconds_in_five_minute_entries(self):
        self.polls(self.t0, 300, heating=True)          # 8:00 to 8:05 – the very first poll counts nothing
        self.polls(self.t0 + 300, 150)                  # 8:05 to 8:07:30, not heating
        rows = self.h.day_rows(self.day)["h1"]
        self.assertEqual(sorted(rows), [self.t0, self.t0 + 300])
        first, now = rows[self.t0], rows[self.t0 + 300]
        self.assertEqual((first["heizt"], first["da"], first["ist"], first["soll"], first["fenster"]), (290, 290, 205, 210, 0))
        self.assertEqual((now["heizt"], now["da"]), (0, 150))   # the five minutes being filled right now count too

    def test_a_stalled_poll_adds_at_most_thirty_seconds(self):
        self.h.sample(self.t0, [heater(heating=True)], [])
        self.h.sample(self.t0 + 200, [heater(heating=True)], [])
        entry = self.h.day_rows(self.day)["h1"][self.t0]
        self.assertEqual((entry["heizt"], entry["da"]), (history.MAX_STEP, history.MAX_STEP))

    def test_window_open_and_a_heater_without_connection(self):
        self.polls(self.t0, 300, window=True)
        self.polls(self.t0 + 300, 300, online=False, heating=True)
        self.h.sample(self.t0 + 600, [heater()], [])
        rows = self.h.day_rows(self.day)["h1"]
        self.assertEqual(rows[self.t0]["fenster"], 290)
        gone = rows[self.t0 + 300]   # an entry without values: the gap shows as „keine Verbindung“
        self.assertEqual((gone["ist"], gone["soll"], gone["heizt"], gone["da"]), (None, None, 0, 0))

    def test_a_sensor_reading_counts_once(self):
        reading = {"id": "s1", "at": "2026-11-24T08:00:30", "temp": 20.44, "humidity": 51.26}
        for k in range(0, 60, 10):   # handed in at every poll, read once a minute
            self.h.sample(self.t0 + k, [], [reading])
        self.h.sample(self.t0 + 60, [], [dict(reading, at="2026-11-24T08:01:30", temp=20.8, humidity=None)])
        self.h.sample(self.t0 + 70, [], [dict(reading, at="2026-11-24T08:02:30", temp=None)])   # no value: skipped
        entry = self.h.day_rows(self.day)["s1"][self.t0]
        self.assertEqual((entry["ist"], entry["feuchte"], entry["heizt"], entry["da"]), (206, 513, None, None))

    def test_written_every_fifteen_minutes_and_read_back_after_a_restart(self):
        self.polls(self.t0, 600, heating=True)
        self.assertFalse(os.path.exists(self.folder))   # not yet: the memory card is spared
        self.mono[0] = history.FLUSH_EVERY
        self.h.sample(self.t0 + 600, [heater()], [])     # the third five minutes begin, the first two are written
        self.assertEqual(self.lines(), [HEADER, f"{self.t0},h1,205,210,290,0,290,", f"{self.t0 + 300},h1,205,210,300,0,300,"])
        again = history.History(self.folder, BERLIN)
        self.assertEqual(again.first_day(), self.day)
        rows = again.day_rows(self.day)["h1"]
        self.assertEqual(sorted(rows), [self.t0, self.t0 + 300])
        self.assertEqual(again.day_rows(self.day, ids={"other"}), {})

    def test_stop_writes_only_finished_five_minutes(self):
        self.polls(self.t0, 400, heating=True)
        self.h.flush()   # 8:05 is still being filled: after a restart it is missing rather than there twice
        self.assertEqual(len(self.lines()), 2)
        self.h.flush()
        self.assertEqual(len(self.lines()), 2)

    def test_an_earlier_day_includes_what_is_not_written_yet(self):
        midnight = int(self.h.moment(self.day + timedelta(days=1)))
        self.polls(midnight - 600, 900, heating=True)   # 23:50 to 0:05, nothing written yet
        rows = self.h.day_rows(self.day)["h1"]
        self.assertEqual(sorted(rows), [midnight - 600, midnight - 300])

    def test_entries_wait_while_the_card_cannot_be_written(self):
        Path(self.folder).write_text("kein Ordner")   # the folder's name is taken by a file
        self.polls(self.t0, 700, heating=True)
        with self.assertLogs("heizapp.history", "WARNING") as logged:
            self.h.flush()
            self.h.flush()
        self.assertEqual(len(logged.records), 1)        # said once, not every 15 minutes
        self.assertEqual(sorted(self.h.day_rows(self.day)["h1"]), [self.t0, self.t0 + 300, self.t0 + 600])
        os.remove(self.folder)
        self.h.flush()
        self.assertEqual(len(self.lines()), 3)

    def test_the_page_refresh_reads_only_what_is_in_memory(self):
        self.polls(self.t0, 600, heating=True)
        self.mono[0] = history.FLUSH_EVERY
        self.h.sample(self.t0 + 600, [heater()], [])     # 8:00 to 8:10 are on the card now
        self.assertEqual(sorted(self.h.day_rows(self.day, files=False)["h1"]), [self.t0, self.t0 + 300, self.t0 + 600])
        later = history.History(self.folder, BERLIN)     # after a restart, before its first poll
        self.assertIsNone(later.day_rows(self.day, files=False))   # not known yet, rather than „nothing heated“
        self.assertEqual(sorted(later.day_rows(self.day)["h1"]), [self.t0, self.t0 + 300])

    def test_a_line_cut_off_by_a_power_cut_is_skipped(self):
        os.makedirs(self.folder)
        Path(self.folder, "2026-11.csv").write_text(f"{HEADER}\n{self.t0},h1,205,210,300,0,300,\n{self.t0 + 300},h1,20")
        rows = history.History(self.folder, BERLIN).day_rows(self.day)["h1"]
        self.assertEqual(list(rows), [self.t0])

    def test_the_same_five_minutes_twice_add_up(self):   # the clock was set back
        os.makedirs(self.folder)
        Path(self.folder, "2026-11.csv").write_text(
            f"{HEADER}\n{self.t0},h1,200,210,200,0,200,\n{self.t0},h1,210,215,200,30,200,\n")
        entry = history.History(self.folder, BERLIN).day_rows(self.day)["h1"][self.t0]
        self.assertEqual((entry["ist"], entry["soll"], entry["heizt"], entry["fenster"], entry["da"]), (205, 215, 300, 30, 300))

    def test_a_day_handed_out_does_not_change_afterwards(self):
        self.polls(self.t0, 300, heating=True)
        self.h.sample(self.t0 + 300, [heater()], [])
        before = self.h.day_rows(self.day)["h1"][self.t0]
        copy = dict(before)
        self.h.sample(self.t0 + 10, [heater(heating=True)], [])   # the clock went back into 8:00
        self.h.sample(self.t0 + 300, [heater()], [])
        self.assertEqual(before, copy)

    def test_days_and_months_follow_the_local_clock(self):
        self.assertEqual(self.h.moment(date(2026, 3, 29), 24) - self.h.moment(date(2026, 3, 29)), 23 * 3600)
        self.assertEqual(self.h.moment(date(2026, 10, 25), 24) - self.h.moment(date(2026, 10, 25)), 25 * 3600)
        night = int(self.h.moment(date(2026, 12, 1)))   # midnight into December
        for t in (night - 20, night - 10, night, night + 300):
            self.h.sample(t, [heater(heating=True)], [])
        self.h.flush()
        self.assertEqual(sorted(os.listdir(self.folder)), ["2026-11.csv", "2026-12.csv"])
        self.assertEqual(list(self.h.day_rows(date(2026, 11, 30))["h1"]), [night - 300])
        self.assertEqual(sorted(self.h.day_rows(date(2026, 12, 1))["h1"]), [night, night + 300])
        self.assertEqual(history.History(self.folder, BERLIN).first_day(), date(2026, 11, 30))

    def test_day_names(self):
        today = date(2026, 11, 25)
        self.assertEqual(history.History.label(today, today), "Heute")
        self.assertEqual(history.History.label(date(2026, 11, 24), today), "Gestern")
        self.assertEqual(history.History.label(date(2026, 11, 23), today), "Montag, 23. November")
        self.assertEqual(history.History.label(date(2025, 12, 31), today), "Mittwoch, 31. Dezember 2025")


class ReckoningTest(unittest.TestCase):
    T = 1000 * history.SLOT

    def test_points_like_the_home_page(self):
        t = self.T
        rows = {"a": {t: row(t, "a", 210, 215, 100, 0, 300), t + 300: row(t + 300, "a", None, None, 0, 0, 0)},
                "b": {t: row(t, "b", 200, 220, 250, 30, 300)},
                "s": {t + 300: row(t + 300, "s", 195, feuchte=480)}}
        points = history.series(rows, ["a", "b"], ["s"])
        # heaters that answered, the setpoint of the first one, the longest one heated (they heat side by side)
        self.assertEqual(points[0], {"t": t, "ist": 20.5, "soll": 21.5, "heizt": 250, "fenster": 30, "da": 300})
        # no heater answered: the sensor still gives the temperature
        self.assertEqual(points[1], {"t": t + 300, "ist": 19.5, "soll": None, "heizt": 0, "fenster": 0, "da": 0})
        self.assertIsNone(history.series(rows, [], ["s"])[0]["da"])   # only a sensor: nothing known about heating

    def test_energy_only_with_a_power_costs_only_with_a_price(self):
        rows = {"a": {self.T + 300 * k: row(self.T + 300 * k, "a", 200, 210, 300, 0, 300) for k in range(12)},
                "b": {self.T + 300 * k: row(self.T + 300 * k, "b", 200, 210, 300, 0, 300) for k in range(6)},
                "z": {self.T + 300 * k: row(self.T + 300 * k, "z", 200, 210, 300, 0, 300) for k in range(2)}}
        heaters = [{"id": "a", "name": "Fenster", "kind": "aeroflow", "power_w": 1300},
                   {"id": "b", "name": "Tür", "kind": "aeroflow", "power_w": None},
                   {"id": "z", "name": "Thermostat", "kind": "zigbee", "power_w": None}]
        points = history.series(rows, ["a", "b", "z"], [])
        tot = history.totals(rows, heaters, points, 32.5)
        self.assertEqual((tot["heat_s"], tot["kwh"], tot["eur"], tot["power_missing"]), (3600, 1.3, 0.42, ["Tür"]))
        self.assertEqual([(d["heat_s"], d["kwh"]) for d in tot["devices"]], [(3600, 1.3), (1800, None), (600, None)])
        self.assertIsNone(history.totals(rows, heaters, points, None)["eur"])
        without = history.totals(rows, [dict(h, power_w=None) for h in heaters], points, 32.5)
        self.assertEqual((without["kwh"], without["eur"]), (None, None))


class ServiceTest(unittest.TestCase):
    """Through the service and the HTTP API, against two simulated heaters."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = None
        self.a = FakeHeater(token="A" * 24, idu="91110001")
        self.b = FakeHeater(token="B" * 24, idu="92220002", room_temp=198)
        conf = Path(self.tmp.name, "heizungen.conf")
        conf.write_text(f"127.0.0.1:{self.a.port} {self.a.token}\n127.0.0.1:{self.b.port} {self.b.token}\n")
        self.service = Service(str(conf), str(Path(self.tmp.name, "data.json")), poll_interval=3600)
        for e in self.service.entries:
            e.device.timeout, e.device.retries = 0.5, 1
        self.service.poll_all(full=True)
        self.service._clock_trusted = True
        self.server = make_server(self.service, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.room = self.call("POST", "/api/rooms", {"name": "Wohnzimmer"})["id"]
        self.call("PATCH", "/api/heaters/91110001", {"room": self.room})
        self.call("PATCH", "/api/heaters/92220002", {"room": self.room})
        self.today = self.service.now().date()
        self.start = int(self.service.history.moment(self.today))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.a.close()
        self.b.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, expect=200, cookie=None):
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

    def record(self):
        """Two hours from midnight, a poll every 10 s: the window heater heats the first hour, the door heater the
        half hour after it."""
        for k in range(0, 7200, 10):
            for e in self.service.entries:
                e.values["heater_state"] = int(k < 3600 if e.id == "91110001" else 3600 <= k < 5400)
            self.service._record(now=self.start + k)

    def data(self):
        return json.loads(Path(self.tmp.name, "data.json").read_text(encoding="utf-8"))

    def test_today_on_the_tiles_with_energy_and_costs(self):
        self.record()
        state = self.call("GET", "/api/state")
        self.assertEqual(state["costs"], {"price_ct": None, "show": False, "show_power": False})   # ab Werk: nothing on the start page
        self.assertEqual(state["history"]["first_day"], self.today.isoformat())
        self.assertEqual(state["rooms"][0]["today"]["heat_s"], 5390)
        self.assertIsNone(state["rooms"][0]["today"]["kwh"])                 # no power set yet
        self.call("PUT", "/api/costs", {"price_ct": "32,5", "show": True, "powers": {"91110001": "1300", "92220002": ""}})
        state = self.call("GET", "/api/state")
        names = {h["id"]: h["name"] for h in state["heaters"]}
        self.assertEqual(state["costs"], {"price_ct": 32.5, "show": True, "show_power": False})
        self.assertEqual(state["rooms"][0]["today"], {"heat_s": 5390, "kwh": 1.3, "eur": 0.42,
                                                      "power_missing": [names["92220002"]]})
        by_id = {h["id"]: h for h in state["heaters"]}
        self.assertEqual((by_id["91110001"]["power_w"], by_id["91110001"]["today"]["heat_s"]), (1300, 3590))
        self.assertEqual((by_id["92220002"]["power_w"], by_id["92220002"]["today"]["kwh"]), (None, None))

    def test_day_view_of_a_room_and_of_a_heater(self):
        self.record()
        view = self.call("GET", f"/api/rooms/{self.room}/history/{self.today.isoformat()}")
        self.assertEqual((view["name"], view["label"], view["slot"], view["start"]), ("Wohnzimmer", "Heute", 300, self.start))
        self.assertEqual(view["tz"], self.service.tz.key)
        self.assertEqual([t["label"] for t in view["ticks"]], ["0 Uhr", "6", "12", "18", "24 Uhr"])
        self.assertEqual(len(view["points"]), 24)
        self.assertEqual([p["heizt"] for p in view["points"][:2]] + [view["points"][-1]["heizt"]], [290, 300, 0])
        self.assertEqual(view["points"][0]["soll"], self.a.values["setpoint"] / 10)
        self.assertEqual([d["heat_s"] for d in view["devices"]], [3590, 1800])
        self.assertEqual((view["heat_s"], view["kwh"], view["eur"]), (5390, None, None))
        one = self.call("GET", f"/api/heaters/92220002/history/{self.today.isoformat()}")
        self.assertEqual((one["kind"], one["heat_s"], len(one["devices"])), ("heater", 1800, 1))
        past = self.call("GET", f"/api/rooms/{self.room}/history/{(self.today - timedelta(days=3)).isoformat()}")
        self.assertEqual((past["points"], past["now"]), ([], None))
        self.call("GET", f"/api/rooms/{self.room}/history/2026-13-40", expect=400)
        self.call("GET", "/api/rooms/nirgends/history/2026-11-01", expect=404)
        self.call("GET", "/api/heaters/nirgends/history/2026-11-01", expect=404)

    def test_stop_keeps_what_was_recorded(self):
        self.record()
        self.service.stop()
        again = history.History(os.path.join(self.tmp.name, "history"), self.service.tz)
        rows = again.day_rows(self.today)
        self.assertEqual(sorted(rows), ["91110001", "92220002"])
        self.assertEqual(len(rows["91110001"]), 23)   # the last five minutes were still being filled

    def test_nothing_recorded_while_the_clock_is_wrong(self):
        original = service_module.host_clock_synced
        service_module.host_clock_synced = lambda: False   # e.g. a Raspberry Pi right after a power cut
        try:
            self.service._clock_trusted = False
            self.service._record(now=self.start + 100)
            self.service._record(now=self.start + 110)
        finally:
            service_module.host_clock_synced = original
        self.assertEqual(self.service.history.day_rows(self.today), {})
        state = self.call("GET", "/api/state")
        self.assertFalse(state["history"]["recording"])
        self.assertIsNone(state["rooms"][0]["today"])   # no „Heute noch nicht geheizt“: nothing is known
        self.assertEqual({h["today"] for h in state["heaters"]}, {None})

    def test_a_failing_recording_is_said_once_and_the_loop_goes_on(self):
        def broken(*args):
            raise OSError("kaputt")
        self.service.history.sample = broken
        with self.assertLogs("heizapp.service", "ERROR") as logged:
            for k in range(3):
                self.service._record(now=self.start + 10 * k)
        self.assertEqual(len(logged.records), 1)

    def test_costs_are_checked(self):
        for body, words in [({"price_ct": "abc"}, "Zahl"), ({"price_ct": "0"}, "über 0"), ({"price_ct": "250"}, "höchstens 200"),
                            ({"powers": {"91110001": "1,3"}}, "ganze Zahl"), ({"powers": {"91110001": "0"}}, "zwischen 1"),
                            ({"powers": {"91110001": "9000"}}, "zwischen 1")]:
            with self.subTest(body=body):
                self.assertIn(words, self.call("PUT", "/api/costs", body, expect=400)["error"])
        self.call("PUT", "/api/costs", {"powers": {"nirgends": "100"}}, expect=404)
        self.call("PUT", "/api/costs", {"powers": ["91110001"]}, expect=400)
        self.assertNotIn("settings", self.data())   # nothing saved by a refused one
        self.call("PUT", "/api/costs", {"price_ct": " 29,9 ", "powers": {"91110001": " 1300 "}})
        self.assertEqual(self.data()["settings"]["costs"], {"price_ct": 29.9, "show": False, "show_power": False})
        self.assertEqual(self.data()["heaters"]["91110001"]["power_w"], 1300)
        self.call("PUT", "/api/costs", {"price_ct": "", "powers": {"91110001": ""}})
        self.assertEqual(self.data()["settings"]["costs"], {"price_ct": None, "show": False, "show_power": False})
        self.assertNotIn("power_w", self.data()["heaters"]["91110001"])

    def test_costs_need_the_setup_the_history_does_not(self):
        self.call("PUT", "/api/setup/pin", {"pin": "1234"})
        mine = self.session
        self.assertTrue(self.call("PUT", "/api/costs", {"price_ct": "30"}, expect=403)["pin_required"])
        self.call("PUT", "/api/costs", {"price_ct": "30"}, cookie=mine)
        self.call("GET", f"/api/rooms/{self.room}/history/{self.today.isoformat()}")   # residents see it without PIN


if __name__ == "__main__":
    unittest.main()
