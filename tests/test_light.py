"""Tests for the lamps over Zigbee (0.4.21): recognised by what they can do, switched per room and one by one, brighter
and darker, name, room and colour of light in Geräte › Licht – and switching the light stays out of the protocol.

Mosquitto and Zigbee2MQTT are played by tests/fake_mqtt.py – nothing real is reached. How the real lamps (IKEA FLOALT,
Innr, Hue) answer is to be checked on the devices.
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_mqtt import FakeBroker, FakeZ2M, light, light_state, plug, thermostat  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import ActionError, Service  # noqa: E402
from heizapp.zigbee import Zigbee  # noqa: E402
from heizapp.zigbee_net import ZigbeeNet, _definition  # noqa: E402

PASSWORD = "app-passwort-nur-fuer-den-test"
FLOALT, INNR, HUE = "0x8c65a3fffe1d00f1", "0x8c65a3fffe1d00f2", "0x8c65a3fffe1d00f3"
PLUG, TRV = "0x8c65a3fffe1d00cc", "0x8c65a3fffe1d00aa"


def until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("timed out")


class Base(unittest.TestCase):
    """Three lamps, a plug and a thermostat in a simulated Zigbee2MQTT; a Flur and a Wohnzimmer, no AeroFlow."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.broker = FakeBroker(users={"flowthermpi": PASSWORD})
        self.addCleanup(self.broker.close)
        self.z2m = FakeZ2M(self.broker, channel=25, join_after=0.2, interview=0.2)
        self.z2m.delay = 0.01
        self.z2m.states[FLOALT] = light_state(on=True, brightness=120, color_temp=300)
        self.z2m.states[INNR] = light_state(on=False)
        self.z2m.states[HUE] = light_state(on=False)
        self.z2m.start([light(FLOALT, "floalt"), light(INNR, "innr"), light(HUE, "hue"), plug(PLUG),
                        thermostat(TRV)])
        (self.tmp / "zigbee-mqtt.json").write_text(json.dumps(
            {"host": "127.0.0.1", "port": self.broker.port, "user": "flowthermpi", "password": PASSWORD}))
        self.net = ZigbeeNet(self.tmp, retry=(0.05,))
        self.addCleanup(self.net.stop)
        self.net.start()
        until(lambda: self.net.problem() is None and all((self.net.thermostat(i) or {}).get("values")
                                                         for i in (FLOALT, INNR, HUE, PLUG, TRV)))
        conf = self.tmp / "heizungen.conf"
        conf.write_text("")
        self.service = Service(str(conf), str(self.tmp / "data.json"), poll_interval=3600)
        self.service.attach_zigbee(self.net)
        self.flur = self.service.create_room({"name": "Flur"})["id"]
        self.wohn = self.service.create_room({"name": "Wohnzimmer"})["id"]

    def lamps(self) -> dict:
        return {x["id"]: x for x in self.service.state()["lights"]}

    def sets(self, ieee) -> list[dict]:
        return [body for i, kind, body in self.z2m.commands if i == ieee and kind == "set"]

    def place(self, lamp, room):
        self.service.set_light_room(lamp, {"room": room})


class RecognitionTest(Base):
    def test_lamps_by_what_they_can_do(self):
        lamps = self.lamps()
        self.assertEqual(list(lamps), [FLOALT, INNR, HUE])   # not the plug, not the thermostat
        self.assertEqual({k: lamps[FLOALT][k] for k in ("name", "model", "room", "state", "brightness", "dim", "colors",
                                                         "color", "lost")},
                         {"name": "Lampe 00F1", "model": "IKEA L1529", "room": None, "state": "ON", "brightness": 47,
                          "dim": True, "colors": ["warm", "neutral"], "color": None, "lost": False})
        self.assertEqual(lamps[INNR]["colors"], ["warm", "neutral", "kalt"])
        self.assertEqual((lamps[INNR]["state"], lamps[HUE]["model"]), ("OFF", "Philips 9290012573A"))
        self.assertEqual([p["id"] for p in self.service.state()["plugs"]], [PLUG])
        self.assertEqual(self.net.summary(), "Zigbee2MQTT 2.14.3 läuft · Kanal 25 · 5 Gerät(e), davon 1 Thermostat(e), "
                                             "1 Steckdose(n) und 3 Lampe(n)")

    def test_what_counts_as_a_lamp(self):
        def kind(exposes):
            device = {}
            _definition({"definition": {"model": "X", "vendor": "Y", "exposes": exposes}}, device)
            return device["light"], device["light_caps"], device["plug"]
        state = {"type": "binary", "name": "state", "property": "state"}
        self.assertEqual(kind([{"type": "light", "features": [state]}]),
                         (True, {"dim": False, "temp": None, "color": False}, False))   # only on and off
        two = [{"type": "light", "endpoint": e, "features": [{**state, "property": f"state_{e}"}]} for e in ("l1", "l2")]
        self.assertEqual(kind(two), (False, None, False))   # two lights in one device: not a lamp of the app
        self.assertEqual(kind([{"type": "switch", "features": [state]},
                               {"type": "numeric", "name": "power", "property": "power"}]), (False, None, True))
        broken = {"type": "numeric", "name": "color_temp", "property": "color_temp", "value_min": 500, "value_max": 153}
        self.assertEqual(kind([{"type": "light", "features": [state, broken]}])[1]["temp"], None)

    def test_a_new_lamp_joins_during_the_search(self):
        self.z2m.join_kind, self.z2m.light_kind = "light", "innr"
        self.net.search(True)
        new = until(lambda: [x for x in self.lamps() if x not in (FLOALT, INNR, HUE)])
        self.assertEqual(self.lamps()[new[0]]["model"], "Innr RCL 240 T")
        zigbee = Zigbee(self.tmp)
        zigbee.net.stop()
        zigbee.net = self.net
        self.assertEqual(zigbee.remove_device(new[0])["message"], "Die Lampe ist entfernt.")
        self.assertNotIn(new[0], self.lamps())


class SwitchTest(Base):
    def test_the_room_light_on_and_off(self):
        self.place(FLOALT, self.wohn)
        self.place(HUE, self.wohn)
        self.place(INNR, self.flur)
        self.assertEqual(self.service.switch_room_light(self.wohn, {"on": True}), {"message": "Das Licht ist an."})
        self.assertEqual((self.sets(FLOALT)[-1], self.sets(HUE)[-1]), ({"state": "ON"}, {"state": "ON"}))
        self.assertEqual(self.sets(INNR), [])   # the Flur stays as it is
        self.assertEqual({k: v["state"] for k, v in self.lamps().items()}, {FLOALT: "ON", INNR: "OFF", HUE: "ON"})
        self.assertEqual(self.service.switch_room_light(self.wohn, {"on": False}), {"message": "Das Licht ist aus."})
        self.assertEqual((self.lamps()[FLOALT]["state"], self.lamps()[HUE]["state"]), ("OFF", "OFF"))
        with self.assertRaises(ActionError) as caught:
            self.service.switch_room_light(self.service.create_room({"name": "Keller"})["id"], {"on": True})
        self.assertEqual((str(caught.exception), caught.exception.status), ("In diesem Raum ist keine Lampe.", 404))
        with self.assertRaises(ValueError):
            self.service.switch_room_light(self.wohn, {"on": "ja"})

    def test_the_colour_comes_with_switching_on(self):
        self.place(FLOALT, self.wohn)
        self.place(INNR, self.wohn)
        self.service.switch_room_light(self.wohn, {"on": False})
        self.assertEqual(self.service.set_light_color(INNR, {"color": "kalt"}), {"message": "„Lampe 00F2“: Lichtfarbe "
                                                                                           "kaltweiß."})
        self.assertEqual(self.sets(INNR)[-1], {"state": "OFF"})   # switched off: nothing sent, it waits for „an“
        self.service.switch_room_light(self.wohn, {"on": True})
        self.assertEqual(self.sets(INNR)[-1], {"state": "ON", "color_temp": 200})
        self.assertEqual(self.sets(FLOALT)[-1], {"state": "ON"})   # without a colour: as it had it last
        self.assertEqual(self.z2m.states[INNR]["color_temp"], 200)
        self.service.set_light_color(FLOALT, {"color": "warm"})   # on: it gets the colour at once
        self.assertEqual(self.sets(FLOALT)[-1], {"color_temp": 370})
        with self.assertRaises(ActionError) as caught:
            self.service.set_light_color(FLOALT, {"color": "kalt"})   # FLOALT goes down to 250 mired (4000 K) only
        self.assertEqual(str(caught.exception), "Diese Lichtfarbe kann die Lampe nicht.")
        self.service.set_light_color(INNR, {"color": ""})
        self.assertIsNone(self.lamps()[INNR]["color"])

    def test_brighter_and_darker(self):
        self.place(FLOALT, self.wohn)
        self.place(HUE, self.wohn)   # off: it stays off
        answer = self.service.dim_room_light(self.wohn, {"step": 1})   # 47 % -> 60 %
        self.assertEqual(answer, {"message": "Helligkeit 60 %.", "brightness": 60})
        self.assertEqual((self.sets(FLOALT)[-1], self.sets(HUE)), ({"brightness": 152}, []))
        self.assertEqual(self.lamps()[FLOALT]["brightness"], 60)
        for _ in range(9):
            answer = self.service.dim_room_light(self.wohn, {"step": -1})
        self.assertEqual((answer["brightness"], self.lamps()[FLOALT]["state"]), (10, "ON"))   # never off
        self.assertEqual(answer["message"], "Dunkler geht es nicht – ganz aus mit „Licht aus“.")
        self.service.switch_room_light(self.wohn, {"on": True})
        self.assertEqual(self.service.dim_room_light(self.wohn, {"step": 1})["brightness"], 50)   # 10 % and 80 %: together
        self.assertEqual({self.lamps()[FLOALT]["brightness"], self.lamps()[HUE]["brightness"]}, {50})
        self.service.switch_light(HUE, {"on": False})
        self.service.dim_room_light(self.wohn, {"step": 1})   # FLOALT alone at 60 %, then HUE again at 50 %: 55 %
        self.service.switch_light(HUE, {"on": True})
        self.assertEqual(self.service.dim_room_light(self.wohn, {"step": 1})["brightness"], 60)   # half a step up …
        self.service.switch_light(HUE, {"on": False})
        self.service.dim_room_light(self.wohn, {"step": 1})   # FLOALT 70 %, HUE still 60 %: 65 %
        self.service.switch_light(HUE, {"on": True})
        self.assertEqual(self.service.dim_room_light(self.wohn, {"step": -1})["brightness"], 60)   # … and down
        self.service.switch_room_light(self.wohn, {"on": False})
        with self.assertRaises(ActionError) as caught:
            self.service.dim_room_light(self.wohn, {"step": 1})
        self.assertEqual((str(caught.exception), caught.exception.status),
                         ("Das Licht ist aus – zuerst „Licht an“ drücken.", 409))
        for wrong in (2, True, "1", None):
            with self.assertRaises(ValueError):
                self.service.dim_room_light(self.wohn, {"step": wrong})

    def test_one_lamp_alone(self):
        self.assertEqual(self.service.switch_light(INNR, {"on": True}), {"message": "„Lampe 00F2“ ist an."})
        self.assertEqual(self.lamps()[INNR]["state"], "ON")
        with self.assertRaises(ActionError) as caught:
            self.service.switch_light(PLUG, {"on": True})   # a plug is no lamp
        self.assertEqual(caught.exception.status, 404)

    def test_a_lamp_without_power(self):
        self.place(FLOALT, self.wohn)
        self.place(HUE, self.wohn)
        self.z2m.asleep.add(HUE)   # switched off at the wall: it hears nothing
        answer = self.service.switch_room_light(self.wohn, {"on": True})
        self.assertEqual(answer, {"message": "Das Licht ist an – aber „Lampe 00F3“ hat nicht geantwortet.", "warning": True})
        self.assertEqual((self.lamps()[HUE]["lost"], self.lamps()[FLOALT]["lost"]), (True, False))
        self.z2m.asleep.add(FLOALT)
        with self.assertRaises(ActionError) as caught:
            self.service.switch_room_light(self.wohn, {"on": False})
        self.assertEqual((str(caught.exception), caught.exception.status),
                         ("Die Lampen haben nicht geantwortet. Ist der Lichtschalter an der Wand an?", 504))
        self.z2m.asleep.clear()
        self.z2m.report(HUE, state="ON")   # it has power again and reports
        until(lambda: not self.lamps()[HUE]["lost"])
        self.assertTrue(self.lamps()[FLOALT]["lost"])
        self.z2m.report(FLOALT, state="ON")
        until(lambda: not self.lamps()[FLOALT]["lost"])
        self.z2m.asleep.add(HUE)
        with mock.patch("heizapp.service.LIGHT_ANSWER", 1.0), self.assertRaises(ActionError):
            self.service.switch_light(HUE, {"on": False})   # no power: still „on“ as last heard, but lost
        self.assertEqual((self.lamps()[HUE]["state"], self.lamps()[HUE]["lost"]), ("ON", True))
        with mock.patch("heizapp.service.LIGHT_ANSWER", 1.0):
            answer = self.service.dim_room_light(self.wohn, {"step": 1})   # without HUE – no waiting for it
        self.assertEqual(answer, {"message": "Helligkeit 60 %.", "brightness": 60})   # FLOALT from 47 %

    def test_without_zigbee2mqtt(self):
        self.place(FLOALT, self.wohn)
        self.z2m.stop()
        until(lambda: self.net.problem())
        with self.assertRaises(ActionError) as caught:
            self.service.switch_room_light(self.wohn, {"on": True})
        self.assertEqual((str(caught.exception), caught.exception.status), ("Zigbee2MQTT läuft gerade nicht.", 503))


class SetupTest(Base):
    def test_name_and_room(self):
        self.assertEqual(self.service.rename_light(FLOALT, {"name": "  Decke\n Wohnzimmer "}),
                         {"message": "Die Lampe heißt jetzt „Decke Wohnzimmer“."})
        self.assertEqual(self.service.set_light_room(FLOALT, {"room": self.wohn}),
                         {"message": "„Decke Wohnzimmer“ ist jetzt im Raum „Wohnzimmer“."})
        self.assertEqual((self.lamps()[FLOALT]["name"], self.lamps()[FLOALT]["room"]), ("Decke Wohnzimmer", self.wohn))
        self.service.rename_light(FLOALT, {"name": ""})
        self.assertEqual(self.lamps()[FLOALT]["name"], "Lampe 00F1")
        with self.assertRaises(ActionError) as caught:
            self.service.set_light_room(FLOALT, {"room": "r-gibtsnicht"})
        self.assertEqual(caught.exception.status, 404)
        self.service.delete_room(self.wohn)   # the room goes: the lamp is in none
        self.assertIsNone(self.lamps()[FLOALT]["room"])
        self.assertNotIn(FLOALT, self.service.store.data.get("light_rooms") or {})

    def test_a_removed_lamp_is_forgotten(self):
        self.service.rename_light(INNR, {"name": "Flur"})
        self.place(INNR, self.flur)
        self.service.set_light_color(INNR, {"color": "warm"})
        self.net.remove(INNR)
        self.service.forget_thermostat(INNR)
        data = self.service.store.data
        self.assertEqual([INNR in (data.get(k) or {}) for k in ("light_names", "light_rooms", "light_colors")],
                         [False] * 3)
        self.assertNotIn(INNR, self.lamps())


class ApiTest(Base):
    """Over the web API: the residents switch without PIN, name, room and colour need the setup – and the protocol
    does not learn when someone switches the light."""

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

    def test_residents_switch_without_pin(self):
        self.call("PUT", f"/api/lights/{INNR}/room", {"room": self.flur})
        self.call("PUT", "/api/setup/pin", {"pin": "1234"})
        self.assertTrue(self.call("PUT", f"/api/lights/{INNR}/name", {"name": "Flur"}, expect=403)["pin_required"])
        self.call("PUT", f"/api/lights/{INNR}/color", {"color": "warm"}, expect=403)
        self.call("PUT", f"/api/lights/{INNR}/room", {"room": ""}, expect=403)
        with self.assertLogs("heizapp.server", level=logging.DEBUG) as said:
            self.assertEqual(self.call("POST", f"/api/rooms/{self.flur}/light", {"on": True}),
                             {"message": "Das Licht ist an."})
            self.assertEqual(self.call("POST", f"/api/rooms/{self.flur}/light/dim", {"step": -1})["brightness"], 70)
            self.call("POST", f"/api/lights/{INNR}/switch", {"on": False})
            self.call("POST", f"/api/rooms/{self.flur}/light/dim", {"step": 1}, expect=409)   # off: an INFO line
        levels = [(r.levelname, r.getMessage().split(" →")[0]) for r in said.records]
        self.assertEqual(levels, [("DEBUG", f"POST /api/rooms/{self.flur}/light"),
                                  ("DEBUG", f"POST /api/rooms/{self.flur}/light/dim"),
                                  ("DEBUG", f"POST /api/lights/{INNR}/switch"),
                                  ("INFO", f"POST /api/rooms/{self.flur}/light/dim")])
        lamp = next(x for x in self.call("GET", "/api/state")["lights"] if x["id"] == INNR)
        self.assertEqual((lamp["room"], lamp["state"]), (self.flur, "OFF"))
        self.call("POST", "/api/lights/0x8c65a3fffe1d0bad/switch", {"on": True}, expect=404)

    def test_switching_says_nothing_in_the_service_log(self):
        self.call("PUT", f"/api/lights/{INNR}/room", {"room": self.flur})
        with self.assertLogs("heizapp", level=logging.DEBUG) as said:
            self.call("POST", f"/api/rooms/{self.flur}/light", {"on": True})
            self.call("POST", f"/api/rooms/{self.flur}/light", {"on": False})
        self.assertEqual([r.levelname for r in said.records if r.levelno >= logging.INFO], [])


if __name__ == "__main__":
    unittest.main()
