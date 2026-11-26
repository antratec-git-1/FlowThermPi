"""Tests for the Zigbee plugs with power metering (like the SONOFF S60ZBTPF): recognised in the Zigbee network, assigned
to an AeroFlow under Strom und Kosten, measuring while it heats, switched on again from the tile.

Mosquitto and Zigbee2MQTT are played by tests/fake_mqtt.py, the AeroFlow heaters by tests/fake_heater.py – nothing real
is reached. How often and how exactly the real plug reports is to be checked on the device.
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

from fake_heater import FakeHeater  # noqa: E402
from fake_mqtt import FakeBroker, FakeZ2M, plug, thermostat  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import PLUG_MIN_SAMPLES, ActionError, Service  # noqa: E402
from heizapp.zigbee import Zigbee  # noqa: E402
from heizapp.zigbee_net import ZigbeeNet, _definition  # noqa: E402

PASSWORD = "app-passwort-nur-fuer-den-test"
TRV = "0x8c65a3fffe1d00aa"
PLUG = "0x8c65a3fffe1d00cc"
SPARE = "0x8c65a3fffe1d00dd"
AERO, SECOND = "91110001", "92220002"


def until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("timed out")


class Base(unittest.TestCase):
    """Two AeroFlow heaters, a thermostat and two plugs in a simulated Zigbee2MQTT."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.broker = FakeBroker(users={"flowthermpi": PASSWORD})
        self.addCleanup(self.broker.close)
        self.z2m = FakeZ2M(self.broker, channel=25, join_after=0.2, interview=0.2)
        self.z2m.start([thermostat(TRV), plug(PLUG), plug(SPARE)])
        (self.tmp / "zigbee-mqtt.json").write_text(json.dumps(
            {"host": "127.0.0.1", "port": self.broker.port, "user": "flowthermpi", "password": PASSWORD}))
        self.net = ZigbeeNet(self.tmp, retry=(0.05,))
        self.addCleanup(self.net.stop)
        self.net.start()
        until(lambda: self.net.problem() is None and all((self.net.thermostat(i) or {}).get("values")
                                                         for i in (TRV, PLUG, SPARE)))
        self.fakes = [FakeHeater(token="A" * 24, idu=AERO), FakeHeater(token="B" * 24, idu=SECOND)]
        for fake in self.fakes:
            self.addCleanup(fake.close)
        self.aeroflow = self.fakes[0]
        conf = self.tmp / "heizungen.conf"
        conf.write_text("".join(f"127.0.0.1:{f.port} {f.token}\n" for f in self.fakes))
        self.service = Service(str(conf), str(self.tmp / "data.json"), poll_interval=3600)
        for e in self.service.entries:
            e.device.timeout, e.device.retries = 0.5, 1
        self.service.attach_zigbee(self.net)
        self.service.poll_all(full=True)

    def heater(self, ident=AERO) -> dict:
        return next(h for h in self.service.state()["heaters"] if h["id"] == ident)

    def sets(self, ieee=PLUG) -> list[dict]:
        return [body for i, kind, body in self.z2m.commands if i == ieee and kind == "set"]

    def assign(self, plugs: dict) -> dict:
        return self.service.set_costs({"price_ct": "32,5", "show": False, "plugs": plugs})

    def reports(self, **values) -> None:
        """The plug reports by itself (the heater behind it heats, someone pressed its button …), the app hears it."""
        self.z2m.report(PLUG, **values)
        until(lambda: all(self.net.thermostat(PLUG)["values"].get(k) == v for k, v in values.items()))

    def polls(self, times: int = 1) -> None:
        for _ in range(times):
            self.service.poll_all()


class NetworkTest(Base):
    def test_plugs_and_thermostats_apart(self):
        devices = {d["ieee"]: d for d in self.net.view()["devices"]}
        self.assertEqual([(devices[i]["thermostat"], devices[i]["plug"]) for i in (TRV, PLUG)],
                         [(True, False), (False, True)])
        self.assertEqual({k: devices[PLUG][k] for k in ("type", "switch", "power_w", "energy_today")},
                         {"type": "Router", "switch": "ON", "power_w": 0, "energy_today": 0.42})
        self.assertEqual(self.net.plugs(), [{"ieee": PLUG, "model": "SONOFF S60ZBTPF"},
                                            {"ieee": SPARE, "model": "SONOFF S60ZBTPF"}])
        self.assertIsNone(self.net.plug(TRV))
        self.assertEqual(self.net.summary(), "Zigbee2MQTT 2.14.3 läuft · Kanal 25 · 3 Gerät(e), davon 1 Thermostat(e) "
                                             "und 2 Steckdose(n)")
        state = self.service.state()
        self.assertEqual([h["id"] for h in state["heaters"]], [AERO, SECOND, TRV])   # a plug is no heater
        self.assertEqual([{k: p[k] for k in ("id", "kind", "name", "model", "heater", "found", "state", "address")}
                          for p in state["plugs"]], [
            {"id": PLUG, "kind": "zigbee", "name": "Steckdose 00CC", "model": "SONOFF S60ZBTPF", "heater": None,
             "found": True, "state": "ON", "address": None},
            {"id": SPARE, "kind": "zigbee", "name": "Steckdose 00DD", "model": "SONOFF S60ZBTPF", "heater": None,
             "found": True, "state": "ON", "address": None}])
        self.assertIsNone(self.heater()["plug"])

    def test_only_one_switch_with_power_metering_counts(self):
        def is_plug(*exposes):
            device = {}
            _definition({"definition": {"model": "X1", "vendor": "Y", "exposes": list(exposes)}}, device)
            return device["plug"]
        switch = {"type": "switch", "features": [{"type": "binary", "name": "state", "property": "state"}]}
        power = {"type": "numeric", "name": "power", "property": "power", "unit": "W"}
        self.assertTrue(is_plug(switch, power))
        self.assertFalse(is_plug(switch))   # a plug without metering
        self.assertFalse(is_plug(power))    # a meter without a switch
        self.assertFalse(is_plug({"type": "switch", "endpoint": "l1", "features": [
            {"type": "binary", "name": "state", "property": "state_l1"}]}, power))   # several outlets
        self.assertFalse(is_plug({"type": "climate", "features": []}, switch, power))   # a thermostat stays one
        self.assertFalse(is_plug({"type": "switch", "features": "kaputt"}, power))

    def test_a_new_plug_joins_during_the_search(self):
        self.z2m.join_kind = "plug"
        self.net.search(True)
        joined = until(lambda: [p["ieee"] for p in self.net.plugs() if p["ieee"] not in (PLUG, SPARE)])
        self.assertEqual(len(joined), 1)
        self.assertEqual([h["kind"] for h in self.service.state()["heaters"]], ["aeroflow", "aeroflow", "zigbee"])


class AssignTest(Base):
    def test_assigning_a_plug(self):
        self.assertEqual(self.assign({AERO: PLUG}), {"message": "Gespeichert."})
        until(lambda: self.net.plug(PLUG)["power_on_behavior"] == "on")   # after a power cut it comes on again
        self.assertEqual(self.sets(), [{"power_on_behavior": "on"}])
        p = self.heater()["plug"]
        self.assertEqual({k: p[k] for k in ("id", "kind", "name", "found", "error", "state", "after_outage", "measured_w",
                                            "samples")},
                         {"id": PLUG, "kind": "zigbee", "name": "Steckdose 00CC", "found": True, "error": None,
                          "state": "ON", "after_outage": "on", "measured_w": None, "samples": 0})
        self.assertEqual(self.service.store.data["heaters"][AERO]["plug"], PLUG)
        self.assertEqual([p["heater"] for p in self.service.state()["plugs"]], [AERO, None])
        self.assign({AERO: PLUG})   # saved again: it comes on after a power cut already – nothing goes out
        self.assertEqual(self.sets(), [{"power_on_behavior": "on"}])
        self.assign({AERO: ""})
        self.assertIsNone(self.heater()["plug"])
        self.assertNotIn("plug", self.service.store.data["heaters"][AERO])

    def test_what_can_be_assigned(self):
        self.assign({AERO: PLUG})
        for plugs, message in [({SECOND: PLUG}, "Steckdose 00CC kann nur zu einer Heizung gehören."),
                               ({TRV: SPARE}, "Ein Thermostat hängt an keiner Steckdose – es hat Batterien."),
                               ({SECOND: TRV}, "Heat4U-922002: Diese Steckdose ist nicht (mehr) im Zigbee-Netz."),
                               ({SECOND: "0x8c65a3fffe1d0fff"},
                                "Heat4U-922002: Diese Steckdose ist nicht (mehr) im Zigbee-Netz.")]:
            with self.assertRaises(ActionError) as ctx:
                self.assign(plugs)
            self.assertEqual(str(ctx.exception), message)
        self.assertEqual(self.service.store.data["heaters"][AERO]["plug"], PLUG)   # nothing changed
        self.assign({AERO: "", SECOND: PLUG})   # moved in one go
        self.assertEqual([p["heater"] for p in self.service.state()["plugs"]], [SECOND, None])

    def test_saved_while_zigbee2mqtt_is_off(self):
        self.z2m.stop()
        until(lambda: self.net.problem() is not None)
        self.assertEqual(self.assign({AERO: PLUG}), {
            "message": "Gespeichert. Steckdose 00CC war gerade nicht zu erreichen – bitte später noch einmal speichern, "
                       "damit sie nach einem Stromausfall wieder angeht.", "warning": True})
        self.assertEqual(self.service.store.data["heaters"][AERO]["plug"], PLUG)   # the assignment is saved anyway
        self.z2m.start()
        until(lambda: self.net.problem() is None)
        self.assertEqual(self.assign({AERO: PLUG}), {"message": "Gespeichert."})
        until(lambda: self.net.plug(PLUG)["power_on_behavior"] == "on")


class MeasureTest(Base):
    def setUp(self):
        super().setUp()
        self.assign({AERO: PLUG})

    def test_the_power_while_heating(self):
        self.reports(power=1302.4)
        self.polls(PLUG_MIN_SAMPLES)   # the heater does not heat: nothing counts
        self.assertEqual(self.heater()["plug"]["samples"], 0)
        self.aeroflow.values["heater_state"] = 1
        self.polls(PLUG_MIN_SAMPLES - 1)
        p = self.heater()["plug"]
        self.assertEqual((p["samples"], p["measured_w"]), (PLUG_MIN_SAMPLES - 1, None))   # not enough yet
        self.polls()
        self.assertEqual(self.heater()["plug"]["measured_w"], 1302)   # a minute of heating
        self.reports(power=60.0)   # an odd value does not move the typical one
        self.polls()
        self.reports(power=0.4)    # the heater says it heats, the plug lets nothing through: not counted
        self.polls()
        p = self.heater()["plug"]
        self.assertEqual((p["samples"], p["measured_w"], p["power"]), (PLUG_MIN_SAMPLES + 1, 1302, 0.4))
        self.assign({AERO: SPARE})   # another plug: what was measured belongs to the old one
        self.assertEqual(self.heater()["plug"]["samples"], 0)

    def test_nothing_counts_without_zigbee2mqtt(self):
        self.reports(power=1302.4)
        self.aeroflow.values["heater_state"] = 1
        self.z2m.stop()
        until(lambda: self.net.problem() is not None)
        self.polls(2)
        self.assertEqual(self.heater()["plug"]["samples"], 0)

    def test_the_protocol_names_the_plug(self):
        self.reports(power=1302.4)
        with self.assertLogs("heizapp", "INFO") as said:
            self.service.log_summary()
        self.assertIn(", Steckdose 1302 W)", said.output[-1])
        self.reports(state="OFF", power=0)   # someone pressed its button: the heater has no power
        self.aeroflow.online = False
        self.polls()
        with self.assertLogs("heizapp", "INFO") as said:
            self.service.log_summary()
        self.assertRegex(said.output[-1], r"Heat4U-911001 nicht erreichbar seit [^·]*, Steckdose aus")


class ApiTest(Base):
    """Over the web API: the tile switches a plug on again – without PIN; assigning it needs the setup."""

    def setUp(self):
        super().setUp()
        self.zigbee = Zigbee(self.tmp)
        self.zigbee.net.stop()
        self.zigbee.net = self.net
        self.server = make_server(self.service, "127.0.0.1", 0, zigbee=self.zigbee)
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

    def test_switching_a_plug_on_again_from_the_tile(self):
        self.call("PUT", "/api/costs", {"price_ct": "", "plugs": {AERO: PLUG}})
        self.call("PUT", "/api/setup/pin", {"pin": "1234"})
        self.assertTrue(self.call("PUT", "/api/costs", {"plugs": {AERO: ""}}, expect=403)["pin_required"])
        self.reports(state="OFF", power=0)   # someone pressed the plug's button
        h = next(h for h in self.call("GET", "/api/state")["heaters"] if h["id"] == AERO)
        self.assertEqual(h["plug"]["state"], "OFF")
        answer = self.call("POST", f"/api/heaters/{AERO}/plug/on")   # the residents: without PIN
        self.assertEqual(answer["message"], "Die Steckdose ist wieder an. Bis sich die Heizung meldet, kann es ein, zwei "
                                            "Minuten dauern.")
        self.assertEqual(self.sets()[-1], {"state": "ON"})
        self.assertEqual(self.net.plug(PLUG)["state"], "ON")
        self.assertEqual(self.call("POST", f"/api/heaters/{SECOND}/plug/on", expect=409)["error"],
                         "Diese Heizung hängt an keiner Steckdose der App.")
        self.call("POST", f"/api/heaters/{TRV}/plug/on", expect=409)

    def test_a_plug_that_does_not_answer(self):
        self.assign({AERO: PLUG})
        self.reports(state="OFF", power=0)
        self.z2m.asleep.add(PLUG)
        with mock.patch("heizapp.service.PLUG_ANSWER", 0.3):
            answer = self.call("POST", f"/api/heaters/{AERO}/plug/on", expect=504)
        self.assertEqual(answer["error"], "Die Steckdose hat nicht geantwortet. Bitte noch einmal versuchen – oder an der "
                                          "Steckdose die Taste einmal drücken.")

    def test_removing_the_plug_in_the_zigbee_network(self):
        self.assign({AERO: PLUG})
        answer = self.call("POST", f"/api/zigbee/devices/{PLUG}/remove", {"force": False})
        self.assertEqual(answer["message"], "Die Steckdose ist entfernt.")
        h = next(h for h in self.call("GET", "/api/state")["heaters"] if h["id"] == AERO)
        self.assertIsNone(h["plug"])
        self.assertNotIn("plug", self.service.store.data["heaters"][AERO])


if __name__ == "__main__":
    unittest.main()
