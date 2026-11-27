"""Tests for the runs of a washing machine and a dryer (0.4.20, heizapp.cycles): when one runs and when it is done, the
notice „fertig“ on the start page, the runs in Strom and the curve of one run.

The power comes from made-up load profiles, every 10 seconds as a WLAN plug reports it – a Zigbee plug only when it
changes. How a real washing machine or dryer draws power, these tests do not know: the limits are start values. The
plug in the app is played by tests/fake_shelly.py. Nothing real is reached.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_energy import BERLIN, T0  # noqa: E402  (T0: Thursday 26.11.2026, 10:00)
from test_wifi_plug import PLUG, Base  # noqa: E402
from heizapp import cycles  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import ActionError  # noqa: E402


def washer(s: float) -> float:
    """A washing machine with 40 °C, s seconds after T0: the power (W)."""
    m = s / 60
    if m < 30:
        return 2.0 if m > 20 else 1.2                  # off – then waiting for its delayed start (Startzeitvorwahl)
    if m < 33:
        return 120.0 if s % 30 < 20 else 6.0          # filling: the drum in pushes
    if m < 48:
        return 2050.0                                   # heating the water, 15 minutes
    if m < 88:
        return 250.0 if s % 30 < 20 else 10.0         # washing
    if m < 91:
        return 4.0                                      # soaking for 3 minutes: part of it
    if m < 106:
        return 150.0 if s % 30 < 10 else 5.0          # rinsing: short pushes, little in between
    if m < 116:
        return 450.0                                    # spinning
    return 1.2                                          # done: only the display


def dryer(s: float) -> float:
    """A heat-pump dryer: two hours drying; then the Knitterschutz – a push of 20 s every 3 minutes for half an hour."""
    m = s / 60
    if 10 <= m < 130:
        return 750.0
    if 132 <= m < 162 and (s - 132 * 60) % 180 < 20:
        return 150.0
    return 0.5


def energy_of(load, first: int, last: int) -> float:   # Wh from first to last (seconds after T0), as the plug reports it
    return sum(load(s) * 10 for s in range(first, last, 10)) / 3600


class RunsTest(unittest.TestCase):
    """heizapp.cycles.Runs alone: the power in, runs and notices out."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        self.runs = cycles.Runs(self.folder, BERLIN, clock=lambda: T0)

    def play(self, ident, kind, load, first, last, step=10, runs=None):
        """The plug reports load(s) every `step` seconds from first to last (seconds after T0)."""
        for s in range(first, last, step):
            t = T0 + s
            (runs or self.runs).sample(t, [{"id": ident, "art": kind, "at": t, "w": load(s)}])

    def found(self, runs=None) -> list[dict]:
        return [{**r, "beginn": r["beginn"] - T0, "ende": r["ende"] - T0}
                for r in (runs or self.runs).between(T0 - 86400, T0 + 86400)]

    def test_a_wash(self):
        self.play("w", "waschmaschine", washer, 0, 60 * 60)
        now = self.runs.running("w", T0 + 3600)   # in the middle: since it began filling
        self.assertEqual((now["since"] - T0, now["minutes"]), (30 * 60, 30))
        self.assertAlmostEqual(now["wh"], energy_of(washer, 30 * 60, 60 * 60), delta=0.1)
        self.play("w", "waschmaschine", washer, 60 * 60, 4 * 3600)
        [run] = self.found()
        self.assertEqual((run["beginn"], run["ende"]), (30 * 60, 116 * 60))   # filling to spinning; the pause is in it
        self.assertEqual((run["art"], run["w_max"], run["heiz_min"]), ("waschmaschine", 2050.0, 15))
        self.assertAlmostEqual(run["wh"], energy_of(washer, 30 * 60, 116 * 60), delta=0.1)
        curve = self.runs.curve("w", int(T0) + 30 * 60)
        self.assertEqual(len(curve), 86)   # a value for every minute
        self.assertEqual(curve[:4], [82.0, 82.0, 82.0, 2050.0])
        self.assertEqual(curve[3:18], [2050.0] * 15)
        [notice] = self.runs.notices(T0 + 4 * 3600)
        self.assertEqual((notice["plug"], notice["art"], notice["at"] - T0), ("w", "waschmaschine", 116 * 60))
        self.assertEqual(notice["id"], f"w-{int(T0) + 116 * 60}")
        self.assertIsNone(self.runs.running("w", T0 + 4 * 3600))
        lines = (Path(self.folder) / "durchgaenge-2026.csv").read_text().splitlines()
        self.assertEqual(lines[0], "beginn,ende,id,art,wh,w_max,heiz_min")
        self.assertEqual(len(lines), 2)

    def test_a_dryer_and_its_knitterschutz(self):
        self.play("t", "trockner", dryer, 0, 4 * 3600)
        [run] = self.found()   # the pushes after it start nothing – nor does the one 2 minutes after it stopped
        self.assertEqual((run["beginn"], run["ende"], run["heiz_min"]), (10 * 60, 130 * 60, 0))
        self.assertAlmostEqual(run["wh"], 1500.0, delta=0.1)   # the push in its rest is not counted
        [notice] = self.runs.notices(T0 + 4 * 3600)
        self.assertEqual(notice["at"] - T0, 130 * 60)

    def test_pushes_and_waiting_are_no_run(self):
        push = lambda s: 300.0 if 600 <= s < 640 else 1.0                # a push of 40 s
        self.play("w", "waschmaschine", push, 0, 1800)
        self.assertIsNone(self.runs.running("w", T0 + 620))
        short = lambda s: 300.0 if 3600 <= s < 3720 else 1.0             # two minutes of power: it runs …
        self.play("w", "waschmaschine", short, 1800, 3700)
        self.assertIsNotNone(self.runs.running("w", T0 + 3700))
        self.play("w", "waschmaschine", short, 3700, 7200)
        self.assertEqual(self.found(), [])                                # … but that was no wash
        self.assertEqual(self.runs.notices(T0 + 7200), [])
        self.play("t", "trockner", lambda s: 150.0 if s % 120 < 50 else 0.5, 0, 3600)   # 50 s pushes: no drying
        self.assertEqual(self.found(), [])

    def test_a_zigbee_plug_reports_only_changes(self):
        heard, last = None, None
        for s in range(0, 4 * 3600, 10):   # it tells a new power – and at least every 5 minutes what it measures
            w = washer(s)
            if w != last or heard is None or T0 + s - heard >= 300:
                heard, last = T0 + s, w
            self.runs.sample(T0 + s, [{"id": "z", "art": "waschmaschine", "at": heard, "w": last}])
        [run] = self.found()
        self.assertEqual((run["beginn"], run["ende"], run["heiz_min"]), (30 * 60, 116 * 60, 15))

    def test_an_update_in_the_middle_of_a_wash(self):
        self.play("w", "waschmaschine", washer, 0, 60 * 60)
        self.runs.flush()   # the app stops …
        again = cycles.Runs(self.folder, BERLIN, clock=lambda: T0 + 3630)   # … and is back after half a minute
        self.assertEqual(again.running("w", T0 + 3630)["since"] - T0, 30 * 60)
        self.play("w", "waschmaschine", washer, 3630, 4 * 3600, runs=again)
        [run] = self.found(again)
        self.assertEqual((run["beginn"], run["ende"], run["heiz_min"]), (30 * 60, 116 * 60, 15))
        self.assertEqual(len(again.notices(T0 + 4 * 3600)), 1)

    def test_back_too_late(self):
        self.play("w", "waschmaschine", washer, 0, 60 * 60)
        self.runs.flush()
        again = cycles.Runs(self.folder, BERLIN, clock=lambda: T0 + 2 * 3600)   # an hour later: it is over, somehow
        [run] = self.found(again)
        self.assertEqual((run["beginn"], run["ende"]), (30 * 60, 60 * 60 - 10))   # as far as the app saw it
        self.assertIsNone(again.running("w", T0 + 2 * 3600))
        self.assertEqual(again.notices(T0 + 2 * 3600), [])   # no „fertig“: the app does not know when

    def test_a_plug_that_falls_silent(self):
        self.play("w", "waschmaschine", washer, 0, 60 * 60)
        for s in range(3600, 3600 + 20 * 60, 10):   # its last report stays: unplugged, the WLAN gone
            self.runs.sample(T0 + s, [{"id": "w", "art": "waschmaschine", "at": T0 + 3590, "w": washer(3590)}])
        [run] = self.found()
        self.assertEqual((run["beginn"], run["ende"]), (30 * 60, 3590))
        self.assertEqual(self.runs.notices(T0 + 3 * 3600), [])
        self.assertIsNone(self.runs.running("w", T0 + 3 * 3600))

    def test_a_device_that_never_rests(self):
        self.play("t", "trockner", lambda s: 20.0, 0, 10 * 3600, step=60)
        [run] = self.found()
        self.assertTrue(cycles.MAX_S <= run["ende"] - run["beginn"] <= cycles.MAX_S + 60)
        self.assertEqual(self.runs.notices(T0 + 10 * 3600), [])   # no „fertig“
        self.assertIsNone(self.runs.running("t", T0 + 10 * 3600))   # and no new run before it rested once

    def test_ok_and_the_next_run(self):
        self.play("w", "waschmaschine", washer, 0, 3 * 3600)
        [notice] = self.runs.notices(T0 + 3 * 3600)
        self.assertFalse(self.runs.ok("w-1"))
        self.assertTrue(self.runs.ok(notice["id"]))
        self.assertEqual(self.runs.notices(T0 + 3 * 3600), [])
        self.assertEqual(cycles.Runs(self.folder, BERLIN, clock=lambda: T0).notices(T0 + 3 * 3600), [])   # in the file
        later = lambda s: washer(s - 3 * 3600)
        self.play("w", "waschmaschine", later, 3 * 3600, 6 * 3600)   # the next wash
        [notice] = self.runs.notices(T0 + 6 * 3600)
        self.play("w", "waschmaschine", lambda s: washer(s - 6 * 3600), 6 * 3600, 6 * 3600 + 34 * 60)
        self.assertEqual(len(self.runs.notices(T0 + 6 * 3600 + 34 * 60)), 1)   # 4 minutes of a new one: still there
        self.play("w", "waschmaschine", lambda s: washer(s - 6 * 3600), 6 * 3600 + 34 * 60, 6 * 3600 + 36 * 60)
        self.assertEqual(self.runs.notices(T0 + 6 * 3600 + 36 * 60), [])   # it really runs: the old „fertig“ goes
        self.assertEqual(len(self.found()), 2)

    def test_a_notice_goes_by_itself(self):
        self.play("w", "waschmaschine", washer, 0, 3 * 3600)
        [notice] = self.runs.notices(T0 + 3 * 3600)
        self.assertEqual(len(self.runs.notices(notice["at"] + 12 * 3600 - 60)), 1)
        self.assertEqual(self.runs.notices(notice["at"] + 12 * 3600), [])

    def test_something_else_hangs_on_it_now(self):
        self.play("w", "waschmaschine", washer, 0, 3 * 3600)
        self.play("w", "waschmaschine", lambda s: washer(s - 3 * 3600), 3 * 3600, 4 * 3600)
        self.assertIsNotNone(self.runs.running("w", T0 + 4 * 3600))
        self.runs.sample(T0 + 4 * 3600, [])   # no washing machine there any more: the run going on is not one
        self.assertIsNone(self.runs.running("w", T0 + 4 * 3600))
        self.assertEqual(len(self.found()), 1)
        self.runs.forget("w")
        self.assertEqual(self.runs.notices(T0 + 4 * 3600), [])


class ServiceTest(Base):
    """In the app: a Shelly plug with a washing machine – the poller hands the readings on, the start page and Strom."""

    def setUp(self):
        super().setUp()
        self.service._clock_trusted = True
        self.adopt()
        self.service.set_plug_appliance(PLUG, {"appliance": "waschmaschine"})
        self.service.set_costs({"price_ct": "30", "show": False})

    def wash(self, first=0, last=4 * 3600, load=washer):
        """The plug reports load(s) every 10 s, as the poller reads it – and the poller hands it on."""
        reading = self.service._wifi_plugs[PLUG]
        for s in range(first, last, 10):
            reading.update(heard=T0 + s, power=load(s), energy_wh=None)
            self.service._record_energy(T0 + s)

    def at(self, s: float):   # the app's clocks at s seconds after T0
        local = datetime.fromtimestamp(T0 + s, BERLIN).replace(tzinfo=None)
        return mock.patch("heizapp.service.time.time", return_value=T0 + s), \
            mock.patch.object(type(self.service), "now", return_value=local)

    def state(self, s: float) -> dict:
        clock, local = self.at(s)
        with clock, local:
            return self.service.state()

    def test_a_wash_on_the_start_page(self):
        self.wash(0, 60 * 60)
        plug = next(p for p in self.state(3600)["plugs"] if p["id"] == PLUG)
        self.assertEqual(plug["run"]["since"], "2026-11-26T10:30:00+01:00")
        self.assertEqual(plug["run"]["minutes"], 30)
        self.assertEqual(self.state(3600)["notices"], [])
        self.wash(60 * 60, 3 * 3600)
        [notice] = self.state(3 * 3600)["notices"]
        self.assertEqual(notice["text"], "Die Waschmaschine ist fertig – seit 11:56.")
        self.assertEqual(notice["plug"], PLUG)
        self.assertEqual(next(p for p in self.state(3 * 3600)["plugs"] if p["id"] == PLUG)["run"], None)
        self.assertEqual(self.state(116 * 60 + 8 * 3600)["notices"][0]["text"],   # 8 hours later, the same day
                         "Die Waschmaschine ist fertig – seit 11:56.")
        for days, text in ((1, "gestern 11:56"), (2, "26.11. 11:56")):
            with mock.patch.object(type(self.service), "now", return_value=datetime(2026, 11, 26 + days, 9, 0)):
                self.assertEqual(self.service._since_text(T0 + 116 * 60), text)
        self.assertEqual(self.service.notice_ok(notice["id"]), {})
        self.assertEqual(self.state(3 * 3600)["notices"], [])
        with self.assertRaises(ActionError) as caught:
            self.service.notice_ok(notice["id"])
        self.assertEqual(caught.exception.status, 404)

    def test_the_runs_in_strom(self):
        self.wash(0, 3 * 3600)
        clock, local = self.at(4 * 3600)
        with clock, local:
            day = self.service.power_view("day", "2026-11-26")
            week = self.service.power_view("week", "2026-11-26")
            fridge = self.service.set_plug_appliance(PLUG, {"appliance": "kuehlschrank"})
            other = self.service.power_view("day", "2026-11-26")
        runs = next(x for x in day["devices"] if x["id"] == PLUG)["runs"]
        wh = round(energy_of(washer, 30 * 60, 116 * 60), 1)
        self.assertEqual((runs["count"], runs["wh"], runs["avg_wh"], runs["before"], runs["running"]),
                         (1, wh, wh, None, None))
        self.assertEqual(runs["items"], [{"start": int(T0) + 1800, "end": int(T0) + 6960, "wh": wh,
                                          "eur": round(wh / 1000 * 30 / 100, 2), "w_max": 2050.0, "heat_min": 15}])
        self.assertIsNone(next(x for x in week["devices"] if x["id"] == PLUG)["runs"]["before"])   # not measured before
        self.assertIn("Kühlschrank", fridge["message"])
        self.assertEqual(next(x for x in other["devices"] if x["id"] == PLUG)["runs"]["count"], 1)   # the wash stays
        clock, local = self.at(4 * 3600)
        with clock, local:
            one = self.service.run_view(PLUG, str(int(T0) + 1800))
        self.assertEqual((one["label"], len(one["minutes"]), one["heat_min"]), ("Heute, 10:30–11:56 Uhr", 86, 15))
        self.assertEqual((one["prev"], one["next"], one["day"]), (None, None, str(int(T0) + 1800)))
        with self.assertRaises(ActionError) as caught:
            self.service.run_view(PLUG, "12345")
        self.assertEqual(caught.exception.status, 404)

    def test_another_device_forgets_the_run_going_on(self):
        self.wash(0, 3 * 3600)
        self.wash(3 * 3600, 4 * 3600, load=lambda s: washer(s - 3 * 3600))   # the next wash runs
        self.assertIsNotNone(self.service.cycles.running(PLUG, T0 + 4 * 3600))
        self.service.set_plug_appliance(PLUG, {"appliance": "trockner"})   # it was the dryer all along
        self.assertIsNone(self.service.cycles.running(PLUG, T0 + 4 * 3600))
        self.assertEqual(self.state(4 * 3600)["notices"], [])


class ApiTest(ServiceTest):
    """Over the web API: „OK“ without PIN, a run only with it."""

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
        self.wash(0, 3 * 3600)
        start = int(T0) + 1800
        self.assertEqual(len(self.fetch("GET", f"/api/plugs/{PLUG}/runs/{start}")["minutes"]), 86)
        self.fetch("GET", f"/api/plugs/{PLUG}/runs/{start + 1}", expect=404)
        [notice] = self.service.cycles.notices(T0 + 3 * 3600)
        self.fetch("PUT", "/api/setup/pin", {"pin": "4711"})   # from now on the setup needs the PIN …
        self.fetch("GET", f"/api/plugs/{PLUG}/runs/{start}", expect=403)
        self.fetch("POST", f"/api/notices/{notice['id']}/ok")   # … „OK“ on the start page does not
        self.fetch("POST", f"/api/notices/{notice['id']}/ok", expect=404)

    def test_a_wash_on_the_start_page(self):   # the service tests above are not run again over the API
        pass

    test_the_runs_in_strom = test_another_device_forgets_the_run_going_on = test_a_wash_on_the_start_page


if __name__ == "__main__":
    unittest.main()
