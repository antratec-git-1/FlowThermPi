"""Tests for heizapp.mqtt (MQTT 3.1.1 with Python's own means) and heizapp.zigbee_net (the app's side of Zigbee2MQTT).

Mosquitto and Zigbee2MQTT are played by tests/fake_mqtt.py – nothing real is reached.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_mqtt import FakeBroker, FakeZ2M, matches, thermostat  # noqa: E402
from heizapp import logs  # noqa: E402
from heizapp.mqtt import Client, MqttError, decode, encode  # noqa: E402
from heizapp.service import ActionError  # noqa: E402
from heizapp.zigbee_net import ZigbeeNet  # noqa: E402

PASSWORD = "app-passwort-nur-fuer-den-test"
IEEE = "0x8c65a3fffe1d00aa"


def until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("timed out")


class PacketTest(unittest.TestCase):
    def test_lengths_round_trip(self):
        for size in (0, 1, 127, 128, 16383, 16384, 2_097_151, 2_097_152):
            buffer = bytearray(encode(3, 1, b"x" * size) + b"\xd0\x00")
            self.assertEqual(decode(buffer), (3, 1, b"x" * size))
            self.assertEqual(decode(buffer), (13, 0, b""))   # the next packet, a PINGRESP
            self.assertEqual(buffer, bytearray())

    def test_incomplete_and_broken(self):
        packet = encode(3, 0, b"hello")
        for cut in range(len(packet)):
            self.assertIsNone(decode(bytearray(packet[:cut])))
        with self.assertRaises(MqttError):
            decode(bytearray(b"\x30\xff\xff\xff\xff\x01"))   # five length bytes
        with self.assertRaises(MqttError):
            decode(bytearray(encode(3, 0, b"")[:1] + b"\xff\xff\xff\x7f"))   # 256 MB

    def test_wildcards_of_the_fake_broker(self):
        self.assertTrue(matches("zigbee2mqtt/+", "zigbee2mqtt/0x1"))
        self.assertFalse(matches("zigbee2mqtt/+", "zigbee2mqtt/bridge/state"))
        self.assertTrue(matches("zigbee2mqtt/bridge/response/#", "zigbee2mqtt/bridge/response/device/remove"))


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker(users={"holitherm": PASSWORD})
        self.addCleanup(self.broker.close)
        self.got = []

    def client(self, password=PASSWORD, keepalive=30):
        client = Client("127.0.0.1", self.broker.port, "holitherm", password, client_id="test",
                        on_message=lambda topic, payload, retained: self.got.append((topic, payload, retained)),
                        keepalive=keepalive)
        self.addCleanup(client.close)
        return client

    def loop(self, client):
        self.failed = []

        def run():
            try:
                client.loop()
            except (MqttError, OSError) as exc:
                self.failed.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_login_subscribe_and_publish(self):
        self.broker.publish("zigbee2mqtt/bridge/state", {"state": "online"}, retain=True)
        self.broker.publish("other/thing", "x", retain=True)
        client = self.client()
        client.connect()
        client.subscribe(["zigbee2mqtt/bridge/#", "zigbee2mqtt/+"])
        self.loop(client)
        until(lambda: self.got)
        self.assertEqual(self.got, [("zigbee2mqtt/bridge/state", b'{"state": "online"}', True)])   # retained, at once
        client.publish("zigbee2mqtt/bridge/request/permit_join", '{"time": 180}')
        self.broker.publish("zigbee2mqtt/0x0000000000000001", "ü".encode())
        self.broker.publish("zigbee2mqtt/0x1/availability", "online")   # not subscribed
        until(lambda: len(self.got) == 3)
        self.assertCountEqual(self.got[1:], [("zigbee2mqtt/bridge/request/permit_join", b'{"time": 180}', False),
                                             ("zigbee2mqtt/0x0000000000000001", "ü".encode(), False)])
        self.assertIn(("zigbee2mqtt/bridge/request/permit_join", b'{"time": 180}'), self.broker.published)
        self.assertEqual(self.broker.connects, [("test", "holitherm", 0)])

    def test_a_big_message(self):
        client = self.client()
        client.connect()
        client.subscribe(["zigbee2mqtt/bridge/info"])
        self.loop(client)
        big = json.dumps({"config_schema": "x" * 900_000}).encode()
        self.broker.publish("zigbee2mqtt/bridge/info", big)
        until(lambda: self.got)
        self.assertEqual(self.got[0][1], big)

    def test_a_wrong_password_is_refused(self):
        with self.assertRaises(MqttError) as ctx:
            self.client("falsch").connect()
        self.assertEqual(str(ctx.exception), "Benutzername oder Passwort falsch")

    def test_nobody_there(self):
        port = self.broker.port
        self.broker.close()
        with self.assertRaises(OSError):
            Client("127.0.0.1", port).connect(timeout=2)

    def test_keepalive(self):
        client = self.client(keepalive=1)
        client.connect()
        client.subscribe(["a"])
        thread = self.loop(client)
        time.sleep(1.6)
        self.assertTrue(thread.is_alive())        # pings were answered
        self.broker.mute_pings = True              # Mosquitto hangs
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(str(self.failed[0]), "Mosquitto antwortet nicht mehr")

    def test_close_ends_the_loop_quietly(self):
        client = self.client()
        client.connect()
        client.subscribe(["a"])
        failed = []

        def run():
            try:
                client.loop()
            except Exception as exc:  # noqa: BLE001 – recorded for the assertion
                failed.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        client.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failed, [])
        with self.assertRaises(MqttError):
            client.publish("a", "b")

    def test_the_broker_goes_away(self):
        client = self.client()
        client.connect()
        client.subscribe(["a"])
        self.broker.drop()
        with self.assertRaises((MqttError, OSError)):
            client.loop()


class ZigbeeNetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.broker = FakeBroker(users={"holitherm": PASSWORD})
        self.addCleanup(self.broker.close)
        self.z2m = FakeZ2M(self.broker, channel=15, join_after=0.2, interview=0.2)
        self.net = ZigbeeNet(self.tmp, retry=(0.05, 0.1))
        self.net.answer, self.net.remove_wait = 1.0, 1.0
        self.addCleanup(self.net.stop)

    def login(self, password=PASSWORD):
        (self.tmp / "zigbee-mqtt.json").write_text(json.dumps(
            {"host": "127.0.0.1", "port": self.broker.port, "user": "holitherm", "password": password}))

    def when(self, check):
        return until(lambda: (lambda v: v if check(v) else None)(self.net.view()))

    def test_without_a_login_it_waits(self):
        self.net.start()
        time.sleep(0.1)
        self.assertEqual(self.net.view()["broker"], "off")
        self.assertEqual(self.broker.connects, [])
        self.login()
        self.net.wake()
        self.when(lambda v: v["broker"] == "connected")

    def test_what_zigbee2mqtt_says(self):
        self.z2m.start([thermostat(IEEE)])
        self.login()
        self.net.start()
        view = self.when(lambda v: v["bridge"] == "online" and v["devices"] and v["devices"][0]["battery"])
        self.assertEqual((view["version"], view["channel"], view["coordinator"]), ("2.14.3", 15, "EmberZNet"))
        device = view["devices"][0]
        self.assertEqual({k: device[k] for k in ("ieee", "vendor", "model", "thermostat", "supported", "interview",
                                                   "power", "firmware", "battery", "signal", "temperature", "new")},
                         {"ieee": IEEE, "vendor": "SONOFF", "model": "TRV-ZBT", "thermostat": True, "supported": True,
                          "interview": "done", "power": "Battery", "firmware": "1.1.3", "battery": 87, "signal": 132,
                          "temperature": 20.5, "new": False})
        self.assertEqual(self.net.summary(), "Zigbee2MQTT 2.14.3 läuft · Kanal 15 · 1 Gerät(e), davon 1 Thermostat(e)")
        self.z2m.stop()
        self.when(lambda v: v["bridge"] == "offline")

    def test_search_finds_a_thermostat(self):
        self.z2m.start()
        self.login()
        self.net.start()
        self.when(lambda v: v["bridge"] == "online")
        with self.assertLogs("heizapp.zigbee", "INFO") as said:
            self.net.search(True)
            view = self.net.view()
            self.assertTrue(view["search"]["active"])
            self.assertGreater(view["search"]["left"], 175)
            view = self.when(lambda v: v["devices"] and v["devices"][0]["interview"] == "done")
        device = view["devices"][0]
        self.assertEqual((device["new"], device["thermostat"]), (True, True))
        self.assertIn("angelernt – SONOFF TRV-ZBT", "\n".join(said.output))
        requests = [json.loads(p) for t, p in self.broker.published if t == "zigbee2mqtt/bridge/request/permit_join"]
        self.assertEqual(requests[0]["time"], 180)
        self.assertRegex(requests[0]["transaction"], r"^[0-9a-f]{12}$")
        self.net.search(False)
        self.assertFalse(self.net.view()["search"]["active"])
        self.when(lambda v: not v["search"]["active"])

    def test_requests_need_zigbee2mqtt(self):
        self.login()
        self.net.start()
        self.when(lambda v: v["broker"] == "connected")
        with self.assertRaises(ActionError) as ctx:
            self.net.search(True)
        self.assertEqual(ctx.exception.status, 409)          # not running
        self.z2m.start()
        self.when(lambda v: v["bridge"] == "online")
        self.z2m.silent = True
        with self.assertRaises(ActionError) as ctx:
            self.net.search(True)
        self.assertEqual(ctx.exception.status, 504)          # no answer
        self.assertEqual(self.net._waiting, {})

    def test_remove(self):
        self.z2m.start([thermostat(IEEE), thermostat("0x8c65a3fffe1d00bb")])
        self.login()
        self.net.start()
        self.when(lambda v: len(v["devices"]) == 2)
        self.z2m.remove_error = "Failed to remove device '0x8c65a3fffe1d00aa' (Error: Delivery failed for '23312')"
        with self.assertRaises(ActionError) as ctx, self.assertLogs("heizapp.zigbee", "WARNING") as logged:
            self.net.remove(IEEE)
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(str(ctx.exception),
                         "Das Gerät hat nicht geantwortet – vielleicht schläft es gerade oder ist zu weit vom Pi weg.")
        self.assertIn("Delivery failed for '23312'", logged.output[0])   # the details: in the log, not on the page
        self.net.remove(IEEE, force=True)
        self.assertEqual([d["ieee"] for d in self.net.view()["devices"]], ["0x8c65a3fffe1d00bb"])
        request = json.loads([p for t, p in self.broker.published if t.endswith("/device/remove")][-1])
        self.assertEqual((request["id"], request["force"]), (IEEE, True))
        with self.assertRaises(ActionError) as ctx:
            self.net.remove(IEEE)
        self.assertEqual(ctx.exception.status, 404)

    def test_comes_back_after_mosquitto_restarts(self):
        self.z2m.start([thermostat(IEEE)])
        self.login()
        self.net.start()
        self.when(lambda v: v["bridge"] == "online")
        self.broker.drop()
        view = self.when(lambda v: len(self.broker.connects) == 2 and v["bridge"] == "online"
                         and v["broker"] == "connected")   # the retained messages bring everything back at once
        self.assertEqual(len(view["devices"]), 1)
        self.assertEqual(len(self.broker.connects), 2)

    def test_a_wrong_login_is_reported_once_and_never_shown(self):
        self.login("falsches-passwort-123")
        with self.assertLogs("heizapp.zigbee", "WARNING") as said:
            self.net.start()
            view = self.when(lambda v: v["broker"] == "error" and len(self.broker.connects) >= 3)
        self.assertEqual(view["problem"], "Benutzername oder Passwort falsch")
        self.assertEqual(len(said.output), 1)
        self.assertNotIn("falsches-passwort-123", json.dumps(view))
        self.assertEqual(logs.redact("Passwort falsches-passwort-123"), "Passwort •••")   # never in the protocol


if __name__ == "__main__":
    unittest.main()
