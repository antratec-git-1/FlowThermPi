"""Tests for „Strom der Steckdosen“ (heizapp.energy): what flowed through every plug, in quarter hours – recorded from
the meter, written to monthly files, kept over a restart, shown per day and downloaded as a table. And, at the plug:
its own name from the Shelly app, and a room for a plug without a heater.

The plugs are played by tests/fake_shelly.py, the heaters by tests/fake_heater.py; nothing real is reached.
"""
from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_wifi_plug import AERO, PLUG, Base  # noqa: E402
from heizapp import energy  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import ActionError, Service  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 11, 26)
T0 = datetime(2026, 11, 26, 10, 0, tzinfo=BERLIN).timestamp()   # 10:00, the start of a quarter hour


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingTest(unittest.TestCase):
    """heizapp.energy.PlugEnergy alone: readings in, quarter hours out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name, "energy")
        self.clock = Clock()
        self.rec = self.new()

    def new(self) -> energy.PlugEnergy:
        return energy.PlugEnergy(str(self.folder), BERLIN, clock=self.clock)

    def read(self, t: float, wh=None, w=None, ident="plug-a", raum=None, heizung=None, now=None):
        self.rec.sample(t if now is None else now, [{"id": ident, "at": t, "wh": wh, "w": w, "raum": raum,
                                                       "heizung": heizung}])

    def day(self, ident="plug-a", day=DAY) -> dict:
        return {int(t - T0) // 60: (r["wh"], r["w_max"]) for t, r in self.rec.rows_between(day, day).get(ident, {}).items()}

    def test_the_meter_goes_into_quarter_hours(self):
        self.read(T0, wh=1000.0, w=0.0)            # the first reading only marks where the meter stands
        self.read(T0 + 600, wh=1300.0, w=1800.0)   # 10:00–10:10: 300 Wh
        self.read(T0 + 1200, wh=1600.0, w=1750.0)  # 10:10–10:20: 300 Wh, split at 10:15
        self.assertEqual(self.day(), {0: (450.0, 1800), 15: (150.0, 1800)})   # until 10:20 it drew 1800 W
        self.read(T0 + 1200, wh=1600.0, w=1750.0, now=T0 + 1210)   # the same reading again: counted once
        self.assertEqual(self.day(), {0: (450.0, 1800), 15: (150.0, 1800)})

    def test_nothing_flowed(self):
        self.read(T0, wh=500.0, w=0.0)
        self.read(T0 + 300, wh=500.0, w=0.0)
        self.assertEqual(self.day(), {0: (0.0, 0)})   # measured – and nothing flowed: not the same as no values

    def test_a_short_stretch_does_not_raise_the_highest_power(self):
        self.read(T0, wh=0.0, w=610.0)
        self.read(T0 + 100, wh=20.0, w=612.0)   # a meter in 10-Wh steps: 20 Wh in 100 s would be 720 W
        self.assertEqual(self.day(), {0: (20.0, 612)})

    def test_a_meter_that_was_reset_counts_from_zero(self):
        self.read(T0, wh=98000.0, w=100.0)
        self.read(T0 + 300, wh=4.0, w=100.0)
        self.assertEqual(self.day(), {0: (4.0, 100)})

    def test_a_jump_the_plug_cannot_have_drawn_counts_the_power(self):
        self.read(T0, wh=1.0, w=1200.0)
        with self.assertLogs("heizapp.energy", "WARNING") as said:
            self.read(T0 + 300, wh=1001.0, w=1200.0)   # 1000 Wh in 5 minutes would be 12 kW: Zigbee2MQTT's bug
        self.assertIn("springt um 1000 Wh in 300 s", said.output[0])
        self.assertEqual(self.day(), {0: (100.0, 1200)})   # 1200 W × 5 minutes instead

    def test_a_plug_without_a_meter_counts_power_times_time(self):
        self.read(T0, w=600.0)
        self.read(T0 + 600, w=900.0)    # until here 600 W: 100 Wh
        self.read(T0 + 1200, w=900.0)   # then 900 W: 150 Wh, half of it after 10:15
        self.assertEqual(self.day(), {0: (175.0, 900), 15: (75.0, 900)})
        self.read(T0 + 1200 + energy.POWER_GAP + 1, w=900.0)   # too long without a reading to guess
        self.assertEqual(self.day(), {0: (175.0, 900), 15: (75.0, 900)})

    def test_a_pause_is_spread_and_a_long_one_left_out(self):
        self.read(T0, wh=0.0, w=0.0)
        self.read(T0 + 3600, wh=400.0, w=0.0)   # an hour without readings: 100 Wh in every quarter hour
        self.assertEqual(self.day(), {0: (100.0, 400), 15: (100.0, 400), 30: (100.0, 400), 45: (100.0, 400)})
        with self.assertLogs("heizapp.energy", "WARNING") as said:
            self.read(T0 + 3600 + energy.MAX_GAP + 900, wh=900.0, w=0.0)
        self.assertIn("hat 6 Stunden nichts gemeldet", said.output[0])
        self.assertEqual(sum(v[0] for v in self.day().values()), 400.0)   # the 500 Wh in those hours: not guessed

    def test_the_place_at_the_time(self):
        self.read(T0, wh=0.0, w=0.0, raum="r1")
        self.read(T0 + 600, wh=20.0, w=120.0, raum="r1", heizung="h1")
        row = self.rec.rows_between(DAY, DAY)["plug-a"][int(T0)]
        self.assertEqual((row["raum"], row["heizung"]), ("r1", "h1"))

    def test_written_every_quarter_hour_and_read_back(self):
        self.read(T0, wh=0.0, w=0.0)
        self.read(T0 + 600, wh=100.0, w=600.0)
        self.read(T0 + 1000, wh=150.0, w=600.0)   # 10:16: the quarter hour 10:00 is finished
        self.assertFalse(self.folder.exists())     # not yet: the card is written every 15 minutes
        self.clock.now += energy.FLUSH_EVERY
        self.read(T0 + 1010, wh=151.0, w=600.0)
        lines = (self.folder / "2026-11.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["t,id,wh,w_max,raum,heizung", f"{int(T0)},plug-a,137.5,600,,"])
        self.assertEqual(self.new().rows_between(DAY, DAY)["plug-a"][int(T0)]["wh"], 137.5)   # from the file
        self.assertEqual(self.new().first_day(), DAY)

    def test_a_restart_loses_nothing(self):
        self.read(T0, wh=0.0, w=0.0)
        self.read(T0 + 1200, wh=200.0, w=600.0)   # 150 Wh in 10:00, 50 Wh in 10:15 (not in the file yet)
        self.rec.flush()                           # the app stops (an update)
        self.rec = self.new()
        self.read(T0 + 1790, wh=260.0, w=600.0)   # 60 Wh more: 10:15 has 50 + 60
        self.assertEqual(self.day(), {0: (150.0, 600), 15: (110.0, 600)})
        marks = json.loads((self.folder / energy.MARKS).read_text())
        self.assertEqual(marks["plug-a"], {"at": T0 + 900, "wh": 150.0, "w": 600.0})

    def test_a_late_reading_adds_to_a_finished_quarter_hour(self):
        self.read(T0, wh=0.0, w=0.0)
        self.read(T0 + 890, wh=44.5, w=200.0, now=T0 + 905)   # reported 10:14:50, handed in 10:15:05
        self.read(T0 + 910, wh=45.5, w=200.0)                 # its next step reaches back 10 s into 10:00
        self.assertEqual(self.day(), {0: (45.0, 200), 15: (0.5, 200)})

    def test_the_table_for_excel(self):
        self.read(T0, wh=0.0, w=0.0, raum="r1")
        self.read(T0 + 900, wh=412.5, w=1950.0, raum="r1", heizung="h1")
        data = energy.table(self.rec.rows_between(DAY, DAY), self.rec.local, {"plug-a": "Küche"}, {"r1": "Bad"},
                            {"h1": "Heizung Bad"})
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))   # UTF-8 with BOM: Excel shows the umlauts
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows, [["Tag", "Beginn", "Ende", "Steckdose", "Raum", "Heizung", "Strom (Wh)", "Höchste Leistung (W)"],
                                ["26.11.2026", "10:00", "10:15", "Küche", "Bad", "Heizung Bad", "412,5", "1650"]])


class PlugTest(Base):
    """In the service: a WLAN plug's energy, its own name and its room."""

    def setUp(self):
        super().setUp()
        self.service._clock_trusted = True
        self.adopt()
        self.day = self.service.now().date()
        self.t0 = self.service.history.moment(self.day, 10)

    def reading(self, t: float, add_wh: float = 0.0, load: float | None = None) -> None:
        self.plug.energy_wh += add_wh
        if load is not None:
            self.plug.load = load
        with mock.patch("heizapp.service.time.time", return_value=t):
            self.service.poll_all()
        self.service._record_energy(t)

    def plug_state(self) -> dict:
        return next(p for p in self.service.state()["plugs"] if p["id"] == PLUG)

    def test_the_energy_of_a_day(self):
        self.reading(self.t0)
        self.reading(self.t0 + 600, add_wh=300, load=1800)
        self.reading(self.t0 + 1200, add_wh=300)
        view = self.service.energy_view(PLUG, self.day.isoformat())
        self.assertEqual([(p["t"] - self.t0, p["wh"], p["w_max"]) for p in view["points"]],
                         [(0, 450.0, 1800), (900, 150.0, 1800)])
        self.assertEqual((view["name"], view["kwh"], view["w_max"], view["slot"], view["eur"]), ("Küche", 0.6, 1800, 900, None))
        self.service.set_costs({"price_ct": "30", "show": False})
        self.assertEqual(self.service.energy_view(PLUG, self.day.isoformat())["eur"], 0.18)
        self.service.stop()   # an update: the app stops …
        self.service = self.new_service()   # … and starts again
        self.service._clock_trusted = True
        self.reading(self.t0 + 1800, add_wh=300)
        view = self.service.energy_view(PLUG, self.day.isoformat())
        self.assertEqual([p["wh"] for p in view["points"]], [450.0, 450.0])   # the 150 Wh of 10:15 are not lost
        with self.assertRaises(ActionError):
            self.service.energy_view("shellyplugmg3-aabbccdd0009", self.day.isoformat())
        name, data, ctype = self.service.energy_table(self.day.isoformat(), self.day.isoformat())
        self.assertEqual((name, ctype), (f"strom-steckdosen-{self.day.isoformat()}.csv", "text/csv; charset=utf-8"))
        self.assertIn(";Küche;;;450,0;1800", data.decode("utf-8-sig"))
        with self.assertRaises(ActionError):
            self.service.energy_table("2026-01-01", "2027-01-02")   # more than a year at once

    def test_the_plug_goes_by_its_name_in_the_shelly_app(self):
        self.assertEqual(self.plug_state()["name"], "Küche")
        self.plug.info["name"] = "Bad"   # renamed in the Shelly app: the search sees it at once
        self.service.find_plugs("192.168.5")
        self.assertEqual(self.plug_state()["name"], "Bad")
        self.plug.info["name"] = "Flur"
        self.service.poll_all()   # read again only every ten minutes
        self.assertEqual(self.plug_state()["name"], "Bad")
        self.service._plug_names_read[PLUG] -= Service.PLUG_NAME_EVERY
        with self.assertLogs("heizapp", "INFO") as said:
            self.service.poll_all()
        self.assertEqual(self.plug_state()["name"], "Flur")
        self.assertIn("Bad heißt jetzt „Flur“", "\n".join(said.output))
        self.plug.info["name"] = "  Heizung\nim   Wintergarten, die ganz hinten an der Wand  "
        self.service.find_plugs("192.168.5")
        self.assertEqual(self.plug_state()["name"], "Heizung im Wintergarten, die ganz hinten")
        self.plug.info["name"] = None   # no name any more: the app's own
        self.service.find_plugs("192.168.5")
        self.assertEqual(self.plug_state()["name"], "WLAN-Steckdose 0001")

    def test_a_plug_in_a_room_without_a_heater(self):
        shed = self.service.create_room({"name": "Keller"})["id"]
        salon = self.service.create_room({"name": "Salon"})["id"]
        self.assertEqual(self.service.set_plug_room(PLUG, {"room": shed}),
                         {"message": "Küche ist jetzt im Raum „Keller“."})
        self.assertEqual(self.plug_state()["room"], shed)
        self.reading(self.t0)
        self.reading(self.t0 + 600, add_wh=50, load=300)
        row = self.service.energy.rows_between(self.day, self.day)[PLUG][int(self.t0)]
        self.assertEqual((row["raum"], row["heizung"]), (shed, None))
        self.service.update_heater(AERO, {"room": salon})
        self.service.set_plug_heater(PLUG, {"heater": AERO})   # at a heater: in the heater's room
        self.assertEqual(self.plug_state()["room"], salon)
        self.assertNotIn(PLUG, self.service.store.data.get("plug_rooms") or {})
        with self.assertRaises(ActionError) as err:
            self.service.set_plug_room(PLUG, {"room": shed})
        self.assertEqual(err.exception.status, 409)
        self.service.set_plug_heater(PLUG, {"heater": None})
        self.assertIsNone(self.plug_state()["room"])
        for body, status in (({"room": "r-unbekannt"}, 404), ({"room": 7}, None)):
            with self.assertRaises(ActionError if status else ValueError):
                self.service.set_plug_room(PLUG, body)
        self.service.set_plug_room(PLUG, {"room": shed})
        self.service.delete_room(shed)   # the room goes: the plug is in none
        self.assertIsNone(self.plug_state()["room"])
        self.service.set_plug_room(PLUG, {"room": salon})
        self.assertEqual(self.service.set_plug_room(PLUG, {"room": ""}), {"message": "Küche ist in keinem Raum mehr."})
        self.service.set_plug_room(PLUG, {"room": salon})
        self.service.delete_wifi_plug(PLUG)
        self.assertEqual(self.service.store.data.get("plug_rooms"), {})


class ApiTest(PlugTest):
    """Over the web API (setup): the room, the day in quarter hours, the table to download."""

    def setUp(self):
        super().setUp()
        self.server = make_server(self.service, "127.0.0.1", 0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def fetch(self, method, path, body=None, expect=200):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                status, headers, raw = res.status, res.headers, res.read()
        except urllib.error.HTTPError as err:
            with err:
                status, headers, raw = err.code, err.headers, err.read()
        self.assertEqual(status, expect, raw)
        return headers, raw

    def test_over_the_api(self):
        room = self.service.create_room({"name": "Werkstatt"})["id"]
        _, raw = self.fetch("PUT", f"/api/plugs/{PLUG}/room", {"room": room})
        self.assertEqual(json.loads(raw)["message"], "Küche ist jetzt im Raum „Werkstatt“.")
        self.reading(self.t0)
        self.reading(self.t0 + 900, add_wh=125, load=500)
        day = self.day.isoformat()
        view = json.loads(self.fetch("GET", f"/api/plugs/{PLUG}/energy/{day}")[1])
        self.assertEqual((view["room"], view["kwh"], len(view["points"])), ("Werkstatt", 0.125, 1))
        headers, raw = self.fetch("GET", f"/api/energy/{day}/{day}")
        self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(headers["Content-Disposition"], f'attachment; filename="strom-steckdosen-{day}.csv"')
        self.assertIn(";Küche;Werkstatt;;125,0;500", raw.decode("utf-8-sig"))
        self.fetch("GET", "/api/energy/2026-01-01/2027-06-01", expect=400)
        self.fetch("GET", f"/api/plugs/{PLUG}/energy/2026-13-45", expect=400)

    def test_the_energy_of_a_day(self):   # the service tests above are not run again over the API
        pass

    test_the_plug_goes_by_its_name_in_the_shelly_app = test_a_plug_in_a_room_without_a_heater = test_the_energy_of_a_day


if __name__ == "__main__":
    unittest.main()
