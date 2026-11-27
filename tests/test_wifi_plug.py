"""Tests for the WLAN plugs with power metering (Shelly, e.g. Plug M Gen3): found by the search, taken over, assigned
to an AeroFlow at the plug (Geräte › Steckdosen; until 0.4.14 under Strom und Kosten, which PUT /api/costs still takes),
measuring while it heats, switched on again from the tile, switched on and off and renamed in Geräte › Steckdosen.

The plugs are played by tests/fake_shelly.py – as Shelly's documentation describes them –, the heaters by
tests/fake_heater.py; nothing real is reached. How the real plug reports is to be checked on the device.
"""
from __future__ import annotations

import ipaddress
import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_heater import FakeHeater  # noqa: E402
from fake_shelly import FakePill, FakePlug, FakeShelly  # noqa: E402
from heizapp import shelly  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import PLUG_MIN_SAMPLES, ActionError, Service  # noqa: E402

PLUG, SPARE, LOCKED = "shellyplugmg3-aabbccdd0001", "shellyplugmg3-aabbccdd0002", "shellyplugmg3-aabbccdd0004"
AERO, SECOND = "91110001", "92220002"
HOME = [ipaddress.ip_network("192.168.5.0/24")]


class Base(unittest.TestCase):
    """Two AeroFlow heaters; in the WLAN two Shelly plugs, one with a password, a power strip, a Pill and an H&T."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.plug = FakePlug(ident=PLUG, name="Küche")
        self.spare = FakePlug(ident=SPARE)
        self.locked = FakePlug(ident=LOCKED, password=True)
        strip = FakePlug(ident="shellypowerstrip-aabbccdd0003", model="S4PL-00416EU", app="PowerStripG4", switches=4)
        self.shellies = [self.plug, self.spare, FakePill(), FakeShelly(), strip, self.locked]
        for fake in self.shellies:
            self.addCleanup(fake.close)
        self.fakes = [FakeHeater(token="A" * 24, idu=AERO), FakeHeater(token="B" * 24, idu=SECOND)]
        for fake in self.fakes:
            self.addCleanup(fake.close)
        self.aeroflow = self.fakes[0]
        self.conf = self.tmp / "heizungen.conf"
        self.conf.write_text("".join(f"127.0.0.1:{f.port} {f.token}\n" for f in self.fakes))
        self.service = self.new_service()

    def new_service(self) -> Service:
        svc = Service(str(self.conf), str(self.tmp / "data.json"), poll_interval=3600)
        for e in svc.entries:
            e.device.timeout, e.device.retries = 0.5, 1
        svc._sensor_targets = lambda nets: [("127.0.0.1", f.port) for f in self.shellies]   # the sweep finds the fakes
        svc.poll_all(full=True)
        return svc

    def heater(self, ident=AERO) -> dict:
        return next(h for h in self.service.state()["heaters"] if h["id"] == ident)

    def adopt(self, pid=PLUG) -> dict:
        self.service.find_plugs("192.168.5")
        return self.service.add_wifi_plug({"plug": pid})

    def assign(self, plugs: dict) -> dict:
        return self.service.set_costs({"price_ct": "32,5", "show": False, "plugs": plugs})

    def polls(self, times: int = 1) -> None:
        for _ in range(times):
            self.service.poll_all()

    def saved(self) -> dict:
        return self.service.store.data["plugs"]


class RecognitionTest(unittest.TestCase):
    def test_which_shellies_are_plugs(self):
        def kind(**info):
            return shelly.kind_of(info)
        self.assertEqual(kind(gen=3, model="S3PL-30110EU", app="PlugMG3"), "plug")         # Plug M Gen3
        self.assertEqual(kind(gen=2, model="SNPL-00112EU", app="PlusPlugS"), "plug")       # Plus Plug S
        self.assertEqual(kind(gen=4, model="S4XX-00000EU", app="NewPlugG4"), "plug")       # a model the app does not know
        self.assertEqual(kind(gen=3, model="S3SN-0U53X", app="Pill"), "pill")
        self.assertIsNone(kind(model="SHPLG-S"))                                           # generation 1: other language
        self.assertIsNone(kind(gen=3, model="S3SW-001P16EU", app="S1PMG3"))                # a relay, no plug
        self.assertEqual(shelly.plug_model({"model": "S3PL-30110EU"}), "Shelly Plug M Gen3")
        self.assertEqual(shelly.plug_model({"model": "S4XX-00000EU", "app": "NewPlugG4"}), "Shelly NewPlugG4")


class SearchTest(Base):
    def test_the_search_finds_plugs_that_measure(self):
        res = self.service.find_plugs("192.168.5")
        self.assertEqual(res["networks"], ["192.168.5.0/24"])
        self.assertEqual([p["id"] for p in res["plugs"]], [PLUG, SPARE, LOCKED])   # no Pill, no H&T, no power strip
        self.assertEqual({k: res["plugs"][0][k] for k in ("name", "device_name", "model", "on", "power", "protected",
                                                          "known")},
                         {"name": "Küche", "device_name": "Küche", "model": "Shelly Plug M Gen3",
                          "on": True, "power": 0.0, "protected": False, "known": False})
        self.assertEqual((res["plugs"][2]["protected"], res["plugs"][2]["on"]), (True, None))
        self.assertEqual([p["id"] for p in self.service.find_sensors("192.168.5")["pills"]], ["shellypill-aabbccddeeff"])
        for fake in self.shellies:   # searching only reads
            self.assertEqual(fake.calls, [])
            self.assertEqual({m for m, _ in fake.requests}, {"GET"})

    def test_taking_a_plug_over(self):
        with self.assertRaises(ActionError) as ctx:   # not found by a search yet
            self.service.add_wifi_plug({"plug": PLUG})
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(self.adopt()["message"], "Küche ist in der App.")
        meta = self.saved()[PLUG]
        self.assertEqual({k: meta[k] for k in ("host", "model", "mac", "device_name")},
                         {"host": f"127.0.0.1:{self.plug.port}", "model": "Shelly Plug M Gen3", "mac": "AABBCCDD0001",
                          "device_name": "Küche"})
        self.assertEqual(meta["day"]["wh"], 12340.0)   # where its counter stood: today's energy starts here
        self.assertEqual(self.service.state()["plugs"], [
            {"id": PLUG, "kind": "wifi", "name": "Küche", "model": "Shelly Plug M Gen3", "heater": None,
             "found": True, "state": "ON", "power": 0.0, "energy_today": 0.0, "address": f"127.0.0.1:{self.plug.port}",
             "device_name": "Küche", "room": None, "error": None}])
        self.assertEqual(self.plug.calls, [])   # taking over only reads
        self.assertEqual(self.service.find_plugs("192.168.5")["plugs"][0]["known"], True)
        with self.assertRaises(ActionError) as ctx:
            self.service.add_wifi_plug({"plug": LOCKED})
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("mit einem Passwort geschützt", str(ctx.exception))
        self.assertNotIn(LOCKED, self.saved())


class AssignTest(Base):
    def test_assigning_a_wifi_plug(self):
        self.adopt()
        self.assertEqual(self.assign({AERO: PLUG}), {"message": "Gespeichert."})
        self.assertEqual(self.plug.calls, [("Switch.SetConfig", {"id": 0, "config": {"initial_state": "on"}})])
        self.assertEqual(self.plug.initial_state, "on")   # after a power cut it comes on again
        p = self.heater()["plug"]
        self.assertEqual({k: p[k] for k in ("id", "kind", "name", "found", "state", "after_outage", "error", "samples")},
                         {"id": PLUG, "kind": "wifi", "name": "Küche", "found": True, "state": "ON",
                          "after_outage": "on", "error": None, "samples": 0})
        self.assertEqual([p["heater"] for p in self.service.state()["plugs"]], [AERO])
        self.assign({AERO: PLUG})   # saved again: nothing goes out
        self.assertEqual(len(self.plug.calls), 1)
        for plugs, message in [({SECOND: PLUG}, "Küche kann nur zu einer Heizung gehören."),
                               ({SECOND: SPARE}, "Heat4U-922002: Diese WLAN-Steckdose ist nicht (mehr) in der App.")]:
            with self.assertRaises(ActionError) as ctx:
                self.assign(plugs)
            self.assertEqual(str(ctx.exception), message)
        self.assign({AERO: ""})
        self.assertIsNone(self.heater()["plug"])
        self.assertIn(PLUG, self.saved())   # still a plug of the app, for another heater

    def test_choosing_the_heater_at_the_plug(self):
        """Geräte › Steckdosen (since 0.4.15): the plug picks its heater, like a sensor its room."""
        self.adopt()
        self.assertEqual(self.service.set_plug_heater(PLUG, {"heater": AERO}),
                         {"message": "Küche gehört jetzt zu Heat4U-911001."})
        self.assertEqual(self.plug.calls, [("Switch.SetConfig", {"id": 0, "config": {"initial_state": "on"}})])
        self.assertEqual(self.heater()["plug"]["id"], PLUG)
        self.adopt(SPARE)
        self.service.set_plug_heater(SPARE, {"heater": AERO})   # a heater hangs on one plug: the first one is free again
        self.assertEqual([(p["id"], p["heater"]) for p in self.service.state()["plugs"]], [(PLUG, None), (SPARE, AERO)])
        self.assertEqual(self.spare.initial_state, "on")
        self.plug.close()   # unplugged: the choice is saved anyway, with a warning
        self.assertEqual(self.service.set_plug_heater(PLUG, {"heater": SECOND}), {
            "message": "Gespeichert. Küche war gerade nicht zu erreichen – bitte später noch einmal "
                       "auswählen, damit sie nach einem Stromausfall wieder angeht.", "warning": True})
        self.assertEqual(self.service.store.data["heaters"][SECOND]["plug"], PLUG)
        with self.assertRaises(ActionError) as ctx:   # never taken over
            self.service.set_plug_heater(LOCKED, {"heater": AERO})
        self.assertEqual(ctx.exception.status, 404)

    def test_saved_while_the_plug_is_unplugged(self):
        self.adopt()
        self.plug.close()
        self.assertEqual(self.assign({AERO: PLUG}), {
            "message": "Gespeichert. Küche war gerade nicht zu erreichen – bitte später noch einmal "
                       "speichern, damit sie nach einem Stromausfall wieder angeht.", "warning": True})
        self.assertEqual(self.service.store.data["heaters"][AERO]["plug"], PLUG)   # the assignment is saved anyway


class MeasureTest(Base):
    def setUp(self):
        super().setUp()
        self.adopt()
        self.assign({AERO: PLUG})

    def test_the_power_while_heating(self):
        self.plug.load = 1302.4
        self.polls(PLUG_MIN_SAMPLES)   # the heater does not heat: nothing counts
        self.assertEqual(self.heater()["plug"]["samples"], 0)
        self.aeroflow.values["heater_state"] = 1
        self.polls(PLUG_MIN_SAMPLES - 1)
        p = self.heater()["plug"]
        self.assertEqual((p["samples"], p["measured_w"], p["power"]), (PLUG_MIN_SAMPLES - 1, None, 1302.4))
        self.polls()
        self.assertEqual(self.heater()["plug"]["measured_w"], 1302)
        with self.assertLogs("heizapp", "INFO") as said:
            self.service.log_summary()
        self.assertIn(", Steckdose 1302 W)", said.output[-1])
        self.plug.on = False   # someone pressed its button: the heater has no power
        self.aeroflow.online = False
        self.polls()
        p = self.heater()["plug"]
        self.assertEqual((p["state"], p["power"], p["samples"]), ("OFF", 0.0, PLUG_MIN_SAMPLES))
        with self.assertLogs("heizapp", "INFO") as said:
            self.service.log_summary()
        self.assertRegex(said.output[-1], r"Heat4U-911001 nicht erreichbar seit [^·]*, Steckdose aus")

    def test_energy_today(self):
        self.plug.energy_wh += 1500
        self.polls()
        self.assertEqual(self.heater()["plug"]["energy_today"], 1.5)
        self.service = self.new_service()   # after a restart the same day: still counted from the morning
        self.assertEqual(self.heater()["plug"]["energy_today"], 1.5)
        with self.service.store.edit() as d:   # the next day: counted from its first reading
            d["plugs"][PLUG]["day"]["date"] = "2026-01-01"
        self.polls()
        self.assertEqual(self.heater()["plug"]["energy_today"], 0.0)
        self.plug.energy_wh = 10.0   # its counter was reset
        self.polls()
        self.assertEqual(self.heater()["plug"]["energy_today"], 0.0)
        self.plug.energy_wh = 260.0
        self.polls()
        self.assertEqual(self.heater()["plug"]["energy_today"], 0.25)

    def test_a_plug_that_switched_itself_off(self):
        self.plug.on, self.plug.errors = False, ["overtemp"]
        with self.assertLogs("heizapp", "WARNING") as said:
            self.polls()
        self.assertIn("Küche meldet: overtemp", said.output[-1])
        p = self.heater()["plug"]
        self.assertEqual((p["state"], p["error"]), ("OFF", "Die Steckdose ist zu heiß geworden und hat abgeschaltet."))

    def test_unplugged_and_found_at_a_new_address(self):
        with self.service.store.edit() as d:   # the router gave its address to the other plug
            d["plugs"][PLUG]["host"] = f"127.0.0.1:{self.spare.port}"
        self.service._plugs_searched = float("inf")   # no search yet
        self.polls()
        self.assertTrue(self.heater()["plug"]["found"])   # one round without an answer: still its last values
        self.service._wifi_plugs[PLUG]["mono"] -= 3600   # its last answer was an hour ago
        p = self.heater()["plug"]
        self.assertEqual((p["found"], p["error"]), (False, "Die Steckdose antwortet nicht – ist sie eingesteckt und im WLAN?"))
        self.service._plugs_searched = float("-inf")
        with mock.patch("heizapp.service.discovery.local_networks", return_value=HOME):
            self.polls()   # searched for in the home network: found by its id
        self.assertEqual(self.saved()[PLUG]["host"], f"127.0.0.1:{self.plug.port}")
        self.assertTrue(self.heater()["plug"]["found"])
        self.assertEqual(self.spare.calls, [])   # nothing went to the other plug


class ApiTest(Base):
    """Over the web API: the tile switches a WLAN plug on again – without PIN; search, take over, remove need the setup."""

    def setUp(self):
        super().setUp()
        self.server = make_server(self.service, "127.0.0.1", 0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, method, path, body=None, expect=200):
        data = json.dumps(body if body is not None else {}).encode() if method != "GET" else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                status, raw = res.status, res.read()
        except urllib.error.HTTPError as err:
            with err:
                status, raw = err.code, err.read()
        self.assertEqual(status, expect, raw)
        return json.loads(raw or b"{}")

    def test_switching_a_wifi_plug_on_again_from_the_tile(self):
        self.assertEqual([p["id"] for p in self.call("POST", "/api/plugs/discover", {"network": "192.168.5"})["plugs"]],
                         [PLUG, SPARE, LOCKED])
        self.call("POST", "/api/plugs", {"plug": PLUG})
        self.call("PUT", "/api/costs", {"price_ct": "", "plugs": {AERO: PLUG}})
        self.call("PUT", "/api/setup/pin", {"pin": "1234"})
        for method, path in [("POST", "/api/plugs/discover"), ("POST", "/api/plugs"), ("DELETE", f"/api/plugs/{PLUG}"),
                             ("PUT", f"/api/plugs/{PLUG}/heater")]:
            self.assertTrue(self.call(method, path, {"plug": SPARE}, expect=403)["pin_required"])
        self.plug.on = False   # someone pressed the plug's button
        self.service.poll_all()
        h = next(h for h in self.call("GET", "/api/state")["heaters"] if h["id"] == AERO)
        self.assertEqual(h["plug"]["state"], "OFF")
        answer = self.call("POST", f"/api/heaters/{AERO}/plug/on")   # the residents: without PIN
        self.assertEqual(answer["message"], "Die Steckdose ist wieder an. Bis sich die Heizung meldet, kann es ein, zwei "
                                            "Minuten dauern.")
        self.assertEqual(self.plug.calls[-1], ("Switch.Set", {"id": 0, "on": True}))
        self.assertTrue(self.plug.on)
        h = next(h for h in self.call("GET", "/api/state")["heaters"] if h["id"] == AERO)
        self.assertEqual(h["plug"]["state"], "ON")   # at once, not only after the next round
        self.assertNotIn(("Switch.Set", {"id": 0, "on": False}), self.plug.calls)   # the tile never switches off

    def test_switching_a_wifi_plug_off_and_on_in_the_setup(self):
        self.call("POST", "/api/plugs/discover", {"network": "192.168.5"})
        self.call("POST", "/api/plugs", {"plug": PLUG})
        self.call("PUT", f"/api/plugs/{PLUG}/heater", {"heater": AERO})
        answer = self.call("POST", f"/api/plugs/{PLUG}/switch", {"on": False})
        self.assertEqual(answer["message"], "Küche ist aus. Heat4U-911001 hat jetzt keinen Strom, bis die Steckdose "
                                            "wieder an ist.")
        self.assertEqual(self.plug.calls[-1], ("Switch.Set", {"id": 0, "on": False}))
        self.assertFalse(self.plug.on)
        h = next(h for h in self.call("GET", "/api/state")["heaters"] if h["id"] == AERO)
        self.assertEqual(h["plug"]["state"], "OFF")   # at once: the start page offers „Steckdose einschalten“
        self.assertEqual(self.call("POST", f"/api/plugs/{PLUG}/switch", {"on": True})["message"], "Küche ist an.")
        self.assertEqual(self.plug.calls[-1], ("Switch.Set", {"id": 0, "on": True}))
        self.assertTrue(self.plug.on)
        self.call("PUT", f"/api/plugs/{PLUG}/heater", {"heater": None})
        self.assertEqual(self.call("POST", f"/api/plugs/{PLUG}/switch", {"on": False})["message"], "Küche ist aus.")
        self.call("POST", f"/api/plugs/{PLUG}/switch", {"on": "aus"}, expect=400)
        self.call("POST", f"/api/plugs/{SPARE}/switch", {"on": False}, expect=404)   # found, but not taken over
        self.assertEqual(self.spare.calls, [])
        self.call("PUT", "/api/setup/pin", {"pin": "1234"})
        for method, path in [("POST", f"/api/plugs/{PLUG}/switch"), ("PUT", f"/api/plugs/{PLUG}/name")]:
            self.assertTrue(self.call(method, path, {"on": True, "name": "x"}, expect=403)["pin_required"])
        self.assertFalse(self.plug.on)

    def test_a_wifi_plug_that_does_not_switch(self):
        self.call("POST", "/api/plugs/discover", {"network": "192.168.5"})
        self.call("POST", "/api/plugs", {"plug": PLUG})
        with mock.patch("heizapp.service.shelly.switch"):   # the plug takes the command, but stays on
            answer = self.call("POST", f"/api/plugs/{PLUG}/switch", {"on": False}, expect=504)
        self.assertEqual(answer["error"], "Die Steckdose hat nicht ausgeschaltet. Bitte noch einmal versuchen – oder an "
                                          "der Steckdose die Taste einmal drücken.")
        self.plug.close()
        self.call("POST", f"/api/plugs/{PLUG}/switch", {"on": False}, expect=504)

    def test_renaming_a_wifi_plug(self):
        self.call("POST", "/api/plugs/discover", {"network": "192.168.5"})
        self.call("POST", "/api/plugs", {"plug": PLUG})
        answer = self.call("PUT", f"/api/plugs/{PLUG}/name", {"name": "  Werkstatt\n hinten "})
        self.assertEqual(answer["message"], "Die Steckdose heißt jetzt „Werkstatt hinten“.")
        self.assertEqual(self.plug.calls[-1], ("Sys.SetConfig", {"config": {"device": {"name": "Werkstatt hinten"}}}))
        self.assertEqual(self.plug.info["name"], "Werkstatt hinten")   # in the plug: the Shelly app shows it too
        self.assertEqual(self.saved()[PLUG]["device_name"], "Werkstatt hinten")
        self.assertEqual([p["name"] for p in self.call("GET", "/api/state")["plugs"]], ["Werkstatt hinten"])
        self.service._plug_names_read.clear()
        self.service.poll_all()   # the name read back from the plug every ten minutes: the same
        self.assertEqual(self.saved()[PLUG]["device_name"], "Werkstatt hinten")
        answer = self.call("PUT", f"/api/plugs/{PLUG}/name", {"name": ""})   # no name: as without one
        self.assertEqual(answer["message"], "Die Steckdose heißt jetzt „WLAN-Steckdose 0001“.")
        self.assertEqual(self.plug.calls[-1], ("Sys.SetConfig", {"config": {"device": {"name": None}}}))
        self.assertIsNone(self.plug.info["name"])
        self.call("PUT", f"/api/plugs/{PLUG}/name", {"name": 7}, expect=400)
        self.call("PUT", f"/api/plugs/{SPARE}/name", {"name": "Keller"}, expect=404)   # found, but not taken over
        self.assertEqual(self.spare.calls, [])
        self.plug.close()
        self.assertEqual(self.call("PUT", f"/api/plugs/{PLUG}/name", {"name": "Keller"}, expect=504)["error"],
                         "Die Steckdose antwortet nicht unter ihrer letzten Adresse. Ist sie eingesteckt und im WLAN?")

    def test_a_wifi_plug_that_refuses_its_name(self):
        self.call("POST", "/api/plugs/discover", {"network": "192.168.5"})
        self.call("POST", "/api/plugs", {"plug": PLUG})
        self.plug.password = True   # a password set in the Shelly app since it was taken over
        answer = self.call("PUT", f"/api/plugs/{PLUG}/name", {"name": "Keller"}, expect=409)
        self.assertEqual(answer["error"], "Die Steckdose ist mit einem Passwort geschützt – die App kann sie nicht "
                                          "umbenennen.")
        self.assertEqual(self.saved()[PLUG]["device_name"], "Küche")

    def test_a_wifi_plug_that_does_not_answer(self):
        self.call("POST", "/api/plugs/discover", {"network": "192.168.5"})
        self.call("POST", "/api/plugs", {"plug": PLUG})
        self.call("PUT", "/api/costs", {"price_ct": "", "plugs": {AERO: PLUG}})
        self.plug.close()
        answer = self.call("POST", f"/api/heaters/{AERO}/plug/on", expect=504)
        self.assertEqual(answer["error"], "Die Steckdose antwortet nicht unter ihrer letzten Adresse. Ist sie eingesteckt "
                                          "und im WLAN?")

    def test_removing_a_wifi_plug(self):
        self.call("POST", "/api/plugs/discover", {"network": "192.168.5"})
        self.call("POST", "/api/plugs", {"plug": PLUG})
        self.assertEqual(self.call("PUT", f"/api/plugs/{PLUG}/heater", {"heater": AERO})["message"],
                         "Küche gehört jetzt zu Heat4U-911001.")
        answer = self.call("DELETE", f"/api/plugs/{PLUG}")
        self.assertEqual(answer["message"], "Die Steckdose ist aus der App entfernt. Sie selbst bleibt, wie sie ist – an "
                                            "oder aus.")
        state = self.call("GET", "/api/state")
        self.assertIsNone(next(h for h in state["heaters"] if h["id"] == AERO)["plug"])
        self.assertEqual(state["plugs"], [])
        self.assertNotIn("plug", self.service.store.data["heaters"][AERO])
        self.assertEqual(self.plug.calls, [("Switch.SetConfig", {"id": 0, "config": {"initial_state": "on"}})])
        self.call("DELETE", f"/api/plugs/{PLUG}", expect=404)


if __name__ == "__main__":
    unittest.main()
