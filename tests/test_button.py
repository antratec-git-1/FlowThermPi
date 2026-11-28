"""Tests for the switches over Zigbee (0.4.23): recognised by what they report, a room's light switched through the app
until the switch is coupled – at its next press, while it is awake – then directly with a Zigbee group of its own that
holds the room's lamps; a press of a switch that sends on and off by turns that went into the void is made good; the
battery on the start page.

Mosquitto and Zigbee2MQTT are played by tests/fake_mqtt.py – nothing real is reached. Whether the real switches (IKEA
RODRET, Hue Smart Button, Hue Dimmschalter) take the coupling and still tell the stick what was pressed is to be checked
on the devices.
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

from fake_mqtt import FakeBroker, FakeZ2M, button, light, light_state, plug  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import Service  # noqa: E402
from heizapp.zigbee import Zigbee  # noqa: E402
from heizapp.zigbee_net import ZigbeeNet, _definition  # noqa: E402

PASSWORD = "app-passwort-nur-fuer-den-test"
FLOALT, INNR, HUE, PLUG = "0x8c65a3fffe1d00f1", "0x8c65a3fffe1d00f2", "0x8c65a3fffe1d00f3", "0x8c65a3fffe1d00cc"
RODRET, SMART, DIMMER, SONOFF = "0x8c65a3fffe1d00b1", "0x8c65a3fffe1d00b2", "0x8c65a3fffe1d00b3", "0x8c65a3fffe1d00b4"
QUICK = {"heizapp.service.BUTTON_CALM": 0.05, "heizapp.service.BUTTON_SAID": 0.05, "heizapp.service.BUTTON_ASK": 0.05,
         "heizapp.service.BUTTON_AFTER": 0.05}


def until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("timed out")


class Base(unittest.TestCase):
    """Three lamps, a plug and four switches in a simulated Zigbee2MQTT; a Flur and a Wohnzimmer – the Deckenlampe
    (FLOALT) and the Stehlampe (HUE) in the Wohnzimmer, the Innr in the Flur."""

    def setUp(self):
        for name, value in QUICK.items():   # the waits of a real press, short
            patcher = mock.patch(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.broker = FakeBroker(users={"flowthermpi": PASSWORD})
        self.addCleanup(self.broker.close)
        self.z2m = FakeZ2M(self.broker, channel=25, join_after=0.2, interview=0.2)
        self.z2m.delay = 0.01
        for lamp in (FLOALT, INNR, HUE):
            self.z2m.states[lamp] = light_state(on=False)
        self.z2m.start([light(FLOALT, "floalt"), light(INNR, "innr"), light(HUE, "hue"), plug(PLUG),
                        button(RODRET, "rodret"), button(SMART, "smart"), button(DIMMER, "dimmer"),
                        button(SONOFF, "sonoff")])
        (self.tmp / "zigbee-mqtt.json").write_text(json.dumps(
            {"host": "127.0.0.1", "port": self.broker.port, "user": "flowthermpi", "password": PASSWORD}))
        self.net = ZigbeeNet(self.tmp, retry=(0.05,))
        self.addCleanup(self.net.stop)
        self.net.start()
        until(lambda: self.net.problem() is None and all((self.net.thermostat(i) or {}).get("values")
                                                         for i in (FLOALT, INNR, HUE, RODRET, SMART, DIMMER, SONOFF)))
        conf = self.tmp / "heizungen.conf"
        conf.write_text("")
        self.service = Service(str(conf), str(self.tmp / "data.json"), poll_interval=3600)
        self.service.attach_zigbee(self.net)
        self.flur = self.service.create_room({"name": "Flur"})["id"]
        self.wohn = self.service.create_room({"name": "Wohnzimmer"})["id"]
        for lamp, room in ((FLOALT, self.wohn), (HUE, self.wohn), (INNR, self.flur)):
            self.service.set_light_room(lamp, {"room": room})
        self.settle()

    def settle(self):
        """Wait until the switch thread and the coupling thread have done everything sent to them so far."""
        for pool in (self.service._buttons, self.service._coupler, self.service._buttons):
            pool.submit(lambda: None).result(timeout=40)

    def tend(self):
        self.service._coupler.submit(self.service._tend_buttons)
        self.settle()

    def press(self, switch, which=""):
        self.z2m.press(switch, which)
        time.sleep(0.15)   # the messages reach the app …
        self.settle()      # … and it has done what they mean

    def buttons(self) -> dict:
        return {x["id"]: x for x in self.service.state()["buttons"]}

    def lamps(self) -> dict:
        return {x["id"]: x["state"] for x in self.service.state()["lights"]}

    def app_sets(self, *lamps) -> list:
        return [(i, body) for i, kind, body in self.z2m.commands if kind == "set" and i in lamps]

    def couple(self, switch, room):
        """Room and coupling of a switch that was just paired – still awake; then it sleeps."""
        self.z2m.awake[switch] = time.monotonic() + 5
        self.service.set_button_room(switch, {"room": room})
        self.settle()
        self.z2m.awake.pop(switch, None)
        self.assertTrue(self.buttons()[switch]["coupled"])


class RecognitionTest(Base):
    def test_switches_by_what_they_report(self):
        b = self.buttons()
        self.assertEqual(list(b), [RODRET, SMART, DIMMER, SONOFF])   # not the lamps, not the plug
        self.assertEqual({k: b[RODRET][k] for k in ("name", "model", "kind", "room", "direct", "coupled", "battery")},
                         {"name": "Schalter 00B1", "model": "IKEA E2201", "kind": "IKEA RODRET", "room": None,
                          "direct": True, "coupled": False, "battery": 90})
        self.assertEqual((b[SMART]["kind"], b[DIMMER]["kind"]), ("Hue Smart Button", "Hue Dimmschalter"))
        self.assertEqual((b[SONOFF]["kind"], b[SONOFF]["direct"]), (None, False))   # only through the app
        self.assertEqual([x["id"] for x in self.service.state()["lights"]], [FLOALT, INNR, HUE])
        self.assertIn("1 Steckdose(n), 3 Lampe(n) und 4 Schalter", self.net.summary())

    def test_what_counts_as_a_switch(self):
        def kind(exposes):
            device = {}
            _definition({"definition": {"model": "X", "vendor": "Y", "exposes": exposes}}, device)
            return device["button"], device["button_actions"]
        action = {"type": "enum", "name": "action", "property": "action", "values": ["single", "double", 7]}
        self.assertEqual(kind([action]), (True, ["single", "double"]))
        lamp = {"type": "light", "features": [{"type": "binary", "name": "state", "property": "state"}]}
        self.assertEqual(kind([lamp, action]), (False, None))   # a lamp with a button is a lamp

    def test_a_new_switch_joins_and_goes(self):
        self.z2m.join_kind, self.z2m.button_kind = "button", "smart"
        self.net.search(True)
        new = until(lambda: [x for x in self.buttons() if x not in (RODRET, SMART, DIMMER, SONOFF)])
        self.assertEqual(self.buttons()[new[0]]["kind"], "Hue Smart Button")
        self.service.set_button_room(new[0], {"room": self.flur})
        self.service.rename_button(new[0], {"name": "Knopf im Flur"})
        self.settle()
        self.assertIn(f"schalter-{new[0]}", self.z2m.groups)   # its group is there (it slept: not coupled yet)
        zigbee = Zigbee(self.tmp)
        zigbee.net.stop()
        zigbee.net = self.net
        self.assertEqual(zigbee.remove_device(new[0])["message"], "Der Schalter ist entfernt.")
        self.service.forget_thermostat(new[0])
        self.settle()
        self.assertNotIn(f"schalter-{new[0]}", self.z2m.groups)   # its group went with it
        self.assertNotIn(new[0], self.buttons())
        self.assertNotIn(new[0], self.service.store.data.get("button_rooms") or {})
        self.assertNotIn(new[0], self.service.store.data.get("button_names") or {})


class CoupleTest(Base):
    def test_coupled_at_the_next_press(self):
        said = self.service.set_button_room(RODRET, {"room": self.wohn})["message"]
        self.assertEqual(said, "„Schalter 00B1“ schaltet jetzt das Licht im Raum „Wohnzimmer“. Drück einmal eine Taste "
                               "am Schalter – dann koppelt ihn die App mit den Lampen.")
        self.settle()   # the app tried at once – the switch sleeps: no error, it is coupled at its next press
        b = self.buttons()[RODRET]
        self.assertEqual((b["room"], b["coupled"], b["error"]), (self.wohn, False, None))
        group = f"schalter-{RODRET}"
        self.assertEqual(sorted(self.z2m.groups[group]["members"]), [FLOALT, HUE])   # its group is ready
        self.assertEqual(self.net.groups()[group]["members"], self.z2m.groups[group]["members"])

        self.press(RODRET, "on")   # not coupled yet: the app switches – and couples it, it is awake now
        self.assertEqual(self.lamps(), {FLOALT: "ON", INNR: "OFF", HUE: "ON"})
        self.assertEqual(self.z2m.binds[RODRET], {group})
        b = self.buttons()[RODRET]
        self.assertEqual((b["coupled"], b["error"]), (True, None))
        self.assertIsNotNone(b["pressed"])

        sent = len(self.app_sets(FLOALT, HUE))
        self.press(RODRET, "off")   # coupled: the switch switches the lamps itself – the app sends nothing
        until(lambda: self.lamps() == {FLOALT: "OFF", INNR: "OFF", HUE: "OFF"})
        self.press(RODRET, "up")    # held: brighter – from off on
        until(lambda: self.lamps()[FLOALT] == "ON" and self.lamps()[HUE] == "ON")
        self.assertEqual(len(self.app_sets(FLOALT, HUE)), sent)
        bind = [body for what, body in self.z2m.requests if what == "device/bind"][-1]
        self.assertEqual({k: bind[k] for k in ("from", "to", "clusters")},
                         {"from": RODRET, "to": group, "clusters": ["genOnOff", "genLevelCtrl"]})

    def test_another_room_and_none_at_once(self):
        """Another room is only other lamps in the switch's group – the lamps always listen: no press needed."""
        self.couple(RODRET, self.wohn)
        said = self.service.set_button_room(RODRET, {"room": self.flur})["message"]
        self.assertEqual(said, "„Schalter 00B1“ schaltet jetzt das Licht im Raum „Flur“.")
        self.settle()
        group = f"schalter-{RODRET}"
        self.assertEqual((self.z2m.groups[group]["members"], self.z2m.binds[RODRET]), ([INNR], {group}))
        sent = len(self.app_sets(FLOALT, INNR, HUE))
        self.press(RODRET, "on")   # the switch itself, already the Flur
        until(lambda: self.lamps() == {FLOALT: "OFF", INNR: "ON", HUE: "OFF"})
        said = self.service.set_button_room(RODRET, {"room": None})["message"]
        self.assertEqual(said, "„Schalter 00B1“ schaltet kein Licht mehr.")
        self.settle()
        self.assertEqual(self.z2m.groups[group]["members"], [])
        self.press(RODRET, "off")   # a switch without room switches nothing
        time.sleep(0.1)
        self.assertEqual(self.lamps()[INNR], "ON")
        self.assertEqual(len(self.app_sets(FLOALT, INNR, HUE)), sent)   # the app neither

    def test_the_groups_follow_the_lamps(self):
        self.couple(RODRET, self.wohn)
        self.service.set_light_room(HUE, {"room": self.flur})
        self.settle()
        self.assertEqual(self.z2m.groups[f"schalter-{RODRET}"]["members"], [FLOALT])
        self.z2m.asleep.add(INNR)   # the Innr has no power: into the group later
        self.couple(SMART, self.flur)
        self.assertEqual(self.z2m.groups[f"schalter-{SMART}"]["members"], [HUE])
        self.z2m.asleep.discard(INNR)
        with mock.patch("heizapp.service.GROUP_RETRY", 0.0):
            self.tend()
        self.assertEqual(sorted(self.z2m.groups[f"schalter-{SMART}"]["members"]), [INNR, HUE])

    def test_a_removed_lamp(self):
        """Removed from the Zigbee network, a lamp is no longer asked to leave the switch's group (it would not answer)."""
        self.couple(RODRET, self.wohn)
        zigbee = Zigbee(self.tmp)
        zigbee.net.stop()
        zigbee.net = self.net
        zigbee.remove_device(FLOALT)
        self.service.forget_thermostat(FLOALT)
        self.settle()
        with self.assertNoLogs("heizapp", level="WARNING"):
            self.tend()
        self.assertNotIn(("group/members/remove", FLOALT), [(w, b.get("device")) for w, b in self.z2m.requests])

    def test_a_deleted_room_empties_the_group(self):
        self.couple(RODRET, self.wohn)
        self.service.delete_room(self.wohn)
        self.settle()
        self.assertEqual(self.z2m.groups[f"schalter-{RODRET}"]["members"], [])
        self.assertEqual((self.buttons()[RODRET]["room"], self.buttons()[RODRET]["coupled"]), (None, False))

    def test_its_group_removed_in_zigbee2mqtt(self):
        self.couple(RODRET, self.wohn)
        with self.z2m.lock:
            self.z2m.groups.pop(f"schalter-{RODRET}")
            self.z2m.binds[RODRET] = set()
        self.z2m._groups()
        until(lambda: f"schalter-{RODRET}" not in self.net.groups())
        self.tend()
        self.assertEqual(self.buttons()[RODRET]["coupled"], False)   # coupled anew at its next press
        self.press(RODRET, "on")
        self.assertEqual(self.buttons()[RODRET]["coupled"], True)
        self.assertEqual(sorted(self.z2m.groups[f"schalter-{RODRET}"]["members"]), [FLOALT, HUE])

    def test_a_coupling_that_fails_at_a_press_is_shown(self):
        self.service.set_button_room(RODRET, {"room": self.wohn})
        self.settle()
        self.z2m.awake_for = 0.0   # it falls asleep at once (far from the Pi)
        self.press(RODRET, "on")   # the app switches – coupling does not work
        b = self.buttons()[RODRET]
        self.assertEqual(self.lamps(), {FLOALT: "ON", INNR: "OFF", HUE: "ON"})
        self.assertEqual((b["coupled"], b["error"]), (False, "Das Gerät hat nicht geantwortet – vielleicht schläft es "
                                                             "gerade oder ist zu weit vom Pi weg."))
        self.z2m.awake_for = 3.0
        self.press(RODRET, "off")   # the next press: it works
        b = self.buttons()[RODRET]
        self.assertEqual((b["coupled"], b["error"]), (True, None))

    def test_a_switch_the_app_does_not_know_works_through_the_app(self):
        said = self.service.set_button_room(SONOFF, {"room": self.flur})["message"]
        self.assertEqual(said, "„Schalter 00B4“ schaltet jetzt das Licht im Raum „Flur“ – über die App.")
        self.press(SONOFF)
        self.assertEqual(self.lamps()[INNR], "ON")
        self.press(SONOFF)
        self.assertEqual(self.lamps()[INNR], "OFF")
        self.assertNotIn(SONOFF, self.z2m.binds)   # never coupled
        self.assertEqual(self.buttons()[SONOFF]["coupled"], False)

    def test_an_old_press_does_not_count(self):
        """Zigbee2MQTT keeps the last message of a switch (retain): after a restart of the app it comes again – it is
        no press."""
        self.service.set_button_room(SONOFF, {"room": self.flur})
        self.press(SONOFF)
        self.assertEqual(self.lamps()[INNR], "ON")
        heard = []
        again = ZigbeeNet(self.tmp, retry=(0.05,))
        again.on_action = lambda ieee, action: heard.append((ieee, action))
        self.addCleanup(again.stop)
        again.start()
        until(lambda: (again.thermostat(SONOFF) or {}).get("values"))
        self.press(SONOFF)   # a new press counts
        until(lambda: heard)
        self.assertEqual(heard, [(SONOFF, "single")])

    def test_reset_and_paired_again_the_coupling_is_gone(self):
        self.couple(RODRET, self.wohn)
        time.sleep(0.01)
        with self.net._lock:
            self.net._joined[RODRET] = time.time()   # it joined again (after a reset)
        time.sleep(0.01)
        self.assertEqual(self.buttons()[RODRET]["coupled"], False)
        self.press(RODRET, "on")
        self.assertEqual(self.buttons()[RODRET]["coupled"], True)


class TurnsTest(Base):
    """The Hue Smart Button and the Hue Dimmschalter send on and off by turns: when the app switched the light in
    between, a press goes into the void – the app makes it good."""

    def test_the_smart_button(self):
        self.couple(SMART, self.wohn)
        self.press(SMART)   # its first turn: on – the lamps were off
        self.assertEqual(self.lamps(), {FLOALT: "ON", INNR: "OFF", HUE: "ON"})
        sent = len(self.app_sets(FLOALT, HUE))
        self.press(SMART)   # off
        self.assertEqual(self.lamps(), {FLOALT: "OFF", INNR: "OFF", HUE: "OFF"})
        self.assertEqual(len(self.app_sets(FLOALT, HUE)), sent)   # all by the switch itself
        self.service.switch_room_light(self.wohn, {"on": True})   # someone uses the app
        time.sleep(0.1)
        self.press(SMART)   # its turn: on – the light is on already: into the void, the app switches it off
        until(lambda: self.lamps() == {FLOALT: "OFF", INNR: "OFF", HUE: "OFF"})
        self.assertEqual(self.app_sets(FLOALT, HUE)[-2:], [(FLOALT, {"state": "OFF"}), (HUE, {"state": "OFF"})])

    def test_the_smart_button_that_no_longer_tells_the_stick(self):
        self.couple(SMART, self.wohn)
        self.z2m.hears_on_off = False   # the stick hears press and release only: nothing to make good
        self.service.switch_room_light(self.wohn, {"on": True})
        time.sleep(0.1)
        sent = len(self.app_sets(FLOALT, HUE))
        self.press(SMART)   # on – into the void
        self.assertEqual(self.lamps(), {FLOALT: "ON", INNR: "OFF", HUE: "ON"})
        self.assertEqual(len(self.app_sets(FLOALT, HUE)), sent)

    def test_the_dimmer_switch(self):
        self.couple(DIMMER, self.wohn)
        self.service.switch_room_light(self.wohn, {"on": True})
        time.sleep(0.1)
        self.press(DIMMER, "on")   # its first turn: on – the light is on already: the app switches it off
        until(lambda: self.lamps() == {FLOALT: "OFF", INNR: "OFF", HUE: "OFF"})
        asked = [body for i, kind, body in self.z2m.commands if kind == "get" and i in (FLOALT, HUE)]
        self.assertIn({"state": ""}, asked)   # the app asked the lamps before it switched
        sent = len(self.app_sets(FLOALT, HUE))
        self.press(DIMMER, "up")   # brighter: the switch tells the lamps itself – the app does nothing
        until(lambda: self.lamps()[FLOALT] == "ON")
        self.press(DIMMER, "on")   # its turn: off – the light is on: it worked, nothing to make good
        until(lambda: self.lamps() == {FLOALT: "OFF", INNR: "OFF", HUE: "OFF"})
        time.sleep(0.2)
        self.settle()
        self.assertEqual(self.lamps(), {FLOALT: "OFF", INNR: "OFF", HUE: "OFF"})
        self.assertEqual(len(self.app_sets(FLOALT, HUE)), sent)


class BatteryTest(Base):
    def test_a_nearly_empty_battery_on_the_start_page(self):
        self.service.rename_button(RODRET, {"name": "Knopf Wohnzimmer"})
        self.z2m.report(RODRET, battery=12)
        until(lambda: self.service.state()["notices"])
        notice = self.service.state()["notices"][0]
        self.assertEqual((notice["id"], notice["text"]), (f"batterie-{RODRET}", "Die Batterie im Schalter „Knopf "
                                                          "Wohnzimmer“ ist fast leer (12 %) – bitte bald wechseln."))
        self.assertEqual(self.service.notice_ok(f"batterie-{RODRET}"), {})
        self.assertEqual(self.service.state()["notices"], [])
        self.z2m.report(RODRET, battery=100)   # a new battery …
        until(lambda: self.buttons()[RODRET]["battery"] == 100)
        self.tend()
        self.z2m.report(RODRET, battery=10)    # … nearly empty some day: the notice again
        until(lambda: self.service.state()["notices"])


class ApiTest(Base):
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

    def test_name_and_room_need_the_setup(self):
        self.call("PUT", f"/api/buttons/{RODRET}/name", {"name": "Knopf"})
        self.call("PUT", f"/api/buttons/{RODRET}/room", {"room": self.flur})
        self.call("PUT", "/api/setup/pin", {"pin": "1234"})
        self.call("PUT", f"/api/buttons/{RODRET}/room", {"room": ""}, expect=403)
        self.call("PUT", f"/api/buttons/{RODRET}/name", {"name": "X"}, expect=403)
        b = next(x for x in self.call("GET", "/api/state")["buttons"] if x["id"] == RODRET)
        self.assertEqual((b["name"], b["room"]), ("Knopf", self.flur))
        self.call("PUT", "/api/buttons/0x8c65a3fffe1d0bad/room", {"room": ""}, expect=403)
        self.z2m.report(RODRET, battery=5)
        until(lambda: self.call("GET", "/api/state")["notices"])
        self.call("POST", f"/api/notices/batterie-{RODRET}/ok")   # anyone may – without PIN
        self.assertEqual(self.call("GET", "/api/state")["notices"], [])


if __name__ == "__main__":
    unittest.main()
