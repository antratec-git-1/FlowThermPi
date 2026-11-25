"""Tests for heizapp.trv: the SONOFF thermostats (TRV-ZBT) as heaters of the app, over Zigbee2MQTT.

Mosquitto and Zigbee2MQTT are played by tests/fake_mqtt.py, an AeroFlow by tests/fake_heater.py – nothing real is
reached. The thermostat's answers follow the device definition in Zigbee2MQTT; how the real one behaves (does a
setpoint in Automatik last until the next plan point, how long a boost really runs …) is to be checked on the device.
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
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_heater import FakeHeater  # noqa: E402
from fake_mqtt import DAYS, FakeBroker, FakeZ2M, schedule_error, thermostat  # noqa: E402
from heizapp import trv  # noqa: E402
from heizapp.server import make_server  # noqa: E402
from heizapp.service import ActionError, Service  # noqa: E402
from heizapp.zigbee import Zigbee  # noqa: E402
from heizapp.zigbee_net import ZigbeeNet, _seen  # noqa: E402

PASSWORD = "app-passwort-nur-fuer-den-test"
IEEE = "0x8c65a3fffe1d00aa"
OTHER = "0x8c65a3fffe1d00bb"
BEWOHNT = [{"start": "00:00", "end": "06:00", "temp": 17}, {"start": "06:00", "end": "22:00", "temp": 22},
           {"start": "22:00", "end": "23:59", "temp": 17}]


def until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("timed out")


def week(day: list[dict]) -> dict:
    return {str(d): day for d in range(1, 8)}


class PlanPointsTest(unittest.TestCase):
    """An app plan as the thermostat's change points."""

    def text(self, windows, frost=70):
        return trv.schedule_text(trv.day_points([f"{w['start']},{w['end']},{round(w['temp'] * 10)}" for w in windows],
                                                frost))

    def test_the_example_from_the_concept(self):
        self.assertEqual(self.text(BEWOHNT), "00:00/17 06:00/22 22:00/17")

    def test_gaps_hold_the_frost_protection(self):
        day = [{"start": "06:00", "end": "08:30", "temp": 21}, {"start": "17:00", "end": "22:00", "temp": 21.5}]
        self.assertEqual(self.text(day), "00:00/7 06:00/21 08:30/7 17:00/21.5 22:00/7")
        self.assertEqual(self.text(day, frost=None), "00:00/5 06:00/21 08:30/5 17:00/21.5 22:00/5")   # not said yet
        self.assertEqual(self.text([]), "00:00/7")

    def test_neighbours_with_the_same_temperature_are_one_point(self):
        day = [{"start": "06:00", "end": "12:00", "temp": 21}, {"start": "12:00", "end": "18:00", "temp": 21},
               {"start": "18:00", "end": "23:59", "temp": 7}]
        self.assertEqual(self.text(day), "00:00/7 06:00/21 18:00/7")

    def test_what_zigbee2mqtt_takes(self):
        day = [{"start": f"{h:02d}:00", "end": f"{h:02d}:30", "temp": 20 + h % 3} for h in range(1, 6)]
        text = self.text(day)
        self.assertEqual(len(text.split()), 11)
        self.assertIsNone(schedule_error(text))   # the same checks as the converter of Zigbee2MQTT
        self.assertIsNone(schedule_error(self.text(BEWOHNT)))

    def test_too_many_changes_in_words(self):
        day = [{"start": f"{h:02d}:00", "end": f"{h:02d}:30", "temp": 21} for h in range(1, 7)]   # 6 windows, gaps
        days = {1: [f"{w['start']},{w['end']},210" for w in day]}
        self.assertEqual(trv.plan_problems(days, 70), ["Montag hätte 13 Wechsel, das Thermostat kann höchstens 12"])
        self.assertEqual(trv.plan_problems({1: days[1][:5]}, 70), [])

    def test_last_seen_as_zigbee2mqtt_writes_it(self):
        utc = datetime(2026, 11, 25, 10, 20, 3, tzinfo=timezone.utc).timestamp()
        self.assertEqual(_seen("2026-11-25T10:20:03Z"), utc)
        self.assertEqual(_seen("2026-11-25T10:20:03.500Z"), utc + 0.5)
        self.assertEqual(_seen("2026-11-25T11:20:03+01:00"), utc)
        self.assertEqual(_seen(utc * 1000), utc)   # „epoch“: milliseconds
        self.assertIsNone(_seen("gestern"))
        self.assertIsNone(_seen(None))


class Base(unittest.TestCase):
    """Service, ZigbeeNet and a simulated Zigbee2MQTT with one paired thermostat."""

    devices = (IEEE,)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.broker = FakeBroker(users={"flowthermpi": PASSWORD})
        self.addCleanup(self.broker.close)
        self.z2m = FakeZ2M(self.broker, channel=25, join_after=0.2, interview=0.2)
        self.z2m.start([thermostat(ieee) for ieee in self.devices])
        (self.tmp / "zigbee-mqtt.json").write_text(json.dumps(
            {"host": "127.0.0.1", "port": self.broker.port, "user": "flowthermpi", "password": PASSWORD}))
        self.net = ZigbeeNet(self.tmp, retry=(0.05,))
        self.addCleanup(self.net.stop)
        self.net.start()
        until(lambda: self.net.problem() is None and all((self.net.thermostat(i) or {}).get("values")
                                                         for i in self.devices))
        patcher = mock.patch.multiple(trv.Thermostat, answer=2.0, plan_answer=3.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conf = self.tmp / "heizungen.conf"
        self.conf.write_text(self.conf_text())
        self.service = Service(str(self.conf), str(self.tmp / "data.json"), poll_interval=3600)
        for e in self.service.entries:
            e.device.timeout, e.device.retries = 0.5, 1
        self.service.attach_zigbee(self.net)
        self.service.poll_all(full=True)

    def conf_text(self) -> str:
        return ""

    def heater(self, ident=IEEE) -> dict:
        return next(h for h in self.service.state()["heaters"] if h["id"] == ident)

    def sets(self, ieee=IEEE) -> list[dict]:
        return [body for i, kind, body in self.z2m.commands if i == ieee and kind == "set"]

    def room(self, *ids) -> str:
        return self.service.create_room({"name": "Bad", "heaters": list(ids or [IEEE])})["id"]

    def results(self, answer) -> list[tuple]:
        return [(r["name"], r["ok"], r["message"]) for r in answer["results"]]


class ThermostatTest(Base):
    def test_a_paired_thermostat_is_a_heater(self):
        h = self.heater()
        self.assertEqual({k: h[k] for k in ("kind", "name", "online", "writable", "room_temp", "setpoint", "mode",
                                            "heating", "window_open", "antifrost", "temp_corr", "plan_slot", "battery",
                                            "battery_low", "signal", "faults", "update", "ota", "smart", "paired", "host",
                                            "hostname", "firmware", "serial", "error")},
                         {"kind": "zigbee", "name": "Thermostat 00AA", "online": True, "writable": True, "room_temp": 20.5,
                          "setpoint": 21.0, "mode": "manual", "heating": True, "window_open": False, "antifrost": 7.0,
                          "temp_corr": 0.0, "plan_slot": 1, "battery": 87, "battery_low": False, "signal": 132,
                          "faults": [], "update": False, "ota": None, "smart": False, "paired": True, "host": "Zigbee",
                          "hostname": "SONOFF TRV-ZBT", "firmware": "1.1.3", "serial": IEEE, "error": None})
        self.assertIsNotNone(h["last_seen"])
        self.assertEqual(self.conf.read_text(), "")   # thermostats never go into heizungen.conf
        self.assertEqual(self.z2m.commands, [])        # looking costs the thermostat nothing

    def test_name_and_room_stay_with_the_thermostat(self):
        room = self.room()
        self.service.update_heater(IEEE, {"name": "Bad Fenster", "room": room})
        meta = json.loads((self.tmp / "data.json").read_text())["heaters"][IEEE]
        self.assertEqual(meta, {"zigbee": True, "room": room, "name": "Bad Fenster"})
        with self.assertRaises(ActionError):
            self.service.update_heater(IEEE, {"host": "192.168.5.80"})
        again = Service(str(self.conf), str(self.tmp / "data.json"), poll_interval=3600)   # the app restarts …
        again.attach_zigbee(ZigbeeNet(None))                                               # … Mosquitto is not there yet
        again.poll_all()
        h = next(h for h in again.state()["heaters"] if h["id"] == IEEE)
        self.assertEqual((h["name"], h["room"], h["online"], h["paired"]), ("Bad Fenster", room, False, False))
        self.assertEqual(h["error"], "Zigbee ist auf diesem Pi noch nicht eingerichtet.")
        self.z2m.devices = []                                                             # gone from Zigbee2MQTT
        self.z2m._devices()
        until(lambda: not self.net.paired(trv.MODEL))
        self.service.poll_all()
        h = self.heater()
        self.assertEqual((h["room"], h["online"], h["paired"]), (room, False, False))
        self.assertIn("nicht (mehr) im Zigbee-Netz angelernt", h["error"])

    def test_warmer_and_colder(self):
        room = self.room()
        answer = self.service.set_setpoint("room", room, 22.5)
        self.assertEqual(self.results(answer), [("Thermostat 00AA", True, "Solltemperatur 22,5 °C")])
        self.assertEqual(self.sets(), [{"occupied_heating_setpoint": 22.5}])
        self.assertEqual((self.heater()["setpoint"], self.heater()["heating"]), (22.5, True))
        self.service.set_setpoint("heater", IEEE, 22.5)   # it has that already: nothing goes out
        self.assertEqual(len(self.sets()), 1)
        self.assertEqual(self.z2m.refused, [])

    def test_off_and_on_again(self):
        room = self.room()
        self.service.set_setpoint("room", room, 21.5)
        self.assertTrue(self.service.set_mode("room", room, "off")["results"][0]["ok"])
        self.assertEqual(self.sets()[-1], {"system_mode": "off"})
        h = self.heater()
        self.assertEqual((h["mode"], h["heating"]), ("off", False))
        before = json.loads((self.tmp / "data.json").read_text())["heaters"][IEEE]["before_off"]
        self.assertEqual(before, {"mode": "manual", "setpoint": 215})
        self.service.set_mode("room", room, "off")   # off again: what is remembered stays
        self.assertEqual(json.loads((self.tmp / "data.json").read_text())["heaters"][IEEE]["before_off"], before)
        self.z2m.report(IEEE, occupied_heating_setpoint=7)   # the thermostat shows its frost protection meanwhile
        until(lambda: self.net.thermostat(IEEE)["values"]["occupied_heating_setpoint"] == 7)
        answer = self.service.set_mode("room", room, "on")
        self.assertEqual(self.results(answer), [("Thermostat 00AA", True, "Eingeschaltet – 21,5 °C")])
        self.assertEqual(self.sets()[-2:], [{"system_mode": "heat"}, {"occupied_heating_setpoint": 21.5}])
        self.assertEqual((self.heater()["mode"], self.heater()["setpoint"]), ("manual", 21.5))
        self.assertNotIn("before_off", json.loads((self.tmp / "data.json").read_text())["heaters"][IEEE])

    def test_back_to_the_plan(self):
        room = self.room()
        self.service.set_mode("room", room, "auto")
        self.assertEqual(self.sets(), [{"system_mode": "auto", "schedule_active_group": "1"}])
        h = self.heater()
        self.assertEqual((h["mode"], h["plan_slot"]), ("auto", 1))
        self.assertEqual(h["setpoint"], FakeZ2M.plan_now({d: "00:00/16 06:00/21 22:00/16" for d in DAYS}))

    def test_full_power(self):
        room = self.room()
        answer = self.service.set_mode("room", room, "boost", 30)
        self.assertEqual(self.results(answer), [("Thermostat 00AA", True, "Boost")])
        self.assertEqual(self.sets(), [{"temporary_mode_mode": "boost", "temporary_mode_duration": 30}])
        h = self.heater()
        self.assertEqual((h["mode"], h["heating"]), ("boost", True))
        self.assertIn(h["boost_remaining"], ("30:00", "29:59", "29:58"))
        self.service.set_mode("room", room, "boost_off")
        self.assertEqual(self.sets()[-1], {"temporary_mode_mode": "disabled"})
        self.assertEqual(self.heater()["mode"], "manual")

    def test_a_boost_ends_with_another_mode_and_by_itself(self):
        self.service.set_mode("heater", IEEE, "boost", 30)
        self.service.set_mode("heater", IEEE, "auto")
        self.assertEqual(self.sets()[-1], {"system_mode": "auto", "schedule_active_group": "1",
                                           "temporary_mode_mode": "disabled"})
        self.service.set_mode("heater", IEEE, "boost", 30)
        e = self.service._entry(IEEE)
        e.device._boost_started = time.time() - 31 * 60       # the 30 minutes are over, Zigbee2MQTT still says boost
        self.service.poll_all()
        self.assertEqual(self.heater()["mode"], "auto")
        asked = until(lambda: [b for i, k, b in self.z2m.commands if k == "get"])   # the question goes out by itself
        self.assertEqual(asked, [{"temporary_mode_mode": ""}])

    def test_the_plan_goes_to_the_thermostat(self):
        plan = self.service.create_plan({"name": "Bewohnt", "days": week(BEWOHNT)})["id"]
        room = self.room()
        self.service.update_room(room, {"plan": plan})
        answer = self.service.send_plan(room)
        self.assertEqual(self.results(answer), [("Thermostat 00AA", True, "übertragen (7 Tag(e) geändert)")])
        self.assertEqual(self.sets(), [{"schedule_group_to_edit": "1", **{f"weekly_schedule_{d}":
                                        "00:00/17 06:00/22 22:00/17" for d in DAYS}}])
        self.assertEqual(self.z2m.refused, [])
        state = self.service.state()
        self.assertFalse(next(r for r in state["rooms"] if r["id"] == room)["plan_outdated"])
        self.assertEqual(self.results(self.service.send_plan(room)), [("Thermostat 00AA", True, "war bereits aktuell")])
        self.assertEqual(len(self.sets()), 1)
        day = [{"start": "06:00", "end": "22:00", "temp": 21}]   # Monday only changes: only Monday goes out
        self.service.update_plan(plan, {"name": "Bewohnt", "days": {**week(BEWOHNT), "1": day}})
        self.assertEqual(self.results(self.service.send_plan(room)),
                         [("Thermostat 00AA", True, "übertragen (1 Tag(e) geändert)")])
        self.assertEqual(self.sets()[-1], {"schedule_group_to_edit": "1", "weekly_schedule_monday": "00:00/7 06:00/21 22:00/7"})

    def test_too_many_changes_are_said_before_anything_is_sent(self):
        day = [{"start": f"{h:02d}:00", "end": f"{h:02d}:30", "temp": 21} for h in range(1, 7)]
        plan = self.service.create_plan({"name": "Viele", "days": {"1": day}})["id"]
        room = self.room()
        self.service.update_room(room, {"plan": plan})
        with self.assertRaises(ActionError) as ctx:
            self.service.send_plan(room)
        self.assertEqual(str(ctx.exception), "Der Heizplan „Viele“ passt nicht auf „Thermostat 00AA“: Montag hätte "
                                             "13 Wechsel, das Thermostat kann höchstens 12. Bitte Zeitfenster "
                                             "zusammenlegen – nichts wurde gesendet.")
        self.assertEqual(self.sets(), [])

    def test_a_thermostat_that_does_not_answer(self):
        self.z2m.asleep.add(IEEE)
        with mock.patch.object(trv.Thermostat, "answer", 0.3):
            answer = self.service.set_setpoint("heater", IEEE, 23)
        self.assertEqual(self.results(answer), [("Thermostat 00AA", False, trv.NOT_ANSWERED)])
        self.assertTrue(self.heater()["online"])   # it was heard a moment ago: it only missed this command
        self.assertEqual(self.heater()["setpoint"], 21.0)

    def test_silence_counts_after_two_hours_and_the_app_asks(self):
        def heard(minutes_ago):   # the last sign of life the app has from it (an older last_seen never counts)
            with self.net._lock:
                self.net._states[IEEE]["heard"] = time.time() - minutes_ago * 60

        self.z2m.asleep.add(IEEE)
        heard(40)   # 40 minutes quiet: still there, but the app asks
        self.service.poll_all()
        self.assertTrue(self.heater()["online"])
        gets = lambda: [b for i, k, b in self.z2m.commands if k == "get"]   # noqa: E731
        self.assertEqual(until(gets), [{"local_temperature": ""}])
        self.service.poll_all()
        time.sleep(0.2)
        self.assertEqual(len(gets()), 1)            # at most every 30 minutes
        heard(130)
        with self.assertLogs("heizapp.service", "WARNING") as said:
            self.service.poll_all()
        h = self.heater()
        self.assertFalse(h["online"])
        self.assertEqual(h["error"], "Das Thermostat hat sich seit 2 Stunden nicht gemeldet – am Thermostat einmal die "
                                     "mittlere Taste drücken.")
        self.assertIn("Thermostat 00AA (0x8c65a3fffe1d00aa) antwortet nicht", said.output[0])
        self.z2m.asleep.clear()
        self.service._entry(IEEE).device._asked = 0.0   # half an hour later: it asks again – and gets an answer
        self.service.poll_all()
        until(lambda: time.time() - self.net.thermostat(IEEE)["heard"] < 5)
        self.service.poll_all()
        self.assertTrue(self.heater()["online"])

    def test_without_zigbee2mqtt(self):
        self.z2m.stop()
        until(lambda: self.net.problem())
        self.service.poll_all()
        self.assertEqual((self.heater()["online"], self.heater()["error"]), (False, "Zigbee2MQTT läuft gerade nicht."))
        answer = self.service.set_setpoint("heater", IEEE, 22)
        self.assertEqual(self.results(answer), [("Thermostat 00AA", False, "Zigbee2MQTT läuft gerade nicht.")])

    def test_window_battery_and_faults(self):
        self.z2m.report(IEEE, open_window_detected=True, battery=18,
                        fault_status="valve_adjustment_issue_detected, battery_too_low_please_replace_the_batteries",
                        update={"state": "available", "installed_version": 4352, "latest_version": 4353})
        until(lambda: self.net.thermostat(IEEE)["values"]["battery"] == 18)
        self.service.poll_all()
        h = self.heater()
        self.assertEqual((h["window_open"], h["battery"], h["battery_low"], h["update"]), (True, 18, True, True))
        self.assertEqual(h["faults"], ["Ventil lässt sich nicht richtig stellen", "Batterie fast leer"])
        self.z2m.report(IEEE, battery=60, fault_status="battery_status_abnormal")
        until(lambda: self.net.thermostat(IEEE)["values"]["battery"] == 60)
        self.assertTrue(self.heater()["battery_low"])   # the device's own warning counts as well

    def test_calibration_in_steps_of_0_2(self):
        answer = self.service.calibrate(IEEE, {"measured": 21.9})
        self.assertEqual(answer["message"], "Temperatur-Korrektur +0,0 °C → +1,4 °C (Heizung zeigte 20,5 °C, "
                                            "gemessen 21,9 °C).")
        self.assertEqual(self.sets(), [{"local_temperature_calibration": 1.4}])
        self.service.calibrate(IEEE, {"correction": "-0,5"})
        self.assertEqual(self.sets()[-1], {"local_temperature_calibration": -0.4})   # 0.2 steps (half to even)
        with self.assertRaises(ActionError) as ctx:
            self.service.calibrate(IEEE, {"measured": 31.2})
        self.assertIn("außerhalb von ±10 °C", str(ctx.exception))

    def test_no_clock_no_reading_out_no_calibration_run(self):
        with self.assertRaises(ActionError):
            self.service.sync_clocks(heater_id=IEEE)
        self.assertEqual(self.service.sync_clocks()["results"], [])
        with self.assertRaises(ActionError) as ctx:
            self.service.import_plans(IEEE)
        self.assertEqual(ctx.exception.status, 409)
        room = self.room()
        sensor = "pill-test:100"
        with self.service.store.edit() as d:
            d["sensors"][sensor] = {"name": "Fühler", "room": room, "pill": "pill-test", "channel": 100}
        with self.assertRaises(ActionError) as ctx:
            self.service.start_calibration(sensor, {"hours": 6})
        self.assertIn("keine AeroFlow-Heizung", str(ctx.exception))
        self.assertEqual(self.z2m.commands, [])

    def test_smart_control_on_and_off(self):
        self.assertIs(self.heater()["smart"], False)   # as from the factory: fully open, then closed
        answer = self.service.set_smart(IEEE, True)
        self.assertEqual(answer["message"], "Thermostat 00AA regelt jetzt sanft.")
        self.assertEqual(self.sets(), [{"smart_temperature_control": "ON"}])
        self.assertIs(self.heater()["smart"], True)
        self.service.set_smart(IEEE, True)   # it does so already: nothing goes out
        self.assertEqual(len(self.sets()), 1)
        answer = self.service.set_smart(IEEE, False)
        self.assertEqual(answer["message"], "Thermostat 00AA regelt jetzt wie ab Werk (ganz auf, ganz zu).")
        self.assertEqual(self.sets()[-1], {"smart_temperature_control": "OFF"})
        with self.assertRaises(ActionError):
            self.service.set_smart(IEEE, "ja")
        self.z2m.asleep.add(IEEE)
        with mock.patch.object(trv.Thermostat, "answer", 0.3), self.assertRaises(ActionError) as ctx:
            self.service.set_smart(IEEE, True)
        self.assertEqual((ctx.exception.status, str(ctx.exception)), (502, "Nicht übernommen: " + trv.NOT_ANSWERED))
        self.assertIs(self.heater()["smart"], False)
        self.assertEqual(self.z2m.refused, [])

    def test_smart_control_not_said_yet_is_asked_for(self):
        with self.net._lock:   # the thermostat has not reported how it regulates
            self.net._states[IEEE]["values"].pop("smart_temperature_control")
        self.assertIsNone(self.heater()["smart"])
        self.service.poll_all()
        gets = lambda: [b for i, k, b in self.z2m.commands if k == "get"]   # noqa: E731
        self.assertEqual(until(gets), [{"smart_temperature_control": ""}])
        until(lambda: self.heater()["smart"] is False)
        self.service.poll_all()
        self.assertEqual(len(gets()), 1)

    def available(self, ieee=IEEE, **values):
        self.z2m.report(ieee, update={"state": "available", "installed_version": 4353, "latest_version": 4354}, **values)
        until(lambda: self.net.thermostat(ieee)["values"]["update"]["state"] == "available")

    def ota(self):
        return self.heater()["ota"] or {}

    def test_firmware_update(self):
        self.available()
        self.assertEqual((self.heater()["update"], self.heater()["ota"]), (True, None))
        self.z2m.ota_every = 0.2
        with self.assertLogs("heizapp", "INFO") as said:
            answer = self.service.update_firmware(IEEE)
            self.assertEqual(answer["message"], "Das Einspielen hat begonnen. Es dauert 10 bis 100 Minuten – das "
                                                "Thermostat heizt dabei weiter und startet am Ende kurz neu.")
            self.assertEqual(until(lambda: self.z2m.requests),
                             [("device/ota_update/update", {"id": IEEE, "transaction": mock.ANY})])
            self.assertEqual(self.ota(), {"state": "running", "progress": None, "remaining": None, "message": None})
            until(lambda: self.ota().get("progress") == 25)   # what the thermostat reports, every 30 s for real
            self.assertEqual(self.ota()["state"], "running")
            with self.assertRaises(ActionError) as ctx:
                self.service.update_firmware(IEEE)
            self.assertEqual((ctx.exception.status, str(ctx.exception)), (409, "Die neue Firmware wird schon eingespielt."))
            until(lambda: self.ota().get("state") == "done", seconds=10)
        self.service.poll_all()
        h = self.heater()
        self.assertEqual((h["firmware"], h["update"], h["ota"]["message"]), ("1.1.4", False, "Neue Firmware eingespielt."))
        self.assertEqual(h["restore"], {"state": "waiting", "text": None})   # in a minute: what it forgot
        logged = "\n".join(said.output)
        self.assertIn("Thermostat Thermostat 00AA (0x8c65a3fffe1d00aa): neue Firmware wird eingespielt (Firmware bisher "
                      "1.1.3, Batterie 87 %)", logged)
        self.assertIn("Zigbee: Firmware von 0x8c65a3fffe1d00aa eingespielt (1.1.3 → 1.1.4)", logged)
        self.assertEqual(self.sets(), [])   # nothing else went to the thermostat

    def test_firmware_update_only_when_it_can_go_well(self):
        with self.assertRaises(ActionError) as ctx:
            self.service.update_firmware(IEEE)
        self.assertEqual((ctx.exception.status, str(ctx.exception)),
                         (409, "Für dieses Thermostat gibt es gerade keine neue Firmware."))
        self.available(battery=55)
        with self.assertRaises(ActionError) as ctx:
            self.service.update_firmware(IEEE)
        self.assertEqual(str(ctx.exception), "Zum Einspielen braucht die Batterie mindestens 70 % (jetzt 55 %) – bitte "
                                             "zuerst neue Batterien einsetzen.")
        self.z2m.stop()
        until(lambda: self.net.problem())
        self.service.poll_all()
        with self.assertRaises(ActionError) as ctx:
            self.service.update_firmware(IEEE)
        self.assertEqual(str(ctx.exception), "Das Thermostat ist gerade nicht erreichbar.")
        self.assertEqual([w for w, b in self.z2m.requests if w.startswith("device/ota")], [])

    def test_firmware_update_that_fails_and_again(self):
        self.available()
        self.z2m.ota_error = "Device didn't respond to OTA request"
        with self.assertLogs("heizapp.zigbee", "WARNING") as said:
            self.service.update_firmware(IEEE)
            until(lambda: self.ota().get("state") == "failed")
        self.assertEqual(self.ota()["message"], "Das Thermostat hat nicht geantwortet – bitte später noch einmal versuchen.")
        self.assertIn("Zigbee2MQTT meldet: Update of '0x8c65a3fffe1d00aa' failed (Device didn't respond to OTA request)",
                      said.output[0])
        self.assertTrue(self.heater()["update"])   # still there: it can be tried again
        self.z2m.ota_error = None
        self.service.update_firmware(IEEE)
        until(lambda: self.ota().get("state") == "done")

    def test_firmware_update_without_zigbee2mqtts_answer(self):
        self.available()
        self.z2m.silent = True   # it never begins: after 10 minutes (here 0.2 s) the page says so
        with mock.patch("heizapp.zigbee_net.OTA_START", 0.2):
            self.service.update_firmware(IEEE)
            self.assertEqual(self.ota()["state"], "running")
            until(lambda: self.ota().get("state") == "failed")
        self.assertEqual(self.ota()["message"], "Zigbee2MQTT hat mit dem Einspielen nicht begonnen – bitte noch einmal "
                                                "versuchen.")
        self.z2m.silent = False   # it runs, but the app misses the answer: the new firmware says it is done
        self.service.update_firmware(IEEE)
        with self.net._lock:
            self.net._ota[IEEE]["transaction"] = "eine-andere"
        until(lambda: self.ota().get("state") == "done")

    def restored(self, state="done", seconds=10.0):
        """Run the app's poller until the settings from before the update are back (or it gave up)."""
        deadline = time.monotonic() + seconds
        while (self.heater()["restore"] or {}).get("state") != state:
            self.assertLess(time.monotonic(), deadline, "timed out")
            self.service.poll_all()
            time.sleep(0.05)
        self.service.poll_all()
        return self.heater()

    def test_settings_come_back_after_a_firmware_update(self):
        room = self.room()
        plan = self.service.create_plan({"name": "Bewohnt", "days": week(BEWOHNT)})["id"]
        self.service.update_room(room, {"plan": plan})
        self.service.send_plan(room)
        self.service.set_mode("room", room, "auto")
        self.service.set_smart(IEEE, True)
        self.service.calibrate(IEEE, {"correction": 0.4})
        self.available()
        factory = "00:00/16 06:00/21 22:00/16"   # as the real one (1.1.0 → 1.1.3): „ab Werk“ and off – here its plan too
        self.z2m.ota_forgets = {"smart_temperature_control": "OFF", "system_mode": "off",
                                **{f"weekly_schedule_{d}": factory for d in DAYS}}
        before = len(self.sets())
        with mock.patch("heizapp.service.RESTORE_SETTLE", 0.5), self.assertLogs("heizapp", "INFO") as said:
            self.service.update_firmware(IEEE)
            until(lambda: self.ota().get("state") == "done", seconds=10)
            until(lambda: self.heater()["smart"] is False)   # forgotten
            self.service.poll_all()   # it waits a minute (here 0.5 s) for the thermostat to settle
            self.assertEqual((self.heater()["restore"], len(self.sets())), ({"state": "waiting", "text": None}, before))
            time.sleep(0.5)
            h = self.restored()
        self.assertEqual((h["smart"], h["mode"], h["temp_corr"], h["setpoint"]), (True, "auto", 0.4, 22.0))
        text = "sanft, Automatik, Heizplan, Korrektur +0,4 °C, Frostschutz 7 °C"
        self.assertEqual(h["restore"], {"state": "done", "text": text})
        self.assertEqual(self.sets()[before:], [
            {"local_temperature_calibration": 0.4, "frost_protection_temperature": 7, "smart_temperature_control": "ON"},
            {"schedule_group_to_edit": "1", **{f"weekly_schedule_{d}": "00:00/17 06:00/22 22:00/17" for d in DAYS}},
            {"system_mode": "auto", "schedule_active_group": "1"}])
        self.assertEqual(self.z2m.plans[IEEE]["1"]["monday"], "00:00/17 06:00/22 22:00/17")
        logged = "\n".join(said.output)
        self.assertIn(f"Thermostat Thermostat 00AA (0x8c65a3fffe1d00aa): gemerkt für nach dem Update: {text}", logged)
        self.assertIn(f"Thermostat Thermostat 00AA (0x8c65a3fffe1d00aa) nach dem Update zurückgestellt: {text} – es meldete "
                      "vorher: ab Werk, aus, einen anderen Heizplan", logged)
        self.assertEqual(self.z2m.refused, [])

    def test_what_is_set_during_the_update_comes_back(self):
        self.available()
        self.z2m.ota_every = 0.3
        with mock.patch("heizapp.service.RESTORE_SETTLE", 0), self.assertLogs("heizapp", "INFO") as said:
            self.service.update_firmware(IEEE)
            self.service.set_setpoint("heater", IEEE, 19.5)   # it heats on meanwhile, and someone wants it cooler
            self.assertEqual(self.ota()["state"], "running")
            until(lambda: self.ota().get("state") == "done", seconds=10)
            h = self.restored()
        self.assertEqual((h["mode"], h["setpoint"], h["smart"]), ("manual", 19.5, False))
        self.assertEqual(self.sets()[-1], {"system_mode": "heat", "occupied_heating_setpoint": 19.5})
        self.assertIn("zurückgestellt: ab Werk, fest auf 19,5 °C, Heizplan, Korrektur ±0,0 °C, Frostschutz 7 °C – es "
                      "meldete vorher: aus", "\n".join(said.output))

    def test_settings_that_do_not_come_back(self):
        self.available()
        with mock.patch.multiple("heizapp.service", RESTORE_SETTLE=0, RESTORE_RETRY=0.2), \
                mock.patch.object(trv.Thermostat, "answer", 0.3), self.assertLogs("heizapp", "INFO") as said:
            self.service.update_firmware(IEEE)
            until(lambda: self.ota().get("state") == "done", seconds=10)
            self.z2m.asleep.add(IEEE)   # it does not take the commands
            h = self.restored("failed")
        self.assertEqual(h["restore"], {"state": "failed", "text": "Die Einstellungen von vorher ließen sich nicht wieder "
                                        "einstellen – bitte Regelung, Aus/Automatik/Ein und Heizplan prüfen."})
        logged = "\n".join(said.output)
        self.assertEqual(logged.count("Zurückstellen nach dem Update hat nicht geklappt"), 2)   # 3 tries in all
        self.assertIn("Einstellungen nach dem Update nicht zurückgestellt (" + trv.NOT_ANSWERED + ") – bitte Regelung, "
                      "Aus/Automatik/Ein und Heizplan prüfen", logged)
        self.service.poll_all()
        self.assertEqual(self.heater()["restore"]["state"], "failed")   # stays on the page for a day

    def test_a_failed_update_changes_nothing(self):
        self.available()
        self.z2m.ota_error = "Device didn't respond to OTA request"
        with mock.patch("heizapp.service.RESTORE_SETTLE", 0):
            self.service.update_firmware(IEEE)
            self.assertEqual(self.heater()["restore"], {"state": "waiting", "text": None})
            until(lambda: self.ota().get("state") == "failed")
            self.service.poll_all()
        self.assertIsNone(self.heater()["restore"])   # the thermostat kept its settings
        self.assertEqual(self.sets(), [])

    def test_removed_in_the_app(self):
        room = self.room()
        self.service.update_heater(IEEE, {"name": "Bad"})
        self.assertEqual(self.service.delete_heater(IEEE)["message"], "Thermostat aus der App entfernt.")
        h = self.heater()   # still paired: it stays in the list, without name and room
        self.assertEqual((h["name"], h["room"]), ("Thermostat 00AA", None))
        self.assertNotIn(IEEE, json.loads((self.tmp / "data.json").read_text())["heaters"])
        self.assertEqual(next(r for r in self.service.state()["rooms"] if r["id"] == room)["heaters"], [])


class MixedRoomTest(Base):
    """An AeroFlow and a thermostat in one room: every button acts on both."""

    devices = (IEEE, OTHER)

    def conf_text(self) -> str:
        self.aeroflow = FakeHeater(token="A" * 24, idu="91110001")
        self.addCleanup(self.aeroflow.close)
        return f"127.0.0.1:{self.aeroflow.port} {self.aeroflow.token}\n"

    def test_one_room_two_kinds(self):
        state = self.service.state()
        self.assertEqual([(h["kind"], h["id"]) for h in state["heaters"]],
                         [("aeroflow", "91110001"), ("zigbee", IEEE), ("zigbee", OTHER)])
        room = self.room("91110001", IEEE)
        self.assertEqual({r["name"]: r["ok"] for r in self.service.set_setpoint("room", room, 22)["results"]},
                         {"Heat4U-911001": True, "Thermostat 00AA": True})
        self.assertEqual(self.aeroflow.values["setpoint"], 220)
        self.assertEqual(self.sets(), [{"occupied_heating_setpoint": 22.0}])
        self.assertEqual(self.sets(OTHER), [])   # not in the room
        plan = self.service.create_plan({"name": "Bewohnt", "days": week(BEWOHNT)})["id"]
        self.service.update_room(room, {"plan": plan})
        self.assertTrue(all(r["ok"] for r in self.service.send_plan(room)["results"]))
        self.assertTrue(all(r["ok"] for r in self.service.set_mode("room", room, "off")["results"]))
        self.assertEqual({h["id"]: h["mode"] for h in self.service.state()["heaters"] if h["room"] == room},
                         {"91110001": "manual", IEEE: "off"})   # the AeroFlow's „off“ is manual on frost protection
        self.assertEqual(self.conf.read_text().strip(), f"127.0.0.1:{self.aeroflow.port} {self.aeroflow.token}")
        later = FakeHeater(token="B" * 24, idu="92220002")
        self.addCleanup(later.close)
        self.service.add_heater({"host": f"127.0.0.1:{later.port}", "token": later.token})   # comes before them
        self.assertEqual([h["kind"] for h in self.service.state()["heaters"]], ["aeroflow", "aeroflow", "zigbee", "zigbee"])
        self.assertNotIn("zigbee", self.conf.read_text().lower())

    def test_one_firmware_update_at_a_time_and_only_for_thermostats(self):
        for ieee in (IEEE, OTHER):
            self.z2m.report(ieee, update={"state": "available", "installed_version": 4353, "latest_version": 4354})
        until(lambda: all(self.net.thermostat(i)["values"]["update"]["state"] == "available" for i in (IEEE, OTHER)))
        self.z2m.ota_every = 0.2
        self.service.update_firmware(IEEE)
        with self.assertRaises(ActionError) as ctx:
            self.service.update_firmware(OTHER)
        self.assertEqual((ctx.exception.status, str(ctx.exception)),
                         (409, "Gerade wird die Firmware von „Thermostat 00AA“ eingespielt – bitte warten, bis sie fertig "
                               "ist."))
        for call in (lambda: self.service.update_firmware("91110001"), lambda: self.service.set_smart("91110001", True)):
            with self.assertRaises(ActionError) as ctx:
                call()
            self.assertEqual((ctx.exception.status, str(ctx.exception)), (409, "Das geht nur beim Thermostat."))
        heater = lambda ident: next(h for h in self.service.state()["heaters"] if h["id"] == ident)   # noqa: E731
        until(lambda: (heater(IEEE)["ota"] or {}).get("state") == "done", seconds=10)
        self.service.update_firmware(OTHER)   # now it may
        self.assertEqual(heater(OTHER)["ota"]["state"], "running")


class ApiTest(Base):
    """Over the web API, with the Zigbee page: removing a thermostat in the Zigbee network also removes it here."""

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
            with urllib.request.urlopen(req, timeout=10) as res:
                status, raw = res.status, res.read()
        except urllib.error.HTTPError as err:
            with err:
                status, raw = err.code, err.read()
        self.assertEqual(status, expect, raw)
        return json.loads(raw or b"{}")

    def test_home_page_buttons_and_removal(self):
        room = self.call("POST", "/api/rooms", {"name": "Bad", "heaters": [IEEE]})["id"]
        self.call("PATCH", f"/api/heaters/{IEEE}", {"name": "Bad Fenster"})
        answer = self.call("POST", f"/api/rooms/{room}/setpoint", {"temp": 19.5})
        self.assertEqual([r["ok"] for r in answer["results"]], [True])
        answer = self.call("POST", f"/api/heaters/{IEEE}/mode", {"mode": "boost", "minutes": 30})
        self.assertEqual([r["message"] for r in answer["results"]], ["Boost"])
        state = self.call("GET", "/api/state")
        h = next(h for h in state["heaters"] if h["id"] == IEEE)
        self.assertEqual((h["name"], h["setpoint"], h["mode"], h["kind"]), ("Bad Fenster", 19.5, "boost", "zigbee"))
        self.call("POST", f"/api/heaters/{IEEE}/clock", expect=409)
        answer = self.call("POST", f"/api/zigbee/devices/{IEEE}/remove", {"force": False})
        self.assertEqual(answer["message"], "Das Thermostat ist entfernt.")
        state = self.call("GET", "/api/state")
        self.assertEqual([h["id"] for h in state["heaters"]], [])
        self.assertEqual(next(r for r in state["rooms"] if r["id"] == room)["heaters"], [])
        self.assertNotIn(IEEE, json.loads((self.tmp / "data.json").read_text())["heaters"])

    def test_smart_and_firmware(self):
        answer = self.call("POST", f"/api/heaters/{IEEE}/smart", {"on": True})
        self.assertEqual(answer["message"], "Thermostat 00AA regelt jetzt sanft.")
        self.call("POST", f"/api/heaters/{IEEE}/smart", {"on": "ja"}, expect=400)
        self.call("POST", f"/api/heaters/{IEEE}/firmware", expect=409)   # nothing new for it
        self.z2m.report(IEEE, update={"state": "available", "installed_version": 4353, "latest_version": 4354})
        until(lambda: self.net.thermostat(IEEE)["values"]["update"]["state"] == "available")
        self.assertIn("10 bis 100 Minuten", self.call("POST", f"/api/heaters/{IEEE}/firmware")["message"])
        heater = lambda: next(h for h in self.call("GET", "/api/state")["heaters"] if h["id"] == IEEE)   # noqa: E731
        until(lambda: (heater()["ota"] or {}).get("state") == "done", seconds=10)
        self.assertIs(until(lambda: heater()["smart"] is False), True)   # the thermostat forgot „sanft“ …
        with mock.patch("heizapp.service.RESTORE_SETTLE", 0):
            self.service.poll_all()   # … the app's poller writes it back
            h = until(lambda: (h := heater())["restore"]["state"] == "done" and h)
        self.assertIs(h["smart"], True)
        self.assertEqual(h["restore"]["text"], "sanft, fest auf 21 °C, Heizplan, Korrektur ±0,0 °C, Frostschutz 7 °C")


class OverviewTextTest(unittest.TestCase):
    """How „Protokoll speichern“ writes a thermostat's firmware update."""

    def test_ota_text(self):
        from heizapp.support import _ota_text
        self.assertEqual(_ota_text(None), "")
        self.assertEqual(_ota_text({"state": "running", "progress": 37.5}), " (neue Firmware wird eingespielt, 38 %)")
        self.assertEqual(_ota_text({"state": "running", "progress": None}), " (neue Firmware wird eingespielt)")
        self.assertEqual(_ota_text({"state": "done"}), " (neue Firmware eingespielt)")
        self.assertEqual(_ota_text({"state": "failed", "message": "Das Thermostat hat nicht geantwortet."}),
                         " (Einspielen hat nicht geklappt: Das Thermostat hat nicht geantwortet.)")


if __name__ == "__main__":
    unittest.main()
