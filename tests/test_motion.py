"""Tests for the motion sensors over Zigbee (0.4.24): recognised by what they report, in a room – several per room –,
and „Licht bei Bewegung“: a motion switches the room's lamps on (by day and at night with their own brightness and
colour of light, with „Nur wenn es dunkel ist“ only when the sensor measured it dark enough while the lamps were off),
nobody moving for the room's minutes switches off what the automation switched on – not what someone switched on by
hand –, switched off by hand it waits its pause; its sensitivity and light, its battery on the start page.

Mosquitto and Zigbee2MQTT are played by tests/fake_mqtt.py – nothing real is reached. How the real Hue motion sensor
reports a motion and when it says that the motion is over is to be checked on the device.
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
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_mqtt import FakeBroker, FakeZ2M, button, detector, light, light_state  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import ActionError, Service  # noqa: E402
from heizapp.zigbee import Zigbee  # noqa: E402
from heizapp.zigbee_net import ZigbeeNet, _definition  # noqa: E402

PASSWORD = "app-passwort-nur-fuer-den-test"
FLOALT, INNR, HUE, RODRET = "0x8c65a3fffe1d00f1", "0x8c65a3fffe1d00f2", "0x8c65a3fffe1d00f3", "0x8c65a3fffe1d00b1"
SML, SML2, VALL = "0x8c65a3fffe1d0a01", "0x8c65a3fffe1d0a02", "0x8c65a3fffe1d0a03"
DAY, NIGHT = datetime(2026, 11, 28, 14, 0), datetime(2026, 11, 28, 23, 30)


def until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("timed out")


class Base(unittest.TestCase):
    """Three lamps, three motion sensors and a switch in a simulated Zigbee2MQTT: in the Flur the FLOALT and the Innr
    with two Hue motion sensors, in the Wohnzimmer the Hue lamp with an IKEA VALLHORN. By day (14:00)."""

    def setUp(self):
        patcher = mock.patch("heizapp.service.LUX_SETTLE", 0.0)   # a brightness counts at once after the lamps went off
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.broker = FakeBroker(users={"flowthermpi": PASSWORD})
        self.addCleanup(self.broker.close)
        self.z2m = FakeZ2M(self.broker, channel=25, join_after=0.2, interview=0.2)
        self.z2m.delay = 0.01
        for lamp in (FLOALT, INNR, HUE):
            self.z2m.states[lamp] = light_state(on=False)
        self.z2m.start([light(FLOALT, "floalt"), light(INNR, "innr"), light(HUE, "hue"), detector(SML, "hue"),
                        detector(SML2, "hue2"), detector(VALL, "vallhorn"), button(RODRET, "rodret")])
        (self.tmp / "zigbee-mqtt.json").write_text(json.dumps(
            {"host": "127.0.0.1", "port": self.broker.port, "user": "flowthermpi", "password": PASSWORD}))
        self.net = ZigbeeNet(self.tmp, retry=(0.05,))
        self.addCleanup(self.net.stop)
        self.net.start()
        until(lambda: self.net.problem() is None and all((self.net.thermostat(i) or {}).get("values")
                                                         for i in (FLOALT, INNR, HUE, SML, SML2, VALL, RODRET)))
        self.conf = self.tmp / "heizungen.conf"
        self.conf.write_text("")
        self.service = self.make_service()
        self.flur = self.service.create_room({"name": "Flur"})["id"]
        self.wohn = self.service.create_room({"name": "Wohnzimmer"})["id"]
        for lamp, room in ((FLOALT, self.flur), (INNR, self.flur), (HUE, self.wohn)):
            self.service.set_light_room(lamp, {"room": room})
        for sensor, room in ((SML, self.flur), (SML2, self.flur), (VALL, self.wohn)):
            self.service.set_detector_room(sensor, {"room": room})
        self.settle()

    def make_service(self) -> Service:
        service = Service(str(self.conf), str(self.tmp / "data.json"), poll_interval=3600)
        service.attach_zigbee(self.net)
        patcher = mock.patch.object(service, "now", return_value=DAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        return service

    def settle(self):
        """Wait until the motion thread (and the switch and coupling threads) have done everything sent so far."""
        for pool in (self.service._motion_pool, self.service._buttons, self.service._coupler, self.service._motion_pool):
            pool.submit(lambda: None).result(timeout=40)

    def move(self, sensor, occupied=True, lux=None):
        """Someone moves in front of the sensor (occupied) – or the sensor says the motion is over."""
        before = (self.net.detector(sensor) or {}).get("seq") or 0
        self.z2m.move(sensor, occupied, lux)
        until(lambda: ((self.net.detector(sensor) or {}).get("seq") or 0) > before)
        time.sleep(0.05)   # the news reaches the motion thread …
        self.settle()      # … and it has done what it means

    def tick(self, later=0.0):
        """A look of the motion thread – later: as if so many seconds had gone by."""
        self.service._motion_pool.submit(self.service._motion_tick, time.time() + later).result(timeout=40)

    def lamps(self) -> dict:
        return {x["id"]: x["state"] for x in self.service.state()["lights"]}

    def sets(self, *lamps) -> list:
        return [(i, body) for i, kind, body in self.z2m.commands if kind == "set" and i in lamps]

    def motion(self, room=None) -> dict:
        return next(r for r in self.service.state()["rooms"] if r["id"] == (room or self.flur))["motion"]

    def on(self, room=None, **settings):
        return self.service.set_motion(room or self.flur, {"on": True, **settings})


class RecognitionTest(Base):
    def test_sensors_by_what_they_report(self):
        state = self.service.state()
        found = {x["id"]: x for x in state["detectors"]}
        self.assertEqual(list(found), [SML, SML2, VALL])   # not the lamps, not the switch
        self.assertEqual([x["id"] for x in state["buttons"]], [RODRET])
        self.assertEqual({k: found[SML][k] for k in ("name", "model", "room", "moving", "lux", "temperature", "battery",
                                                     "sensitivity", "sensitivities", "led", "led_settable", "silent")},
                         {"name": "Melder 0A01", "model": "Philips 9290012607", "room": self.flur, "moving": False,
                          "lux": 12, "temperature": 20.4, "battery": 100, "sensitivity": "high",
                          "sensitivities": ["low", "medium", "high"], "led": False, "led_settable": True,
                          "silent": False})
        self.assertEqual(found[SML2]["sensitivities"], ["low", "medium", "high", "very_high", "max"])
        self.assertEqual({k: found[VALL][k] for k in ("model", "temperature", "sensitivity", "sensitivities", "led",
                                                      "led_settable")},
                         {"model": "IKEA E2134", "temperature": None, "sensitivity": None, "sensitivities": [],
                          "led": None, "led_settable": False})
        self.assertIn("3 Lampe(n), 1 Schalter und 3 Bewegungsmelder", self.net.summary())
        self.assertEqual(self.motion(self.flur)["detectors"], [SML, SML2])   # a room with sensors: its settings
        self.assertFalse(self.motion(self.flur)["on"])                       # – off from the factory

    def test_what_counts_as_a_motion_sensor(self):
        def kind(exposes):
            device = {}
            _definition({"definition": {"model": "X", "vendor": "Y", "exposes": exposes}}, device)
            return device["detector"], device["detector_caps"], device["button"]
        occupancy = {"type": "binary", "name": "occupancy", "property": "occupancy", "access": 1}
        self.assertEqual(kind([occupancy]), (True, {"prop": "occupancy", "lux": False, "temp": False,
                                                    "sensitivity": None, "led": False}, False))
        presence = {"type": "binary", "name": "presence", "property": "presence", "access": 1}
        self.assertEqual(kind([presence])[1]["prop"], "presence")   # a sensor that also sees people sitting still
        action = {"type": "enum", "name": "action", "property": "action", "values": ["single"]}
        self.assertEqual(kind([occupancy, action])[::2], (True, False))   # with a button: still a motion sensor
        self.assertEqual(kind([{**occupancy, "type": "numeric"}])[0], False)
        lamp = {"type": "light", "features": [{"type": "binary", "name": "state", "property": "state"}]}
        self.assertEqual(kind([lamp, occupancy])[0], False)   # a lamp with a sensor is a lamp
        read_only = {"type": "enum", "property": "motion_sensitivity", "access": 1, "values": ["low"]}
        self.assertIsNone(kind([occupancy, read_only])[1]["sensitivity"])   # only what can be set

    def test_old_news_is_no_motion(self):
        said = []
        self.net.on_motion = lambda ieee, occupied: said.append((ieee, occupied))
        self.net._take_values(SML, {"occupancy": True, "last_seen": "2026-11-28T06:00:00+01:00"}, retained=True)
        self.assertEqual(said, [])   # a retained „yes“ is how it was – not someone moving now
        self.net._take_values(SML, {"occupancy": True})
        self.net._take_values(SML, {"occupancy": True, "illuminance": 40})   # the same motion, a new brightness
        self.net._take_values(SML, {"occupancy": False})
        self.assertEqual(said, [(SML, True), (SML, False)])


class LightTest(Base):
    def test_motion_switches_the_room_on(self):
        self.move(SML)
        self.assertEqual(self.sets(FLOALT, INNR), [])   # „Licht bei Bewegung“ is off from the factory
        self.move(SML, False)
        self.assertEqual(self.on()["message"], "„Flur“: Licht bei Bewegung an.")
        self.move(SML)
        self.assertEqual(self.lamps(), {FLOALT: "ON", INNR: "ON", HUE: "OFF"})
        self.assertEqual(sorted(self.sets(FLOALT, INNR)),   # by day: 100 %, neutralweiß (4000 K)
                         [(FLOALT, {"state": "ON", "color_temp": 250, "brightness": 254}),
                          (INNR, {"state": "ON", "color_temp": 250, "brightness": 254})])
        m = self.motion()
        self.assertEqual((m["auto"], m["moving"], m["off_at"]), (True, True, None))   # off only once nobody moves
        self.move(SML, False)
        self.assertIsNotNone(self.motion()["off_at"])

    def test_off_after_the_minutes_without_motion(self):
        self.on(minutes=10)
        self.move(SML)
        self.move(SML, False)
        self.tick(9 * 60)
        self.assertEqual(self.lamps()[FLOALT], "ON")
        self.tick(10 * 60 + 1)
        self.assertEqual(self.lamps(), {FLOALT: "OFF", INNR: "OFF", HUE: "OFF"})
        self.assertEqual(self.sets(FLOALT)[-1], (FLOALT, {"state": "OFF"}))
        self.assertFalse(self.motion()["auto"])
        self.assertNotIn(self.flur, self.service.store.data.get("motion_auto") or {})

    def test_every_motion_starts_the_minutes_anew(self):
        self.on(minutes=10)
        self.move(SML)
        self.move(SML, False)
        self.net._motion[SML]["at"] = time.time() - 9.5 * 60   # the motion was over nine and a half minutes ago
        self.tick()
        self.assertEqual(self.lamps()[FLOALT], "ON")
        self.move(SML)        # someone again: on already – only the minutes start anew
        self.move(SML, False)
        self.tick(60)
        self.assertEqual(self.lamps()[FLOALT], "ON")
        self.tick(10 * 60 + 1)
        self.assertEqual(self.lamps()[FLOALT], "OFF")

    def test_one_sensor_still_sees_someone(self):
        self.on()
        self.move(SML)
        self.move(SML2)
        self.move(SML, False)
        self.tick(11 * 60)
        self.assertEqual(self.lamps()[FLOALT], "ON")   # the other sensor still sees someone
        self.move(SML2, False)
        self.tick(11 * 60)
        self.assertEqual(self.lamps()[FLOALT], "OFF")

    def test_a_silent_yes_does_not_keep_the_light_on(self):
        self.on()
        self.move(SML)   # … and then nothing more from it – for well over an hour
        self.tick(80 * 60)
        self.assertEqual(self.lamps()[FLOALT], "OFF")

    def test_at_night_dimmed_and_warm(self):
        self.service.now.return_value = NIGHT
        self.on()
        self.move(SML)
        self.assertEqual(sorted(self.sets(FLOALT, INNR)),   # 20 %, warmweiß (2700 K)
                         [(FLOALT, {"state": "ON", "color_temp": 370, "brightness": 51}),
                          (INNR, {"state": "ON", "color_temp": 370, "brightness": 51})])
        night = lambda h, m=0, **s: Service._night({**Service.MOTION_DEFAULTS, **s}, datetime(2026, 11, 28, h, m))  # noqa
        self.assertEqual([night(21, 59), night(22), night(0, 30), night(5, 59), night(6)], [False, True, True, True, False])
        self.assertFalse(night(23, night=False))
        self.assertTrue(night(13, night_from="12:00", night_to="14:00"))   # not over midnight
        self.assertFalse(night(13, night_from="14:00", night_to="14:00"))  # the same time: never

    def test_chosen_lamps_and_sensors(self):
        self.on(lamps=[INNR], detectors=[SML2])
        self.move(SML)
        self.assertEqual(self.sets(FLOALT, INNR), [])   # this sensor does not switch the light
        self.move(SML2)
        self.assertEqual(self.lamps(), {FLOALT: "OFF", INNR: "ON", HUE: "OFF"})

    def test_rooms_on_their_own(self):
        self.on(self.wohn)
        self.move(SML)
        self.move(VALL)
        self.assertEqual(self.lamps(), {FLOALT: "OFF", INNR: "OFF", HUE: "ON"})
        self.assertEqual(self.sets(HUE), [(HUE, {"state": "ON", "color_temp": 250, "brightness": 254})])

    def test_switched_off_the_automation_still_ends_what_it_began(self):
        self.on()
        self.move(SML)
        self.move(SML, False)
        self.service.set_motion(self.flur, {"on": False})
        self.tick(11 * 60)
        self.assertEqual(self.lamps()[FLOALT], "OFF")
        self.move(SML)
        self.assertEqual(self.lamps()[FLOALT], "OFF")


class HandTest(Base):
    def test_switched_off_by_hand_waits_its_pause(self):
        self.on(pause=1)
        self.move(SML)
        self.move(SML, False)
        self.service.switch_room_light(self.flur, {"on": False})
        self.assertFalse(self.motion()["auto"])
        self.assertIsNotNone(self.motion()["pause_until"])
        count = len(self.sets(FLOALT))
        self.move(SML)   # within the minute: nothing – one can leave the Flur in the dark
        self.assertEqual(len(self.sets(FLOALT)), count)
        self.service._motion_pause[self.flur] = time.time() - 1   # the minute is over
        self.move(SML, False)
        self.move(SML)
        self.assertEqual(self.lamps()[FLOALT], "ON")

    def test_switched_off_at_the_wall(self):
        self.on()
        self.move(SML)
        for lamp in (FLOALT, INNR):
            self.z2m.report(lamp, state="OFF")
        until(lambda: self.lamps()[FLOALT] == "OFF" and self.lamps()[INNR] == "OFF")
        self.tick()
        self.assertFalse(self.motion()["auto"])
        self.assertIsNotNone(self.motion()["pause_until"])

    def test_switched_on_by_hand_stays_on(self):
        self.on()
        self.service.switch_room_light(self.flur, {"on": True})
        self.move(SML)
        self.move(SML, False)
        self.tick(11 * 60)
        self.assertEqual(self.lamps()[FLOALT], "ON")
        self.assertFalse(self.motion()["auto"])

    def test_one_lamp_on_by_hand_is_taken_over(self):
        self.on()
        self.move(SML)
        self.move(SML, False)
        self.service.switch_light(FLOALT, {"on": True})   # on already – now by hand
        self.tick(11 * 60)
        self.assertEqual((self.lamps()[FLOALT], self.lamps()[INNR]), ("ON", "OFF"))

    def test_light_by_hand_goes_off_too_if_the_room_wants_it(self):
        self.assertIn("geht auch aus", self.service.set_motion(self.flur, {"hand_off": True})["message"])
        self.on()
        self.service.switch_room_light(self.flur, {"on": True})
        self.tick()          # since now on
        self.tick(9 * 60)
        self.assertEqual(self.lamps()[FLOALT], "ON")
        self.tick(10 * 60 + 1)
        self.assertEqual(self.lamps(), {FLOALT: "OFF", INNR: "OFF", HUE: "OFF"})

    def test_a_coupled_switch_switched_off(self):
        self.z2m.awake[RODRET] = time.monotonic() + 5
        self.service.set_button_room(RODRET, {"room": self.flur})
        self.settle()
        self.z2m.awake.pop(RODRET, None)
        self.on()
        self.move(SML)
        self.z2m.press(RODRET, "off")   # it switches the lamps itself – the app only hears of it
        until(lambda: self.lamps()[FLOALT] == "OFF")
        time.sleep(0.1)
        self.settle()
        self.assertFalse(self.motion()["auto"])
        self.assertIsNotNone(self.motion()["pause_until"])


class DarkTest(Base):
    def test_only_when_dark(self):
        self.on(dark_only=True, lux=30)
        self.tick()                       # the lamps are off …
        self.move(SML, False, lux=500)    # … and the sensor measures daylight
        self.tick()
        self.move(SML)
        self.assertEqual(self.sets(FLOALT, INNR), [])
        self.move(SML, False, lux=5)      # dusk
        self.tick()
        self.move(SML)
        self.assertEqual(self.lamps()[FLOALT], "ON")

    def test_not_known_switches_on(self):
        self.on(dark_only=True)
        self.move(SML)   # nothing measured while the lamps were off yet: better light than none
        self.assertEqual(self.lamps()[FLOALT], "ON")

    def test_measured_with_the_lamps_on_does_not_count(self):
        self.on(dark_only=True, lux=30, pause=0)
        self.service.switch_room_light(self.flur, {"on": True})
        self.tick()
        self.move(SML, False, lux=400)    # the lamps' own light
        self.service.switch_room_light(self.flur, {"on": False})
        self.tick()
        self.move(SML)
        self.assertEqual(self.lamps()[FLOALT], "ON")


class KeepTest(Base):
    def test_the_automation_outlasts_a_restart(self):
        self.on()
        self.move(SML)
        self.move(SML, False)
        self.assertIn(self.flur, self.service.store.data["motion_auto"])
        self.service = self.make_service()   # the app starts again – the lamps are still on
        self.tick(11 * 60)
        self.assertEqual(self.lamps()[FLOALT], "OFF")


class ForgetTest(Base):
    def test_a_removed_sensor_is_forgotten(self):
        self.service.rename_detector(SML, {"name": "Flur oben"})
        self.on(detectors=[SML, SML2])
        self.service.forget_thermostat(SML)
        data = self.service.store.data
        self.assertNotIn(SML, data.get("detector_names") or {})
        self.assertNotIn(SML, data.get("detector_rooms") or {})
        self.assertEqual(data["motion_light"][self.flur]["detectors"], [SML2])

    def test_a_deleted_room_takes_its_settings(self):
        self.on()
        self.move(SML)
        self.service.delete_room(self.flur)
        data = self.service.store.data
        self.assertNotIn(self.flur, data["motion_light"])
        self.assertNotIn(self.flur, data["motion_auto"])
        self.assertNotIn(SML, data["detector_rooms"])


class SettingsTest(Base):
    def test_sensitivity_and_light(self):
        self.assertEqual(self.service.set_detector(SML, {"sensitivity": "low"})["message"],
                         "„Melder 0A01“: Empfindlichkeit niedrig.")
        self.assertEqual(self.service.set_detector(SML, {"led": True})["message"],
                         "„Melder 0A01“: Sein Licht blinkt bei Bewegung.")
        found = {x["id"]: x for x in self.service.state()["detectors"]}
        self.assertEqual((found[SML]["sensitivity"], found[SML]["led"]), ("low", True))
        with self.assertRaises(ActionError):
            self.service.set_detector(VALL, {"sensitivity": "low"})   # the VALLHORN has none to set
        with self.assertRaises(ActionError):
            self.service.set_detector(SML, {"sensitivity": "max"})    # the first Hue has no „max“
        self.z2m.asleep.add(SML)
        with mock.patch("heizapp.service.DETECTOR_ANSWER", 0.3), self.assertRaises(ActionError) as caught:
            self.service.set_detector(SML, {"led": False})
        self.assertEqual(caught.exception.status, 504)
        self.assertIn("einmal vor ihm bewegen", str(caught.exception))

    def test_the_settings_are_checked(self):
        for bad in ({"minutes": 0}, {"minutes": 121}, {"day_level": 0}, {"night_from": "24:00"}, {"night_to": "6"},
                    {"day_color": "lila"}, {"pause": 31}, {"lux": True}, {"on": "ja"}, {"lamps": HUE}, {"what": 1}):
            with self.assertRaises(ValueError, msg=bad):
                self.service.set_motion(self.flur, bad)
        with self.assertRaises(ActionError):
            self.service.set_motion(self.flur, {"lamps": [HUE]})   # not in the Flur
        say = lambda body: self.service.set_motion(self.flur, body)["message"]  # noqa: E731
        self.assertEqual(say({"minutes": 1}), "„Flur“: Das Licht geht aus, wenn sich 1 Minute lang nichts bewegt hat.")
        self.assertEqual(say({"night_from": "21:30"}), "„Flur“: nachts von 21:30 Uhr bis 6 Uhr 20 %, warmweiß.")
        self.assertEqual(say({"day_color": "kalt"}), "„Flur“: tagsüber 100 %, kaltweiß.")
        self.assertEqual(say({"dark_only": True}), "„Flur“: Licht bei Bewegung nur, wenn es dunkler ist als 30 Lux.")
        self.assertEqual(say({"pause": 0}), "„Flur“: Nach dem Ausschalten von Hand wartet die Automatik nicht.")
        self.assertEqual(say({"lamps": [INNR, INNR]}), "„Flur“: Bei Bewegung gehen die gewählten Lampen an.")
        self.assertEqual(self.service.store.data["motion_light"][self.flur]["lamps"], [INNR])


class BatteryTest(Base):
    def test_battery_on_the_start_page(self):
        self.z2m.report(SML, battery=10)
        notice = until(lambda: self.service.state()["notices"])[0]
        self.assertEqual(notice["text"], "Die Batterie im Bewegungsmelder „Melder 0A01“ ist fast leer (10 %) – bitte "
                                         "bald wechseln.")
        self.service.notice_ok(notice["id"])
        self.assertEqual(self.service.state()["notices"], [])
        self.z2m.report(SML, battery=100)   # a new battery: next time it is said again
        until(lambda: (self.net.detector(SML) or {}).get("battery") == 100)
        self.service._coupler.submit(self.service._tend_buttons).result(timeout=40)
        self.assertNotIn(SML, self.service.store.data.get("detector_battery_ok") or {})


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

    def test_the_settings_need_the_setup(self):
        self.call("PUT", f"/api/detectors/{SML}/name", {"name": "Flur oben"})
        self.call("PUT", f"/api/detectors/{SML}/room", {"room": self.flur})
        self.call("PUT", f"/api/detectors/{SML}/settings", {"sensitivity": "medium"})
        self.call("PUT", f"/api/rooms/{self.flur}/motion", {"on": True, "minutes": 5})
        self.call("PUT", f"/api/rooms/{self.flur}/motion", {"minutes": 500}, expect=400)
        self.call("PUT", "/api/setup/pin", {"pin": "1234"})
        self.call("PUT", f"/api/rooms/{self.flur}/motion", {"on": False}, expect=403)
        self.call("PUT", f"/api/detectors/{SML}/settings", {"led": True}, expect=403)
        state = self.call("GET", "/api/state")
        flur = next(r for r in state["rooms"] if r["id"] == self.flur)
        self.assertEqual((flur["motion"]["on"], flur["motion"]["minutes"]), (True, 5))
        d = next(x for x in state["detectors"] if x["id"] == SML)
        self.assertEqual((d["name"], d["sensitivity"]), ("Flur oben", "medium"))


if __name__ == "__main__":
    unittest.main()
