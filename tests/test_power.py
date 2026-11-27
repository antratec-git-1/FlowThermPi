"""Tests for „Strom“ (0.4.19): what hangs on a plug („Angeschlossen“), the overview of a day, a week and a month with
the comparison to the time before and the standby of every plug, and „Strom heute“ on the start page.

The readings go straight into the recording (heizapp.energy) – as if a plug had reported every quarter hour; the plug
in the app and the heaters are played by tests/fake_shelly.py and tests/fake_heater.py. Nothing real is reached.
"""
from __future__ import annotations

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_energy import BERLIN, DAY, T0, Clock  # noqa: E402
from test_wifi_plug import AERO, PLUG, Base  # noqa: E402
from heizapp import energy  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import ActionError  # noqa: E402

NOW = datetime(2026, 11, 26, 14, 0)   # Thursday, 14:00 – local wall-clock time, as Service.now() gives it
FRIDGE, OLD = "shellyplugmg3-aabbccdd0007", "shellyplugmg3-aabbccdd0008"   # plugs no longer in the app, only measured


class DayTest(unittest.TestCase):
    """PlugEnergy.day_totals: „Strom heute“ – the file is read once, what is written later is added as it goes in."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name, "energy")
        self.clock = Clock()
        self.rec = energy.PlugEnergy(str(self.folder), BERLIN, clock=self.clock)

    def read(self, t: float, wh: float, ident="plug-a"):
        self.rec.sample(t, [{"id": ident, "at": t, "wh": wh, "w": None}])

    def test_the_day_is_read_once(self):
        self.read(T0, 1000.0)
        self.read(T0 + 900, 1100.0)
        self.assertEqual(self.rec.day_totals(DAY), {"plug-a": 100.0})   # nothing in the file yet
        self.clock.now += energy.FLUSH_EVERY
        self.read(T0 + 1800, 1150.0)   # 10:00 and 10:15 go into the file
        self.assertTrue(Path(self.folder, "2026-11.csv").exists())
        self.assertEqual(self.rec.day_totals(DAY), {"plug-a": 150.0})
        self.read(T0 + 2700, 1175.0, ident="plug-b")   # a second plug: its first reading only marks its meter
        self.read(T0 + 2700, 1175.0)
        with mock.patch("heizapp.energy.open", create=True, side_effect=AssertionError("read again")):
            self.assertEqual(self.rec.day_totals(DAY), {"plug-a": 175.0})   # the quarter hour since from memory
        self.clock.now += energy.FLUSH_EVERY
        self.read(T0 + 3600, 1180.0)
        self.assertEqual(self.rec.day_totals(DAY), {"plug-a": 180.0})   # nothing counted twice after the write
        again = energy.PlugEnergy(str(self.folder), BERLIN, clock=self.clock)   # a restart: read from the file
        self.assertEqual(again.day_totals(DAY), {"plug-a": 180.0})
        self.assertEqual(self.rec.day_totals(DAY + timedelta(days=1)), {})   # the next day starts empty
        self.assertEqual(self.rec.day_totals(DAY), {"plug-a": 180.0})


class PowerTest(Base):
    """In the service: „Angeschlossen“ at a plug, the overview „Strom“ and „Strom heute“."""

    def setUp(self):
        super().setUp()
        self.service._clock_trusted = True
        self.adopt()

    def stamp(self, local: datetime) -> float:
        return self.service._stamp(local)

    def fill(self, ident: str, first: date, standby_wh: float, wash_wh: float = 0.0) -> None:
        """Readings every quarter hour from midnight of `first` up to NOW: standby_wh in each quarter hour – and from
        10:00 to 11:00 wash_wh more in each (a washing machine at work)."""
        rec, t, end = self.service.energy, self.stamp(datetime(first.year, first.month, first.day)), self.stamp(NOW)
        meter = 1000.0
        rec.sample(t, [{"id": ident, "at": t, "wh": meter, "w": None}])
        while t < end:
            meter += standby_wh + (wash_wh if rec.local(t).hour == 10 else 0.0)
            t += energy.SLOT
            rec.sample(t, [{"id": ident, "at": t, "wh": meter, "w": None}])
        rec.flush()

    def view(self, span: str, day: str) -> dict:
        with mock.patch("heizapp.service.time.time", return_value=self.stamp(NOW)), \
                mock.patch.object(type(self.service), "now", return_value=NOW):
            return self.service.power_view(span, day)

    def plug_state(self) -> dict:
        return next(p for p in self.service.state()["plugs"] if p["id"] == PLUG)

    def test_what_hangs_on_a_plug(self):
        self.assertEqual(self.plug_state()["appliance"], "sonstiges")   # ab Werk
        self.assertEqual(self.service.set_plug_appliance(PLUG, {"appliance": "waschmaschine"}),
                         {"message": "Küche: angeschlossen ist jetzt „Waschmaschine“."})
        self.assertEqual(self.plug_state()["appliance"], "waschmaschine")
        self.assertEqual(self.service.store.data["plug_appliances"], {PLUG: "waschmaschine"})
        for body in ({"appliance": "spuelmaschine"}, {"appliance": None}, {}):
            with self.subTest(body=body), self.assertRaises(ActionError) as err:
                self.service.set_plug_appliance(PLUG, body)
            self.assertEqual(err.exception.status, 400)
        with self.assertRaises(ActionError) as err:
            self.service.set_plug_appliance("shellyplugmg3-aabbccdd0009", {"appliance": "trockner"})
        self.assertEqual(err.exception.status, 404)
        self.service.set_plug_heater(PLUG, {"heater": AERO})   # at an AeroFlow: a heater hangs on it
        self.assertEqual(self.plug_state()["appliance"], "heizung")
        with self.assertRaises(ActionError) as err:
            self.service.set_plug_appliance(PLUG, {"appliance": "trockner"})
        self.assertEqual(err.exception.status, 409)
        self.service.set_plug_appliance(PLUG, {"appliance": "heizung"})   # that is what it is
        self.service.set_plug_heater(PLUG, {"heater": None})   # „– keine –“: still a heater, one without the app
        self.assertEqual(self.plug_state()["appliance"], "heizung")
        self.service.set_plug_appliance(PLUG, {"appliance": "sonstiges"})   # ab Werk again: nothing kept
        self.assertEqual(self.service.store.data["plug_appliances"], {})
        self.service.set_plug_appliance(PLUG, {"appliance": "trockner"})
        self.service.delete_wifi_plug(PLUG)   # the plug goes: what hangs on it is forgotten
        self.assertEqual(self.service.store.data["plug_appliances"], {})

    def test_a_plug_chosen_for_a_heater_in_the_costs_is_a_heater(self):   # PUT /api/costs with plugs (older pages)
        self.assign({AERO: PLUG})
        self.assertEqual(self.service.store.data["plug_appliances"], {PLUG: "heizung"})

    def test_a_running_week_with_the_week_before(self):
        self.service.set_plug_appliance(PLUG, {"appliance": "waschmaschine"})
        with self.service.store.edit() as d:   # a fridge plug removed meanwhile: what hung on it is kept here
            d.setdefault("plug_appliances", {})[FRIDGE] = "kuehlschrank"
        self.service.set_costs({"price_ct": "30"})
        self.fill(PLUG, date(2026, 11, 9), standby_wh=0.5, wash_wh=250.0)   # 2 W, every day one kWh at 10:00
        self.fill(FRIDGE, date(2026, 11, 9), standby_wh=10.0)                # 40 W all the time
        v = self.view("week", "2026-11-26")
        self.assertEqual((v["first"], v["last"], v["label"], v["dates"], v["running"]),
                         ("2026-11-23", "2026-11-29", "Diese Woche", "23.11. – 29.11.2026", True))
        self.assertEqual((v["prev"], v["next"], v["price_ct"]), ("2026-11-16", None, 30.0))
        self.assertEqual(v["before"], {"label": "Woche davor", "cut": self.stamp(datetime(2026, 11, 19, 14, 0))})
        washer, fridge = v["devices"]   # the biggest first
        self.assertEqual((washer["id"], washer["name"], washer["appliance"], washer["known"]),
                         (PLUG, "Küche", "waschmaschine", True))
        self.assertEqual((washer["wh"], washer["eur"], washer["before_wh"]), (4172.0, 1.25, 4172.0))   # Mon … Thu 14:00
        self.assertEqual((washer["standby_w"], washer["standby_eur_year"]), (2.0, 5.26))   # 2 W ≈ 17,5 kWh a year
        self.assertEqual((fridge["id"], fridge["appliance"], fridge["known"]), (FRIDGE, "kuehlschrank", False))
        self.assertEqual((fridge["wh"], fridge["before_wh"], fridge["standby_w"]), (3440.0, 3440.0, None))   # no standby
        self.assertEqual(v["total"], {"wh": 7612.0, "eur": 2.28, "before_wh": 7612.0})
        self.fill(OLD, date(2026, 11, 25), standby_wh=1.0)   # measured only since Wednesday: no comparison
        v = self.view("week", "2026-11-23")
        old = next(x for x in v["devices"] if x["id"] == OLD)
        self.assertEqual((old["wh"], old["before_wh"], old["standby_w"]), (152.0, None, 4.0))
        self.assertEqual(v["total"], {"wh": 7764.0, "eur": 2.33, "before_wh": None})   # nor for the sum

    def test_finished_weeks_days_and_a_month(self):
        self.fill(PLUG, date(2026, 11, 9), standby_wh=0.5, wash_wh=250.0)
        v = self.view("week", "2026-11-20")
        self.assertEqual((v["first"], v["label"], v["running"], v["prev"], v["next"]),
                         ("2026-11-16", "Letzte Woche", False, "2026-11-09", "2026-11-23"))
        self.assertEqual((v["devices"][0]["wh"], v["devices"][0]["before_wh"], v["devices"][0]["eur"]), (7336.0, 7336.0, None))
        self.assertIsNone(v["before"]["cut"])   # a finished week: with the whole week before
        v = self.view("week", "2026-11-09")
        self.assertEqual((v["label"], v["dates"], v["prev"]), ("09.11. – 15.11.2026", "09.11. – 15.11.2026", None))
        self.assertIsNone(v["devices"][0]["before_wh"])   # nothing measured the week before
        v = self.view("day", "2026-11-26")
        self.assertEqual((v["label"], v["before"], v["next"]), ("Heute", None, None))
        self.assertEqual((v["devices"][0]["wh"], v["devices"][0]["before_wh"]), (1028.0, None))   # a day: no comparison
        self.assertEqual(self.view("day", "2026-11-25")["label"], "Gestern")
        v = self.view("month", "2026-11-03")
        self.assertEqual((v["first"], v["last"], v["label"], v["prev"], v["next"]),
                         ("2026-11-01", "2026-11-30", "November", None, None))
        self.assertEqual((v["devices"][0]["wh"], v["devices"][0]["before_wh"]), (18844.0, None))   # since 9.11.
        self.assertEqual(v["before"]["label"], "Oktober")
        v = self.view("month", "2025-02-14")   # long before: nothing measured, every plug still listed
        self.assertEqual((v["label"], v["last"], v["next"]), ("Februar 2025", "2025-02-28", "2025-03-01"))
        self.assertEqual([(x["id"], x["wh"]) for x in v["devices"]], [(PLUG, None)])
        self.assertEqual(v["total"], {"wh": None, "eur": None, "before_wh": None})
        for span, day, status in (("year", "2026-11-26", 404), ("day", "2026-13-45", 400)):
            with self.subTest(span=span, day=day), self.assertRaises(ActionError) as err:
                self.view(span, day)
            self.assertEqual(err.exception.status, status)

    def test_a_shorter_month_is_compared_to_its_end(self):
        march = datetime(2026, 3, 30, 12, 0)
        with mock.patch.object(type(self.service), "now", return_value=march):
            cut = self.service.power_view("month", "2026-03-30")["before"]["cut"]
        self.assertEqual(cut, self.stamp(datetime(2026, 3, 1)))   # February has no 30th: all of it

    def test_strom_heute_on_the_start_page(self):
        self.assertIsNone(self.service.state()["power_today"])   # ab Werk: not on the start page
        self.service.set_costs({"price_ct": "30", "show_power": True})
        self.assertEqual(self.service.state()["power_today"], {"wh": None, "eur": None, "plugs": 0})   # nothing yet
        now = self.service.history.moment(self.service.now().date())
        self.service.energy.sample(now, [{"id": PLUG, "at": now, "wh": 1000.0, "w": None}])
        self.service.energy.sample(now + 900, [{"id": PLUG, "at": now + 900, "wh": 1500.0, "w": None}])
        self.assertEqual(self.service.state()["power_today"], {"wh": 500.0, "eur": 0.15, "plugs": 1})
        self.service.set_costs({"price_ct": "30", "show": True})   # an older page without the switch keeps it
        self.assertTrue(self.service.state()["costs"]["show_power"])
        self.service.set_costs({"price_ct": "", "show_power": False})
        self.assertEqual(self.service.state()["costs"], {"price_ct": None, "show": False, "show_power": False})
        self.assertIsNone(self.service.state()["power_today"])


class ApiTest(PowerTest):
    """Over the web API (setup): „Angeschlossen“ and the overview."""

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
                status, raw = res.status, res.read()
        except urllib.error.HTTPError as err:
            with err:
                status, raw = err.code, err.read()
        self.assertEqual(status, expect, raw)
        return json.loads(raw)

    def test_over_the_api(self):
        answer = self.fetch("PUT", f"/api/plugs/{PLUG}/appliance", {"appliance": "trockner"})
        self.assertEqual(answer["message"], "Küche: angeschlossen ist jetzt „Trockner“.")
        self.fetch("PUT", f"/api/plugs/{PLUG}/appliance", {"appliance": "rasenmaeher"}, expect=400)
        self.fetch("PUT", "/api/plugs/shellyplugmg3-aabbccdd0009/appliance", {"appliance": "trockner"}, expect=404)
        self.fill(PLUG, date(2026, 11, 23), standby_wh=0.5)
        today = self.service.now().date().isoformat()
        v = self.fetch("GET", f"/api/power/week/{today}")
        self.assertEqual([(x["id"], x["appliance"]) for x in v["devices"]], [(PLUG, "trockner")])
        self.fetch("GET", f"/api/power/year/{today}", expect=404)
        self.fetch("GET", "/api/power/day/2026-13-45", expect=400)

    def test_what_hangs_on_a_plug(self):   # the service tests above are not run again over the API
        pass

    test_a_plug_chosen_for_a_heater_in_the_costs_is_a_heater = test_a_running_week_with_the_week_before = \
        test_finished_weeks_days_and_a_month = test_a_shorter_month_is_compared_to_its_end = \
        test_strom_heute_on_the_start_page = test_what_hangs_on_a_plug


if __name__ == "__main__":
    unittest.main()
